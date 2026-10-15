"""Celery worker：AI 抠图任务执行与状态机。

状态机：pending -> processing -> success
                          └-> failed（积分原路退回）

可靠性设计：
- task_acks_late：任务执行完才 ACK，worker 被 kill 不丢任务
- UpstreamError 自动重试 2 次（每次隔 15s），耗尽后标 failed 并退回积分
- worker_max_tasks_per_child=50：定期重建进程，防模型/连接内存泄漏
"""
from datetime import datetime
import logging

from celery import Celery
from celery.schedules import crontab

from . import models
from .config import settings
from .credits import refund_credits
from .db import SessionLocal
from .upstream import UpstreamError, get_segmenter

logger = logging.getLogger("bff.worker")

celery_app = Celery(
    "wechat_tools",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=False,
    task_track_started=True,
    worker_max_tasks_per_child=50,
    task_acks_late=True,
    worker_prefetch_multiplier=1,  # 长任务（10s~5min）不预取，均匀分发
)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=15)
def run_cutout(self, task_no: str):
    """抠图任务：调上游分割模型，结果 URL 写回 task。"""
    db = SessionLocal()
    try:
        task = db.query(models.Task).filter(models.Task.task_no == task_no).first()
        if not task:
            return {"status": "not_found"}
        if task.status != "pending":
            return {"status": task.status}  # 幂等：重投/重复消费直接返回

        task.status = "processing"
        db.commit()

        result = get_segmenter().segment(
            task.params.get("image_url", ""), task.params.get("output_format", "png")
        )

        task.status = "success"
        task.result_url = result.url
        task.finished_at = datetime.now()
        db.commit()
        return {"status": "success", "elapsed_ms": result.elapsed_ms}

    except UpstreamError as e:
        # 上游网络/限流：重试；耗尽后标失败并退回积分
        db.rollback()
        if self.request.retries >= self.max_retries:
            _mark_failed(db, task_no, f"upstream retry exhausted: {e}")
            return {"status": "failed", "error": str(e)[:200]}
        raise self.retry(exc=e)
    except Exception as e:  # noqa: BLE001 —— worker 内兜底：任何异常都必须落终态 + 退款
        db.rollback()
        _mark_failed(db, task_no, str(e))
        return {"status": "failed", "error": str(e)[:200]}
    finally:
        db.close()


def _mark_failed(db, task_no: str, error: str) -> None:
    """任务失败落终态 + 原路退回积分（幂等：success 任务不退、已 failed 不重复退）。"""
    task = db.query(models.Task).filter(models.Task.task_no == task_no).first()
    if not task or task.status == "success":
        return
    task.status = "failed"
    task.error = error[:500]
    task.finished_at = datetime.now()
    if task.cost_credits > 0:
        user = db.query(models.User).filter(models.User.id == task.user_id).first()
        if user:
            refund_credits(db, user, task.cost_credits, ref_id=task_no)
    db.commit()


# ============ 网关发放补偿（quota_grants pending -> synced/failed） ============

@celery_app.task
def compensate_quota_grants():
    """每分钟扫 quota_grants：退避到期的 pending 重试 add_quota。

    add_quota 无幂等键（adapter 契约第 5 条），重试安全靠 quota_grants 状态机：
    只有 pending 会被扫到，synced 不再动；failed（重试耗尽）由管理台人工介入。
    """
    from .quota_sync import compensate_pending

    db = SessionLocal()
    try:
        stats = compensate_pending(db)
        if stats["attempted"]:
            celery_app.logger.info("quota grant compensation: %s", stats)
        return stats
    finally:
        db.close()


# ============ 日终对账（P2） ============

@celery_app.task
def reconcile_daily():
    """每天 03:00 日终对账：发放账本自洽 + 补偿积压 + 管理员池子余额阈值告警。

    告警出口为 celery logger（关键字 RECON）；接 webhook 通知时改 quota_sync.daily_reconcile。
    """
    from .quota_sync import daily_reconcile

    db = SessionLocal()
    try:
        return daily_reconcile(db)
    finally:
        db.close()


# ============ 视频任务（网关扣费，BFF 只编排） ============

_VIDEO_POLL_INTERVAL = 15       # 秒
_VIDEO_POLL_MAX_RETRIES = 80    # 15s * 80 = 20 分钟封顶


