"""ORM 模型：多小程序统一用户体系 + 任务 + 积分。

设计要点：
- users.phone 为业务主键（可空：游客态；唯一：跨小程序同一自然人）
- user_identities 一个 user 对 N 个小程序的 openid，绑定时做账号合并
- apps 表驱动多小程序：新增小程序 = 插一行配置，BFF 代码零改动
- tasks 是 AI 工具通用任务表，type 区分工具（cutout/upscale/...），
  params/result 用 JSON 列，新工具无需改表
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .db import Base

# SQLite 测试环境要求主键为 Integer 才自增；MySQL 生产仍为 BIGINT
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # 手机号：跨小程序唯一主键；游客态为空（MySQL 唯一索引允许多个 NULL）
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True, nullable=True)
    nickname: Mapped[str] = mapped_column(String(64), default="微信用户", server_default="微信用户")
    avatar_url: Mapped[str] = mapped_column(String(512), default="", server_default="")
    # 积分：挂 users 上，全小程序通用（A 小程序充值 B 可用）
    # 注意：账本在 new-api quota（见方案 4.4），本列仅存运营侧积分语义，逐步降级为展示流水
    credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # 网关密码的 BFF 侧校验哈希（bcrypt）：web 页面手机号+密码登录用；明文不落盘
    password_hash: Mapped[str] = mapped_column(String(128), default="", server_default="")
    # 手机号验证时间/来源：区分「填了号」和「微信授权验过号」
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    phone_source: Mapped[str] = mapped_column(String(16), default="", server_default="")  # wechat_mp/sms/import
    # 小程序管理台管理员（ADMIN_PHONES 环境变量初始化，见 config）
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    status: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # 1 正常 0 禁用
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, server_default="now()"
    )


class UserIdentity(Base):
    """用户在各小程序的微信身份（openid 按小程序隔离）。"""

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("app_id", "openid", name="uq_identity_app_openid"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    app_id: Mapped[str] = mapped_column(String(32), nullable=False)  # 小程序 appid
    openid: Mapped[str] = mapped_column(String(64), nullable=False)
    unionid: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # session_key 缓存：解密类接口（如手机号 code 换取）需要，建议定期刷新
    session_key: Mapped[str] = mapped_column(String(128), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")


class App(Base):
    """多小程序注册表：工具清单/主题/虚拟支付 offer 全在这里配置。"""

    __tablename__ = "apps"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    app_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)  # 微信 appid
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 每个小程序独立 secret/sign_key，泄露不影响其他小程序
    secret: Mapped[str] = mapped_column(String(128), default="", server_default="")
    sign_key: Mapped[str] = mapped_column(String(128), nullable=False)  # HMAC 签名密钥
    # tools: [{"code":"cutout","cost":10,"enabled":true}, ...]
    tools: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    # theme: {"primary":"#F97316","title":"AI工具箱"}
    theme: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    # 虚拟支付 offerId（二期；为空且 VPAY_ENABLED 时该小程序不显示购买入口）
    vpay_offer_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    status: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")


class Task(Base):
    """AI 工具任务（通用）：提交即冻结(扣除)积分，失败原路退回。"""

    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("task_no", name="uq_tasks_task_no"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    task_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    app_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(32), default="cutout", server_default="cutout")
    # pending(排队中) processing(处理中) success failed
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", index=True)
    # 入参：{"image_url": "...", "output_format": "png"}
    params: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    result_url: Mapped[str] = mapped_column(String(1024), default="", server_default="")
    cost_credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str] = mapped_column(String(512), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, server_default="now()"
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CreditLog(Base):
    """积分流水：充值/赠送/签到/广告/消耗/退回，对账与审计靠它。"""

    __tablename__ = "credit_logs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    app_id: Mapped[str] = mapped_column(String(32), default="", server_default="")
    # change_type: register_gift | checkin | ad_reward | spend | refund | vpay
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # 正数入账，负数出账
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)  # 变动后余额，防篡改对账
    ref_type: Mapped[str] = mapped_column(String(32), default="", server_default="")  # task/order
    ref_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    remark: Mapped[str] = mapped_column(String(255), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")


class CheckinLog(Base):
    """签到记录：唯一约束(user_id, checkin_date) 保证每日一次幂等。"""

    __tablename__ = "checkin_logs"
    __table_args__ = (UniqueConstraint("user_id", "checkin_date", name="uq_checkin_user_date"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    app_id: Mapped[str] = mapped_column(String(32), default="", server_default="")
    checkin_date: Mapped[date] = mapped_column(Date, nullable=False)
    credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")


class AdRewardLog(Base):
    """激励视频广告奖励记录：按天计数限次。"""

    __tablename__ = "ad_reward_logs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    app_id: Mapped[str] = mapped_column(String(32), default="", server_default="")
    credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")


class VpayOrder(Base):
    """虚拟支付订单（二期）：结果走消息推送，按 order_no 幂等。"""

    __tablename__ = "vpay_orders"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    app_id: Mapped[str] = mapped_column(String(32), default="", server_default="")
    offer_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    amount: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 支付金额(分)
    credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 到账积分
    # created(待支付) paid(已支付) refunded(已退款)
    status: Mapped[str] = mapped_column(String(16), default="created", server_default="created")
    wx_order_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserUpstream(Base):
    """BFF 手机号用户 ↔ new-api 影子账号 一对一映射（新底座核心表）。

    - upstream_name = mp<手机号>（确定性派生，用户抢不了）
    - pat_enc：该影子账号的长期 PAT（Fernet 加密）。GET /api/user/token 会轮换作废旧值，
      因此只在建号/显式刷新时写入；PAT 失效时置空，由调用方走显式刷新路径。
    - quota_cache：余额展示缓存（代理 GET /api/user/self 所得），**非账本、不作最终判断依据**
      —— 账本在 new-api quota（方案 4.4 定案 2026-10-15 二次修订）。
    """

    __tablename__ = "user_upstream"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    upstream: Mapped[str] = mapped_column(String(16), default="newapi", server_default="newapi")
    upstream_uid: Mapped[int] = mapped_column(BigInteger, nullable=False)  # new-api users.id
    upstream_name: Mapped[str] = mapped_column(String(64), nullable=False)  # mp<手机号>
    pat_enc: Mapped[str] = mapped_column(String(256), default="", server_default="")
    quota_cache: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    quota_cached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, server_default="now()"
    )
    __table_args__ = (
        UniqueConstraint("user_id", "upstream", name="uq_user_upstream_user"),
        UniqueConstraint("upstream", "upstream_uid", name="uq_user_upstream_uid"),
        UniqueConstraint("upstream", "upstream_name", name="uq_user_upstream_name"),
    )


class QuotaGrant(Base):
    """网关加额发放队列（P1 核心表）：一切 add_quota 的唯一入口，自带幂等与补偿。

    为什么存在：new-api POST /api/user/manage 无幂等键，重复调用重复加钱。
    所有发放（签到/广告/充值/注册积分折算）先在这里占位（ref_id UNIQUE），
    再同步尝试 add_quota；失败留 pending 由补偿 worker 退避重试。

    - ref_id 格式：`checkin:{user_id}:{date}` / `ad:{user_id}:{date}:{n}` /
      `vpay:{order_no}` / `gift:{user_id}`（注册积分折算）
    - amount：积分语义（展示用）；quota_value：实际打到网关的 quota 数值
    - status：pending(占位待发) -> synced(已到账) / failed(重试耗尽，人工介入)
    """

    __tablename__ = "quota_grants"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    app_id: Mapped[str] = mapped_column(String(32), default="", server_default="")
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)  # checkin/ad_reward/vpay/gift
    ref_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # 积分语义
    quota_value: Mapped[int] = mapped_column(Integer, nullable=False)  # 网关 quota
    # pending -> synced / failed
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", index=True)
    retries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str] = mapped_column(String(512), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, server_default="now()"
    )


class AppModel(Base):
    """上架模型表（管理台配置）：代码零写死，模型/定价/上下架全由管理员操作。"""

    __tablename__ = "app_models"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # 网关模型名
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)  # 小程序展示名
    # 能力：t2v 文生视频 / i2v 图生视频 / t2i 文生图（预留，后续按需扩枚举）
    ability: Mapped[str] = mapped_column(String(16), default="t2v", server_default="t2v")
    price_credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 展示价（积分）
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    remark: Mapped[str] = mapped_column(String(255), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, server_default="now()"
    )


class WxScanSession(Base):
    """PC 扫码登录会话（小程序扫码进小程序 / PC 扫码登录小程序侧状态机）。

    状态机：pending(待扫) -> scanned(已扫待确认) -> confirmed(已确认，关联 user_id)
            / cancelled(用户拒绝) / expired(超时)。
    - state 由 BFF 生成（32 hex），防伪造；PC 端拿 state 生成小程序码（scene 带 state），
      用户扫后 options.scene 带 state，小程序调 /auth/scan 确认。
    - PC 端轮询 /auth/scan/status 拿确认结果与用户基础信息，完成 PC 侧登录态签发。
    """

    __tablename__ = "wx_scan_sessions"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    state: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    app_id: Mapped[str] = mapped_column(String(32), nullable=False)  # 小程序 appid
    scene: Mapped[str] = mapped_column(String(32), default="login", server_default="login")
    # pending / scanned / confirmed / cancelled / expired
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    wx_openid: Mapped[str] = mapped_column(String(64), default="", server_default="")
    expire_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, server_default="now()"
    )


class RechargeOrder(Base):
    """充值订单（安卓微信 JSAPI / iOS 站外引导统一订单表）。

    - 下单即建 created 订单（prepay_id 在网关侧）；微信异步回调经 settle_recharge_order
      走 quota_grants 幂等闸门发放配额（new-api quota 唯一账本）。
    - 金额单位：amount_fen（分）与微信一致；credits（到账积分）按 RECHARGE_YUAN_TO_CREDITS 折算。
    """

    __tablename__ = "recharge_orders"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    app_id: Mapped[str] = mapped_column(String(32), default="", server_default="")
    platform: Mapped[str] = mapped_column(String(16), default="android", server_default="android")  # android/ios
    amount_fen: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 微信金额（分）
    credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 到账积分
    # created(待支付) paid(已支付) syncing(发放中) synced(已到账) failed(结算失败)
    status: Mapped[str] = mapped_column(String(16), default="created", server_default="created", index=True)
    wx_order_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, server_default="now()"
    )
