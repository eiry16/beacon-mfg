#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填 source_url（审计议题 #1 建议 #3：每条记录需有专属可溯源链接）。

探查结论（probe_fields2.py，采样 93 条）：
- source_url 字段存在于全部记录，但 100% 为空。
- amap.poi_id 存在于 ~91% 记录（形如 B0H1X753S0），是高德 POI ID，可反推来源 URL。

策略：
1. 扫描 data/gb，从每条有 amap.poi_id 的记录构建 id -> amap place URL 映射。
2. 把该 URL 写回同记录的 source_url（仅当 source_url 为空，幂等，不覆盖人工已填）。
3. data/en 镜像按相同 id 复用同一 URL（EN 镜像本身无 amap 字段，靠 id 关联）。
4. 无 amap.poi_id 的记录（多为 unverified_poi / 待核实）source_url 留空，诚实不编。

用法：
    python scripts/_backfill_source_url.py            # dry-run：只统计
    python scripts/_backfill_source_url.py --apply   # 写入
"""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GB_DIR = ROOT / "data" / "gb"
EN_DIR = ROOT / "data" / "en"
AMAP_URL = "https://www.amap.com/place/{}"


def load_records(path: Path):
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        return json.loads(text), "array"
    recs = []
    for line in text.splitlines():
        if line.strip():
            recs.append(json.loads(line))
    return recs, "jsonl"


def save_records(path: Path, recs, fmt: str) -> None:
    if fmt == "array":
        path.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
            encoding="utf-8",
        )


def main() -> int:
    apply = "--apply" in sys.argv
    gb_files = glob.glob(str(GB_DIR / "**" / "*.json"), recursive=True)
    en_files = glob.glob(str(EN_DIR / "**" / "*.json"), recursive=True)

    # 1) 建 id -> source_url 映射（仅来自有 amap.poi_id 的 ZH 记录）
    id2url: dict[str, str] = {}
    total_gb = 0
    for f in gb_files:
        recs, _ = load_records(Path(f))
        for r in recs:
            total_gb += 1
            amap = r.get("amap")
            if isinstance(amap, dict):
                pid = amap.get("poi_id")
                if pid:
                    id2url[r["id"]] = AMAP_URL.format(pid)

    # 2) 写回 ZH
    set_zh = 0
    for f in gb_files:
        recs, fmt = load_records(Path(f))
        changed = False
        for r in recs:
            if r.get("id") in id2url and not r.get("source_url"):
                r["source_url"] = id2url[r["id"]]
                set_zh += 1
                changed = True
        if changed and apply:
            save_records(Path(f), recs, fmt)

    # 3) 写回 EN（按相同 id）
    set_en = 0
    for f in en_files:
        recs, fmt = load_records(Path(f))
        changed = False
        for r in recs:
            if r.get("id") in id2url and not r.get("source_url"):
                r["source_url"] = id2url[r["id"]]
                set_en += 1
                changed = True
        if changed and apply:
            save_records(Path(f), recs, fmt)

    print(f"ZH 记录总数（扫描）: {total_gb}")
    print(f"有 amap.poi_id 可溯源的记录: {len(id2url)} "
          f"({len(id2url) * 100.0 / total_gb:.1f}% of ZH)")
    print(f"ZH 写入 source_url: {set_zh}" + ("" if apply else "  (dry-run, 未写入)"))
    print(f"EN 写入 source_url: {set_en}" + ("" if apply else "  (dry-run, 未写入)"))
    print("（无 amap.poi_id 的记录 source_url 留空，诚实不编）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
