"""new-api 网关适配层：影子账号 / 加额 / 用户 PAT（同步 httpx，BFF 与 worker 共用）。

契约依据：D:\\code\\newapi-bff\\newapi-bff\\app\\newapi_client.py（2026-08-22 对上游实测核实）。

血泪教训（全部内建，改这里之前先读）：
1. new-api 会话上限 50（UserSessionActiveLimit），**硬拒绝、不淘汰最旧**，TTL 30 天；
   login 换到 PAT 后必须立刻 DELETE /api/user/sessions/{sid} 归还，否则会话打满全线瘫痪。
2. GET /api/user/token 每次调用都重新生成 PAT 并作废旧值 → **严禁重试**，只在建号/显式刷新时调用一次。
3. 管理员兜底 login 必须带冷静期（ADMIN_LOGIN_COOLDOWN_SECONDS）：多实例共用管理员时
   谁轮换谁踢别人，无冷静期会乒乓烧穿会话配额 + 账号级签发额度（USER_SESSION_ISSUANCE_LIMIT）。
4. POST /api/user/ 建号**不写 email**；password 实测 max 20 位（24 位报 max tag 校验错）。
5. POST /api/user/manage 加额 {id, action:"add_quota", mode:"add", value} —— mode/value 必填，
   **无幂等键**，调用方（充值回调）必须自己做事务闸门 + 补偿，本模块不负责幂等。

管理员凭证优先级（越靠前越不消耗会话配额）：
  1) env NEWAPI_ADMIN_PAT / NEWAPI_ADMIN_UID
  2) NEWAPI_ADMIN_CRED_FILE 落盘缓存（进程重启复用；401 自愈时先重读文件，
     可能是同机其他实例刚 healed 的新值 —— 绝不能直接 login 轮换，否则乒乓）
  3) NEWAPI_ADMIN_USERNAME/PASSWORD login 兜底（消耗 1 个会话，换完立刻归还）
"""
import json
import logging
import os
import time
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .config import settings

logger = logging.getLogger("bff.newapi_adapter")

# PAT 换取前密码长度上限：new-api 建号/改密实测 max 20 位（见模块 docstring 第 4 条）
PASSWORD_MAX_LEN = 20
PASSWORD_MIN_LEN = 8


