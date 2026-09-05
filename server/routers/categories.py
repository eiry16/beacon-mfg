"""品类与地区路由"""
from fastapi import APIRouter, HTTPException, status

from loaders import supplier_loader
from loaders.region_loader import get_all_cities, get_city_info
from models.responses import CategoryListResponse, RegionListResponse, CitySupplierResponse

router = APIRouter(tags=["categories"])


# ─── GET /categories ────────────────────────────────────────────────────────────

@router.get("/categories", response_model=CategoryListResponse)
async def list_categories():
    """返回所有品类列表（不含详细供应商数据）"""
    categories = supplier_loader.get_categories()
    return CategoryListResponse(categories=categories)


# ─── GET /regions ─────────────────────────────────────────────────────────────

@router.get("/regions", response_model=RegionListResponse)
async def list_regions():
    """返回所有城市列表及元数据"""
    cities = get_all_cities()
    metadata = supplier_loader.get_categories()[0:1]  # placeholder

    # 从 region_loader 获取 metadata
    from loaders.region_loader import get_metadata
    meta = get_metadata()

    return RegionListResponse(cities=cities, metadata=meta)


# ─── GET /regions/{city} ───────────────────────────────────────────────────────

@router.get("/regions/{city}")
async def get_city_suppliers(city: str):
    """返回指定城市（省-市格式）的供应商 ID 列表"""
    # city 可能是 "东莞" 或 "广东-东莞"
    all_cities = get_all_cities()
    city_key = None

    for ck in all_cities:
        if ck == city or ck.endswith(f"-{city}"):
            city_key = ck
            break

    if city_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "CITY_NOT_FOUND",
                    "message": f"未找到城市 '{city}'",
                    "details": {"city": city, "hint": "使用 '省-市' 格式如 '广东-东莞'"},
                }
            },
        )

    info = get_city_info(city_key)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "CITY_INDEX_ERROR",
                    "message": f"城市索引数据异常：'{city_key}'",
                    "details": {"city_key": city_key},
                }
            },
        )

    return CitySupplierResponse(
        city=city_key,
        count=info.get("count", 0),
        supplier_ids=info.get("ids", []),
    )
