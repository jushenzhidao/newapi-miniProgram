"""认证路由：游客态登录 / 手机号绑定合并 / 当前用户 / 资料更新。

登录体系（手机号主键，跨小程序统一）：
1. POST /auth/session    wx.login 静默登录，建游客态 user（phone 空，送注册积分），零门槛
2. POST /auth/bind-phone 关键动作前弹手机号授权，code 服务端换取手机号；
                          同手机号已存在账号 -> 自动合并（身份/积分/任务迁移），游客态无感升级
"""
import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, newapi_adapter, wechat
from ..config import settings
from ..credits import grant_credits
from ..db import get_db
from ..deps import get_app, get_current_user
from ..models import App, User, UserUpstream, WxScanSession
from ..schemas import (
    ApiResponse,
    AuthData,
    BindPhoneReq,
    ScanCancelReq,
    ScanCreateOut,
    ScanCreateReq,
    ScanLoginOut,
    ScanLoginReq,
    ScanStatusOut,
    SessionLoginReq,
    UserOut,
)
from ..security import create_token, hash_password

logger = logging.getLogger("bff.auth")

router = APIRouter()


def _issue(db: Session, user: User, app: App, is_new: bool,
           gateway_linked: bool | None = None) -> ApiResponse:
    db.commit()
    return ApiResponse(
        data=AuthData(
            token=create_token(user.id, app.app_id),
            user_id=user.id,
            is_new_user=is_new,
            phone_bound=bool(user.phone),
            credits=user.credits,
            gateway_linked=gateway_linked,
        ).model_dump()
    )


# ============ 网关影子账号（new-api 联动） ============


def _random_password() -> str:
    """随机网关密码（用户未输入时的兜底）：保底字母+数字，≤20 位（new-api 实测上限）。"""
    return "Aa1" + secrets.token_urlsafe(13)


def _ensure_upstream(db: Session, user: User, password: str | None = None) -> bool:
    """确保 new-api 影子账号与映射就绪；返回是否就绪。上游异常抛 NewApiError。

    幂等可重入：
    - 映射行存在且 upstream_uid>0 → 不重复建号（search 只在缺失时调用）
    - PAT 缺失且有明文密码 → 换一次 PAT（GET /api/user/token 会作废旧 PAT，绝不重复调）
    - 无明文密码的补偿场景 → 映射补齐、PAT 置空，等用户设密码/web 登录时再刷新
    """
    if not user.phone:
        return False
    username = newapi_adapter.derive_username(user.phone)
    upstream = (
        db.query(UserUpstream)
        .filter(UserUpstream.user_id == user.id, UserUpstream.upstream == "newapi")
        .first()
    )
    plain_pw = password

    if upstream is None:
        row = newapi_adapter.search_user(username)  # 上游已建过（此前映射写入失败）→ 直接认领
        if row:
            uid = int(row["id"])
        else:
            pw = plain_pw or _random_password()
            uid = newapi_adapter.create_shadow_user(username, pw, newapi_adapter.masked_phone(user.phone))
            plain_pw = pw  # 建号密码即本次 PAT 交换密码
        upstream = UserUpstream(
            user_id=user.id, upstream="newapi", upstream_uid=uid, upstream_name=username,
        )
        db.add(upstream)
        db.flush()

    if not upstream.pat_enc and plain_pw:
        # 换长期 PAT（顺带校验账密可用）；成功才写 pat_enc，失败下次重试
        pat, uid_now = newapi_adapter.login_get_pat(username, plain_pw)
        upstream.pat_enc = newapi_adapter.encrypt_pat(pat)
        upstream.upstream_uid = uid_now

    upstream.updated_at = datetime.now()
    db.add(upstream)
    db.commit()
    if not upstream.pat_enc:
        logger.warning("upstream %s ready but PAT missing (no plaintext password)", username)
    return True


