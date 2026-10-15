"""网关加额统一入口：占位 -> add_quota -> synced，pending 由补偿 worker 重试。

为什么需要这个模块（方案 4.4 定案 2026-10-15：new-api quota 为唯一账本）：
- new-api POST /api/user/manage **无幂等键**，重复调用重复加钱；
- 所以任何发放必须先在 quota_grants 表占位（ref_id UNIQUE），再调网关；
- 同步失败不回滚占位（回滚会导致重复发放窗口），留 pending 交补偿 worker 退避重试。

并发语义：
- 占位靠 UNIQUE(ref_id) 兜底：并发/重复调用命中 IntegrityError 视为已发放，直接返回；
- 同一用户多笔 pending 串行无依赖（add_quota 天然可加），无需行锁。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, newapi_adapter
from .config import settings

logger = logging.getLogger("bff.quota_sync")

# 退避：第 n 次重试前至少间隔 2^n 分钟（1,2,4,8...），封顶 60 分钟
_RETRY_BACKOFF_MINUTES_CAP = 60


class GrantDuplicate(Exception):
    """ref_id 已占位（并发/重复发放），调用方按幂等返回即可。"""

    def __init__(self, ref_id: str):
        super().__init__(f"grant already exists: {ref_id}")
        self.ref_id = ref_id


def credits_to_quota(amount_credits: int) -> int:
    return int(amount_credits) * settings.QUOTA_PER_CREDIT


def _backoff_deadline(retries: int) -> datetime:
    minutes = min(2 ** retries, _RETRY_BACKOFF_MINUTES_CAP)
    return datetime.now() - timedelta(minutes=minutes)


def enqueue_grant(
    db: Session,
    user_id: int,
    change_type: str,
    ref_id: str,
    amount: int,
    app_id: str = "",
) -> models.QuotaGrant | None:
    """占位一笔发放。ref_id 已存在（并发/重复）抛 GrantDuplicate。

    注意：
    - 只占位不调网关；调用方在事务提交后自行调 sync_grant 同步尝试；
    - IntegrityError 会毒化当前事务，本函数不吞不滚——由调用方 rollback 后按幂等处理。
    """
    if amount <= 0:
        raise ValueError("grant amount must be positive")
    grant = models.QuotaGrant(
        user_id=user_id,
        app_id=app_id,
        change_type=change_type,
        ref_id=ref_id,
        amount=amount,
        quota_value=credits_to_quota(amount),
    )
    db.add(grant)
    try:
        db.flush()
    except IntegrityError as e:
        raise GrantDuplicate(ref_id) from e
    return grant


def sync_grant(db: Session, grant: models.QuotaGrant) -> str:
    """同步尝试一笔发放：add_quota -> synced / retries+1。返回最终 status。

    上游不可达（502/网络）与业务拒绝（400）都进重试；区别只在日志等级。
    影子账号缺失视为数据不一致：置 failed 立即告警（人工对账），绝不静默丢。
    """
    upstream = (
        db.query(models.UserUpstream)
        .filter(models.UserUpstream.user_id == grant.user_id,
                models.UserUpstream.upstream == "newapi")
        .first()
    )
    if upstream is None:
        grant.status = "failed"
        grant.last_error = "user has no upstream mapping (shadow account missing)"
        db.add(grant)
        db.commit()
        logger.error("grant %s failed permanently: %s", grant.ref_id, grant.last_error)
        return grant.status

    try:
        newapi_adapter.add_quota(upstream.upstream_uid, grant.quota_value)
    except newapi_adapter.NewApiError as e:
        grant.retries += 1
        grant.last_error = str(e)[:500]
        if grant.retries >= settings.GRANT_MAX_RETRIES:
            grant.status = "failed"
            logger.error("grant %s exhausted retries (%s): %s",
                         grant.ref_id, grant.retries, e)
        db.add(grant)
        db.commit()
        return grant.status

    grant.status = "synced"
    grant.synced_at = datetime.now()
    grant.last_error = ""
    db.add(grant)
    db.commit()
    logger.info("grant %s synced: +%s credits (%s quota) to uid %s",
                grant.ref_id, grant.amount, grant.quota_value, upstream.upstream_uid)
    return grant.status


def flush_local_credits(db: Session, user: models.User, app_id: str = "") -> bool:
    """把 users.credits 里的存量积分（游客态赠送/旧账本）一次性折算成网关 quota 并清零。

    bind_phone 合并/绑定后调用。幂等：gift:{user_id} 占位；credits=0 直接返回。
    """
    if user.credits <= 0:
        return True
    amount = user.credits
    try:
        grant = enqueue_grant(db, user.id, "gift", f"gift:{user.id}", amount, app_id=app_id)
    except GrantDuplicate:
        # 已折算过（重试路径）：直接清零本地，不再重复发放
        grant = None
    if grant is not None:
        user.credits = 0
        db.add(user)
        db.commit()
    else:
        db.rollback()
        user.credits = 0
        db.add(user)
        db.commit()
        return True
    status = sync_grant(db, grant)
    return status == "synced"


def compensate_pending(db: Session, batch: int = 50) -> dict:
    """补偿 worker 入口：扫退避到期的 pending 发放重试。返回统计。"""
    rows = (
        db.query(models.QuotaGrant)
        .filter(models.QuotaGrant.status == "pending")
        .order_by(models.QuotaGrant.id.asc())
        .limit(batch * 3)
        .all()
    )
    done = synced = failed = 0
    for g in rows:
        if g.updated_at and g.updated_at > _backoff_deadline(g.retries):
            continue  # 未到退避时间
        status = sync_grant(db, g)
        done += 1
        synced += status == "synced"
        failed += status == "failed"
        if done >= batch:
            break
    return {"attempted": done, "synced": synced, "failed": failed}


# ============ 虚拟支付结算（幂等闸门，方案 4.4「幂等铁律」） ============

def settle_vpay_order(db: Session, order_no: str) -> str:
    """微信回调结算：created -> paid -> syncing -> synced。返回终态语义。

    顺序铁律：先落库占位、再调网关，严禁先加额后记账。
    - 闸门1 created->paid：重复回调（微信重推）在第一闸直接返回 duplicate；
    - 闸门2 paid->syncing：防「结算中」被并发重放；
    - 真正的发放幂等由 quota_grants UNIQUE(ref_id=vpay:{order_no}) 兜底——
      就算前两闸全被绕过，发放也只发生一次。
    """
    now = datetime.now()
    order = (
        db.query(models.VpayOrder)
        .filter(models.VpayOrder.order_no == order_no)
        .with_for_update()
        .first()
    )
    if order is None:
        return "unknown_order"
    if order.status == "synced":
        return "already_synced"
    if order.status == "created":
        order.status = "paid"
        order.paid_at = now
        db.add(order)
        db.commit()
    if order.status != "paid":
        return f"unexpected_status:{order.status}"

    # paid -> syncing（另一进程已 syncing 时这里 0 行命中）
    n = (
        db.query(models.VpayOrder)
        .filter(models.VpayOrder.order_no == order_no, models.VpayOrder.status == "paid")
        .update({"status": "syncing"})
    )
    db.commit()
    if n == 0:
        return "already_settling"

    # 发放（quota_grants 幂等：重复结算不会重复加钱）
    grant = models.QuotaGrant(
        user_id=order.user_id, app_id=order.app_id, change_type="vpay",
        ref_id=f"vpay:{order_no}", amount=order.credits,
        quota_value=credits_to_quota(order.credits),
    )
    db.add(grant)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        grant = (
            db.query(models.QuotaGrant)
            .filter(models.QuotaGrant.ref_id == f"vpay:{order_no}")
            .first()
        )
        if grant and grant.status == "synced":
            _mark_order(db, order_no, "synced")
            return "already_synced"
        return "already_settling"

    status = sync_grant(db, grant)
    _mark_order(db, order_no, "synced" if status == "synced" else "syncing")
    return f"settled:{status}"


def _mark_order(db: Session, order_no: str, status: str) -> None:
    db.query(models.VpayOrder).filter(
        models.VpayOrder.order_no == order_no
    ).update({"status": status})
    db.commit()


# ============ 充值结算（幂等闸门，复用 quota_grants 发放队列） ============


def _mark_recharge(db: Session, order_no: str, status: str) -> None:
    db.query(models.RechargeOrder).filter(
        models.RechargeOrder.order_no == order_no
    ).update({"status": status})
    db.commit()


def settle_recharge_order(db: Session, order_no: str) -> str:
    """微信充值回调结算：created -> paid -> syncing -> synced。返回终态语义。

    顺序铁律（对齐 settle_vpay_order）：
    - 闸门1 created->paid：重复回调（微信重推）在首闸直接返回 already_synced；
    - 闸门2 paid->syncing：防「发放中」被并发重放；
    - 真正发放幂等由 quota_grants UNIQUE(ref_id=recharge:{order_no}) 兜底——
      就算前两闸全被绕过，配额也只加一次。
    """
    now = datetime.now()
    order = (
        db.query(models.RechargeOrder)
        .filter(models.RechargeOrder.order_no == order_no)
        .with_for_update()
        .first()
    )
    if order is None:
        return "unknown_order"
    if order.status == "synced":
        return "already_synced"
    if order.status == "created":
        order.status = "paid"
        order.paid_at = now
        db.add(order)
        db.commit()
    if order.status != "paid":
        return f"unexpected_status:{order.status}"

    n = (
        db.query(models.RechargeOrder)
        .filter(models.RechargeOrder.order_no == order_no, models.RechargeOrder.status == "paid")
        .update({"status": "syncing"})
    )
    db.commit()
    if n == 0:
        return "already_settling"

    grant = models.QuotaGrant(
        user_id=order.user_id, app_id=order.app_id, change_type="recharge",
        ref_id=f"recharge:{order_no}", amount=order.credits,
        quota_value=credits_to_quota(order.credits),
    )
    db.add(grant)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        grant = (
            db.query(models.QuotaGrant)
            .filter(models.QuotaGrant.ref_id == f"recharge:{order_no}")
            .first()
        )
        if grant and grant.status == "synced":
            _mark_recharge(db, order_no, "synced")
            return "already_synced"
        return "already_settling"

    status = sync_grant(db, grant)
    _mark_recharge(db, order_no, "synced" if status == "synced" else "syncing")
    return f"settled:{status}"


# ============ 用户侧网关访问（PAT / 余额代理） ============

def get_user_pat(db: Session, user_id: int) -> tuple[str, int] | None:
    """取用户影子账号 PAT。未就绪返回 None（调用方引导绑定/重试 bind-phone）。"""
    upstream = (
        db.query(models.UserUpstream)
        .filter(models.UserUpstream.user_id == user_id,
                models.UserUpstream.upstream == "newapi")
        .first()
    )
    if not upstream or not upstream.pat_enc:
        return None
    try:
        return newapi_adapter.decrypt_pat(upstream.pat_enc), upstream.upstream_uid
    except newapi_adapter.NewApiError:
        return None


def get_gateway_balance(db: Session, user_id: int, use_cache: bool = True) -> dict:
    """余额代理：quota_cache 命中直接返回，否则代查网关并回写缓存。

    返回 {quota_raw, credits_display, cached, linked}；
    linked=False 表示影子账号/PAT 未就绪，前端引导重试 bind-phone。
    """
    upstream = (
        db.query(models.UserUpstream)
        .filter(models.UserUpstream.user_id == user_id,
                models.UserUpstream.upstream == "newapi")
        .first()
    )
    if not upstream or not upstream.pat_enc:
        return {"quota_raw": None, "credits_display": None, "cached": False, "linked": False}

    fresh = (
        use_cache and upstream.quota_cached_at
        and (datetime.now() - upstream.quota_cached_at).total_seconds() < settings.BALANCE_CACHE_SECONDS
    )
    if fresh:
        return {"quota_raw": upstream.quota_cache,
                "credits_display": upstream.quota_cache // settings.QUOTA_PER_CREDIT,
                "cached": True, "linked": True}

    try:
        info = newapi_adapter.get_user_self(*get_user_pat(db, user_id))
        quota = int(info.get("quota") or 0)
    except newapi_adapter.NewApiError as e:
        # PAT 失效/网络异常：返回旧缓存（可能为 0），不强杀会话
        logger.warning("gateway balance fetch failed for user %s: %s", user_id, e)
        return {"quota_raw": upstream.quota_cache or None,
                "credits_display": (upstream.quota_cache or 0) // settings.QUOTA_PER_CREDIT or None,
                "cached": True, "linked": True}

    upstream.quota_cache = quota
    upstream.quota_cached_at = datetime.now()
    db.add(upstream)
    db.commit()
    return {"quota_raw": quota,
            "credits_display": quota // settings.QUOTA_PER_CREDIT,
            "cached": False, "linked": True}


# ============ 日终对账（P2） ============

def daily_reconcile(db: Session) -> dict:
    """日终对账：发放账本自洽 + 管理员池子余额阈值告警。

    三项检查：
    1. quota_grants 自洽——quota_value 应等于 amount * QUOTA_PER_CREDIT（换算系数改过会暴露）
    2. 补偿积压——pending 超过阈值说明网关持续不可用，须人工介入
    3. 管理员池子余额——低于 RECON_MIN_ADMIN_QUOTA 告警（所有用户的网关调用共用它兜底）

    MVP 告警出口是日志（logger.warning，关键字 RECON 便于检索）；接钉钉/企微 webhook 时
    只需在 _recon_alert 里加通知渠道，不影响对账逻辑。
    """
    report: dict = {"checked_at": datetime.now().isoformat(), "alerts": []}

    # 1) 发放账本自洽（抽样全量算，量级小）
    bad_grants = (
        db.query(models.QuotaGrant)
        .filter(
            models.QuotaGrant.status == "synced",
            models.QuotaGrant.quota_value != models.QuotaGrant.amount * settings.QUOTA_PER_CREDIT,
        )
        .count()
    )
    if bad_grants:
        report["alerts"].append(f"RECON: {bad_grants} 笔 synced 发放的 quota_value 与换算系数不符（改过 QUOTA_PER_CREDIT？）")

    # 2) 补偿积压
    pending = db.query(models.QuotaGrant).filter(models.QuotaGrant.status == "pending").count()
    failed = db.query(models.QuotaGrant).filter(models.QuotaGrant.status == "failed").count()
    report["pending_grants"] = pending
    report["failed_grants"] = failed
    if pending >= settings.RECON_PENDING_ALERT:
        report["alerts"].append(f"RECON: pending 发放积压 {pending} 笔（阈值 {settings.RECON_PENDING_ALERT}），网关是否持续异常？")
    if failed:
        report["alerts"].append(f"RECON: failed 发放 {failed} 笔，超过重试上限需人工处理（/admin/grants?status=failed）")

    # 3) 管理员池子余额（管理员本人也应有 user_upstream 映射；没有则跳过并提示）
    admin_up = (
        db.query(models.UserUpstream)
        .filter(models.UserUpstream.user_id == settings.NEWAPI_ADMIN_UID, models.UserUpstream.status == 1)
        .first()
        if settings.NEWAPI_ADMIN_UID
        else None
    )
    if admin_up and settings.RECON_MIN_ADMIN_QUOTA > 0:
        try:
            info = newapi_adapter.get_user_self(*get_user_pat(db, admin_up.user_id))
            balance = int(info.get("quota") or 0)
            report["admin_quota"] = balance
            if balance < settings.RECON_MIN_ADMIN_QUOTA:
                report["alerts"].append(
                    f"RECON: 管理员池子余额 {balance} 低于阈值 {settings.RECON_MIN_ADMIN_QUOTA}，请及时充值"
                )
        except newapi_adapter.NewApiError as e:
            report["alerts"].append(f"RECON: 管理员余额查询失败：{e.message}（不影响发放，请检查凭证）")

    for msg in report["alerts"]:
        logger.warning(msg)
    if not report["alerts"]:
        logger.info("RECON: 日终对账通过，无告警")
    return report