class NewApiError(Exception):
    """new-api 返回业务失败或网络错误。message 可直接展示，detail 只进日志。"""

    def __init__(self, message: str, status_code: int = 502, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


# ============ HTTP 客户端（同步；BFF 路由跑线程池，worker 原生同步） ============

_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        if not settings.NEWAPI_BASE_URL:
            raise NewApiError("网关未配置（NEWAPI_BASE_URL 为空）", status_code=503)
        _client = httpx.Client(
            base_url=settings.NEWAPI_BASE_URL,
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
            trust_env=False,  # 不走本机代理
        )
    return _client


def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


def user_headers(pat: str, uid: int) -> dict:
    """用户态请求头：Authorization + New-Api-User 缺一不可（契约第 2 条）。"""
    return {"Authorization": f"Bearer {pat}", "New-Api-User": str(uid)}


def _unwrap(resp: httpx.Response, action: str) -> Any:
    """统一解析 new-api 信封 {"success": bool, "message": str, "data": ...}。"""
    if resp.status_code >= 500:
        raise NewApiError(f"网关服务异常（{action}）", status_code=502,
                          detail=f"HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        body = resp.json()
    except ValueError:
        raise NewApiError(f"网关响应异常（{action}）", detail=f"HTTP {resp.status_code}: {resp.text[:300]}")
    if resp.status_code == 401:
        raise NewApiError("网关鉴权失败（凭据可能已被轮换）", status_code=401,
                          detail=str(body)[:300])
    if isinstance(body, dict) and body.get("success") is False:
        raise NewApiError(f"网关拒绝（{action}）：{body.get('message', 'unknown')}",
                          status_code=400, detail=str(body)[:300])
    return body.get("data") if isinstance(body, dict) else body


# ============ 管理员凭证（env > 落盘 > login 兜底 + 冷静期） ============

_admin_cache: dict = {"pat": None, "uid": 0}
_last_admin_login_at: float | None = None  # monotonic；None=本进程从未兜底轮换过


def _cred_file_path() -> str:
    return settings.NEWAPI_ADMIN_CRED_FILE


def _load_admin_cred() -> None:
    """冷启装载：env 直供优先，其次落盘缓存。"""
    if settings.NEWAPI_ADMIN_PAT and settings.NEWAPI_ADMIN_UID:
        _admin_cache["pat"] = settings.NEWAPI_ADMIN_PAT
        _admin_cache["uid"] = settings.NEWAPI_ADMIN_UID
        return
    if _reload_admin_cred():
        return


def _reload_admin_cred() -> bool:
    """从落盘文件重读 PAT；发现新值（同机其他实例 healed）则采纳并返回 True。

    401 自愈第一步：磁盘值优先于内存旧值。绝不直接 login 轮换（乒乓教训，见模块 docstring 第 3 条）。
    """
    try:
        with open(_cred_file_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        pat, uid = d.get("pat"), d.get("uid")
        if pat and uid and pat != _admin_cache["pat"]:
            _admin_cache["pat"] = pat
            _admin_cache["uid"] = int(uid)
            logger.warning("admin PAT updated from cred file uid=%s", uid)
            return True
    except (OSError, ValueError, KeyError):
        pass
    return False


def _save_admin_cred() -> None:
    """PAT 落盘（0600）；失败不影响主流程，只是下次冷启多耗一个会话。"""
    try:
        path = _cred_file_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pat": _admin_cache["pat"], "uid": _admin_cache["uid"]}, f)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError as e:
        logger.warning("save admin cred failed: %s", e)


def _admin_login_fallback() -> tuple[str, int]:
    """兜底：账密 login 换 PAT 并归还 session。冷静期内拒绝重复轮换。"""
    global _last_admin_login_at
    if not (settings.NEWAPI_ADMIN_USERNAME and settings.NEWAPI_ADMIN_PASSWORD):
        raise NewApiError("管理员凭据不可用且未配置兜底账密", status_code=503)
    now = time.monotonic()
    if _last_admin_login_at is not None and now - _last_admin_login_at < settings.ADMIN_LOGIN_COOLDOWN_SECONDS:
        raise NewApiError("管理员兜底轮换冷静期内，请稍后重试", status_code=429)
    _last_admin_login_at = now

    data = _unwrap(_http().post("/api/user/login", json={
        "username": settings.NEWAPI_ADMIN_USERNAME,
        "password": settings.NEWAPI_ADMIN_PASSWORD,
    }), "admin login")
    session_token = (data or {}).get("access_token", "")
    uid = int(((data or {}).get("user") or {}).get("id", 0))
    sid = ((data or {}).get("session") or {}).get("sid")
    if not session_token or not uid:
        raise NewApiError("管理员登录响应缺少 access_token/uid", detail=str(data)[:300])

    pat = ""
    try:
        pat = str(_unwrap(_http().get(
            "/api/user/token", headers=user_headers(session_token, uid)), "admin get pat"))
    finally:
        # 归还会话：无论如何都要删（契约第 1 条）；失败只告警
        if sid:
            try:
                _http().delete(f"/api/user/sessions/{sid}", headers=user_headers(session_token, uid))
            except Exception as e:  # noqa: BLE001
                logger.warning("return admin session failed: %s", e)
    if not pat:
        raise NewApiError("获取管理员 PAT 失败", detail="empty token data")
    _admin_cache["pat"] = pat
    _admin_cache["uid"] = uid
    _save_admin_cred()
    return pat, uid


def _admin_cred() -> tuple[str, int]:
    if _admin_cache["pat"] is None:
        _load_admin_cred()
    if _admin_cache["pat"] is None:
        return _admin_login_fallback()
    return _admin_cache["pat"], _admin_cache["uid"]


def _admin_request(method: str, path: str, *, action: str, retry_auth: bool = True, **kw) -> Any:
    pat, uid = _admin_cred()
    try:
        resp = _http().request(method, path, headers=user_headers(pat, uid), **kw)
    except httpx.HTTPError as e:
        raise NewApiError(f"网关网络异常（{action}）", detail=repr(e))
    if resp.status_code == 401 and retry_auth:
        # 自愈：先重读落盘（其他实例可能刚 healed）；不行且冷静期外才兜底 login
        if _reload_admin_cred():
            return _admin_request(method, path, action=action, retry_auth=False, **kw)
        _admin_login_fallback()
        return _admin_request(method, path, action=action, retry_auth=False, **kw)
    return _unwrap(resp, action)


# ============ 影子账号管理 ============

def derive_username(phone: str) -> str:
    """手机号 -> 派生影子账号名：mp<手机号>（13 位 ≤ new-api username 上限 20）。"""
    return f"mp{phone}"


def masked_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else phone


def search_user(keyword: str) -> dict | None:
    """按 username 精确反查（建号后拿 uid 的唯一手段，契约第 8 条）。"""
    items = _admin_request("GET", "/api/user/search", action="search user",
                           params={"keyword": keyword})
    rows = (items or {}).get("items") if isinstance(items, dict) else items
    for row in rows or []:
        if (row or {}).get("username") == keyword:
            return row
    return None


def create_shadow_user(username: str, password: str, display_name: str) -> int:
    """建影子账号并反查 uid。返回 uid。重名 -> NewApiError(400)。

    注意：不写 email（契约第 8 条）；password 最长 PASSWORD_MAX_LEN。
    """
    data = _admin_request("POST", "/api/user/", action="create user", json={
        "username": username, "password": password, "display_name": display_name,
    })
    row = search_user(username)
    if not row:
        raise NewApiError("影子账号已创建但反查 uid 失败", detail=f"create resp={data} keyword={username}")
    return int(row["id"])


def set_password(uid: int, username: str, password: str, display_name: str = "") -> None:
    """管理员改密（不需要旧密码）。uid 不变，余额/Key/日志全保留。"""
    payload: dict[str, Any] = {"id": uid, "username": username, "password": password}
    if display_name:
        payload["display_name"] = display_name
    _admin_request("PUT", "/api/user/", action="update user", json=payload)


def delete_user(uid: int) -> None:
    """删号，立即生效（仅用于错误补偿场景，如建号后映射写入失败需清理）。"""
    _admin_request("DELETE", f"/api/user/{uid}", action="delete user")


def add_quota(uid: int, value: int) -> None:
    """给影子账号加额度。**无幂等键** —— 调用方必须先过事务闸门（见模块 docstring 第 5 条）。"""
    if value <= 0:
        raise NewApiError("add_quota value 必须为正数", status_code=400)
    _admin_request("POST", "/api/user/manage", action="add quota", json={
        "id": uid, "action": "add_quota", "mode": "add", "value": int(value),
    })


# ============ 用户 PAT（影子账号自己的长期令牌） ============

def login_get_pat(username: str, password: str) -> tuple[str, int]:
    """账密登录 -> 换长期 PAT -> 立刻归还会话。返回 (pat, uid)。

    GET /api/user/token 会作废旧 PAT（契约第 2 条）→ 本函数只在建号或显式刷新时调用，
    绝不作为常规鉴权手段，也**不做任何重试**。
    """
    data = _unwrap(_http().post("/api/user/login", json={
        "username": username, "password": password,
    }), "user login")
    session_token = (data or {}).get("access_token", "")
    uid = int(((data or {}).get("user") or {}).get("id", 0))
    sid = ((data or {}).get("session") or {}).get("sid")
    if not session_token or not uid:
        raise NewApiError("网关登录响应异常", detail=str(data)[:300])
    pat = ""
    try:
        pat = str(_unwrap(_http().get(
            "/api/user/token", headers=user_headers(session_token, uid)), "get pat"))
    finally:
        if sid:
            try:
                _http().delete(f"/api/user/sessions/{sid}", headers=user_headers(session_token, uid))
            except Exception as e:  # noqa: BLE001
                logger.warning("return user session failed: %s", e)
    if not pat:
        raise NewApiError("获取用户 PAT 失败", detail="empty token data")
    return pat, uid


# ============ PAT 加密落库（Fernet，密钥 PAT_ENC_KEY） ============

def _fernet() -> Fernet:
    if not settings.PAT_ENC_KEY:
        raise NewApiError("PAT_ENC_KEY 未配置，拒绝读写明文 PAT", status_code=503)
    return Fernet(settings.PAT_ENC_KEY.encode() if isinstance(settings.PAT_ENC_KEY, str)
                  else settings.PAT_ENC_KEY)


def encrypt_pat(pat: str) -> str:
    return _fernet().encrypt(pat.encode()).decode("ascii")


def decrypt_pat(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError) as e:
        raise NewApiError("PAT 解密失败（密钥不匹配？）", status_code=503, detail=repr(e))


# ============ 查询类（P1 余额/流水/模型广场复用） ============

def get_user_self(pat: str, uid: int) -> dict:
    """查用户自身信息（含 quota）—— 余额展示的唯一真相源。"""
    return _unwrap(_http().get("/api/user/self", headers=user_headers(pat, uid)), "user self") or {}


def get_user_logs(pat: str, uid: int, page: int = 0, page_size: int = 20) -> dict:
    """查用户消耗日志（/api/log/self）。"""
    return _unwrap(_http().get("/api/log/self", headers=user_headers(pat, uid),
                               params={"p": page, "page_size": page_size, "type": 0}),
                   "user logs") or {}


def list_models(pat: str | None = None, uid: int | None = None) -> list[dict]:
    """模型列表：传用户 PAT 用用户态；不传则用管理员态（管理台上架挑选用）。"""
    if pat and uid:
        headers = user_headers(pat, uid)
    else:
        a_pat, a_uid = _admin_cred()
        headers = user_headers(a_pat, a_uid)
    data = _unwrap(_http().get("/v1/models", headers=headers), "list models")
    return (data or {}).get("data", []) if isinstance(data, dict) else (data or [])


# ============ 视频任务（P1 抽象层） ============

class VideoTaskAdapter:
    """视频任务适配器基类：提交/查询两段式。

    不同厂商在 new-api 侧的任务端点形态差异大（可灵 /v1/video/generations 式 vs
    豆包 content-generation 式），**首个上架模型确定时**按其网关形态实现子类并
    register_video_adapter() 注册；未注册的模型在提交时得到明确报错，而不是乱打端点。
    """

    name = "base"

    def submit(self, model: str, params: dict, pat: str, uid: int) -> str:
        """提交任务，返回网关任务 ID。扣费由网关在该用户影子账号上原生完成。"""
        raise NotImplementedError(f"视频任务端点待适配（adapter={self.name}）")

    def query(self, gateway_task_id: str, pat: str, uid: int) -> dict:
        """查询任务。返回 {"status": "processing|success|failed", "video_url": str, "error": str}。"""
        raise NotImplementedError(f"视频任务端点待适配（adapter={self.name}）")


_VIDEO_ADAPTERS: dict[str, VideoTaskAdapter] = {}


def register_video_adapter(model_name: str, adapter: VideoTaskAdapter) -> None:
    _VIDEO_ADAPTERS[model_name] = adapter


def get_video_adapter(model_name: str) -> VideoTaskAdapter:
    adapter = _VIDEO_ADAPTERS.get(model_name)
    if adapter is None:
        raise NewApiError(f"模型 {model_name} 的视频任务尚未适配（等首个上架模型确定端点形态）",
                          status_code=501)
    return adapter


# 冷启预载（import 时尝试装载缓存/env，失败静默——运行时再兜底）
_load_admin_cred()
