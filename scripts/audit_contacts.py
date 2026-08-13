#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 联系方式审核辅助 —— 把抓取数据按电话类型分类，生成待核实清单

合规原则：企业名录只发布公开经营联系方式。
- 座机（区号开头 0xx）/ 400/800 热线 → 可优先核实发布
- 个人手机号（1[3-9]xxxxxxxxx）→ 疑似个人号码，禁止直接发布，需官网找座机
- 无电话 / "待核实" → 需从官网补充

用法:
    python scripts/audit_contacts.py          # 输出统计 + 生成 docs/audit_report.md
    python scripts/audit_contacts.py --only-mobile   # 只看手机号清单（重点攻破）
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUPPLIERS_DIR = ROOT / "data" / "suppliers"
REPORT = ROOT / "docs" / "audit_report.md"

MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
LANDLINE_RE = re.compile(r"^0\d{2,3}-?\d{7,8}$")
HOTLINE_RE = re.compile(r"^(400|800)\d{5,}")


def classify(phone):
    """返回电话类型: good(座机/热线) / masked(已脱敏手机) / mixed(座机+脱敏手机) / none(无) / unknown"""
    if not phone or str(phone).strip() in ("", "待核实"):
        return "none"
    parts = [p.strip() for p in re.split(r"[;；,，]", str(phone)) if p.strip()]
    types = set()
    for p in parts:
        digits = re.sub(r"\D", "", p)
        if "*" in p:
            types.add("masked")  # 脱敏手机号（合规，可发布）
        elif HOTLINE_RE.match(digits):
            types.add("hotline")
        elif LANDLINE_RE.match(digits):
            types.add("landline")
        elif MOBILE_RE.match(digits):
            types.add("mobile_raw")  # 完整手机号（不应存在，validate 会拦截）
        else:
            types.add("unknown")
    has_biz = bool(types & {"landline", "hotline"})
    has_masked = "masked" in types
    if has_masked and has_biz:
        return "mixed"
    if has_masked:
        return "masked"
    if has_biz:
        return "good"
    if "mobile_raw" in types:
        return "mobile_raw"
    if "unknown" in types:
        return "unknown"
    return "none"


LABELS = {
    "good": ("座机/400/800（完整可发布）", "核实后改 is_template=false 即可发布"),
    "masked": ("手机号已脱敏（公开层展示）", "作认领钩子：企业看到脱敏号会来认领；认领后由商家提交完整业务号（合法授权）"),
    "mixed": ("座机 + 脱敏手机", "座机完整展示 + 手机号脱敏展示，均可发布"),
    "none": ("无电话 / 待核实", "需从官网补充联系方式"),
    "mobile_raw": ("完整手机号（违规，应立即脱敏）", "validate.py 会拦截，运行脱敏处理"),
    "unknown": ("格式无法识别", "人工查看原始电话"),
}


def load_all():
    items = []
    for path in sorted(SUPPLIERS_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            for s in json.load(f):
                if s.get("source") == "template":
                    continue  # 跳过示例模板
                items.append(s)
    return items


def row(s, tag):
    region = s.get("region", {})
    return (
        f"- `{s['id']}` **{s['company']}**（{region.get('province','')}·{region.get('city','')}）\n"
        f"  电话：`{s.get('contact_phone','')}` | 品类：{s.get('category','')}\n"
        f"  操作：{tag}"
    )


def main():
    parser = argparse.ArgumentParser(description="联系方式审核辅助")
    parser.add_argument("--only-mobile", action="store_true", help="只输出疑似手机号清单")
    args = parser.parse_args()

    items = load_all()
    groups = {"good": [], "masked": [], "mixed": [], "none": [], "mobile_raw": [], "unknown": []}
    for s in items:
        groups[classify(s.get("contact_phone"))].append(s)

    total = len(items)
    print(f"共 {total} 条待核实记录：")
    for k in ("good", "masked", "mixed", "none", "mobile_raw", "unknown"):
        print(f"  {LABELS[k][0]:<32} {len(groups[k]):>4} 条")

    if args.only_mobile:
        for s in groups["masked"]:
            print(row(s, LABELS["masked"][1]))
        return

    # 生成报告
    lines = [
        "# BeaconMFG 联系方式审核报告",
        "",
        f"生成时间：2026-08-13 | 待核实记录：**{total}** 条 | 发布前必须逐条完成核实",
        "",
        "> 合规策略：座机/400/官网电话完整发布；**个人手机号一律脱敏展示（1XX****XXXX）**，",
        "> 脱敏号兼作认领钩子——企业看到自己的号后主动认领，认领时由商家自行提交完整业务联系方式（合法授权）。",
        "> 操作流程：核实 → 更新联系方式 → 改 is_template=false → 提交。",
        "",
    ]
    for k in ("good", "masked", "mixed", "none", "mobile_raw", "unknown"):
        title, tag = LABELS[k]
        lines.append(f"## {title}（{len(groups[k])} 条）\n")
        for s in groups[k]:
            lines.append(row(s, tag))
        lines.append("")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已生成：{REPORT}")


if __name__ == "__main__":
    main()
