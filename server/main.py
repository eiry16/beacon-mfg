"""
BeaconMFG API 服务
FastAPI + Uvicorn
"""
from __future__ import annotations

import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config import RATE_LIMIT_READ, RATE_LIMIT_WRITE, RATE_WINDOW
from loaders import supplier_loader
from models.responses import ErrorResponse

from routers import categories, claim, me, stats, suppliers

# ─── 速率限制中间件 ────────────────────────────────────────────────────────────

class RateLimitMiddleware(BaseHTTPMiddleware):
    """简单内存计数器速率限制"""

    def __init__(self, app, read_limit: int, write_limit: int, window: int):
        super().__init__(app)
        self.read_limit = read_limit
        self.write_limit = write_limit
        self.window = window
        # { key: [(timestamp, count), ...] }
        self._counters: dict[str, list[tuple[float, int]]] = defaultdict(list)

    def _clean(self, key: str) -> None:
        """清理过期记录"""
        now = time.time()
        self._counters[key] = [
            (t, c) for t, c in self._counters[key] if now - t < self.window
        ]

    def _count(self, key: str) -> int:
        """返回当前窗口内的请求数"""
        self._clean(key)
        return sum(c for _, c in self._counters[key])

    def _increment(self, key: str, n: int = 1) -> None:
        now = time.time()
        self._counters[key].append((now, n))
        self._clean(key)

    def _limit_key(self, request: Request) -> str:
        """速率限制的 key：GET 用 IP，其他用 API Key"""
        if request.method == "GET":
            return f"ip:{request.client.host}"
        # 尝试从 Header 提取 API Key
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return f"key:{auth[7:]}"
        return f"ip:{request.client.host}"

    async def dispatch(self, request: Request, call_next: Callable):
        key = self._limit_key(request)
        is_write = request.method in ("POST", "PUT", "PATCH", "DELETE")
        limit = self.write_limit if is_write else self.read_limit

        count = self._count(key)
        if count >= limit:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": f"请求过于频繁，请稍后再试（{limit}次/分钟）",
                        "details": {
                            "limit": limit,
                            "window_seconds": self.window,
                            "retry_after_seconds": self.window,
                        },
                    }
                },
                headers={"Retry-After": str(self.window)},
            )

        self._increment(key)
        return await call_next(request)


# ─── 启动/关闭生命周期 ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时加载数据
    print("正在加载数据...")
    supplier_loader.load()
    print(f"数据加载完成，共 {supplier_loader.supplier_count()} 条供应商记录")
    yield
    # 关闭时清理（暂无需要）
    print("服务关闭")


# ─── FastAPI 应用 ────────────────────────────────────────────────────────────

app = FastAPI(
    title="BeaconMFG API",
    description="制造业供应商检索与认主 API",
    version="0.1.0",
    lifespan=lifespan,
)

# 速率限制中间件
app.add_middleware(
    RateLimitMiddleware,
    read_limit=RATE_LIMIT_READ,
    write_limit=RATE_LIMIT_WRITE,
    window=RATE_WINDOW,
)

# ─── 全局异常处理 ────────────────────────────────────────────────────────────

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """统一 HTTP 异常格式"""
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "HTTP_ERROR",
                "message": str(exc.detail),
                "details": None,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """参数校验错误"""
    errors = exc.errors()
    first = errors[0] if errors else {}
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "INVALID_PARAMS",
                "message": "参数校验失败",
                "details": {
                    "field": ".".join(str(loc) for loc in first.get("loc", [])),
                    "reason": first.get("msg", "校验失败"),
                    "all_errors": errors,
                },
            }
        },
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """未处理异常"""
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "服务器内部错误",
                "details": {"type": type(exc).__name__, "str": str(exc)},
            }
        },
    )


# ─── 注册路由 ─────────────────────────────────────────────────────────────────

app.include_router(suppliers.router)
app.include_router(categories.router)
app.include_router(claim.router)
app.include_router(me.router)
app.include_router(stats.router)


# ─── 根路径 ──────────────────────────────────────────────────────────────────

@app.get("/", tags=["root"])
async def root():
    return {
        "service": "BeaconMFG API",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/health",
        "stats": "/stats",
    }