@router.post("/session", response_model=ApiResponse)
def session_login(req: SessionLoginReq, app: App = Depends(get_app), db: Session = Depends(get_db)):
    """wx.login 静默登录：游客态零门槛，全站可逛。"""
    try:
        wx = wechat.code2session(app.app_id, app.secret or settings.WECHAT_SECRET, req.code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    openid = wx["openid"]
    identity = (
        db.query(models.UserIdentity)
        .filter(models.UserIdentity.app_id == app.app_id, models.UserIdentity.openid == openid)
        .first()
    )
    if identity:
        user = db.query(User).filter(User.id == identity.user_id).first()
        if not user or user.status != 1:
            raise HTTPException(status_code=403, detail="user disabled")
        # session_key 可能过期，刷新缓存
        identity.session_key = wx.get("session_key", identity.session_key)
        db.add(identity)
        return _issue(db, user, app, is_new=False)

    # 新用户：游客态 + 注册赠送积分
    try:
        with db.begin():
            user = User(nickname="微信用户", credits=settings.REGISTER_GIFT_CREDITS)
            db.add(user)
            db.flush()
            db.add(
                models.UserIdentity(
                    user_id=user.id,
                    app_id=app.app_id,
                    openid=openid,
                    unionid=wx.get("unionid", ""),
                    session_key=wx.get("session_key", ""),
                )
            )
            grant_credits(
                db, user, settings.REGISTER_GIFT_CREDITS, "register_gift",
                app_id=app.app_id, remark="新用户注册赠送",
            )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"create user failed: {e}")
    return _issue(db, user, app, is_new=True)


@router.post("/bind-phone", response_model=ApiResponse)
def bind_phone(req: BindPhoneReq, app: App = Depends(get_app),
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """手机号绑定 + 账号合并 + 网关影子账号（新底座 P0）。

    流程：DB 事务内完成 绑定/合并/密码哈希 → 事务外调 new-api 建影子账号 + 写映射
    （外部 HTTP 不进 DB 事务，失败可重试：重试命中幂等路径继续补偿）。
    """
    if user.phone:
        # 已绑定：幂等返回；顺带做影子账号补偿（上次建号/映射失败的重试入口）
        try:
            gateway_linked = _ensure_upstream(db, user, req.password)
        except newapi_adapter.NewApiError as e:
            logger.error("ensure upstream failed for user %s: %s (%s)", user.id, e, e.detail)
            gateway_linked = False
        return _issue(db, user, app, is_new=False, gateway_linked=gateway_linked)

    # dev 模拟通道：touristappid 测试号无手机号快速验证组件权限，本地联调用
    # phone_code 传 "mock:<11位手机号>" 直接短路微信换号；生产（APP_ENV != dev）拒绝
    if req.phone_code.startswith("mock:"):
        if settings.APP_ENV != "dev":
            raise HTTPException(status_code=403, detail="mock phone binding only allowed in dev")
        phone = req.phone_code[5:]
        if not (phone.isdigit() and len(phone) == 11):
            raise HTTPException(status_code=400, detail="mock phone must be 11 digits")

    if not phone:
        try:
            phone = wechat.get_phone_number(app.app_id, app.secret or settings.WECHAT_SECRET, req.phone_code)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    try:
        with db.begin():
            existing = (
                db.query(User)
                .filter(User.phone == phone, User.id != user.id)
                .first()
            )
            if existing:
                # ---- 账号合并（MVP 简化：游客剩余积分直接累加；任务/身份迁移） ----
                db.query(models.UserIdentity).filter(
                    models.UserIdentity.user_id == user.id
                ).update({models.UserIdentity.user_id: existing.id})
                db.query(models.Task).filter(
                    models.Task.user_id == user.id
                ).update({models.Task.user_id: existing.id})
                existing.credits += user.credits
                if user.nickname != "微信用户":
                    existing.nickname = user.nickname
                if req.password:
                    existing.password_hash = hash_password(req.password)
                    existing.phone_verified_at = datetime.now()
                    existing.phone_source = "wechat_mp"
                db.add(existing)
                db.add(
                    models.CreditLog(
                        user_id=existing.id,
                        app_id=app.app_id,
                        change_type="merge",
                        amount=user.credits,
                        balance_after=existing.credits,
                        remark=f"游客账号合并（from user {user.id}）",
                    )
                )
                db.delete(user)  # 游客号的身份已迁走，可删
                db.flush()
                merged = existing
            else:
                user.phone = phone
                if req.password:
                    user.password_hash = hash_password(req.password)
                user.phone_verified_at = datetime.now()
                user.phone_source = "wechat_mp"
                db.add(user)
                merged = user
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"bind phone failed: {e}")

    # ---- 网关联动（事务外）：建影子账号 + 写映射 + 换 PAT ----
    try:
        gateway_linked = _ensure_upstream(db, merged, req.password)
    except newapi_adapter.NewApiError as e:
        logger.error("ensure upstream failed for user %s: %s (%s)", merged.id, e, e.detail)
        # 手机号已绑定成功（上面事务已提交），网关未就绪 → 显式报错引导重试；
        # 重试会命中上方幂等路径继续补偿
        raise HTTPException(
            status_code=502,
            detail="手机号绑定成功，但网关账号创建失败，请稍后重试（重试不会重复扣手机号授权）",
        )

    # ---- 存量积分折算（游客赠送/旧账本 -> 网关 quota，一次性，幂等 gift:{user_id}） ----
    from ..quota_sync import flush_local_credits
    try:
        flush_local_credits(db, merged, app_id=app.app_id)
    except Exception as e:  # noqa: BLE001 —— 折算失败不阻塞绑定（补偿路径：重进个人页再触发）
        logger.warning("flush local credits failed for user %s: %s", merged.id, e)
    return _issue(db, merged, app, is_new=False, gateway_linked=gateway_linked)


