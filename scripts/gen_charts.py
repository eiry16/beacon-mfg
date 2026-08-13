#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 数据可视化 —— 生成 SVG 图表嵌入 README（纯标准库，无第三方依赖）

生成:
- docs/charts/categories.svg   品类分布条形图
- docs/charts/cities.svg       城市分布条形图（TOP 8）

用法: python scripts/gen_charts.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUPPLIERS_DIR = ROOT / "data" / "suppliers"
CHARTS_DIR = ROOT / "docs" / "charts"

# BeaconMFG 主题色（teal 系）
COLOR = "#0f6e56"       # 主色
COLOR_LIGHT = "#5dcaa5"
GRID = "#d5d5d5"
TEXT = "#333333"
MUTED = "#777777"


def load_all():
    items = []
    for path in sorted(SUPPLIERS_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            for s in json.load(f):
                items.append(s)
    return items


def bar_chart(title, rows, filename, max_val=None):
    """rows: [(标签, 数值)] 生成水平条形图 SVG"""
    n = len(rows)
    bar_h = 26
    gap = 14
    chart_w = 560
    label_w = 130
    pad_left = 8
    pad_top = 54
    height = pad_top + n * (bar_h + gap) + 24

    if not max_val:
        max_val = max((v for _, v in rows), default=1) or 1

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {chart_w} {height}" width="{chart_w}" height="{height}">',
        f'  <rect width="{chart_w}" height="{height}" fill="#ffffff"/>',
        f'  <text x="{pad_left}" y="30" font-family="sans-serif" font-size="17" font-weight="600" fill="{TEXT}">{title}</text>',
        f'  <text x="{pad_left}" y="48" font-family="sans-serif" font-size="12" fill="{MUTED}">BeaconMFG · 数据统计（2026-08-13）</text>',
    ]
    for i, (label, val) in enumerate(rows):
        y = pad_top + i * (bar_h + gap)
        bar_w = max(6, int(val / max_val * (chart_w - label_w - pad_left - 30)))
        # 网格线（满格）
        lines.append(
            f'  <line x1="{label_w + pad_left}" y1="{y + bar_h + 4}" '
            f'x2="{chart_w - 22}" y2="{y + bar_h + 4}" stroke="{GRID}" stroke-width="1"/>'
        )
        # 标签
        lines.append(
            f'  <text x="{pad_left}" y="{y + bar_h / 2 + 4}" font-family="sans-serif" '
            f'font-size="13" fill="{TEXT}">{label}</text>'
        )
        # 条形
        lines.append(
            f'  <rect x="{label_w + pad_left}" y="{y}" width="{bar_w}" height="{bar_h}" '
            f'rx="6" fill="{COLOR}"/>'
        )
        # 数值
        lines.append(
            f'  <text x="{label_w + pad_left + bar_w + 8}" y="{y + bar_h / 2 + 4}" '
            f'font-family="sans-serif" font-size="13" font-weight="600" fill="{COLOR}">{val}</text>'
        )
    lines.append("</svg>")
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    out = CHARTS_DIR / filename
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成: {out.relative_to(ROOT)}")


def main():
    items = load_all()

    # 品类分布
    by_cat = {}
    for s in items:
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + 1
    cat_rows = sorted(by_cat.items(), key=lambda x: -x[1])
    bar_chart("各品类供应商骨架数量", cat_rows, "categories.svg")

    # 城市分布 TOP8
    by_city = {}
    for s in items:
        c = s.get("region", {}).get("city") or "未知"
        by_city[c] = by_city.get(c, 0) + 1
    city_rows = sorted(by_city.items(), key=lambda x: -x[1])[:8]
    bar_chart("供应商城市分布 TOP8", city_rows, "cities.svg")


if __name__ == "__main__":
    main()
