"""Pydantic 数据模型"""
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


# ─── 基础子模型 ─────────────────────────────────────────────────────────────

class Region(BaseModel):
    province: str
    city: str


class Claim(BaseModel):
    status: str = Field(description="unclaimed | claimed | verified")
    claimed_at: Optional[str] = None
    verified_at: Optional[str] = None


class Agent(BaseModel):
    skill_url: Optional[str] = None
    skill_name: Optional[str] = None
    skill_version: Optional[str] = None
    skill_filtered: bool = False


class Supplier(BaseModel):
    id: str
    company: str
    category: str
    keywords: list[str] = Field(default_factory=list)
    region: Optional[Region] = None
    address: Optional[str] = None
    contact_phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    certifications: list[str] = Field(default_factory=list)
    note: Optional[str] = None
    source: Optional[str] = None
    location: Optional[str] = None
    is_template: bool = False
    verified_at: Optional[str] = None
    claim: Optional[Claim] = Field(default=None, description="认主状态（可选字段）")
    agent: Optional[Agent] = Field(default=None, description="Agent 信息（可选字段）")
    _rank_score: Optional[float] = Field(default=None, exclude=True)
    _rank_debug: Optional[dict] = Field(default=None, exclude=True)


# ─── 查询参数模型 ─────────────────────────────────────────────────────────────

class SearchQuery(BaseModel):
    keyword: Optional[str] = None
    category: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    radius: Optional[float] = Field(default=None, description="搜索半径 km")
    claimed: Optional[bool] = None
    verified: Optional[bool] = None
    has_skill: Optional[bool] = None
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=20, ge=1, le=100)
    sort: str = Field(default="relevance", pattern="^(relevance|distance|completeness)$")
    ranking_profile: str = Field(
        default="default",
        pattern="^(default|nearest|verified|complete)$",
    )


class RankingResult(BaseModel):
    supplier_id: str
    total_score: float
    category_score: float
    verified_score: float
    completeness_score: float
    distance_score: float


# ─── 认主流程模型 ─────────────────────────────────────────────────────────────

class ClaimVerifyStart(BaseModel):
    supplier_id: str


class ClaimVerifyStartResponse(BaseModel):
    session_id: str
    qrcode_url: str


class ClaimSendCode(BaseModel):
    session_id: str
    phone: str


class ClaimSendCodeResponse(BaseModel):
    sent: bool


class ClaimVerifyCode(BaseModel):
    session_id: str
    code: str = Field(min_length=6, max_length=6)


class ClaimVerifyCodeResponse(BaseModel):
    success: bool
    supplier_id: Optional[str] = None
    claim_token: Optional[str] = None


class ClaimConfirm(BaseModel):
    claim_token: str


class ClaimConfirmResponse(BaseModel):
    status: str
    can_add_skill: bool


# ─── 管理 API 模型 ────────────────────────────────────────────────────────────

class MeUpdate(BaseModel):
    contact_phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    website: Optional[str] = None
    note: Optional[str] = None


class MeSkillAdd(BaseModel):
    skill_url: str


class MeSkillAddResponse(BaseModel):
    success: bool
    filtered: bool
    skill_name: Optional[str] = None
    message: str


# ─── 工具白名单过滤结果 ──────────────────────────────────────────────────────

class SkillFilterResult(BaseModel):
    allowed: bool
    original_tools: list[str]
    removed_tools: list[str]
    filtered_skill: Optional[dict] = None
    warning: Optional[str] = None
