"""任务路由：AI 抠图提交 / 视频生成提交 / 状态查询 / 历史列表。

提交流程（事务顺序很关键）：
1. 校验工具开关与单价（apps.tools 优先，回退 settings）
2. 同一事务内：扣除积分（提交即冻结） + 创建任务(pending)
3. 事务提交后才发 Celery —— 避免 worker 抢跑查不到任务
失败由 worker 原路退回积分（见 app/worker.py）

视频链路（新底座，2026-10-15 定案：网关 quota 为账本）：
- 扣费不走 BFF 积分——用用户影子账号 PAT 直调网关，扣费/退回网关原生完成；
- BFF 只做编排：校验上架/参数 → 建 task 记录 → Celery 提交+轮询。
"""
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, newapi_adapter
from ..config import settings
from ..credits import CreditError, spend_credits
from ..db import get_db
from ..deps import get_app, get_current_user
from ..models import App, AppModel, Task, User
from ..quota_sync import get_user_pat
from ..schemas import ApiResponse, CutoutReq, TaskOut, VideoTaskReq
from ..worker import run_cutout, run_video_task

router = APIRouter()


def _gen_task_no(prefix: str = "C") -> str:
    return prefix + datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(4).upper()


def _tool_cost(app: App, code: str = "cutout") -> tuple[bool, int]:
    """返回 (enabled, cost)。apps.tools 配置优先，未配置用默认值。"""
    for tool in app.tools or []:
        if tool.get("code") == code:
            return bool(tool.get("enabled", True)), int(tool.get("cost", settings.CUTOUT_COST_CREDITS))
    return True, settings.CUTOUT_COST_CREDITS


@router.post("/cutout", response_model=ApiResponse)
def create_cutout(req: CutoutReq, app: App = Depends(get_app),
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    enabled, cost = _tool_cost(app, "cutout")
    if not enabled:
        raise HTTPException(status_code=403, detail="tool disabled")

    task_no = _gen_task_no()
    try:
        with db.begin():
            spend_credits(
                db, user, cost, app_id=app.app_id, ref_id=task_no, remark="AI抠图"
            )
            db.add(
                models.Task(
                    task_no=task_no,
                    user_id=user.id,
                    app_id=app.app_id,
                    type="cutout",
                    status="pending",
                    params={"image_url": req.image_url, "output_format": req.output_format},
                    cost_credits=cost,
                )
            )
    except CreditError as e:
        raise HTTPException(status_code=402, detail=e.message)

    # 事务已提交，发队列（worker 失败会自动重试，耗尽后标 failed 并退回积分）
    run_cutout.delay(task_no)
    return ApiResponse(data={"task_no": task_no, "status": "pending", "cost_credits": cost})


@router.post("/video", response_model=ApiResponse)
def create_video(req: VideoTaskReq, app: App = Depends(get_app),
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """视频生成提交（网关扣费）。前置校验全在 BFF，提交/轮询在 worker。"""
    model_row = (
        db.query(AppModel)
        .filter(AppModel.model_name == req.model_name, AppModel.enabled.is_(True))
        .first()
    )
    if not model_row:
        raise HTTPException(status_code=404, detail="模型不存在或已下架")
    if model_row.ability not in ("t2v", "i2v"):
        raise HTTPException(status_code=400, detail="该模型不支持视频生成")

    # 影子账号 PAT 就绪校验（扣费主体是用户的网关账号）
    pat = get_user_pat(db, user.id)
    if pat is None:
        raise HTTPException(status_code=409, detail="网关账号未就绪，请重新绑定手机号或稍后重试")

    task_no = _gen_task_no("V")
    try:
        with db.begin():
            db.add(
                models.Task(
                    task_no=task_no,
                    user_id=user.id,
                    app_id=app.app_id,
                    type="video",
                    status="pending",
                    params={
                        "model": req.model_name,
                        "ability": model_row.ability,
                        **req.params,
                    },
                    cost_credits=model_row.price_credits,  # 展示价；实扣由网关完成
                )
            )
    except CreditError as e:  # 防御：视频链路不扣 BFF 积分，保留兜底
        raise HTTPException(status_code=402, detail=e.message)

    run_video_task.delay(task_no)
    return ApiResponse(data={
        "task_no": task_no, "status": "pending",
        "price_credits": model_row.price_credits,
    })


@router.get("/{task_no}", response_model=ApiResponse)
def get_task(task_no: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.task_no == task_no, Task.user_id == user.id).first()
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return ApiResponse(data=TaskOut.model_validate(task).model_dump())


@router.get("", response_model=ApiResponse)
def list_tasks(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tasks = (
        db.query(Task)
        .filter(Task.user_id == user.id)
        .order_by(Task.id.desc())
        .limit(20)
        .all()
    )
    return ApiResponse(data=[TaskOut.model_validate(t).model_dump() for t in tasks])
