"""积分路由：余额代理 / 网关流水 / 每日签到 / 激励视频奖励 / 充值。

账本口径（2026-10-15 定案）：new-api quota 为唯一账本，BFF 只做展示和编排。
- GET /balance    代理网关余额（60s 缓存）
- GET /logs       代理网关消耗流水（/api/log/self）
- POST /checkin   发放队列占位 -> add_quota（失败留 pending 由补偿 worker 重试）
- POST /ad-reward 同上
- POST /recharge-order   安卓微信 JSAPI 下单（返回 wx.requestPayment 参数）
- POST /recharge-notify  微信支付异步回调（免签名，AES-GCM 解密 + quota_grants 幂等发放）
- GET  /recharge-guide   iOS 站外引导充值页（合规绕过苹果虚拟支付红线）

激励视频注意：微信流量主需累计 UV >= 1000 才能开通，未开通前该接口
返回 403（前端隐藏入口），真实路径是「上线初期纯免费 -> UV 达标开广告」。
"""
import json
import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..credits import CreditError, do_checkin, grant_ad_reward
from ..db import get_db
from ..deps import get_app, get_current_user
from ..models import App, QuotaGrant, RechargeOrder, User
from ..quota_sync import get_gateway_balance, get_user_pat, sync_grant
from .. import newapi_adapter, wechat_pay
from ..schemas import (
    AdRewardOut,
    ApiResponse,
    BalanceOut,
    CheckinOut,
    GatewayLogOut,
    RechargeGuideOut,
    RechargeOrderOut,
    RechargeOrderReq,
)

logger = logging.getLogger("bff.credits")
router = APIRouter()


