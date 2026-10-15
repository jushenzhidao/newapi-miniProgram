"""微信服务端 API 客户端：jscode2session / access_token / 手机号换取。

注意：头像昵称等资料已废弃 getUserProfile 接口，前端用 chooseAvatar 后由
小程序提交到 /auth/profile 更新（见 routers/auth.py）。
"""
import time

import httpx

from .config import settings

_BASE = "https://api.weixin.qq.com"


def code2session(app_id: str, secret: str, code: str) -> dict:
    """wx.login code -> {openid, session_key, unionid?}。失败抛 ValueError。"""
    resp = httpx.get(
        f"{_BASE}/sns/jscode2session",
        params={"appid": app_id, "secret": secret, "js_code": code, "grant_type": "authorization_code"},
        timeout=10,
    )
    data = resp.json()
    if "openid" not in data:
        raise ValueError(f"jscode2session failed: {data}")
    return data


def get_access_token(app_id: str, secret: str) -> str:
    """stable access_token，Redis 缓存 7000s（微信有效期 7200s）。"""
    from .redis_client import get_redis

    cache_key = f"wx:access_token:{app_id}"
    redis = get_redis()
    if redis is not None:
        cached = redis.get(cache_key)
        if cached:
            return cached

    resp = httpx.get(
        f"{_BASE}/cgi-bin/token",
        params={
            "grant_type": "client_credential",
            "appid": app_id,
            "secret": secret,
        },
        timeout=10,
    )
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise ValueError(f"get access_token failed: {data}")

    if redis is not None:
        redis.set(cache_key, token, ex=7000)
    return token


def get_phone_number(app_id: str, secret: str, phone_code: str) -> str:
    """新版 getPhoneNumber：code 服务端换取手机号（无需本地 AES 解密）。

    phone_code 来自 <button open-type="getPhoneNumber"> 的 bindgetphonenumber 事件。
    """
    token = get_access_token(app_id, secret)
    resp = httpx.post(
        f"{_BASE}/wxa/business/getuserphonenumber",
        params={"access_token": token},
        json={"code": phone_code},
        timeout=10,
    )
    data = resp.json()
    if data.get("errcode") != 0:
        raise ValueError(f"getuserphonenumber failed: {data}")
    return data["phone_info"]["purePhoneNumber"]


def phone_bind_check(app_id: str) -> bool:
    """占位：确认小程序已完成微信认证（手机号快速验证需要）。"""
    return bool(settings.WECHAT_APPID and settings.WECHAT_SECRET)


def now_ts() -> int:
    return int(time.time())
