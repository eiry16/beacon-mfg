"""供应商检索路由"""
from __future__ import annotations

import math
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from auth.api_key import get_current_supplier_id
from loaders import supplier_loader
from loaders.supplier_loader import get_all_suppliers, get_categories, get_supplier
from models.responses import SupplierListResponse, SupplierDetailResponse, PaginationMeta
from services import ranker
from services.ranker import rank_suppliers, _haversine

router = APIRouter(prefix="/suppliers", tags=["suppliers"])


# ─── 统一错误辅助 ─────────────────────────────────────────────────────────────

def make_error(code: str, message: str, details: dict | None = None, status_code: int = 400):
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": details}},
    )


# ─── 过滤候选集 ───────────────────────────────────────────────────────────────

def _filter_candidates(
    suppliers: list[dict],
    keyword: Optional[str],
    claimed: Optional[bool],
    verified: Optional[bool],
    has_skill: Optional[bool],
) -> list[dict]:
    result = []
    for s in suppliers:
        # claimed/verified 过滤
        claim = s.get("claim", {}) or {}
        claim_status = claim.get("status", "unclaimed")
        if claimed is True and claim_status not in ("claimed", "verified"):
            continue
        if claimed is False and claim_status != "unclaimed":
            continue
        if verified is True and claim_status != "verified":
            continue
        if verified is False and claim_status == "verified":
            continue
        # has_skill 过滤
        if has_skill is True and not s.get("agent", {}).get("skill_url"):
            continue
        if has_skill is False and s.get("agent", {}).get("skill_url"):
            continue
        # keyword 模糊过滤（名称/品类/关键词/地址）
        if keyword:
            kw_lower = keyword.lower()
            searchable = " ".join(
                filter(None, [
                    s.get("company", ""),
                    s.get("category", ""),
                    " ".join(s.get("keywords", [])),
                    s.get("address", ""),
                    s.get("note", ""),
                ])
            ).lower()
            if kw_lower not in searchable:
                continue
        result.append(s)
    return result


def _keyword_to_list(keyword: Optional[str]) -> list[str]:
    """将逗号分隔的关键词字符串转为列表"""
    if not keyword:
        return []
    return [kw.strip() for kw in keyword.replace("，", ",").split(",") if kw.strip()]


# ─── GET /suppliers ────────────────────────────────────────────────────────────

