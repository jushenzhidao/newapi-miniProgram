"""微信支付 JSAPI v3 适配层（安卓充值）。

仅依赖 cryptography（项目已装），实现三件事：
1. 下单 Authorization 头 RSA-SHA256 签名（商户私钥）
2. 调起支付 paySign（RSA-SHA256，小程序 wx.requestPayment 用）
3. 异步回调 AES-256-GCM 解密（APIv3 密钥）；可选平台证书验签

配置见 app.config.settings：
  WXPAY_MCH_ID / WXPAY_APP_ID / WXPAY_SERIAL_NO / WXPAY_PRIVATE_KEY(PEM) /
  WXPAY_API_V3_KEY / WXPAY_PLATFORM_PUBLIC_KEY(可选) / WXPAY_NOTIFY_URL

参考：微信支付开发者文档 v3（pay.weixin.qq.com/wiki/doc/apiv3）。
"""
import base64
import json
import logging
import secrets
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

logger = logging.getLogger("bff.wechat_pay")

_BASE = "https://api.mch.weixin.qq.com"


class WxPayError(Exception):
    """微信支付业务/网络失败。message 可展示，detail 仅进日志。"""

    def __init__(self, message: str, status_code: int = 502, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


def _private_key():
    """加载商户 API 私钥 PEM。

    支持两种来源（按优先级）：
    1. WXPAY_PRIVATE_KEY_FILE：文件路径（生产推荐，密钥不进 .env 明文）
    2. WXPAY_PRIVATE_KEY：.env 内联，换行用 \\n 转义（加载时还原）
    """
    raw = ""
    if getattr(settings, "WXPAY_PRIVATE_KEY_FILE", ""):
        try:
            with open(settings.WXPAY_PRIVATE_KEY_FILE, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            raise WxPayError("读取商户私钥文件失败", status_code=503, detail=str(e))
    if not raw.strip() and getattr(settings, "WXPAY_PRIVATE_KEY", ""):
        raw = settings.WXPAY_PRIVATE_KEY.replace("\\n", "\n")
    if not raw.strip():
        raise WxPayError("微信支付商户私钥未配置（WXPAY_PRIVATE_KEY/WXPAY_PRIVATE_KEY_FILE）", status_code=503)
    try:
        return serialization.load_pem_private_key(raw.encode("utf-8"), password=None)
    except ValueError as e:
        raise WxPayError("微信支付商户私钥格式错误", status_code=503, detail=str(e))


def _rsa_sign(message: str) -> str:
    key = _private_key()
    signature = key.sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def _gen_nonce() -> str:
    return secrets.token_hex(16)


def _build_authorization(method: str, url_path: str, body: str) -> str:
    ts = str(int(time.time()))
    nonce = _gen_nonce()
    message = f"{method}\n{url_path}\n{ts}\n{nonce}\n{body}\n"
    sig = _rsa_sign(message)
    return (
        f'WECHATPAY2-SHA256-RSA2048 mchid="{settings.WXPAY_MCH_ID}",'
        f'nonce_str="{nonce}",signature="{sig}",timestamp="{ts}",'
        f'serial_no="{settings.WXPAY_SERIAL_NO}"'
    )


def create_jsapi_order(
    out_trade_no: str,
    total_fen: int,
    description: str,
    openid: str,
    app_id: str | None = None,
) -> str:
    """JSAPI 下单，返回 prepay_id。失败抛 WxPayError。

    total_fen 单位「分」；description 截断 127 字（微信限制）。
    """
    if not settings.WXPAY_MCH_ID or not settings.WXPAY_APP_ID:
        raise WxPayError("微信支付商户未配置（WXPAY_MCH_ID/WXPAY_APP_ID）", status_code=503)
    app_id = app_id or settings.WXPAY_APP_ID
    body = {
        "appid": app_id,
        "mchid": settings.WXPAY_MCH_ID,
        "description": description[:127],
        "out_trade_no": out_trade_no,
        "notify_url": settings.WXPAY_NOTIFY_URL,
        "amount": {"total": int(total_fen), "currency": "CNY"},
        "payer": {"openid": openid},
    }
    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    url_path = "/v3/pay/transactions/jsapi"
    headers = {
        "Authorization": _build_authorization("POST", url_path, payload),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "wechat-tools-bff",
    }
    try:
        resp = httpx.post(
            f"{_BASE}{url_path}", content=payload.encode("utf-8"), headers=headers, timeout=15
        )
    except httpx.HTTPError as e:
        raise WxPayError("微信支付下单网络异常", detail=repr(e))
    if resp.status_code != 200:
        raise WxPayError("微信支付下单失败", detail=resp.text[:400])
    data = resp.json()
    prepay_id = data.get("prepay_id")
    if not prepay_id:
        raise WxPayError("微信支付下单响应缺少 prepay_id", detail=str(data)[:400])
    return prepay_id


def build_pay_sign(prepay_id: str, app_id: str | None = None) -> dict:
    """组装小程序 wx.requestPayment 参数。返回 {timeStamp, nonceStr, package, signType, paySign}。"""
    app_id = app_id or settings.WXPAY_APP_ID
    ts = str(int(time.time()))
    nonce = _gen_nonce()
    message = f"{app_id}\n{ts}\n{nonce}\n{prepay_id}\n"
    pay_sign = _rsa_sign(message)
    return {
        "timeStamp": ts,
        "nonceStr": nonce,
        "package": f"prepay_id={prepay_id}",
        "signType": "RSA",
        "paySign": pay_sign,
    }


def verify_notify(timestamp: str, nonce: str, raw_body: bytes, signature_b64: str) -> bool:
    """验签回调（微信平台证书 EC 公钥）。未配置平台公钥则跳过（依赖回调解密 + 订单幂等）。"""
    pub = settings.WXPAY_PLATFORM_PUBLIC_KEY
    if not pub:
        return True
    try:
        key = serialization.load_pem_public_key(pub.encode("utf-8"))
        message = f"{timestamp}\n{nonce}\n{raw_body.decode('utf-8')}\n".encode("utf-8")
        key.verify(base64.b64decode(signature_b64), message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, KeyError) as e:
        logger.warning("wxpay notify verify failed: %s", e)
        return False


def decrypt_notify(resource: dict) -> dict:
    """解密回调解密 resource {ciphertext, nonce, associated_data}。返回明文 JSON dict。"""
    if not settings.WXPAY_API_V3_KEY:
        raise WxPayError("APIv3 密钥未配置，无法回调解密", status_code=503)
    key = base64.b64decode(settings.WXPAY_API_V3_KEY)
    if len(key) != 32:
        raise WxPayError("APIv3 密钥必须为 32 字节", status_code=503)
    try:
        ciphertext = base64.b64decode(resource["ciphertext"])
        nonce = (resource.get("nonce") or "").encode("utf-8")
        aad = (resource.get("associated_data") or "").encode("utf-8")
        plain = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except (KeyError, ValueError) as e:
        raise WxPayError("回调解密失败", detail=str(e))
    return json.loads(plain.decode("utf-8"))
