"""排名算法实现

公式：
  品类匹配度 = (精确命中数 + 0.5*模糊命中数) / 查询关键词总数  (上限 1.0)
  认证状态   = verified:1.0  claimed:0.6  unclaimed:0.0
  完善度     = 8项核心字段非空数 / 8
  距离       = max(0, 1 - 距离km / 200)，无坐标给 0.5
  综合分     = 0.4*品类 + 0.3*认证 + 0.2*完善度 + 0.1*距离

支持 profile：
  default   - 综合评分
  nearest   - 距离优先（权重调整为 品类0.2 认证0.2 完善度0.2 距离0.4）
  verified  - 认证优先（权重调整为 品类0.2 认证0.4 完善度0.2 距离0.2）
  complete  - 完善度优先（权重调整为 品类0.2 认证0.2 完善度0.4 距离0.2）
"""
from __future__ import annotations

import math
from typing import Any, Optional

from config import RANK_WEIGHTS

# 默认权重
_WEIGHTS = {
    "default":   {"category": 0.4, "verified": 0.3, "completeness": 0.2, "distance": 0.1},
    "nearest":   {"category": 0.2, "verified": 0.2, "completeness": 0.2, "distance": 0.4},
    "verified":  {"category": 0.2, "verified": 0.4, "completeness": 0.2, "distance": 0.2},
    "complete":  {"category": 0.2, "verified": 0.2, "completeness": 0.4, "distance": 0.2},
}

# Haversine 公式计算两点间距离（km）
def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0  # 地球半径 km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def category_match_score(supplier_keywords: list[str], query_keywords: list[str]) -> float:
    """品类匹配度得分（精确命中 + 0.5*模糊命中）/查询词总数，上限 1.0"""
    if not query_keywords:
        return 1.0  # 无关键词时全部通过

    exact = sum(1 for kw in query_keywords if kw in supplier_keywords)
    total = len(query_keywords)
    return min(1.0, (exact + 0.5 * (total - exact)) / total)


def verified_score(claim_status: str) -> float:
    """认证状态得分"""
    mapping = {"verified": 1.0, "claimed": 0.6, "unclaimed": 0.0}
    return mapping.get(claim_status, 0.0)


def completeness_score_func(completeness: float) -> float:
    """信息完善度得分（直接使用预计算的 completeness 值）"""
    return min(1.0, max(0.0, completeness))


def distance_score(
    supplier_lat: Optional[float],
    supplier_lng: Optional[float],
    query_lat: Optional[float],
    query_lng: Optional[float],
) -> float:
    """距离得分：max(0, 1 - 距离km/200)，无坐标给 0.5"""
    if supplier_lat is None or supplier_lng is None or query_lat is None or query_lng is None:
        return 0.5
    dist = _haversine(supplier_lat, supplier_lng, query_lat, query_lng)
    return max(0.0, 1.0 - dist / 200.0)


def rank_supplier(
    supplier: dict[str, Any],
    query_keywords: list[str],
    query_lat: Optional[float],
    query_lng: Optional[float],
    profile: str = "default",
) -> tuple[float, dict[str, float]]:
    """对单条供应商记录计算综合排名分，返回 (总分, 分项得分字典)"""
    weights = _WEIGHTS.get(profile, _WEIGHTS["default"])

    # 品类匹配度
    sup_kws = supplier.get("keywords", [])
    cat_score = category_match_score(sup_kws, query_keywords)

    # 认证状态
    claim_status = supplier.get("claim", {}).get("status", "unclaimed")
    ver_score = verified_score(claim_status)

    # 完善度（使用预计算值）
    comp_score = supplier.get("_completeness", 0.0)

    # 距离
    dist_score = distance_score(
        supplier.get("lat"),
        supplier.get("lng"),
        query_lat,
        query_lng,
    )

    total = (
        weights["category"] * cat_score
        + weights["verified"] * ver_score
        + weights["completeness"] * comp_score
        + weights["distance"] * dist_score
    )

    components = {
        "category_score": round(cat_score, 4),
        "verified_score": round(ver_score, 4),
        "completeness_score": round(comp_score, 4),
        "distance_score": round(dist_score, 4),
    }

    return round(total, 6), components


def rank_suppliers(
    suppliers: list[dict[str, Any]],
    query_keywords: list[str],
    query_lat: Optional[float],
    query_lng: Optional[float],
    profile: str = "default",
) -> list[tuple[dict[str, Any], float, dict[str, float]]]:
    """对一批供应商排序，返回 (record, total_score, components) 列表，按总分降序"""
    scored = [
        (sup, *rank_supplier(sup, query_keywords, query_lat, query_lng, profile))
        for sup in suppliers
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored
