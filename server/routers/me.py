"""管理 API（需 Bearer API Key 认证）"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from auth.api_key import get_current_supplier_id
from loaders import supplier_loader
from models.responses import ErrorResponse
from models.supplier import MeUpdate, MeSkillAddResponse
from services import skill_filter

router = APIRouter(prefix="/me", tags=["me"])

# ─── 内存中的供应商更新记录（stub，真实环境写数据库） ────────────────────────
_updated_suppliers: dict[str, dict] = {}


# ─── GET /me ─────────────────────────────────────────────────────────────────

@router.get("")
async def get_me(supplier_id: str = Depends(get_current_supplier_id)):
    """返回当前供应商信息"""
    supplier = supplier_loader.get_supplier(supplier_id)
    if supplier is None:
        # stub：未找到时返回占位信息
        updated = _updated_suppliers.get(supplier_id)
        if updated:
            return updated
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "SUPPLIER_NOT_FOUND",
                    "message": f"供应商 '{supplier_id}' 不存在",
                    "details": {"supplier_id": supplier_id},
                }
            },
        )
    return {
        "id": supplier["id"],
        "company": supplier.get("company"),
        "category": supplier.get("category"),
        "keywords": supplier.get("keywords", []),
        "region": supplier.get("region"),
        "address": supplier.get("address"),
        "contact_phone": supplier.get("contact_phone"),
        "email": supplier.get("email"),
        "website": supplier.get("website"),
        "lat": supplier.get("lat"),
        "lng": supplier.get("lng"),
        "certifications": supplier.get("certifications", []),
        "note": supplier.get("note"),
        "claim": supplier.get("claim"),
        "agent": supplier.get("agent"),
    }


# ─── PATCH /me ───────────────────────────────────────────────────────────────

@router.patch("")
async def update_me(
    body: MeUpdate,
    supplier_id: str = Depends(get_current_supplier_id),
):
    """更新联系信息"""
    supplier = supplier_loader.get_supplier(supplier_id)
    if supplier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "SUPPLIER_NOT_FOUND",
                    "message": f"供应商 '{supplier_id}' 不存在",
                    "details": {"supplier_id": supplier_id},
                }
            },
        )

    # 合并更新
    updated = dict(supplier)
    if body.contact_phone is not None:
        updated["contact_phone"] = body.contact_phone
    if body.email is not None:
        updated["email"] = body.email
    if body.address is not None:
        updated["address"] = body.address
    if body.website is not None:
        updated["website"] = body.website
    if body.note is not None:
        updated["note"] = body.note

    _updated_suppliers[supplier_id] = updated
    return updated


# ─── POST /me/skill ──────────────────────────────────────────────────────────

@router.post("/skill", response_model=MeSkillAddResponse)
async def add_skill(
    body: MeSkillAdd,
    supplier_id: str = Depends(get_current_supplier_id),
):
    """提交 skill_url，经过白名单过滤后存储"""
    supplier = supplier_loader.get_supplier(supplier_id)
    if supplier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "SUPPLIER_NOT_FOUND",
                    "message": f"供应商 '{supplier_id}' 不存在",
                    "details": {"supplier_id": supplier_id},
                }
            },
        )

    # 从 URL 获取 skill JSON
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(body.skill_url)
            resp.raise_for_status()
            skill_json = resp.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_SKILL_URL",
                    "message": f"无法从 skill_url 获取内容：{exc}",
                    "details": {"skill_url": body.skill_url},
                }
            },
        )

    # 白名单过滤
    result = skill_filter.filter_skill(skill_json)

    # 更新 agent 信息
    updated = dict(supplier)
    agent = dict(supplier.get("agent", {}) or {})
    agent["skill_url"] = body.skill_url
    agent["skill_name"] = skill_json.get("name") or skill_json.get("title", "")
    agent["skill_version"] = skill_json.get("version", "1.0.0")
    agent["skill_filtered"] = bool(result.removed_tools)
    updated["agent"] = agent
    _updated_suppliers[supplier_id] = updated

    message = "Skill 已保存"
    if result.warning:
        message += f"。警告：{result.warning}"

    return MeSkillAddResponse(
        success=True,
        filtered=bool(result.removed_tools),
        skill_name=agent["skill_name"],
        message=message,
    )


# ─── DELETE /me/skill ───────────────────────────────────────────────────────

@router.delete("/skill")
async def delete_skill(supplier_id: str = Depends(get_current_supplier_id)):
    """下架 Skill"""
    supplier = supplier_loader.get_supplier(supplier_id)
    if supplier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "SUPPLIER_NOT_FOUND",
                    "message": f"供应商 '{supplier_id}' 不存在",
                    "details": {"supplier_id": supplier_id},
                }
            },
        )

    updated = dict(supplier)
    updated["agent"] = None
    _updated_suppliers[supplier_id] = updated

    return {"success": True, "message": "Skill 已下架"}
