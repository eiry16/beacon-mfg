#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 供应商检索脚本（纯标准库，无第三方依赖）

用法示例:
    python scripts/query.py --keyword "CNC加工"
    python scripts/query.py --keyword "小批量" --city 深圳 --limit 5
    python scripts/query.py --province 广东 --cert 高新技术企业
    python scripts/query.py --category 精密机械加工 --limit 10
    python scripts/query.py --keyword 注塑 --include-template
    # 英文数据集（海外 Agent）:
    python scripts/query.py --keyword "CNC Machining" --city Shenzhen --en --limit 5
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "data" / "index.json"
SUPPLIERS_DIR = ROOT / "data" / "suppliers"
EN_DIR = ROOT / "data" / "en"


def load_index():
    with open(INDEX, encoding="utf-8") as f:
        return json.load(f)


def load_all_suppliers(english=False):
    """遍历数据文件，合并为 {supplier: {_category: ...}}。english=True 时读 data/en/"""
    if english:
        suppliers = []
        for path in sorted(EN_DIR.glob("*.json")):
            with open(path, encoding="utf-8") as f:
                for item in json.load(f):
                    item["_category"] = item.get("category_en") or item.get("category")
                    item["_english"] = True
                    suppliers.append(item)
        return suppliers

    index = load_index()
    suppliers = []
    for cat in index["categories"]:
        path = SUPPLIERS_DIR / cat["file"]
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for item in json.load(f):
                item["_category"] = cat["name"]
                suppliers.append(item)
    return suppliers


def match(record, keyword):
    """关键词匹配：对 keywords 列表做子串匹配（忽略大小写）。英文数据同时匹配 keywords_en"""
    kw = keyword.strip().lower()
    if not kw:
        return False
    pools = list(record.get("keywords", [])) + list(record.get("keywords_en", []))
    return any(kw in k.lower() for k in pools)


def _norm(s):
    """去掉省市后缀做兼容匹配"""
    return (s or "").replace("省", "").replace("市", "").replace("自治区", "")


def search(suppliers, args):
    results = []
    for s in suppliers:
        # 模板数据默认排除
        if s.get("is_template") and not args.include_template:
            continue
        if args.category and s.get("_category") != args.category:
            continue
        if args.category_en and s.get("_category") != args.category_en:
            continue
        if args.city and _norm(s.get("region", {}).get("city")) != _norm(args.city):
            continue
        if args.province and _norm(s.get("region", {}).get("province")) != _norm(args.province):
            continue
        if args.cert and not any(c == args.cert for c in s.get("certifications", [])):
            continue
        if args.keyword:
            # 支持空格分隔的多关键词 AND
            kws = [k for k in args.keyword.split() if k]
            if not all(match(s, k) for k in kws):
                continue
        results.append(s)
    return results


def fmt(record):
    region = record.get("region", {})
    if record.get("_english"):
        lines = [
            f"Company: {record.get('company_en', record.get('company', ''))} "
            f"({region.get('province', '')} · {region.get('city', '')})",
            f"Products: {' / '.join(record.get('keywords_en') or record.get('keywords', []))}",
            f"Category: {record.get('category_en', record.get('category', ''))}",
        ]
        if record.get("contact_phone"):
            lines.append(f"Phone: {record['contact_phone']}")
        if record.get("address_en"):
            lines.append(f"Address: {record['address_en']}")
        if record.get("note_en"):
            lines.append(f"Note: {record['note_en']}")
        lines.append(f"Source: public directory, verified {record.get('verified_at', '?')}")
        return "\n".join(lines)

    certs = "、".join(record.get("certifications", [])) or "无"
    lines = [
        f"公司：{record['company']}（{region.get('province', '')}·{region.get('city', '')}）",
        f"主营：{' / '.join(record.get('keywords', []))}",
        f"认证：{certs}",
    ]
    if record.get("website"):
        lines.append(f"官网：{record['website']}")
    if record.get("contact_phone"):
        lines.append(f"电话：{record['contact_phone']}")
    if record.get("contact_email"):
        lines.append(f"邮箱：{record['contact_email']}")
    if record.get("note"):
        lines.append(f"备注：{record['note']}")
    if record.get("is_template"):
        lines.append("⚠ 模板数据（占位），真实数据填充中")
    else:
        lines.append(f"数据来源：公开渠道，核实于 {record.get('verified_at', '?')}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="BeaconMFG 供应商检索")
    parser.add_argument("--keyword", help="产品关键词（空格分隔多词 AND）")
    parser.add_argument("--category", help="品类名（见 data/index.json）")
    parser.add_argument("--category-en", dest="category_en", help="英文品类名（--en 模式）")
    parser.add_argument("--city", help="城市过滤，如 深圳 / Shenzhen")
    parser.add_argument("--province", help="省份过滤，如 广东 / Guangdong")
    parser.add_argument("--cert", help="认证标签过滤，如 高新技术企业")
    parser.add_argument("--limit", type=int, default=5, help="返回条数上限")
    parser.add_argument("--include-template", action="store_true", help="包含模板示例数据")
    parser.add_argument("--en", action="store_true", help="检索英文数据集（data/en）")
    args = parser.parse_args()

    if not any([args.keyword, args.category, args.category_en, args.city, args.province, args.cert]):
        parser.print_help()
        sys.exit(1)

    suppliers = load_all_suppliers(english=args.en)
    if not suppliers and args.en:
        print("英文数据集为空：请先运行 scripts/translate_en.py 生成 data/en/")
        sys.exit(0)
    results = search(suppliers, args)

    if not results:
        print("未找到匹配供应商。提示：换关键词（如 CNC加工→数控加工），或放宽地区/认证条件。")
        sys.exit(0)

    print(f"共匹配 {len(results)} 家，展示前 {min(args.limit, len(results))} 家：\n")
    for r in results[: args.limit]:
        print(fmt(r))
        print("-" * 46)


if __name__ == "__main__":
    main()
