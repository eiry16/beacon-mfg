"""统计与健康检查路由"""
import time
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from loaders import supplier_loader
from loaders.region_loader import get_all_cities
from models.responses import HealthResponse, StatsResponse

router = APIRouter(tags=["system"])

# 服务启动时间
_START_TIME = time.time()


# ─── GET /stats ───────────────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsResponse)
async def get_stats():
    """全局统计信息"""
    categories = supplier_loader.get_categories()
    return StatsResponse(
        total_suppliers=supplier_loader.supplier_count(),
        total_categories=len(categories),
        total_cities=len(get_all_cities()),
        load_version=supplier_loader.get_load_version(),
        data_version="0.1.0",
    )


# ─── GET /health ─────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查"""
    return HealthResponse(
        status="ok",
        data_loaded=supplier_loader.is_loaded(),
        uptime_seconds=round(time.time() - _START_TIME, 2),
    )


# ─── POST /reload（管理端点） ─────────────────────────────────────────────────

@router.post("/reload")
async def reload_data(request: Request):
    """热更新数据（不重启服务）"""
    # 简单保护：检查 Header
    if request.headers.get("X-Reload-Secret") != "beacon-mfg-reload":
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "FORBIDDEN",
                    "message": "缺少 X-Reload-Secret 头部",
                    "details": None,
                }
            },
        )
    supplier_loader.reload()
    return {
        "success": True,
        "message": "数据已热更新",
        "load_version": supplier_loader.get_load_version(),
    }
