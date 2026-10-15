"""管理台路由（P1）：模型上架 / 任务监控 / 发放监控 / 用户查询。

鉴权：require_admin（is_admin 列或 ADMIN_PHONES）。全部只读为主，
写操作仅限 app_models CRUD（模型/定价/排序/上下架）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import newapi_adapter
from ..db import get_db
from ..deps import require_admin
from ..models import AppModel, QuotaGrant, Task, User
from ..schemas import ApiResponse, AppModelOut

router = APIRouter(dependencies=[Depends(require_admin)])


# ============ 模型上架 ============

class AppModelUpsert(BaseModel):
    model_name: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=64)
    ability: str = Field(default="t2v", pattern="^(t2v|i2v|t2i)$")
    price_credits: int = Field(default=0, ge=0)
    sort: int = Field(default=0)
    enabled: bool = True
    remark: str = Field(default="", max_length=255)


@router.get("/gateway/models", response_model=ApiResponse)
def gateway_models():
    """网关可用模型候选列表（管理台上架挑选用，管理员态拉取）。"""
    try:
        items = newapi_adapter.list_models()
    except newapi_adapter.NewApiError as e:
        raise HTTPException(status_code=502, detail=f"网关模型列表拉取失败：{e.message}")
    return ApiResponse(data=items)


@router.get("/models", response_model=ApiResponse)
def list_app_models(db: Session = Depends(get_db)):
    """已上架模型（含下架，管理台全量视图；小程序端走 /models 只取 enabled）。"""
    rows = db.query(AppModel).order_by(AppModel.sort.asc(), AppModel.id.desc()).all()
    return ApiResponse(data=[AppModelOut.model_validate(r).model_dump() for r in rows])


@router.post("/models", response_model=ApiResponse)
def create_app_model(req: AppModelUpsert, db: Session = Depends(get_db)):
    if db.query(AppModel).filter(AppModel.model_name == req.model_name).first():
        raise HTTPException(status_code=409, detail="该模型已上架")
    row = AppModel(**req.model_dump())
    db.add(row)
    db.commit()
    return ApiResponse(data=AppModelOut.model_validate(row).model_dump())


@router.put("/models/{model_id}", response_model=ApiResponse)
def update_app_model(model_id: int, req: AppModelUpsert, db: Session = Depends(get_db)):
    row = db.query(AppModel).filter(AppModel.id == model_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="模型不存在")
    dup = db.query(AppModel).filter(
        AppModel.model_name == req.model_name, AppModel.id != model_id
    ).first()
    if dup:
        raise HTTPException(status_code=409, detail="该模型名已被其他上架记录使用")
    for k, v in req.model_dump().items():
        setattr(row, k, v)
    db.add(row)
    db.commit()
    return ApiResponse(data=AppModelOut.model_validate(row).model_dump())


@router.delete("/models/{model_id}", response_model=ApiResponse)
def delete_app_model(model_id: int, db: Session = Depends(get_db)):
    row = db.query(AppModel).filter(AppModel.id == model_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="模型不存在")
    db.delete(row)
    db.commit()
    return ApiResponse(data={"deleted": model_id})


# ============ 监控 ============

@router.get("/tasks", response_model=ApiResponse)
def monitor_tasks(page: int = 1, size: int = 20, status: str = "",
                  db: Session = Depends(get_db)):
    """任务监控：最新优先，可按 status 过滤。"""
    q = db.query(Task)
    if status:
        q = q.filter(Task.status == status)
    rows = q.order_by(Task.id.desc()).offset((page - 1) * size).limit(min(size, 100)).all()
    return ApiResponse(data=[_task_out(t) for t in rows])


@router.get("/grants", response_model=ApiResponse)
def monitor_grants(page: int = 1, size: int = 20, status: str = "",
                   db: Session = Depends(get_db)):
    """发放监控：pending/failed 排前（充值/签到到账问题第一排查入口）。"""
    q = db.query(QuotaGrant)
    if status:
        q = q.filter(QuotaGrant.status == status)
    else:
        # 未过滤时把非 synced 的排前面，最后按 id 倒序
        rows = (
            q.filter(QuotaGrant.status != "synced").order_by(QuotaGrant.id.desc())
            .limit(min(size, 100)).all()
        )
        rest = (
            q.filter(QuotaGrant.status == "synced").order_by(QuotaGrant.id.desc())
            .limit(min(size, 100)).all()
        )
        return ApiResponse(data=[_grant_out(g) for g in rows + rest])
    rows = q.order_by(QuotaGrant.id.desc()).offset((page - 1) * size).limit(min(size, 100)).all()
    return ApiResponse(data=[_grant_out(g) for g in rows])


@router.get("/users", response_model=ApiResponse)
def search_users(phone: str = "", page: int = 1, size: int = 20,
                 db: Session = Depends(get_db)):
    """用户查询：按手机号精确/前缀搜。手机号打码返回。"""
    q = db.query(User)
    if phone:
        q = q.filter(User.phone.like(f"{phone}%"))
    rows = q.order_by(User.id.desc()).offset((page - 1) * size).limit(min(size, 100)).all()
    from ..newapi_adapter import masked_phone
    return ApiResponse(data=[{
        "user_id": u.id,
        "phone_masked": masked_phone(u.phone) if u.phone else None,
        "nickname": u.nickname,
        "is_admin": bool(u.is_admin),
        "phone_bound": bool(u.phone),
        "status": u.status,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    } for u in rows])


# ============ 输出辅助 ============

def _task_out(t: Task) -> dict:
    return {
        "task_no": t.task_no, "user_id": t.user_id, "app_id": t.app_id,
        "type": t.type, "status": t.status, "result_url": t.result_url or None,
        "cost_credits": t.cost_credits, "error": t.error or "",
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "finished_at": t.finished_at.isoformat() if t.finished_at else None,
    }


def _grant_out(g: QuotaGrant) -> dict:
    return {
        "id": g.id, "user_id": g.user_id, "app_id": g.app_id,
        "change_type": g.change_type, "ref_id": g.ref_id,
        "amount": g.amount, "quota_value": g.quota_value,
        "status": g.status, "retries": g.retries, "last_error": g.last_error or "",
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "synced_at": g.synced_at.isoformat() if g.synced_at else None,
    }
