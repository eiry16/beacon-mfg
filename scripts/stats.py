#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 数据统计（生成 README 数据现状表格用）

用法:
    python scripts/stats.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "data" / "index.json"
SUPPLIERS_DIR = ROOT / "data" / "suppliers"


def main():
    with open(INDEX, encoding="utf-8") as f:
        index = json.load(f)

    rows = []
    total_real = 0
    total_template = 0
    for cat in index["categories"]:
        path = SUPPLIERS_DIR / cat["file"]
        items = []
        if path.exists():
            with open(path, encoding="utf-8") as f:
                items = json.load(f)
        real = sum(1 for i in items if not i.get("is_template"))
        tpl = sum(1 for i in items if i.get("is_template"))
        total_real += real
        total_template += tpl
        status = "真实数据" if real else ("模板数据" if tpl else "空")
        rows.append((cat["name"], real + tpl, real, status))

    print(f"BeaconMFG 数据统计：{len(rows)} 个品类，真实 {total_real} 条，模板 {total_template} 条\n")
    print(f"{'品类':<12}{'总数':>6}{'真实':>6}  状态")
    for name, total, real, status in rows:
        print(f"{name:<12}{total:>6}{real:>6}  {status}")


if __name__ == "__main__":
    main()
