"""P1 单测：quota_sync 发放幂等与补偿 / vpay 闸门 / 管理台鉴权。

用 sqlite 内存库跑（models 兼容），不碰真实网关（add_quota 全 mock）。
"""
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("PAT_ENC_KEY", "xnAsTlDXsmUJMTcUexE0Il9kcl3MFGKCq4A0V0lKzjg=")

from app import models, quota_sync  # noqa: E402
from app.db import Base  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def user_with_upstream(db, monkeypatch):
    """用户 + 影子账号映射（PAT 已加密落库）。"""
    u = models.User(nickname="测试", phone="13800138000", password_hash="x")
    db.add(u)
    db.flush()
    db.add(models.UserUpstream(
        user_id=u.id, upstream="newapi", upstream_uid=1001, upstream_name="mp13800138000",
        pat_enc=quota_sync.newapi_adapter.encrypt_pat("pat-test"),
    ))
    db.commit()
    return u


@pytest.fixture()
def mock_add_quota(monkeypatch):
    """记录 add_quota 调用；可注入异常。"""
    calls = []

    def fake(uid, value):
        if quota_sync._FAIL_NEXT[0] is not None:
            err = quota_sync._FAIL_NEXT[0]
            quota_sync._FAIL_NEXT[0] = None
            raise err
        calls.append((uid, value))  # 只记成功发放（失败重试不算到账）

    monkeypatch.setattr(quota_sync.newapi_adapter, "add_quota", fake)
    quota_sync._CALLS = calls
    return calls


# quota_sync 模块级测试钩子（生产代码不读）
quota_sync._FAIL_NEXT = [None]
quota_sync._CALLS = []


def test_enqueue_grant_unique_ref(db, user_with_upstream):
    g1 = quota_sync.enqueue_grant(db, user_with_upstream.id, "checkin", "checkin:1:2026-10-15", 10)
    assert g1 is not None
    with pytest.raises(quota_sync.GrantDuplicate):
        quota_sync.enqueue_grant(db, user_with_upstream.id, "checkin", "checkin:1:2026-10-15", 10)


def test_sync_grant_success(db, user_with_upstream, mock_add_quota):
    g = quota_sync.enqueue_grant(db, user_with_upstream.id, "checkin", "ref-ok", 10)
    db.commit()
    status = quota_sync.sync_grant(db, g)
    assert status == "synced"
    assert g.synced_at is not None
    assert mock_add_quota == [(1001, 10 * 5000)]


def test_sync_grant_retry_then_fail_cap(db, user_with_upstream, mock_add_quota):
    g = quota_sync.enqueue_grant(db, user_with_upstream.id, "vpay", "ref-retry", 10)
    db.commit()
    quota_sync._FAIL_NEXT[0] = quota_sync.newapi_adapter.NewApiError("boom", status_code=502)
    assert quota_sync.sync_grant(db, g) == "pending"
    assert g.retries == 1
    # 重试成功
    assert quota_sync.sync_grant(db, g) == "synced"


def test_sync_grant_no_upstream_fails_permanently(db, mock_add_quota):
    u = models.User(nickname="无映射")
    db.add(u)
    db.commit()
    g = quota_sync.enqueue_grant(db, u.id, "checkin", "ref-noup", 10)
    db.commit()
    assert quota_sync.sync_grant(db, g) == "failed"
    assert "upstream" in g.last_error


def test_compensate_skips_recent_and_syncs_due(db, user_with_upstream, mock_add_quota):
    g = quota_sync.enqueue_grant(db, user_with_upstream.id, "checkin", "ref-comp", 10)
    db.commit()
    g.retries = 3  # 退避 8 分钟
    g.updated_at = datetime.now() - timedelta(minutes=10)  # 已到期
    db.commit()
    stats = quota_sync.compensate_pending(db)
    assert stats["synced"] == 1


def test_settle_vpay_order_idempotent(db, user_with_upstream, mock_add_quota):
    db.add(models.VpayOrder(order_no="NO123", user_id=user_with_upstream.id,
                            app_id="wx1", amount=1000, credits=50))
    db.commit()
    r1 = quota_sync.settle_vpay_order(db, "NO123")
    assert r1 == "settled:synced"
    # 微信重复回调
    r2 = quota_sync.settle_vpay_order(db, "NO123")
    assert r2 == "already_synced"
    # 发放只发生一次
    assert len(mock_add_quota) == 1