@celery_app.task(bind=True, max_retries=_VIDEO_POLL_MAX_RETRIES + 5, default_retry_delay=15)
def run_video_task(self, task_no: str):
    """视频任务：提交网关 -> 自轮询直到终态。

    - 扣费/失败退回由网关在该用户影子账号上**原生完成**，本任务不碰积分；
    - 提交失败：重试 3 次后标 failed（网关拒绝属业务失败不重试）；
    - 轮询：self.retry(countdown) 自调度，20 分钟未终态标 failed（timeout）。
    注意 max_retries 覆盖轮询次数（自调度靠 retry 计数），提交阶段用显式上限 3。
    """
    from .newapi_adapter import NewApiError, VideoTaskAdapter, get_video_adapter
    from .quota_sync import get_user_pat

    db = SessionLocal()
    try:
        task = db.query(models.Task).filter(models.Task.task_no == task_no).first()
        if not task:
            return {"status": "not_found"}

        pat_info = get_user_pat(db, task.user_id)
        if pat_info is None:
            _mark_failed(db, task_no, "user gateway PAT not ready")
            return {"status": "failed"}
        pat, uid = pat_info

        # ---- 阶段 1：提交 ----
        if task.status == "pending":
            task.status = "processing"
            db.commit()
            adapter = get_video_adapter(task.params.get("model", ""))
            try:
                gateway_task_id = adapter.submit(task.params.get("model", ""),
                                                 task.params, pat, uid)
            except NewApiError as e:
                if e.status_code == 501:  # 未适配：立即终态，不重试
                    _mark_failed(db, task_no, str(e))
                    return {"status": "failed"}
                db.rollback()
                if self.request.retries >= 3:
                    _mark_failed(db, task_no, f"gateway submit failed: {e}")
                    return {"status": "failed"}
                raise self.retry(exc=e)
            task.params = {**task.params, "gateway_task_id": gateway_task_id}
            db.add(task)
            db.commit()

        # ---- 阶段 2：轮询 ----
        gateway_task_id = task.params.get("gateway_task_id", "")
        if not gateway_task_id:
            _mark_failed(db, task_no, "missing gateway_task_id")
            return {"status": "failed"}
        adapter = get_video_adapter(task.params.get("model", ""))
        try:
            result = adapter.query(gateway_task_id, pat, uid)
        except NewApiError as e:
            db.rollback()
            if self.request.retries >= _VIDEO_POLL_MAX_RETRIES:
                _mark_failed(db, task_no, f"video poll timeout/exhausted: {e}")
                return {"status": "failed"}
            raise self.retry(exc=e, countdown=_VIDEO_POLL_INTERVAL)

        if result.get("status") == "success":
            task.status = "success"
            task.result_url = result.get("video_url", "")[:1024]
            task.finished_at = datetime.now()
            db.commit()
            return {"status": "success"}
        if result.get("status") == "failed":
            _mark_failed(db, task_no, result.get("error") or "gateway task failed")
            return {"status": "failed"}

        # processing：继续轮询
        if self.request.retries >= _VIDEO_POLL_MAX_RETRIES:
            _mark_failed(db, task_no, "video poll timeout (20min)")
            return {"status": "failed"}
        raise self.retry(countdown=_VIDEO_POLL_INTERVAL)

    except Exception as e:  # noqa: BLE001 —— 兜底落终态（网关侧扣费不退，人工对账兜底）
        db.rollback()
        logger.error("run_video_task unexpected error %s: %s", task_no, e)
        _mark_failed_no_refund(db, task_no, str(e))
        return {"status": "failed", "error": str(e)[:200]}
    finally:
        db.close()


def _mark_failed_no_refund(db, task_no: str, error: str) -> None:
    """视频任务失败：网关侧计费由网关自行处理，BFF 不退积分（cost_credits 仅展示价）。"""
    task = db.query(models.Task).filter(models.Task.task_no == task_no).first()
    if not task or task.status == "success":
        return
    task.status = "failed"
    task.error = error[:500]
    task.finished_at = datetime.now()
    db.commit()


celery_app.conf.beat_schedule = {
    "compensate-quota-grants-every-minute": {
        "task": "app.worker.compensate_quota_grants",
        "schedule": 60.0,
    },
    "reconcile-daily-0300": {
        "task": "app.worker.reconcile_daily",
        "schedule": crontab(hour=3, minute=0),
    },
}
