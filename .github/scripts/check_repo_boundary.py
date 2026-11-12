#!/usr/bin/env python3
"""公开仓库边界守卫 —— 本仓库只放「MCP 能跑起来的最小集」。

这是**唯一权威白名单**，随仓库分发。CI（.github/workflows/repo-boundary.yml）
与本机 pre-commit 钩子都调本文件，判据同源，不另写一套（另写一套必然漂移）。

判据用「白名单 + 禁入路径」双层，而不是黑名单：
白名单让**任何新增跟踪文件默认被拦**，必须有人显式修改这里并说明理由 ——
这才是「不用反复提醒 agent」的机制。禁入路径用于挡住宽前缀下的派生副本。

除路径外，还扫**所有公开跟踪文件的正文**里是否出现确凿不该公开的叙述
（事故经过、取证线索、本机身份等）。只拦确凿项：日期、实测数据、设计理由
都是正常工程注释，不在拦截列表里 —— 门禁喊狼来了比没有门禁更糟。
明文 PII 由本地脱敏链路处理，这里不重复实现一套判据。

用法：
    python .github/scripts/check_repo_boundary.py     # exit 1 = 越界
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 允许的根级文件：许可、对外声明、发布链与守卫自身
ALLOW_EXACT = frozenset({
    ".gitignore",
    "LICENSE",
    "README.md",
    "DATA_LICENSE.md",
    "glama.json",
    ".github/workflows/mcp-release.yml",
    ".github/workflows/npm-publish.yml",
    ".github/workflows/repo-boundary.yml",
    ".github/scripts/check_repo_boundary.py",
})

# 允许的目录前缀：MCP 包本体 + 寻源内核（npm prepack 从这里拷贝，缺了三个寻源工具会降级）
ALLOW_PREFIX = ("mcp/", "skills/rfq-kernel/")

# 禁入路径**优先级高于白名单**：防止宽前缀被派生副本借用
DENY_PREFIX = (
    "data/", "skills/schema/", "skills/registry/", "skills/vendors/",
    "skills/profiles/", "skills/_vendors_backup_", "skills/README.md",
    "docs/", "scripts/", "server/", "pages/", "APK/", "dist/", "cache/",
    "templates/", "agent-skill/", "examples/", "_backup/", "_archive/",
    ".workbuddy/", "mcp/rfq-kernel/", "mcp/test_update.js",
)

# .gitignore 里不许被悄悄删掉的关键行（白名单之外的第二道防线）
REQUIRED_IGNORES = (
    "server/", "scripts/", "pages/", "APK/", "data/", "skills/registry/",
    "skills/vendors/", "docs/", "skills/schema/", "mcp/rfq-kernel/",
)

# 公开文件正文里绝不该出现的叙述。只列**确凿**项，不列「实测 / 日期 / 设计理由」。
# 加词前先确认当前 HEAD 里没有命中，否则 CI 一上线就红。
BANNED_NARRATIVES = (
    "force push", "悬空对象", "旧 SHA", "旧SHA", "内部码表",
    "schema-private", "snapshot-2026", "id_ed25519", "ghp_", "AKIA",
    "陆斌", r"C:\Users", "filter-repo", "无人通道", "独立私有仓库",
)
# 规则文件自身必然含这些词，扫自己会永远变红；它在白名单里，改动会被 review。
SELF_REL = ".github/scripts/check_repo_boundary.py"


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def gitignore_lines() -> set[str]:
    return {ln.strip() for ln in (ROOT / ".gitignore").read_text(
        encoding="utf-8").splitlines()}


def evaluate(files: list[str], ignores: set[str]) -> list[str]:
    """纯函数，便于正反例测试（不改仓库状态）。"""
    problems: list[str] = []
    for f in files:
        if f.startswith(DENY_PREFIX):
            problems.append(f"禁入路径（优先级高于白名单）: {f}")
        elif f in ALLOW_EXACT or f.startswith(ALLOW_PREFIX):
            continue
        else:
            problems.append(f"白名单外的跟踪文件: {f} —— 新增前请先改本脚本白名单并说明理由")
    for need in REQUIRED_IGNORES:
        if need not in ignores:
            problems.append(f".gitignore 缺失关键忽略行: {need}")
    return problems


def narrative_problems(files: list[str], root: Path = ROOT) -> list[str]:
    """扫公开文件正文里的确凿泄密叙述；root 可换，便于正反例测试。"""
    out: list[str] = []
    for f in files:
        if f == SELF_REL:
            continue
        p = root / f
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for w in BANNED_NARRATIVES:
            if w in text:
                line = text[:text.index(w)].count("\n") + 1
                out.append(f"公开文件出现不该有的叙述: {f}:{line} —— {w!r}")
                break
    return out


def main() -> int:
    files = tracked_files()
    problems = evaluate(files, gitignore_lines()) + narrative_problems(files)
    if problems:
        print(f"✗ 公开仓库边界检查失败（{len(problems)} 项）：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("", file=sys.stderr)
        print("本仓库只放 MCP 运行最小集。要新增跟踪文件，先改本脚本的白名单并在提交说明理由；",
              file=sys.stderr)
        print("若只是需要版本备份，就在该目录单独 git init 或推私有仓库 —— 备份 ≠ 公开。",
              file=sys.stderr)
        return 1
    print(f"✓ 公开仓库边界 OK：{len(files)} 个跟踪文件全在白名单内，"
          f"{len(REQUIRED_IGNORES)} 条关键忽略行齐全，"
          f"{len(files) - 1} 个文件正文叙述扫描 0 命中")
    return 0


if __name__ == "__main__":
    sys.exit(main())
