"""Skill 内容白名单过滤

允许的工具类型：catalog, rfq, quote, live_chat, search, query
禁止的工具类型：shell, exec, system, code_interpreter, download, upload,
                database, delete
"""
from __future__ import annotations

from typing import Any
from models.supplier import SkillFilterResult

# 白名单工具类型（允许）
ALLOWED_TOOL_TYPES: set[str] = {
    "catalog",
    "rfq",
    "quote",
    "live_chat",
    "search",
    "query",
}

# 黑名单工具类型（禁止）
BLOCKED_TOOL_TYPES: set[str] = {
    "shell",
    "exec",
    "system",
    "code_interpreter",
    "download",
    "upload",
    "database",
    "delete",
}


def filter_skill(skill_json: dict[str, Any]) -> SkillFilterResult:
    """扫描 skill JSON，移除禁止的工具定义，返回过滤后的安全版本"""
    original_tools: list[str] = []
    removed_tools: list[str] = []

    # 收集原始工具列表（支持多种常见字段名）
    raw_tools: list[dict] = []
    for field in ("tools", "capabilities", "functions", "actions"):
        val = skill_json.get(field)
        if isinstance(val, list):
            raw_tools.extend(val)

    for tool in raw_tools:
        if isinstance(tool, dict):
            tool_type = tool.get("type") or tool.get("kind") or tool.get("name", "")
            if tool_type in BLOCKED_TOOL_TYPES:
                removed_tools.append(tool_type)
            else:
                original_tools.append(tool_type)
        elif isinstance(tool, str):
            if tool in BLOCKED_TOOL_TYPES:
                removed_tools.append(tool)
            else:
                original_tools.append(tool)

    # 构建过滤后的副本（只移除禁止的工具，保留其他所有字段）
    filtered = _remove_blocked_tools(skill_json)

    if removed_tools:
        warning = (
            f"以下工具已被移除（禁止执行危险操作）：{', '.join(sorted(removed_tools))}"
        )
    else:
        warning = None

    # 如果 tools 字段被完全清空，发警告但不阻止
    if not filtered.get("tools") and skill_json.get("tools"):
        warning = (warning or "") + " (工具列表已被完全过滤，内容可能受限)"

    return SkillFilterResult(
        allowed=not removed_tools,
        original_tools=original_tools,
        removed_tools=removed_tools,
        filtered_skill=filtered,
        warning=warning,
    )


def _remove_blocked_tools(skill: dict[str, Any]) -> dict[str, Any]:
    """返回移除禁止工具后的深拷贝"""
    import copy
    result = copy.deepcopy(skill)

    for field in ("tools", "capabilities", "functions", "actions"):
        if field in result and isinstance(result[field], list):
            result[field] = [
                t
                for t in result[field]
                if not (
                    isinstance(t, dict)
                    and (t.get("type") or t.get("kind") or t.get("name", "")) in BLOCKED_TOOL_TYPES
                )
                and not (isinstance(t, str) and t in BLOCKED_TOOL_TYPES)
            ]

    return result
