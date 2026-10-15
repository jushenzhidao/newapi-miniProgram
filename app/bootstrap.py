"""启动自举：把「小程序注册到 apps 表」这一步自动化。

设计意图（飞哥 2026-10-15 要求）：部署时只填 .env，不碰 MySQL。
- 单小程序：BOOTSTRAP_APP_ID / BOOTSTRAP_APP_SECRET / BOOTSTRAP_APP_SIGN_KEY / BOOTSTRAP_APP_NAME
- 多小程序（未来批量模板）：BOOTSTRAP_APPS_JSON = '[{"app_id":..,"secret":..,"sign_key":..,"name":..,"status":1}]'

幂等 upsert：已存在则更新 secret/sign_key/name（支持密钥轮换），不存在则插入。
sign_key 为空直接跳过该 app（apps.sign_key 为 NOT NULL，绝不写空值炸表）。
异常不致命：自举失败只记日志，不阻断 BFF 启动（已手工插过 apps 的实例照常工作）。
"""
import json
import logging

from sqlalchemy.orm import Session

from .config import settings
from .models import App

logger = logging.getLogger("bff.bootstrap")


def _upsert_app(
    db: Session,
    app_id: str,
    secret: str,
    sign_key: str,
    name: str,
    status: int = 1,
) -> None:
    if not app_id:
        return
    if not sign_key:
        logger.warning("[bootstrap] 跳过 app_id=%s：sign_key 为空（apps.sign_key 必填，不写空值）", app_id)
        return

    existing = db.query(App).filter(App.app_id == app_id).first()
    if existing:
        existing.secret = secret
        existing.sign_key = sign_key
        existing.name = name or existing.name
        existing.status = status
        db.add(existing)
        logger.info("[bootstrap] 更新小程序 apps 行：app_id=%s name=%s", app_id, name or existing.name)
    else:
        db.add(App(app_id=app_id, secret=secret, sign_key=sign_key, name=name, status=status))
        logger.info("[bootstrap] 注册新小程序 apps 行：app_id=%s name=%s", app_id, name)


def ensure_bootstrap_apps(db: Session) -> None:
    """启动时调用：依据环境变量 upsert apps 表。"""
    # 1) 单小程序（具身之道等首个小程序）
    if settings.BOOTSTRAP_APP_ID:
        _upsert_app(
            db,
            settings.BOOTSTRAP_APP_ID,
            settings.BOOTSTRAP_APP_SECRET,
            settings.BOOTSTRAP_APP_SIGN_KEY,
            settings.BOOTSTRAP_APP_NAME,
        )

    # 2) 批量小程序（未来模板矩阵，可选）
    if settings.BOOTSTRAP_APPS_JSON:
        try:
            items = json.loads(settings.BOOTSTRAP_APPS_JSON)
        except json.JSONDecodeError as e:
            logger.error("[bootstrap] BOOTSTRAP_APPS_JSON 解析失败，已忽略：%s", e)
            items = None
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                try:
                    st = int(it.get("status", 1))
                except (TypeError, ValueError):
                    st = 1
                _upsert_app(
                    db,
                    str(it.get("app_id", "")),
                    str(it.get("secret", "")),
                    str(it.get("sign_key", "")),
                    str(it.get("name", "")),
                    st,
                )

    db.commit()