@router.get("/balance", response_model=ApiResponse)
def balance(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """网关余额（唯一账本）。linked=False -> 引导重试 bind-phone。"""
    data = get_gateway_balance(db, user.id)
    return ApiResponse(data=BalanceOut(**data).model_dump())


@router.get("/logs", response_model=ApiResponse)
def logs(page: int = 1, size: int = 20, user: User = Depends(get_current_user),
         db: Session = Depends(get_db)):
    """网关消耗流水代理（new-api /api/log/self）。未就绪返回空列表 + linked=false。"""
    pat = get_user_pat(db, user.id)
    if pat is None:
        return ApiResponse(data={"linked": False, "items": []})
    _, uid = pat
    try:
        data = newapi_adapter.get_user_logs(pat[0], uid, page=max(page - 1, 0),
                                            page_size=min(size, 100))
    except newapi_adapter.NewApiError as e:
        logger.warning("gateway logs proxy failed for user %s: %s", user.id, e)
        raise HTTPException(status_code=502, detail="网关流水查询失败，请稍后重试")
    items = data.get("items") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        out.append(GatewayLogOut(
            created_at=it.get("created_at"),
            model_name=str(it.get("model_name") or ""),
            quota=int(it.get("quota") or 0),
            prompt_tokens=int(it.get("prompt_tokens") or 0),
            completion_tokens=int(it.get("completion_tokens") or 0),
            type=int(it.get("type") or 0),
            remark=str(it.get("remark") or ""),
        ).model_dump())
    return ApiResponse(data={"linked": True, "items": out})


@router.post("/checkin", response_model=ApiResponse)
def checkin(app: App = Depends(get_app), user: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    try:
        with db.begin():
            amount, ref_id, streak = do_checkin(db, user, app_id=app.app_id)
    except CreditError as e:
        # 40010 已签到 -> 409 冲突
        status = 409 if e.code == 40010 else 400
        raise HTTPException(status_code=status, detail=e.message)

    # 事务外发放（占位已提交，失败留 pending 由补偿 worker 兜底）
    grant = db.query(QuotaGrant).filter(QuotaGrant.ref_id == ref_id).first()
    synced = True
    if grant is not None and grant.status == "pending":
        synced = sync_grant(db, grant) == "synced"
    return ApiResponse(data=CheckinOut(
        credits=amount, streak_days=streak, synced=synced,
    ).model_dump())


@router.post("/ad-reward", response_model=ApiResponse)
def ad_reward(app: App = Depends(get_app), user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    # 流量主未开通（MVP）：前置开关由前端控制，这里兜底校验 app.tools 配置
    ad_enabled = any(t.get("code") == "ad_reward" and t.get("enabled") for t in (app.tools or []))
    if not ad_enabled:
        raise HTTPException(status_code=403, detail="ad reward not available yet")
    try:
        with db.begin():
            amount, ref_id, count = grant_ad_reward(db, user, app_id=app.app_id)
    except CreditError as e:
        status = 429 if e.code == 40011 else 400
        raise HTTPException(status_code=status, detail=e.message)

    grant = db.query(QuotaGrant).filter(QuotaGrant.ref_id == ref_id).first()
    synced = True
    if grant is not None and grant.status == "pending":
        synced = sync_grant(db, grant) == "synced"
    return ApiResponse(data=AdRewardOut(
        credits=amount, today_count=count,
        daily_limit=settings.AD_REWARD_DAILY_LIMIT, synced=synced,
    ).model_dump())


# ============ 充值（安卓 JSAPI / iOS 引导） ============


@router.post("/recharge-order", response_model=ApiResponse)
def recharge_order(
    req: RechargeOrderReq,
    app: App = Depends(get_app),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """安卓微信 JSAPI 下单，返回 wx.requestPayment 参数。

    amount 单位「元」；credits=amount*RECHARGE_YUAN_TO_CREDITS。
    未开启/非安卓 -> 明确报错（iOS 必须走站外引导，合规红线）。
    """
    if not settings.WXPAY_ENABLED:
        raise HTTPException(status_code=403, detail="recharge not enabled")
    if req.platform != "android":
        raise HTTPException(
            status_code=400,
            detail="only android supports in-app JSAPI pay; ios use /credits/recharge-guide",
        )
    identity = (
        db.query(models.UserIdentity)
        .filter(models.UserIdentity.user_id == user.id, models.UserIdentity.app_id == app.app_id)
        .first()
    )
    if not identity:
        raise HTTPException(status_code=400, detail="user identity not found for this app")
    openid = identity.openid
    total_fen = int(req.amount * 100)
    credits = int(req.amount * settings.RECHARGE_YUAN_TO_CREDITS)
    # 订单号：R + 时间(秒) + uid + 随机，保证唯一且 human-readable
    order_no = "R" + datetime.now().strftime("%Y%m%d%H%M%S") + str(user.id) + secrets.token_hex(4)
    try:
        prepay_id = wechat_pay.create_jsapi_order(
            order_no, total_fen, "具身之道积分充值", openid, app.app_id
        )
    except wechat_pay.WxPayError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    db.add(models.RechargeOrder(
        order_no=order_no, user_id=user.id, app_id=app.app_id, platform="android",
        amount_fen=total_fen, credits=credits, status="created",
    ))
    db.commit()
    sign = wechat_pay.build_pay_sign(prepay_id, app.app_id)
    return ApiResponse(data=RechargeOrderOut(
        order_no=order_no,
        timeStamp=sign["timeStamp"],
        nonceStr=sign["nonceStr"],
        package=sign["package"],
        signType=sign["signType"],
        paySign=sign["paySign"],
    ).model_dump())


@router.get("/recharge-guide", response_model=ApiResponse)
def recharge_guide(user: User = Depends(get_current_user)):
    """iOS 引导充值：返回站外 PC/H5 充值页 URL（合规绕过苹果 3.1.1 红线）。"""
    return ApiResponse(data=RechargeGuideOut(url=settings.RECHARGE_WEB_URL).model_dump())


@router.post("/recharge-notify")
async def recharge_notify(request: Request, db: Session = Depends(get_db)):
    """微信支付异步回调（免 BFF 签名；微信用平台证书验签 + APIv3 密钥解密）。

    返回 {"code":"SUCCESS","message":"成功"} + HTTP 200 告知微信受理成功（否则会重推）。
    配额发放走 quota_grants 幂等闸门（recharge:{order_no}），重复回调不重复加额。
    """
    body = await request.body()
    ts = request.headers.get("Wechatpay-Timestamp", "")
    nonce = request.headers.get("Wechatpay-Nonce", "")
    sig = request.headers.get("Wechatpay-Signature", "")
    if not wechat_pay.verify_notify(ts, nonce, body, sig):
        return JSONResponse(status_code=401, content={"code": "FAIL", "message": "verify failed"})
    try:
        data = json.loads(body.decode("utf-8"))
        plain = wechat_pay.decrypt_notify(data.get("resource", {}))
    except Exception as e:  # noqa: BLE001
        logger.error("recharge notify parse/decrypt failed: %s", e)
        return JSONResponse(status_code=200, content={"code": "FAIL", "message": "bad notify"})
    if plain.get("trade_state") != "SUCCESS":
        # 非成功态（支付中/关闭/失败）直接回 SUCCESS 让微信不再推送
        return JSONResponse(status_code=200, content={"code": "SUCCESS", "message": "ignored"})
    order_no = plain.get("out_trade_no", "")
    if not order_no:
        return JSONResponse(status_code=200, content={"code": "FAIL", "message": "missing order no"})
    from ..quota_sync import settle_recharge_order

    try:
        result = settle_recharge_order(db, order_no)
    except Exception as e:  # noqa: BLE001
        logger.error("settle recharge order %s failed: %s", order_no, e)
        return JSONResponse(status_code=200, content={"code": "FAIL", "message": "settle failed"})
    if result == "unknown_order":
        return JSONResponse(status_code=200, content={"code": "FAIL", "message": "unknown order"})
    return JSONResponse(status_code=200, content={"code": "SUCCESS", "message": "成功"})
