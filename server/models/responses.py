"""API 响应模型"""
from __future__ import annotations

from typing import Any, Generic, Optional, TypeVar
from pydantic import BaseModel, Field

T = TypeVar("T")


# ─── 统一错误响应 ─────────────────────────────────────────────────────────────

class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[dict[str, Any]] = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# ─── 通用分页响应 ─────────────────────────────────────────────────────────────

class PaginationMeta(BaseModel):
    total: int
    page: int
    per_page: int
    total_pages: int
    query: dict[str, Any] = Field(default_factory=dict)


class SupplierListResponse(BaseModel):
    meta: PaginationMeta
    suppliers: list[dict[str, Any]]


class CategoryListResponse(BaseModel):
    categories: list[dict[str, Any]]


class RegionListResponse(BaseModel):
    cities: list[str]
    metadata: dict[str, Any]


class CitySupplierResponse(BaseModel):
    city: str
    count: int
    supplier_ids: list[str]


class SupplierDetailResponse(BaseModel):
    supplier: dict[str, Any]


class StatsResponse(BaseModel):
    total_suppliers: int
    total_categories: int
    total_cities: int
    load_version: int
    data_version: str


class HealthResponse(BaseModel):
    status: str
    data_loaded: bool
    uptime_seconds: float