@router.get("/me", response_model=ApiResponse)
def me(user: User = Depends(get_current_user)):
    return ApiResponse(data=UserOut.model_validate(user).model_dump())


@router.post("/profile", response_model=ApiResponse)
def update_profile(body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """chooseAvatar 选择的头像 + 昵称（getUserProfile 已废弃，走前端提交）。"""
    nickname = str(body.get("nickname", ""))[:64]
    avatar_url = str(body.get("avatar_url", ""))[:512]
    if nickname:
        user.nickname = nickname
    if avatar_url:
        user.avatar_url = avatar_url
    db.add(user)
    db.commit()
    return ApiResponse(data=UserOut.model_validate(user).model_dump())


# ============ PC 扫码登录（小程序扫码进小程序 / PC 扫码登录小程序侧） ============


def _scan_issue(db: Session, sess: WxScanSession, app: App) -> ApiResponse:
    """扫码确认后返回 JWT（小程序侧直接登录）。need_phone 引导绑手机号。"""
    user = db.query(User).filter(User.id == sess.user_id).first()
    if user is None or user.status != 1:
        raise HTTPException(status_code=403, detail="user not found or disabled")
    token = create_token(user.id, app.app_id)
    return ApiResponse(data=ScanLoginOut(
        need_phone=not bool(user.phone),
        token=token,
        user_id=user.id,
        is_new_user=False,
        phone_bound=bool(user.phone),
        credits=user.credits,
        gateway_linked=None,
    ).model_dump())


@router.post("/scan/create", response_model=ApiResponse)
def scan_create(req: ScanCreateReq, app: App = Depends(get_app), db: Session = Depends(get_db)):
    """PC 端创建扫码会话：生成 state + 过期时间，返回给 PC 生成小程序码（scene 带 state）。

    需签名（PC 端作为 BFF 的一个 app，见 README「新增小程序」插 apps 行 + sign_key）。
    """
    state = secrets.token_hex(16)
    expire_at = datetime.now() + timedelta(seconds=settings.SCAN_EXPIRE_SECONDS)
    sess = WxScanSession(state=state, app_id=app.app_id, scene=req.scene, status="pending",
                         expire_at=expire_at)
    db.add(sess)
    db.commit()
    return ApiResponse(data=ScanCreateOut(state=state, expire_seconds=settings.SCAN_EXPIRE_SECONDS).model_dump())


@router.get("/scan/status", response_model=ApiResponse)
def scan_status(state: str, app: App = Depends(get_app), db: Session = Depends(get_db)):
    """PC 端轮询扫码状态。confirmed 时带用户基础信息（PC 侧据此签发自身登录态）。"""
    sess = (
        db.query(WxScanSession)
        .filter(WxScanSession.state == state, WxScanSession.app_id == app.app_id)
        .first()
    )
    if sess is None:
        return ApiResponse(code=40401, message="scan session not found", data={"status": "not_found"})
    if sess.status == "pending" and datetime.now() > sess.expire_at:
        sess.status = "expired"
        db.add(sess)
        db.commit()
    out = ScanStatusOut(status=sess.status)
    if sess.status == "confirmed" and sess.user_id:
        u = db.query(User).filter(User.id == sess.user_id).first()
        if u:
            out.user_id = u.id
            out.nickname = u.nickname
            out.avatar_url = u.avatar_url
            out.need_phone = not bool(u.phone)
            out.phone_masked = newapi_adapter.masked_phone(u.phone) if u.phone else None
    return ApiResponse(data=out.model_dump())


@router.post("/scan", response_model=ApiResponse)
def scan_login(req: ScanLoginReq, app: App = Depends(get_app), db: Session = Depends(get_db)):
    """小程序确认扫码（对齐前端 api.scanLogin {code, state, appId}，skipAuth 但需签名头）。

    流程：查 state 会话 -> code2session 拿 openid -> 查/建游客态 user ->
    关联会话并 confirmed -> 返回 JWT（老用户免密直接登录；游客态带 token 引导绑手机号）。
    """
    sess = (
        db.query(WxScanSession)
        .filter(WxScanSession.state == req.state, WxScanSession.app_id == app.app_id)
        .first()
    )
    if sess is None or sess.status == "expired":
        raise HTTPException(status_code=404, detail="scan session not found or expired")
    if sess.status == "cancelled":
        raise HTTPException(status_code=409, detail="scan cancelled by user")
    if sess.status == "confirmed":
        return _scan_issue(db, sess, app)  # 幂等：已确认，直接回结果

    try:
        wx = wechat.code2session(app.app_id, app.secret or settings.WECHAT_SECRET, req.code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    openid = wx["openid"]

    identity = (
        db.query(models.UserIdentity)
        .filter(models.UserIdentity.app_id == app.app_id, models.UserIdentity.openid == openid)
        .first()
    )
    if identity:
        user = db.query(User).filter(User.id == identity.user_id).first()
        if not user or user.status != 1:
            raise HTTPException(status_code=403, detail="user disabled")
        identity.session_key = wx.get("session_key", identity.session_key)
        db.add(identity)
    else:
        try:
            with db.begin():
                user = User(nickname="微信用户", credits=settings.REGISTER_GIFT_CREDITS)
                db.add(user)
                db.flush()
                db.add(models.UserIdentity(
                    user_id=user.id, app_id=app.app_id, openid=openid,
                    unionid=wx.get("unionid", ""), session_key=wx.get("session_key", ""),
                ))
                grant_credits(db, user, settings.REGISTER_GIFT_CREDITS, "register_gift",
                              app_id=app.app_id, remark="扫码新用户注册赠送")
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"create user failed: {e}")

    sess.user_id = user.id
    sess.wx_openid = openid
    sess.status = "confirmed"
    db.add(sess)
    db.commit()
    return _scan_issue(db, sess, app)


@router.post("/cancel", response_model=ApiResponse)
def scan_cancel(req: ScanCancelReq, app: App = Depends(get_app), db: Session = Depends(get_db)):
    """用户拒绝授权静默上报（对齐前端 api.reportCancel {state, scene, appId}）。

    pending/scanned 会话置 cancelled，通知 PC 端轮询到此状态。skipAuth 但需签名头。
    """
    sess = (
        db.query(WxScanSession)
        .filter(WxScanSession.state == req.state, WxScanSession.app_id == app.app_id)
        .first()
    )
    if sess and sess.status in ("pending", "scanned"):
        sess.status = "cancelled"
        db.add(sess)
        db.commit()
    return ApiResponse(data={"state": req.state, "status": "cancelled"})
