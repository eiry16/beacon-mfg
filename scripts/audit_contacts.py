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
    """返回电话类型: good(座机/热线) / mixed(座机+手机) / mobile_warn(疑似手机) / none(无) / unknown"""
    if not phone or str(phone).strip() in ("", "待核实"):
        return "none"
    parts = [p.strip() for p in re.split(r"[;；,，]", str(phone)) if p.strip()]
    types = set()
    for p in parts:
        digits = re.sub(r"\D", "", p)
        if HOTLINE_RE.match(digits):
            types.add("hotline")
        elif LANDLINE_RE.match(digits):
            types.add("landline")
        elif MOBILE_RE.match(digits):
            types.add("mobile")
        else:
            types.add("unknown")
    has_biz = bool(types & {"landline", "hotline"})
    has_mobile = "mobile" in types
    if has_mobile and has_biz:
        return "mixed"
    if has_mobile:
        return "mobile_warn"
    if has_biz:
        return "good"
    if "unknown" in types:
        return "unknown"
    return "none"


LABELS = {
    "good": ("可优先核实（座机/400/800）", "核实后改 is_template=false 即可发布"),
    "mixed": ("座机+手机混合", "保留座机/热线，删除手机号部分"),
    "mobile_warn": ("疑似个人手机号（禁止直接发布）", "需从官网找座机/业务电话替换"),
    "none": ("无电话 / 待核实", "需从官网补充联系方式"),
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
    groups = {"good": [], "mixed": [], "mobile_warn": [], "none": [], "unknown": []}
    for s in items:
        groups[classify(s.get("contact_phone"))].append(s)

    total = len(items)
    print(f"共 {total} 条待核实记录：")
    for k in ("good", "mixed", "mobile_warn", "none", "unknown"):
        print(f"  {LABELS[k][0]:<28} {len(groups[k]):>4} 条")

    if args.only_mobile:
        for s in groups["mobile_warn"]:
            print(row(s, LABELS["mobile_warn"][1]))
        return

    # 生成报告
    lines = [
        "# BeaconMFG 联系方式审核报告",
        "",
        f"生成时间：2026-08-13 | 待核实记录：**{total}** 条 | 发布前必须逐条完成核实",
        "",
        "> 合规红线：只发布企业公开经营联系方式（座机/400/官网电话）；**个人手机号禁止直接发布**。",
        "> 操作流程：核实 → 更新 contact_phone → 改 is_template=false → 提交。",
        "",
    ]
    for k in ("good", "mixed", "mobile_warn", "none", "unknown"):
        title, tag = LABELS[k]
        lines.append(f"## {title}（{len(groups[k])} 条）\n")
        for s in groups[k]:
            lines.append(row(s, tag))
        lines.append("")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已生成：{REPORT}")


if __name__ == "__main__":
    main()
