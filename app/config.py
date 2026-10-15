"""应用配置：统一从环境变量/.env 读取，禁硬编码密钥。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_ENV: str = "dev"

    # 数据层
    DATABASE_URL: str = "mysql+pymysql://root:root@127.0.0.1:3306/wechat_tools?charset=utf8mb4"
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    # 安全
    JWT_SECRET: str = "dev-only-change-me-in-production"
    JWT_EXPIRE_DAYS: int = 7
    SIGN_WINDOW_SECONDS: int = 300

    # 微信（默认小程序；多小程序配置在 apps 表）
    WECHAT_APPID: str = ""
    WECHAT_SECRET: str = ""

    # 积分规则
    REGISTER_GIFT_CREDITS: int = 100
    CUTOUT_COST_CREDITS: int = 10
    CHECKIN_CREDITS: int = 10
    AD_REWARD_CREDITS: int = 20
    AD_REWARD_DAILY_LIMIT: int = 10

    # 抠图上游：tencent | oneapis | selfhosted
    UPSTREAM_MODE: str = "tencent"
    UPSTREAM_TIMEOUT_SECONDS: int = 60
    TENCENT_SECRET_ID: str = ""
    TENCENT_SECRET_KEY: str = ""
    TENCENT_REGION: str = "ap-guangzhou"
    ONEAPIS_BASE_URL: str = ""
    ONEAPIS_API_KEY: str = ""
    ONEAPIS_CUTOUT_PATH: str = "/v1/images/segment"

    # 上传（小程序图片落地；worker 与 bff 共用目录）
    UPLOAD_DIR: str = "/srv/uploads"
    UPLOAD_MAX_MB: int = 10

    # new-api 网关（影子账号 / 额度账本，见 newapi_adapter.py 模块 docstring）
    NEWAPI_BASE_URL: str = ""                # 例 https://newapi-bff.oneapis.cn（new-api 本体域）
    # 管理员凭证优先级：PAT(env) > 落盘缓存 > 账密 login 兜底（消耗会话配额，慎用）
    NEWAPI_ADMIN_PAT: str = ""
    NEWAPI_ADMIN_UID: int = 0
    NEWAPI_ADMIN_USERNAME: str = ""          # 兜底 login 用，可不配
    NEWAPI_ADMIN_PASSWORD: str = ""
    NEWAPI_ADMIN_CRED_FILE: str = "data/admin_cred.json"  # PAT 落盘缓存（0600）
    ADMIN_LOGIN_COOLDOWN_SECONDS: int = 300  # 兜底轮换冷静期，防乒乓烧穿会话/签发额度
    # 用户 PAT 加密密钥（Fernet, openssl rand -base64 32）；为空则拒绝写入映射表
    PAT_ENC_KEY: str = ""

    # 额度换算与发放（P1）
    # 积分 -> quota 换算系数：quota_value = amount * QUOTA_PER_CREDIT
    # 1 元 = 10 积分（BFF 口径）；QuotaPerUnit 待网关后台确认后校准此值
    QUOTA_PER_CREDIT: int = 5000
    # 管理台管理员手机号（逗号分隔）；启动/绑定时不自动提权，仅 require_admin 校验用
    ADMIN_PHONES: str = ""
    # quota_grants 补偿：最大重试次数（超过置 failed 人工介入）
    GRANT_MAX_RETRIES: int = 10
    # 余额代理缓存秒数（quota_cache 命中窗口）
    BALANCE_CACHE_SECONDS: int = 60

    # 日终对账（P2）：pending 积压告警阈值 / 管理员池子最低余额（0=不查）
    RECON_PENDING_ALERT: int = 20
    RECON_MIN_ADMIN_QUOTA: int = 1_000_000_000

    # 虚拟支付（MVP 关闭）
    VPAY_ENABLED: bool = False
    VPAY_MCH_ID: str = ""
    VPAY_API_V3_KEY: str = ""

    # 微信支付 JSAPI（安卓充值；iOS 走站内引导到站外 PC/H5，见 RECHARGE_WEB_URL）
    WXPAY_ENABLED: bool = False
    WXPAY_MCH_ID: str = ""          # 微信支付商户号
    WXPAY_APP_ID: str = ""          # 下单用小程序 appid（安卓 JSAPI 的 appid）
    WXPAY_SERIAL_NO: str = ""       # 商户 API 证书序列号（apiv3 签名 Authorization 用）
    # 商户 API 私钥 PEM 全文（.env 多行用三引号/换行转义；生产建议读文件）
    WXPAY_PRIVATE_KEY: str = ""
    # 商户 API 私钥文件路径（生产推荐：密钥不进 .env 明文；与 WXPAY_PRIVATE_KEY 二选一）
    WXPAY_PRIVATE_KEY_FILE: str = ""
    WXPAY_API_V3_KEY: str = ""      # APIv3 密钥（32 字节，回调解密 AES-GCM key）
    # 微信平台证书公钥 PEM（可选；配置后严格验签回调，否则跳过验签）
    WXPAY_PLATFORM_PUBLIC_KEY: str = ""
    # 支付结果异步回调地址（微信调，需公网可达 HTTPS；免 BFF 签名）
    WXPAY_NOTIFY_URL: str = ""
    # 扫码登录会话有效期（秒）
    SCAN_EXPIRE_SECONDS: int = 300
    # 充值：元 -> 积分（与用户侧展示一致：1 元 = 10 积分）；amount 单位为「元」
    RECHARGE_YUAN_TO_CREDITS: int = 10
    # iOS 引导充值页（站外 PC/H5，合规绕过苹果虚拟支付红线）
    RECHARGE_WEB_URL: str = "https://oneapis.cn/recharge"

    # Celery
    CELERY_BROKER_URL: str = "redis://127.0.0.1:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://127.0.0.1:6379/2"

    # ============ 小程序自举注册（apps 表，启动自动 upsert，免手工 SQL） ============
    # 单小程序（具身之道等首个）：部署只需填这三个，BFF 启动自动写/更新 apps 表。
    #   sign_key 用 openssl rand -hex 32 生成，且与前端 miniprogram/utils/config.js 的
    #   SIGN_KEY 保持一致（否则前端签名被 BFF 全拒）。
    #   secret = 微信公众平台该小程序的 AppSecret（bind-phone 换手机号要用）。
    BOOTSTRAP_APP_ID: str = ""
    BOOTSTRAP_APP_SECRET: str = ""
    BOOTSTRAP_APP_SIGN_KEY: str = ""
    BOOTSTRAP_APP_NAME: str = "具身之道"
    # 多小程序（未来批量模板，可选）：JSON 数组，每项 {app_id,secret,sign_key,name,status}
    BOOTSTRAP_APPS_JSON: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
