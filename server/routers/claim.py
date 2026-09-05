"""认主 API（微信验证码流程 Phase 0 stub）

四个端点：
  POST /claim/verify-start   → 返回 session_id + stub 二维码 URL
  POST /claim/send-code     → stub 直接返回 {sent: true}
  POST /claim/verify-code  → stub 任意 6 位数字均可通过
  POST /claim/confirm      → stub 随机分配 supplier_id
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import Field

from models.responses import ErrorResponse
from pydantic import BaseModel

router = APIRouter(prefix="/claim", tags=["claim"])

# 内存会话存储（Phase 0 stub）
_verify_sessions: dict[str, dict] = {}


# ─── POST /claim/verify-start ─────────────────────────────────────────────────

class VerifyStartRequest(BaseModel):
    supplier_id: str = Field(description="待认主的供应商 ID")


class VerifyStartResponse(BaseModel):
    session_id: str
    qrcode_url: str


@router.post("/verify-start", response_model=VerifyStartResponse)
async def verify_start(req: VerifyStartRequest):
    session_id = str(uuid.uuid4())
    _verify_sessions[session_id] = {
        "supplier_id": req.supplier_id,
        "created_at": datetime.utcnow().isoformat(),
        "code_verified": False,
        "claim_token": None,
    }
    return VerifyStartResponse(
        session_id=session_id,
        # stub 二维码 URL
        qrcode_url="https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket=STUB_TICKET_PHASE0",
    )


# ─── POST /claim/send-code ────────────────────────────────────────────────────

class SendCodeRequest(BaseModel):
    session_id: str
    phone: str = Field(min_length=11, max_length=11)


class SendCodeResponse(BaseModel):
    sent: bool


@router.post("/send-code", response_model=SendCodeResponse)
async def send_code(req: SendCodeRequest):
    if req.session_id not in _verify_sessions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_SESSION",
                    "message": "session_id 无效或已过期",
                    "details": {"session_id": req.session_id},
                }
            },
        )
    # stub：直接成功
    return SendCodeResponse(sent=True)


# ─── POST /claim/verify-code ──────────────────────────────────────────────────

class VerifyCodeRequest(BaseModel):
    session_id: str
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class VerifyCodeResponse(BaseModel):
    success: bool
    supplier_id: str | None = None
    claim_token: str | None = None


@router.post("/verify-code", response_model=VerifyCodeResponse)
async def verify_code(req: VerifyCodeRequest):
    session = _verify_sessions.get(req.session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_SESSION",
                    "message": "session_id 无效或已过期",
                    "details": {"session_id": req.session_id},
                }
            },
        )

    # stub：任意 6 位数字均可通过
    session["code_verified"] = True
    claim_token = str(uuid.uuid4())
    session["claim_token"] = claim_token

    return VerifyCodeResponse(
        success=True,
        supplier_id=session["supplier_id"],
        claim_token=claim_token,
    )


# ─── POST /claim/confirm ─────────────────────────────────────────────────────

class ConfirmRequest(BaseModel):
    claim_token: str


class ConfirmResponse(BaseModel):
    status: str
    can_add_skill: bool


@router.post("/confirm", response_model=ConfirmResponse)
async def confirm_claim(req: ConfirmRequest):
    # 查找持有该 claim_token 的 session
    session = None
    for sess in _verify_sessions.values():
        if sess.get("claim_token") == req.claim_token:
            session = sess
            break

    if not session:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_TOKEN",
                    "message": "claim_token 无效或已过期",
                    "details": {"claim_token": req.claim_token[:8] + "..."},
                }
            },
        )

    if not session.get("code_verified"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "CODE_NOT_VERIFIED",
                    "message": "验证码未通过，无法确认认主",
                    "details": {},
                }
            },
        )

    # stub：随机分配 supplier_id（实际上用 session 中已有的）
    return ConfirmResponse(
        status="claimed",
        can_add_skill=True,
    )
