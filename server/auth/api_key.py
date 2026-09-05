"""API Key 验证依赖

Stub 实现：任意有效 UUID 格式的 Bearer token 均可通过验证。
生产环境应替换为真实密钥查询逻辑。
"""
from __future__ import annotations

import re
import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from models.responses import ErrorResponse

# HTTPBearer 实例
bearer_scheme = HTTPBearer(auto_error=False)

# 简单的内存 API Key → supplier_id 映射（用于 stub）
_api_key_map: dict[str, str] = {}


def _is_valid_uuid(key: str) -> bool:
    """检查是否为合法 UUID 格式"""
    pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    return bool(re.match(pattern, key.lower()))


def register_key(api_key: str, supplier_id: str) -> None:
    """注册一个 API Key（用于 stub/测试）"""
    _api_key_map[api_key] = supplier_id


def get_supplier_by_key(api_key: str) -> str | None:
    """查询 API Key 对应的 supplier_id"""
    return _api_key_map.get(api_key)


async def get_current_supplier_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> str:
    """从 Bearer token 提取 supplier_id"""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "缺少 Authorization: Bearer <api_key> 头部",
                    "details": None,
                }
            },
        )

    key = credentials.credentials

    # stub：UUID 格式即可通过
    if not _is_valid_uuid(key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "INVALID_API_KEY",
                    "message": "无效的 API Key 格式（需为 UUID）",
                    "details": {"key_prefix": key[:8] + "..." if len(key) > 8 else key},
                }
            },
        )

    # stub：UUID 格式即为有效 Key；若已在映射表中返回对应 supplier_id，否则返回 key 的哈希摘要作为 stub id
    supplier_id = _api_key_map.get(key) or f"stub-{key[:8]}"
    return supplier_id


async def get_optional_supplier_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> str | None:
    """可选认证：未提供 token 时返回 None"""
    if credentials is None:
        return None

    try:
        return await get_current_supplier_id(credentials)
    except HTTPException:
        return None
