"""上传路由：小程序本地图 -> BFF 存储 -> 返回可访问 URL。

安全设计（与 /api 其他接口不同）：
- wx.uploadFile 的 multipart body 由框架组装，前端无法预计算 HMAC，
  因此上传接口豁免签名，但强制要求 Authorization Bearer token
  （仅登录用户可传，未登录 401）。
- 限类型（jpg/png/webp/gif）、限大小（默认 10MB）、按天分目录存储。
- 结果 URL 形如 /static/uploads/2026/10/14/xxx.png，由 main.py 挂载
  的 StaticFiles 提供访问；worker 与 bff 共用同一 uploads volume。
"""
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..config import settings
from ..deps import get_app, get_current_user
from ..models import App, User

router = APIRouter()

_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_MAX_SIZE = settings.UPLOAD_MAX_MB * 1024 * 1024


@router.post("/image")
async def upload_image(
    file: UploadFile = File(...),
    app: App = Depends(get_app),
    user: User = Depends(get_current_user),
):
    # 1. 类型校验
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"unsupported file type: {ext}")

    # 2. 大小校验（读到底再判断，防止超大包拖垮内存可加流式限制，MVP 从权）
    content = await file.read()
    if len(content) > _MAX_SIZE:
        raise HTTPException(
            status_code=413, detail=f"file too large, max {settings.UPLOAD_MAX_MB}MB"
        )
    if not content:
        raise HTTPException(status_code=400, detail="empty file")

    # 3. 按天分目录存储：uploads/YYYY/MM/DD/{uuid}{ext}
    now = datetime.now()
    rel_dir = os.path.join("uploads", now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))
    abs_dir = os.path.join(settings.UPLOAD_DIR, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    abs_path = os.path.join(abs_dir, filename)
    with open(abs_path, "wb") as f:
        f.write(content)

    url = f"/static/{rel_dir.replace(os.sep, '/')}/{filename}"
    return {
        "code": 0,
        "message": "ok",
        "data": {"url": url, "size": len(content), "filename": filename},
    }
