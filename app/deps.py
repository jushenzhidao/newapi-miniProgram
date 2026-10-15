"""FastAPI 依赖：签名校验、当前应用（apps 表）、当前用户（JWT）。"""
import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import App, User
from .security import SignatureError, verify_signature


def get_app(x_app_id: str = Header(default=""), db: Session = Depends(get_db)) -> App:
    """按 X-App-Id 加载小程序配置。未注册/停用 -> 401。"""
    if not x_app_id:
        raise HTTPException(status_code=401, detail="missing X-App-Id")
    app = db.query(App).filter(App.app_id == x_app_id).first()
    if not app or app.status != 1:
        raise HTTPException(status_code=401, detail="unknown or disabled app")
    return app


def get_current_user(
    authorization: str = Header(default=""),
    app: App = Depends(get_app),
    db: Session = Depends(get_db),
) -> User:
    """解析 Bearer 令牌，校验 app 与令牌颁发对象一致。"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("Bearer "):]
    try:
        from .security import decode_token

        payload = decode_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    if payload.get("app") != app.app_id:
        raise HTTPException(status_code=401, detail="token not for this app")
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user or user.status != 1:
        raise HTTPException(status_code=401, detail="user not found or disabled")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """管理台鉴权：is_admin 列命中，或手机号在 ADMIN_PHONES 环境变量里（兜底入口）。"""
    admin_phones = {p.strip() for p in (settings.ADMIN_PHONES or "").split(",") if p.strip()}
    if not (user.is_admin or (user.phone and user.phone in admin_phones)):
        raise HTTPException(status_code=403, detail="admin only")
    return user