@router.get("", response_model=SupplierListResponse)
async def list_suppliers(
    request: Request,
    keyword: Annotated[Optional[str], Query(description="关键词搜索（逗号分隔）")] = None,
    category: Annotated[Optional[str], Query(description="品类名称")] = None,
    city: Annotated[Optional[str], Query(description="城市名，如'东莞'")] = None,
    province: Annotated[Optional[str], Query(description="省份名，如'广东'")] = None,
    lat: Annotated[Optional[float], Query(description="纬度")] = None,
    lng: Annotated[Optional[float], Query(description="经度")] = None,
    radius: Annotated[Optional[float], Query(description="搜索半径 km")] = None,
    claimed: Annotated[Optional[bool], Query(description="已认主")] = None,
    verified: Annotated[Optional[bool], Query(description="已认证")] = None,
    has_skill: Annotated[Optional[bool], Query(description="有 Skill")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: Annotated[str, Query(pattern="^(relevance|distance|completeness)$")] = "relevance",
    ranking_profile: Annotated[
        str,
        Query(pattern="^(default|nearest|verified|complete)$"),
    ] = "default",
):
    include_debug = (
        request.headers.get("X-Include-Ranking-Debug", "").lower() == "true"
    )

    # 1. 确定候选集
    if category:
        cats = get_categories()
        cat_names = {c["name"] for c in cats}
        if category not in cat_names:
            make_error(
                "INVALID_PARAMS",
                f"品类 '{category}' 不在索引中",
                {"field": "category", "reason": "不在索引中"},
            )
        candidates = get_all_suppliers() if False else supplier_loader.get_suppliers_by_category(category)
    elif province or city:
        # 按城市筛选
        city_key = f"{province}-{city}" if province and city else (province or city or "")
        candidates = supplier_loader.get_suppliers_by_city(city_key)
    else:
        candidates = get_all_suppliers()

    # 2. 关键词/状态过滤
    candidates = _filter_candidates(candidates, keyword, claimed, verified, has_skill)

    # 3. 距离预过滤（如果指定了位置和半径）
    if lat is not None and lng is not None and radius is not None:
        filtered = []
        for s in candidates:
            sup_lat = s.get("lat")
            sup_lng = s.get("lng")
            if sup_lat is not None and sup_lng is not None:
                d = _haversine(lat, lng, sup_lat, sup_lng)
                if d <= radius:
                    filtered.append(s)
            # 无坐标：保守包含
            else:
                filtered.append(s)
        candidates = filtered

    # 4. 排序/排名
    query_kws = _keyword_to_list(keyword)

    if sort == "distance" and lat is not None and lng is not None:
        # 按距离排序
        scored = []
        for s in candidates:
            sup_lat = s.get("lat")
            sup_lng = s.get("lng")
            if sup_lat is not None and sup_lng is not None:
                d = _haversine(lat, lng, sup_lat, sup_lng)
            else:
                d = float("inf")
            scored.append((s, -d))
        scored.sort(key=lambda x: x[1])
        ordered = [s for s, _ in scored]
    elif sort == "completeness":
        # 按完善度排序
        ordered = sorted(
            candidates,
            key=lambda s: s.get("_completeness", 0.0),
            reverse=True,
        )
    else:
        # relevance 模式：综合排名
        scored = ranker.rank_suppliers(
            candidates, query_kws, lat, lng, ranking_profile
        )
        ordered = [s for s, _, _ in scored]

    # 5. 分页
    total = len(ordered)
    total_pages = math.ceil(total / per_page)
    start = (page - 1) * per_page
    page_items = ordered[start:start + per_page]

    # 6. 补充 _completeness（从缓存取出）
    for item in page_items:
        item["_completeness"] = supplier_loader.completeness_score(item["id"])

    # 7. 构建响应
    results = []
    for item in page_items:
        rec: dict[str, Any] = {
            "id": item["id"],
            "company": item.get("company"),
            "category": item.get("category"),
            "keywords": item.get("keywords", []),
            "region": item.get("region"),
            "address": item.get("address"),
            "contact_phone": item.get("contact_phone"),
            "email": item.get("email"),
            "website": item.get("website"),
            "lat": item.get("lat"),
            "lng": item.get("lng"),
            "certifications": item.get("certifications", []),
            "note": item.get("note"),
            "claim": item.get("claim"),
            "agent": item.get("agent"),
            "_completeness_score": item.get("_completeness", 0.0),
        }

        # 排名分（relevance 模式下才计算）
        if sort == "relevance" and item in ordered[:page_items.index(item) + 1]:
            # 找到对应的排名信息
            for s, score, comps in scored:
                if s["id"] == item["id"]:
                    rec["_rank_score"] = score
                    if include_debug:
                        rec["_rank_debug"] = comps
                    break

        results.append(rec)

    return SupplierListResponse(
        meta=PaginationMeta(
            total=total,
            page=page,
            per_page=per_page,
            total_pages=total_pages,
            query={
                "keyword": keyword,
                "category": category,
                "city": city,
                "province": province,
                "ranking_profile": ranking_profile,
            },
        ),
        suppliers=results,
    )


# ─── GET /suppliers/{id} ───────────────────────────────────────────────────────

@router.get("/{supplier_id}", response_model=SupplierDetailResponse)
async def get_supplier_detail(
    supplier_id: str,
    request: Request,
):
    include_debug = (
        request.headers.get("X-Include-Ranking-Debug", "").lower() == "true"
    )
    supplier = get_supplier(supplier_id)
    if not supplier:
        make_error(
            "NOT_FOUND",
            f"供应商 '{supplier_id}' 不存在",
            {"supplier_id": supplier_id},
            404,
        )

    result = {
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
        "_completeness_score": supplier_loader.completeness_score(supplier_id),
    }

    if include_debug:
        result["_rank_debug"] = {
            "note": "单条查询不参与排名，仅展示基本信息"
        }

    return SupplierDetailResponse(supplier=result)


# ─── GET /suppliers/{id}/skill ────────────────────────────────────────────────

@router.get("/{supplier_id}/skill")
async def get_supplier_skill(
    supplier_id: str,
):
    supplier = get_supplier(supplier_id)
    if not supplier:
        make_error(
            "NOT_FOUND",
            f"供应商 '{supplier_id}' 不存在",
            {"supplier_id": supplier_id},
            404,
        )
    agent = supplier.get("agent", {}) or {}
    skill_url = agent.get("skill_url")
    if not skill_url:
        make_error(
            "NOT_FOUND",
            "该供应商尚未提交 Skill",
            {"supplier_id": supplier_id},
            404,
        )
    return {
        "supplier_id": supplier_id,
        "skill_url": skill_url,
        "skill_name": agent.get("skill_name"),
        "skill_version": agent.get("skill_version"),
    }
