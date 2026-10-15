"""安全层：请求签名校验（HMAC-SHA256 + nonce 防重放）与会话令牌（JWT）。

签名约定（小程序编译期内置 sign_key，随每个请求带四个头）：
    X-App-Id:    小程序 appid
    X-Timestamp: 秒级时间戳
    X-Nonce:     随机串（一次性）
    X-Sign:      hex(HMAC-SHA256(sign_key, "{app_id}\n{ts}\n{nonce}\n{sha256hex(body)}"))

防重放：时间窗校验 + Redis SETNX nonce（窗口内同一 nonce 只能用一次）。
"""
import hashlib
import hmac
import time
import uuid

import jwt
from fastapi import Request

from .config import settings


class SignatureError(Exception):
    def __init__(self, message: str = "signature verify failed"):
        self.message = message
        super().__init__(message)


def compute_sign(sign_key: str, app_id: str, timestamp: str, nonce: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    msg = f"{app_id}\n{timestamp}\n{nonce}\n{body_hash}"
    return hmac.new(sign_key.encode(), msg.encode(), hashlib.sha256).hexdigest()


def verify_signature(request: Request, app, body: bytes) -> None:
    """校验请求签名。app 为 apps 表记录（含 sign_key）。失败抛 SignatureError。"""
    app_id = request.headers.get("X-App-Id", "")
    timestamp = request.headers.get("X-Timestamp", "")
    nonce = request.headers.get("X-Nonce", "")
    sign = request.headers.get("X-Sign", "")

    if not all([app_id, timestamp, nonce, sign]):
        raise SignatureError("missing signature headers")
    if app_id != app.app_id:
        raise SignatureError("app_id mismatch")

    # 1. 时间窗（防旧签名包重放）
    try:
        ts = int(timestamp)
    except ValueError:
        raise SignatureError("bad timestamp")
    if abs(time.time() - ts) > settings.SIGN_WINDOW_SECONDS:
        raise SignatureError("timestamp out of window")

    # 2. HMAC 比对（constant-time）
    expect = compute_sign(app.sign_key, app_id, timestamp, nonce, body)
    if not hmac.compare_digest(expect, sign.lower()):
        raise SignatureError("sign mismatch")

    # 3. nonce 一次性（Redis 不可用时放行，保证可用性优先）
    from .redis_client import get_redis

    redis = get_redis()
    if redis is not None:
        key = f"sign:nonce:{app_id}:{nonce}"
        ok = redis.set(key, "1", nx=True, ex=settings.SIGN_WINDOW_SECONDS * 2)
        if not ok:
            raise SignatureError("nonce replay")


# ============ 会话令牌（JWT） ============


def create_token(user_id: int, app_id: str) -> str:
    payload = {
        "sub": str(user_id),
        "app": app_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + settings.JWT_EXPIRE_DAYS * 86400,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    """解析令牌，过期/篡改抛 jwt.PyJWTError。"""
    return jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])


def gen_nonce() -> str:
    return uuid.uuid4().hex


# ============ 密码哈希（bcrypt：网关密码的 BFF 侧校验，明文不落盘） ============

import bcrypt  # noqa: E402  (置于函数区，保持本文件顶部仅标准库+jwt)


def hash_password(plain: str) -> str:
    """bcrypt 哈希，返回可存 users.password_hash 的字符串（含盐）。"""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")


def verify_password(plain: str, password_hash: str) -> bool:
    """校验密码；空哈希/格式错误一律 False。"""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False
