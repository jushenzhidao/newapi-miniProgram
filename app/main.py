"""FastAPI 入口：签名中间件、路由注册、统一异常处理。"""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .credits import CreditError
from .db import SessionLocal
from .models import App
from .routers import admin, auth, credits, tasks, upload
from .security import SignatureError, verify_signature
from .upstream import UpstreamError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("wechat-tools-bff")

app = FastAPI(
    title="WeChat Tools BFF",
    version="0.2.0",
    description="AI 工具小程序矩阵统一 BFF（用户体系/积分/任务/抠图）",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# ============ 签名中间件：/api/ 业务请求必须带 HMAC 签名 ============

# /api/upload 豁免原因：wx.uploadFile 的 multipart body 由框架组装，
# 前端无法预计算 body 哈希；该接口改由 Bearer token 强校验（routers/upload.py）。
_SIGN_EXEMPT = ("/api/health", "/api/upload/image", "/api/credits/recharge-notify")


@app.middleware("http")
async def signature_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path in _SIGN_EXEMPT or path.startswith("/api/docs") \
            or path.startswith("/api/openapi"):
        return await call_next(request)

    # 读 body 并缓存，Starlette 的 Request.body() 命中 _body 缓存，下游路由可重复读
    body = await request.body()
    request._body = body

    app_id = request.headers.get("X-App-Id", "")
    if not app_id:
        return JSONResponse(status_code=401, content={"code": 40101, "message": "missing X-App-Id"})

    db = SessionLocal()
    try:
        app_row = db.query(App).filter(App.app_id == app_id).first()
        if not app_row or app_row.status != 1:
            return JSONResponse(status_code=401, content={"code": 40103, "message": "unknown or disabled app"})
        verify_signature(request, app_row, body)
    except SignatureError as e:
        return JSONResponse(status_code=401, content={"code": 40102, "message": e.message})
    finally:
        db.close()
    return await call_next(request)


# ============ 统一异常 -> 业务错误码 ============


@app.exception_handler(CreditError)
async def credit_error_handler(request: Request, exc: CreditError):
    status = {40201: 402, 40010: 409, 40011: 429}.get(exc.code, 400)
    return JSONResponse(status_code=status, content={"code": exc.code, "message": exc.message})


@app.exception_handler(UpstreamError)
async def upstream_error_handler(request: Request, exc: UpstreamError):
    logger.error("upstream error: %s", exc)
    return JSONResponse(status_code=502, content={"code": 50001, "message": "upstream service error"})


# ============ 路由注册 ============

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(credits.router, prefix="/api/credits", tags=["credits"])
app.include_router(upload.router, prefix="/api/upload", tags=["upload"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])

# 上传文件静态访问：/static/uploads/... -> UPLOAD_DIR/uploads/...
_uploads_root = os.path.join(settings.UPLOAD_DIR)
if not os.path.isdir(_uploads_root):
    os.makedirs(_uploads_root, exist_ok=True)
app.mount("/static", StaticFiles(directory=_uploads_root), name="static")


@app.get("/api/health", tags=["health"])
def health():
    return {"code": 0, "message": "ok", "env": settings.APP_ENV}


# ============ 启动自举：注册小程序到 apps 表（免手工 SQL） ============
@app.on_event("startup")
def _startup_bootstrap():
    from .bootstrap import ensure_bootstrap_apps

    db = SessionLocal()
    try:
        ensure_bootstrap_apps(db)
    except Exception as e:  # noqa: BLE001 —— 自举失败不致命，已手工插过 apps 的实例照常工作
        logger.error("bootstrap apps failed: %s", e)
    finally:
        db.close()
