#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作树全号体检 + 全号还原（`git checkout` 把脱敏版写回工作树之后的自救工具）。

背景：`.gitattributes` 约定 maskphone 过滤的路径（data/gb/**、data/en/**、
data/phone-index.jsonl）**工作树必须保留全号**，只有 `git add` 入库时才由
scripts/mask_phones.py 脱敏。但 `git checkout -- <路径>` / `git restore <路径>`
会把 **index 里的脱敏版**写回工作树，而 `git status` 依旧显示干净
（因为 clean(全号) == clean(脱敏)——这正是脱敏设计的副作用），事故完全无声。

2026-09-17 实测代价：一次 `git checkout -- data/gb/` → 全树 42578 个手机号变掩码；
随后 sync_assets 依此重建号码索引 → data/phone-index.jsonl 与 APK 内置资产一起被脱敏，
若照常发布，手机端会拿到 138****0000 这种不可拨号的假号。

用法：
    python scripts/restore_full_phones.py              # 体检 + 分析（不写盘）
    python scripts/restore_full_phones.py --check      # 只体检，报掩码水位
    python scripts/restore_full_phones.py --apply      # 还原并重建号码索引 + App 资产

还原源（按可用性自动收集，全部参与逐字段回验）：
    1. dist/site/data/gb|en/**   —— 上一次 Pages 部署的快照（全号）
    2. data/gb|en、dist 号码索引 —— 未受污染的兄弟目录 / 首号兜底

安全闸门（两道，都不通过就不写盘）：
    a. 逐字段：候选全号脱敏后必须**正好等于**当前掩码值（逆运算可验证）
    b. 逐文件：整文件还原后过一遍 clean filter，输出必须与原掩码内容逐字节相同
       → 保证「入库内容零变化」，git status 依然干净
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import mask_phones  # noqa: E402

FIELDS = ("contact_phone", "address", "address_en")
MASK_RE = re.compile(r"\d{3}\*{4}\d{4}")
# 正常水位：源里查不到全号的残差（个位数）。超过就说明工作树被脱敏覆盖过。
TOLERANCE = 300

TARGETS = [ROOT / "data" / "gb", ROOT / "data" / "en"]
POOLS = [ROOT / "dist" / "site" / "data" / "gb", ROOT / "dist" / "site" / "data" / "en",
         ROOT / "data" / "gb", ROOT / "data" / "en"]
SNAP_IDX = ROOT / "dist" / "site" / "data" / "phone-index.jsonl"
IDX = ROOT / "data" / "phone-index.jsonl"


def masked_count() -> int:
    """工作树里掩码手机号的总数（data/gb + data/en + 号码索引）。"""
    total = 0
    for base in TARGETS:
        for p in base.rglob("*.json"):
            try:
                total += len(MASK_RE.findall(p.read_text(encoding="utf-8")))
            except OSError:
                continue
    if IDX.exists():
        total += len(MASK_RE.findall(IDX.read_text(encoding="utf-8", errors="replace")))
    return total


def _scan(dirs) -> tuple[dict[str, list], set[str]]:
    """收集 {id: [(field, value), ...]}（保序、允许同 id 多版本）与 id 全集。"""
    pool: dict[str, list] = {}
    seen: set[str] = set()
    for base in dirs:
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.json")):
            try:
                arr = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(arr, list):
                continue
            for r in arr:
                if not isinstance(r, dict):
                    continue
                rid = r.get("id")
                if not rid:
                    continue
                seen.add(rid)
                slot = pool.setdefault(rid, [])
                for f in FIELDS:
                    v = r.get(f)
                    if isinstance(v, str) and v and "*" not in v:
                        slot.append((f, v))
    return pool, seen


def load_pool() -> tuple[dict[str, list], set[str]]:
    pool, seen = _scan(POOLS)
    if SNAP_IDX.exists():
        for ln in SNAP_IDX.read_text(encoding="utf-8").splitlines():
            if "," not in ln:
                continue
            k, v = ln.split(",", 1)
            if "*" not in v:
                pool.setdefault(k.strip(), []).append(("contact_phone", v.strip()))
    return pool, seen


def unmasks(current: str, cands) -> str | None:
    """在候选里找「脱敏后正好等于 current」的全号 —— 逐字段可验证的逆运算。"""
    for _f, v in cands:
        if mask_phones.mask_mobile_in_text(v) == current:
            return v
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只体检，不还原")
    ap.add_argument("--apply", action="store_true",
                    help="还原并随后重建号码索引 + App 内置资产")
    args = ap.parse_args()

    before = masked_count()
    print(f"工作树掩码水位：{before} 处（正常水位 <{TOLERANCE}）")
    if before <= TOLERANCE:
        print("✓ 工作树全号状态正常，无需还原")
        return 0
    print("⚠ 工作树被脱敏覆盖过（多半有人跑过 git checkout / git restore）")
    if args.check:
        return 1

    pool, snap_ids = load_pool()
    print(f"还原源：{len(snap_ids)} 个 id、{len(pool)} 个带可用全号")

    changed: list[tuple[Path, str]] = []
    patched_rec = patched_field = 0
    no_src: list[str] = []
    cur_ids: set[str] = set()
    for base in TARGETS:
        for p in sorted(base.rglob("*.json")):
            raw = p.read_text(encoding="utf-8")
            if "*" not in raw:
                continue
            try:
                arr = json.loads(raw)
            except Exception:
                print(f"  [跳过] 解析失败 {p}")
                continue
            if not isinstance(arr, list):
                continue
            touched = False
            for r in arr:
                if not isinstance(r, dict):
                    continue
                if r.get("id"):
                    cur_ids.add(r["id"])
                if mask_phones.is_claimed(r):
                    continue
                n = 0
                for f in FIELDS:
                    v = r.get(f)
                    if not isinstance(v, str) or "*" not in v:
                        continue
                    src = unmasks(v, pool.get(r.get("id"), []))
                    if src is None:
                        no_src.append(f"{r.get('id')}.{f} {v}")
                        continue
                    r[f] = src
                    n += 1
                if n:
                    patched_rec += 1
                    patched_field += n
                    touched = True
            if not touched:
                continue
            new = json.dumps(arr, ensure_ascii=False, indent=2)
            if mask_phones.process(new) != mask_phones.process(raw):
                print(f"  [拒绝] {p.name}：clean filter 回验不一致")
                continue
            changed.append((p, new))

    print(f"待还原文件 {len(changed)} 个 | 记录 {patched_rec} | 字段 {patched_field}"
          f" | 无源可还原 {len(no_src)}")
    if snap_ids - cur_ids:
        print(f"⚠ 全号源里有、当前工作树缺失的 id：{len(snap_ids - cur_ids)}"
              f"  例 {sorted(snap_ids - cur_ids)[:3]}")
    if no_src:
        print(f"  无源样例：{no_src[:3]}")
    if not args.apply:
        print("（分析模式，未写盘；加 --apply 落盘）")
        return 0

    for p, new in changed:
        p.write_text(new, encoding="utf-8")
    after = masked_count()
    print(f"✓ 已还原 {len(changed)} 个文件：掩码水位 {before} → {after}")

    # 号码索引是 data/gb 的派生层，还原完必须重建（顺带刷新 App 内置资产）
    tool = ROOT / "APK" / "tools" / "sync_assets.py"
    if tool.exists():
        done = subprocess.run([sys.executable, str(tool), "--apply"], cwd=str(ROOT))
        if done.returncode:
            print("✗ sync_assets 失败，请手动重跑", file=sys.stderr)
            return 1
    print("✓ 完成。建议接着跑：python scripts/postfetch.py --only assets,manifest,validate"
          "（重对齐 manifest 的 sha1，否则 App 会静默丢弃索引）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
