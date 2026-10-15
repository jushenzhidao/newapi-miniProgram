"""积分服务：冻结(扣除)/退回/发放，全部带行锁 + 流水。

并发模型说明：
- MySQL 行锁（SELECT ... FOR UPDATE）保证同一用户积分扣减串行化；
- credit_logs.balance_after 记录每次变动后余额，用于对账与异常审计；
- 扣除积分在创建任务的事务内完成（提交即冻结），失败由 worker 原路退回。

账本口径（2026-10-15 定案：new-api quota 为唯一账本）：
- 签到/广告等「入账」已切 quota_sync 发放队列（占位 -> add_quota -> synced）；
- 本文件的 spend/refund/grant_credits 仅供**存量抠图流程**（腾讯上游、不走网关）
  与游客注册赠送使用，逐步收敛；新功能一律走 quota_sync。
"""
from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .config import settings


class CreditError(Exception):
    def __init__(self, message: str, code: int = 40001):
        self.message = message
        self.code = code
        super().__init__(message)


def _write_log(
    db: Session,
    user: models.User,
    change_type: str,
    amount: int,
    balance_after: int,
    app_id: str = "",
    ref_type: str = "",
    ref_id: str = "",
    remark: str = "",
) -> models.CreditLog:
    log = models.CreditLog(
        user_id=user.id,
        app_id=app_id,
        change_type=change_type,
        amount=amount,
        balance_after=balance_after,
        ref_type=ref_type,
        ref_id=ref_id,
        remark=remark,
    )
    db.add(log)
    return log


def grant_credits(
    db: Session,
    user: models.User,
    amount: int,
    change_type: str,
    app_id: str = "",
    remark: str = "",
) -> models.CreditLog:
    """加积分（注册赠送/签到/广告/充值到账）。调用方负责事务边界。"""
    if amount <= 0:
        raise CreditError("grant amount must be positive")
    db.refresh(user)
    user.credits += amount
    db.add(user)
    return _write_log(db, user, change_type, amount, user.credits, app_id=app_id, remark=remark)


def spend_credits(
    db: Session,
    user: models.User,
    amount: int,
    change_type: str = "spend",
    app_id: str = "",
    ref_type: str = "task",
    ref_id: str = "",
    remark: str = "",
) -> models.CreditLog:
    """扣积分（提交任务即冻结）。余额不足抛 CreditError(40201)。"""
    if amount <= 0:
        raise CreditError("spend amount must be positive")
    # 行锁重读，防并发超扣
    locked = (
        db.query(models.User)
        .filter(models.User.id == user.id)
        .with_for_update()
        .one()
    )
    if locked.credits < amount:
        raise CreditError("insufficient credits", code=40201)
    locked.credits -= amount
    db.add(locked)
    db.flush()
    user.credits = locked.credits
    return _write_log(
        db, locked, change_type, -amount, locked.credits,
        app_id=app_id, ref_type=ref_type, ref_id=ref_id, remark=remark,
    )


def refund_credits(
    db: Session,
    user: models.User,
    amount: int,
    app_id: str = "",
    ref_type: str = "task",
    ref_id: str = "",
    remark: str = "任务失败退回",
) -> models.CreditLog:
    """退回积分（任务失败）。"""
    locked = (
        db.query(models.User)
        .filter(models.User.id == user.id)
        .with_for_update()
        .one()
    )
    locked.credits += amount
    db.add(locked)
    db.flush()
    user.credits = locked.credits
    return _write_log(
        db, locked, "refund", amount, locked.credits,
        app_id=app_id, ref_type=ref_type, ref_id=ref_id, remark=remark,
    )


def do_checkin(db: Session, user: models.User, app_id: str = "") -> tuple[int, str, int]:
    """每日签到（新账本：签到日志 + 发放占位，网关 add_quota 由调用方在事务外完成）。

    返回 (本次积分, ref_id, 连续天数)。重复签到抛 CreditError(40010)。
    幂等三重保证：显式预检 + CheckinLog 唯一约束 + quota_grants UNIQUE(ref_id)。
    """
    from . import quota_sync  # 局部导入避免循环依赖（quota_sync 引 models/config）

    today = date.today()
    existed = (
        db.query(models.CheckinLog)
        .filter(models.CheckinLog.user_id == user.id, models.CheckinLog.checkin_date == today)
        .first()
    )
    if existed:
        raise CreditError("already checked in today", code=40010)

    amount = settings.CHECKIN_CREDITS
    ref_id = f"checkin:{user.id}:{today.isoformat()}"
    db.add(models.CheckinLog(user_id=user.id, app_id=app_id, checkin_date=today, credits=amount))
    try:
        quota_sync.enqueue_grant(db, user.id, "checkin", ref_id, amount, app_id=app_id)
    except quota_sync.GrantDuplicate:
        raise CreditError("already checked in today", code=40010)
    db.flush()

    # 连续签到天数（MVP 简化为累计签到天数，连续判定二期加）
    streak = (
        db.query(func.count(func.distinct(models.CheckinLog.checkin_date)))
        .filter(models.CheckinLog.user_id == user.id)
        .scalar()
        or 1
    )
    return amount, ref_id, streak


def grant_ad_reward(db: Session, user: models.User, app_id: str = "") -> tuple[int, str, int]:
    """激励视频奖励（新账本：奖励记录 + 发放占位）。超限抛 CreditError(40011)。

    返回 (本次积分, ref_id, 今日已领次数)。
    """
    from . import quota_sync

    today = date.today()
    today_count = (
        db.query(func.count(models.AdRewardLog.id))
        .filter(
            models.AdRewardLog.user_id == user.id,
            func.date(models.AdRewardLog.created_at) == today,
        )
        .scalar()
        or 0
    )
    if today_count >= settings.AD_REWARD_DAILY_LIMIT:
        raise CreditError("daily ad reward limit reached", code=40011)

    amount = settings.AD_REWARD_CREDITS
    ref_id = f"ad:{user.id}:{today.isoformat()}:{today_count + 1}"
    db.add(models.AdRewardLog(user_id=user.id, app_id=app_id, credits=amount))
    try:
        quota_sync.enqueue_grant(db, user.id, "ad_reward", ref_id, amount, app_id=app_id)
    except quota_sync.GrantDuplicate:
        # 同 ref 重复：按已领处理（超限语义），不重复发放
        raise CreditError("daily ad reward limit reached", code=40011)
    db.flush()
    return amount, ref_id, today_count + 1


def get_balance(db: Session, user_id: int) -> int:
    return db.query(models.User).filter(models.User.id == user_id).one().credits


def list_logs(db: Session, user_id: int, page: int = 1, size: int = 20) -> list[models.CreditLog]:
    return (
        db.query(models.CreditLog)
        .filter(models.CreditLog.user_id == user_id)
        .order_by(models.CreditLog.id.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