def test_settle_vpay_order_upstream_fail_stays_syncing(db, user_with_upstream, mock_add_quota):
    db.add(models.VpayOrder(order_no="NO456", user_id=user_with_upstream.id,
                            app_id="wx1", amount=1000, credits=50))
    db.commit()
    quota_sync._FAIL_NEXT[0] = quota_sync.newapi_adapter.NewApiError("gw down", status_code=502)
    r = quota_sync.settle_vpay_order(db, "NO456")
    assert r == "settled:pending"
    order = db.query(models.VpayOrder).filter_by(order_no="NO456").one()
    assert order.status == "syncing"  # 留在 syncing，等补偿 worker
    # 重试发放成功（compensate 的退避跳过已有专项测试覆盖）
    g = db.query(models.QuotaGrant).filter_by(ref_id="vpay:NO456").one()
    assert quota_sync.sync_grant(db, g) == "synced"
    assert len(mock_add_quota) == 1


def test_flush_local_credits_once(db, user_with_upstream, mock_add_quota):
    user_with_upstream.credits = 100
    db.commit()
    assert quota_sync.flush_local_credits(db, user_with_upstream) is True
    assert user_with_upstream.credits == 0
    # 再次调用（重试路径）不重复发放
    user_with_upstream.credits = 0
    assert quota_sync.flush_local_credits(db, user_with_upstream) is True
    assert len(mock_add_quota) == 1


def test_password_rule_in_schemas():
    from app.schemas import BindPhoneReq
    from pydantic import ValidationError
    ok = BindPhoneReq(phone_code="c", password="abc12345")
    assert ok.password == "abc12345"
    for bad in ("short1a", "1234567890", "abcdefgh", "a" * 21 + "1"):
        with pytest.raises(ValidationError):
            BindPhoneReq(phone_code="c", password=bad)


# ============ P2：日终对账 ============

def test_daily_reconcile_clean(db, user_with_upstream, monkeypatch):
    """无异常时 report 无告警；管理员余额正常不触发阈值告警。"""
    from app.config import settings

    g = quota_sync.enqueue_grant(db, user_with_upstream.id, "checkin", "recon-ok", 10)
    db.commit()
    quota_sync.sync_grant(db, g)

    # 管理员池子 = user_with_upstream（NEWAPI_ADMIN_UID 指到它的上游 uid）
    monkeypatch.setattr(settings, "NEWAPI_ADMIN_UID", user_with_upstream.id, raising=False)
    monkeypatch.setattr(
        quota_sync.newapi_adapter, "get_user_self",
        lambda *a, **kw: {"quota": 2_000_000_000, "used_quota": 1},
        raising=True,
    )
    monkeypatch.setattr(settings, "RECON_MIN_ADMIN_QUOTA", 1_000_000_000, raising=False)
    monkeypatch.setattr(settings, "RECON_PENDING_ALERT", 20, raising=False)

    report = quota_sync.daily_reconcile(db)
    assert report["alerts"] == []
    assert report["admin_quota"] == 2_000_000_000


def test_daily_reconcile_alerts(db, user_with_upstream, monkeypatch):
    """系数不一致 + 积压 + 余额不足 三类告警都要出来。"""
    from app.config import settings

    # 1) 系数不一致的 synced 发放
    g = quota_sync.enqueue_grant(db, user_with_upstream.id, "checkin", "recon-bad", 10)
    db.commit()
    g.status = "synced"
    g.quota_value = 999  # 与 amount * QUOTA_PER_CREDIT 不符
    db.add(g)
    # 2) pending 积压
    quota_sync.enqueue_grant(db, user_with_upstream.id, "ad_reward", "recon-pending", 20)
    db.commit()

    monkeypatch.setattr(settings, "NEWAPI_ADMIN_UID", user_with_upstream.id, raising=False)
    monkeypatch.setattr(
        quota_sync.newapi_adapter, "get_user_self",
        lambda *a, **kw: {"quota": 1, "used_quota": 1},
        raising=True,
    )
    monkeypatch.setattr(settings, "RECON_MIN_ADMIN_QUOTA", 1_000_000_000, raising=False)
    monkeypatch.setattr(settings, "RECON_PENDING_ALERT", 1, raising=False)

    report = quota_sync.daily_reconcile(db)
    joined = " | ".join(report["alerts"])
    assert "换算系数不符" in joined
    assert "积压" in joined
    assert "低于阈值" in joined


def test_reconcile_worker_task_registered():
    """celery beat 里有日终对账 + 补偿两个调度。"""
    from app.worker import celery_app

    names = set(celery_app.conf.beat_schedule.keys())
    assert "reconcile-daily-0300" in names
    assert "compensate-quota-grants-every-minute" in names
