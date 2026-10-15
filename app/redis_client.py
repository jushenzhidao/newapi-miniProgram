"""Redis 客户端（单例，惰性连接；连不上时业务降级不崩溃）。"""
import redis

from .config import settings

_client: redis.Redis | None = None
_tried = False


def get_redis() -> redis.Redis | None:
    global _client, _tried
    if not _tried:
        _tried = True
        try:
            _client = redis.Redis.from_url(
                settings.REDIS_URL, decode_responses=True, socket_connect_timeout=2
            )
            _client.ping()
        except Exception:
            _client = None  # 无 Redis：签名 nonce 防重放与限流降级关闭
    return _client
