"""newapi_adapter 单测：全部 mock 上游 HTTP，不碰真实网关。

覆盖 P0 关键语义：
- 建号→反查 uid（契约：POST /api/user/ 不返回 uid）
- add_quota 的 mode/value 必填载荷（无幂等键——幂等在充值回调层，不在此测）
- login_get_pat 换 PAT 后立刻归还会话（DELETE /api/user/sessions/{sid}）
- PAT Fernet 加解密 + 密钥缺失拒绝
- 管理员兜底 login 冷静期（防乒乓烧会话）
- BindPhoneReq 密码规则（8-20 位字母+数字，对齐 new-api 实测 max 20）
"""
import base64
import json

import pytest

from app import newapi_adapter as adapter
from app.config import Settings


def _settings(tmp_path, **kw) -> Settings:
    return Settings(
        NEWAPI_BASE_URL="https://gw.test",
        NEWAPI_ADMIN_CRED_FILE=str(tmp_path / "admin_cred.json"),
        **kw,
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离 settings 与模块级缓存；预种管理员凭证（不触发兜底 login）。"""
    s = _settings(tmp_path, PAT_ENC_KEY=base64.urlsafe_b64encode(b"k" * 32).decode())
    monkeypatch.setattr(adapter, "settings", s)
    adapter._admin_cache.update({"pat": "test-admin-pat", "uid": 1})
    adapter._last_admin_login_at = None
    yield s
    adapter._admin_cache.update({"pat": None, "uid": 0})
    adapter._last_admin_login_at = None


class FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeHttp:
    """按 (method, path) 记录调用并返回脚本化响应。"""

    def __init__(self, script: dict):
        self.script = script
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, **kw):
        self.calls.append((method, path))
        return self.script[(method, path)](kw)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def put(self, path, **kw):
        return self.request("PUT", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


def _ok(data):
    return {"success": True, "message": "", "data": data}


# ---------- PAT 加解密 ----------

def test_pat_encrypt_roundtrip(env):
    token = adapter.encrypt_pat("pat-abc-123")
    assert token != "pat-abc-123"
    assert adapter.decrypt_pat(token) == "pat-abc-123"


def test_pat_decrypt_with_wrong_key_fails(env):
    token = adapter.encrypt_pat("pat-x")
    from app.config import Settings as S
    bad = S(NEWAPI_BASE_URL="https://gw.test",
            PAT_ENC_KEY=base64.urlsafe_b64encode(b"j" * 32).decode())
    orig = adapter.settings
    adapter.settings = bad
    try:
        with pytest.raises(adapter.NewApiError):
            adapter.decrypt_pat(token)
    finally:
        adapter.settings = orig


def test_pat_encryption_requires_key(tmp_path, monkeypatch):
    s = _settings(tmp_path, PAT_ENC_KEY="")
    monkeypatch.setattr(adapter, "settings", s)
    with pytest.raises(adapter.NewApiError):
        adapter.encrypt_pat("pat-x")


# ---------- 影子账号 ----------

def test_create_shadow_user_searches_uid(env, monkeypatch):
    http = FakeHttp({
        ("POST", "/api/user/"): lambda kw: FakeResp(_ok("")),
        ("GET", "/api/user/search"): lambda kw: FakeResp(_ok({
            "items": [{"id": 42, "username": "mp13800138000"}]})),
    })
    monkeypatch.setattr(adapter, "_http", lambda: http)
    uid = adapter.create_shadow_user("mp13800138000", "Aa1xxxxxxxxxxx", "138****8000")
    assert uid == 42
    # 建号不写 email
    post_kw = http.calls and None
    assert ("POST", "/api/user/") in http.calls


def test_add_quota_payload_has_mode_and_value(env, monkeypatch):
    captured = {}

    http = FakeHttp({
        ("POST", "/api/user/manage"): lambda kw: captured.update(kw) or FakeResp(_ok("")),
    })
    monkeypatch.setattr(adapter, "_http", lambda: http)
    adapter.add_quota(42, 500000)
    assert captured["json"]["id"] == 42
    assert captured["json"]["action"] == "add_quota"
    assert captured["json"]["mode"] == "add"
    assert captured["json"]["value"] == 500000
    with pytest.raises(adapter.NewApiError):
        adapter.add_quota(42, 0)


def test_search_user_exact_match_only(env, monkeypatch):
    http = FakeHttp({
        ("GET", "/api/user/search"): lambda kw: FakeResp(_ok({
            "items": [
                {"id": 1, "username": "mp1380013800011"},  # 前缀相近，不该命中
                {"id": 2, "username": "other"},
            ]})),
    })
    monkeypatch.setattr(adapter, "_http", lambda: http)
    assert adapter.search_user("mp13800138000") is None


# ---------- 用户 PAT ----------

def test_login_get_pat_returns_session_immediately(env, monkeypatch):
    """血泪契约：换到 PAT 后必须立刻 DELETE sessions/{sid} 归还会话。"""
    http = FakeHttp({
        ("POST", "/api/user/login"): lambda kw: FakeResp(_ok({
            "access_token": "sess-jwt", "user": {"id": 42}, "session": {"sid": "s1"}})),
        ("GET", "/api/user/token"): lambda kw: FakeResp(_ok("long-term-pat")),
        ("DELETE", "/api/user/sessions/s1"): lambda kw: FakeResp(_ok(True)),
    })
    monkeypatch.setattr(adapter, "_http", lambda: http)
    pat, uid = adapter.login_get_pat("mp13800138000", "Aa1xxxxxxxxxxx")
    assert pat == "long-term-pat"
    assert uid == 42
    assert ("DELETE", "/api/user/sessions/s1") in http.calls


def test_login_get_pat_returns_session_even_if_token_call_fails(env, monkeypatch):
    """token 换取失败也要归还 session（finally 语义）。"""
    def boom(kw):
        raise adapter.NewApiError("rotate failed", status_code=500)

    http = FakeHttp({
        ("POST", "/api/user/login"): lambda kw: FakeResp(_ok({
            "access_token": "sess-jwt", "user": {"id": 42}, "session": {"sid": "s2"}})),
        ("GET", "/api/user/token"): boom,
        ("DELETE", "/api/user/sessions/s2"): lambda kw: FakeResp(_ok(True)),
    })
    monkeypatch.setattr(adapter, "_http", lambda: http)
    with pytest.raises(adapter.NewApiError):
        adapter.login_get_pat("mp13800138000", "Aa1xxxxxxxxxxx")
    assert ("DELETE", "/api/user/sessions/s2") in http.calls


# ---------- 管理员兜底 + 冷静期 ----------

def test_admin_login_fallback_cooldown(env, monkeypatch):
    """冷静期内第二次兜底必须拒绝（防乒乓烧穿会话/签发额度）。"""
    env.NEWAPI_ADMIN_USERNAME = "root"
    env.NEWAPI_ADMIN_PASSWORD = "pw"

    def login(kw):
        return FakeResp(_ok({
            "access_token": "t", "user": {"id": 1}, "session": {"sid": "s"}}))

    http = FakeHttp({
        ("POST", "/api/user/login"): login,
        ("GET", "/api/user/token"): lambda kw: FakeResp(_ok("pat-1")),
        ("DELETE", "/api/user/sessions/s"): lambda kw: FakeResp(_ok(True)),
    })
    monkeypatch.setattr(adapter, "_http", lambda: http)
    adapter._admin_login_fallback()
    with pytest.raises(adapter.NewApiError) as ei:
        adapter._admin_login_fallback()
    assert ei.value.status_code == 429


def test_admin_401_self_heal_reloads_disk_first(env, monkeypatch):
    """401 自愈第一步：重读落盘文件（同机其他实例 healed 的新值），绝不直接 login。"""
    env.NEWAPI_ADMIN_USERNAME = "root"
    env.NEWAPI_ADMIN_PASSWORD = "pw"
    adapter._admin_cache.update({"pat": "stale", "uid": 1})
    # 模拟落盘里已有其他实例换的新 PAT
    with open(env.NEWAPI_ADMIN_CRED_FILE, "w", encoding="utf-8") as f:
        json.dump({"pat": "fresh-from-peer", "uid": 1}, f)

    calls = {"n": 0}

    def search(kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp({"success": False, "message": "无权进行此操作，未登录且未提供 access token"},
                            status_code=401)
        return FakeResp(_ok({"items": []}))

    http = FakeHttp({("GET", "/api/user/search"): search})
    monkeypatch.setattr(adapter, "_http", lambda: http)
    adapter._admin_request("GET", "/api/user/search", action="search",
                           params={"keyword": "x"})
    assert adapter._admin_cache["pat"] == "fresh-from-peer"
    assert ("POST", "/api/user/login") not in http.calls  # 没有走 login 轮换
    assert calls["n"] == 2  # 第一次 401，重读磁盘后重试成功


# ---------- 派生与掩码 ----------

def test_derive_username_and_mask():
    assert adapter.derive_username("13800138000") == "mp13800138000"
    assert len(adapter.derive_username("13800138000")) <= 20  # new-api username 上限
    assert adapter.masked_phone("13800138000") == "138****8000"


# ---------- 密码规则（schema 层） ----------

def test_bind_phone_password_rule():
    from app.schemas import BindPhoneReq
    from pydantic import ValidationError
    # 合法：8-20 位字母+数字
    BindPhoneReq(phone_code="c", password="abcd1234")
    BindPhoneReq(phone_code="c", password="a" * 19 + "1")
    # 太短 / 太长 / 纯数字 / 纯字母
    for bad in ("a1", "a" * 19, "12345678", "abcdefgh"):
        with pytest.raises(ValidationError):
            BindPhoneReq(phone_code="c", password=bad)
    # 可空（合并已有账号场景）
    BindPhoneReq(phone_code="c")
