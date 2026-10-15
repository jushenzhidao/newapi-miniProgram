"""Pydantic 请求/响应模型（v2）。"""
import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# ============ 通用 ============


class ApiResponse(BaseModel):
    """统一响应包装：code=0 成功，非 0 业务错误（见 utils/errors.py 错误码）。"""

    code: int = 0
    message: str = "ok"
    data: dict | list | None = None


# ============ 认证 ============


class SessionLoginReq(BaseModel):
    """wx.login 静默登录：游客态零门槛。"""

    code: str = Field(min_length=1, max_length=128)


class BindPhoneReq(BaseModel):
    """<button open-type="getPhoneNumber"> 返回的 code（服务端换取手机号）。

    password：网关密码（注册弹框让用户一次设好），同用于后续 web 页面登录。
    长度 8-20：new-api 建号/改密实测 max 20 位（24 位报 max tag 校验错）。
    可空：合并到已有账号时不强制重设密码。
    """

    phone_code: str = Field(min_length=1, max_length=512)
    password: str | None = Field(None, min_length=8, max_length=20)

    @field_validator("password")
    @classmethod
    def _password_rule(cls, v: str | None) -> str | None:
        """8-20 位且必须含字母+数字。不用 pattern 字段（pydantic v2 的 rust regex
        不支持 lookahead）。上限 20 对齐 new-api 建号/改密实测 max。"""
        if v is None:
            return v
        if not (8 <= len(v) <= 20):
            raise ValueError("密码长度须为 8-20 位")
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("密码须同时包含字母和数字")
        return v


class AuthData(BaseModel):
    token: str
    user_id: int
    is_new_user: bool
    phone_bound: bool
    credits: int
    # 网关影子账号是否就绪（False=上游失败，需重试 bind-phone 修复，见 auth.bind_phone）
    gateway_linked: bool | None = None


# ============ 用户 ============


class UserOut(BaseModel):
    user_id: int
    nickname: str
    avatar_url: str
    phone: str | None
    credits: int
    phone_bound: bool
    # 小程序管理台入口开关（观礼台仅 is_admin 可见）
    is_admin: bool = False

    model_config = {"from_attributes": True}


# ============ 任务 ============


class CutoutReq(BaseModel):
    """AI 抠图提交：图片 URL（小程序先 wx.uploadFile 到 BFF 或已有 CDN 链接）。"""

    image_url: str = Field(min_length=8, max_length=1024)
    output_format: str = Field(default="png", pattern="^(png|webp)$")


class VideoTaskReq(BaseModel):
    """视频生成提交（新底座，走网关扣费）。

    params 透传给模型适配器（prompt / image_url / duration / ratio 等），
    结构由 app_models.ability 决定（t2v 必填 prompt；i2v 必填 image_url），adapter 内校验。
    """

    model_name: str = Field(min_length=1, max_length=64)
    params: dict = Field(default_factory=dict)


class TaskOut(BaseModel):
    task_no: str
    type: str
    status: str
    result_url: str | None = None
    cost_credits: int
    error: str
    created_at: datetime
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


# ============ 积分 ============


class CheckinOut(BaseModel):
    credits: int              # 本次获得（积分语义）
    total_credits: int | None = None  # 已废弃（网关为账本）——余额看 /credits/balance
    streak_days: int          # 累计签到天数
    synced: bool = True       # False = 记录成功但网关发放排队中（补偿会到账）


class AdRewardOut(BaseModel):
    credits: int
    total_credits: int | None = None
    today_count: int          # 今日已领取次数
    daily_limit: int
    synced: bool = True


class BalanceOut(BaseModel):
    """网关余额（唯一账本）代理展示。linked=False 引导重试 bind-phone。"""
    linked: bool
    quota_raw: int | None = None
    credits_display: int | None = None  # quota // QUOTA_PER_CREDIT，展示口径
    cached: bool = False


class GatewayLogOut(BaseModel):
    """网关消耗流水（/api/log/self 代理，字段按 new-api 日志裁剪）。"""
    created_at: int | None = None       # unix 秒
    model_name: str = ""
    quota: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    type: int = 0
    remark: str = ""


# ============ 管理台 ============


class AppModelOut(BaseModel):
    model_name: str
    display_name: str
    ability: str
    price_credits: int
    sort: int
    enabled: bool
    remark: str

    model_config = {"from_attributes": True}


class CreditLogOut(BaseModel):
    change_type: str
    amount: int
    balance_after: int
    remark: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ============ 扫码登录（PC 扫码进小程序） ============


class ScanCreateReq(BaseModel):
    """PC 端创建扫码会话：小程序 app_id 取自 X-App-Id 签名头，scene 区分业务。"""
    scene: str = "login"


class ScanCreateOut(BaseModel):
    state: str
    expire_seconds: int


class ScanStatusOut(BaseModel):
    """PC 端轮询结果。confirmed 时带用户基础信息（PC 侧据此签发自身登录态）。"""
    status: str                # pending/scanned/confirmed/cancelled/expired
    need_phone: bool = False
    user_id: int | None = None
    nickname: str | None = None
    avatar_url: str | None = None
    phone_masked: str | None = None


class ScanLoginReq(BaseModel):
    """小程序确认（对齐前端 api.scanLogin {code, state, appId}）。appId 取自签名头。"""
    code: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=64)


class ScanLoginOut(BaseModel):
    """小程序侧确认结果：need_phone=true 时引导绑手机号（带游客态 token 调 bind-phone）。"""
    need_phone: bool
    token: str
    user_id: int
    is_new_user: bool
    phone_bound: bool
    credits: int
    gateway_linked: bool | None = None


class ScanCancelReq(BaseModel):
    """用户拒绝授权上报（对齐前端 api.reportCancel {state, scene, appId}）。"""
    state: str = Field(min_length=1, max_length=64)
    scene: str = ""


# ============ 充值（安卓 JSAPI / iOS 引导） ============


class RechargeOrderReq(BaseModel):
    """下单入参（对齐前端 api.createRechargeOrder {amount, platform, appId}）。amount 单位「元」。"""
    amount: int = Field(gt=0, le=100000)
    platform: str = "android"   # android | ios（ios 不在此下单，走 /recharge-guide）
    app_id: str = ""            # 覆盖签名头 app_id（一般留空）


class RechargeOrderOut(BaseModel):
    """wx.requestPayment 参数（驼峰字段对齐小程序端）。"""
    order_no: str
    timeStamp: str
    nonceStr: str
    package: str               # prepay_id=xxx
    signType: str = "RSA"
    paySign: str


class RechargeGuideOut(BaseModel):
    """iOS 引导信息：站外 PC/H5 充值页 URL（+可选二维码）。"""
    url: str
    qr_image: str = ""
