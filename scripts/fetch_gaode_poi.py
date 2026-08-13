#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 数据抓取框架 —— 高德开放 API（合规绿区，官方 POI 数据）

用途：按品类关键词 + 城市拉取制造业企业 POI 骨架（名称/地址/经纬度/电话），
作为供应商名录的"骨架"，业务联系方式后续从企业官网补充核实。

合规说明：
- 高德开放平台是官方开发者 API，个人开发者免费额度够用（每日限次）
- 仅拉取企业公开 POI 信息，不涉及个人隐私
- 需要你去 https://lbs.amap.com 注册开发者账号获取 Web 服务 Key

用法:
    export AMAP_KEY=你的key   (或直接改下面的常量)
    python scripts/fetch_gaode_poi.py --keyword "CNC加工" --city 东莞 --limit 50
"""
import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

AMAP_KEY = ""  # TODO: 填入高德 Web 服务 Key，或通过环境变量 AMAP_KEY 传入
BASE = "https://restapi.amap.com/v3/place/text"

ROOT = Path(__file__).resolve().parent.parent
SUPPLIERS_DIR = ROOT / "data" / "suppliers"


def fetch(keyword, city, limit, offset=20):
    """分页拉取高德 POI，返回候选记录列表"""
    url = BASE + "?" + urllib.parse.urlencode({
        "key": AMAP_KEY,
        "keywords": keyword,
        "city": city,
        "citylimit": "true",
        "types": "商务住宅|科教文化服务|公司企业",  # 制造业企业常见分类
        "offset": offset,
        "page": 1,
        "extensions": "base",
    })
    print(f"请求: {url}")
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("status") != "1":
        print(f"API 返回错误: {data.get('info')}")
        return []
    pois = data.get("pois", [])
    print(f"命中 {len(pois)} 条（按需扩展分页）")
    return pois


def normalize_region(province, city):
    """规范省市名称：去掉 省/市/自治区 等后缀，保证与查询参数一致"""
    return (
        province.replace("省", "").replace("市", "").replace("自治区", "")
        .replace("壮族", "").replace("回族", "").replace("维吾尔", ""),
        city.replace("市", ""),
    )


def to_supplier(poi, category, keyword, seq):
    """POI → 供应商骨架（待核实，is_template=true 直到人工核实后改 false）"""
    province, city = normalize_region(poi.get("pname", ""), poi.get("cityname", ""))
    return {
        "id": f"CN-MFG-{seq:04d}",
        "company": poi.get("name", ""),
        "category": category,
        "keywords": [keyword],
        "region": {"province": province, "city": city},
        "address": poi.get("address") or (poi.get("pname", "") + poi.get("adname", "")),
        "contact_phone": poi.get("tel") or "待核实",
        "lat": float(poi.get("location", "").split(",")[1]) if poi.get("location") else None,
        "lng": float(poi.get("location", "").split(",")[0]) if poi.get("location") else None,
        "source": "public_directory",  # 高德 POI 属公开名录
        "source_url": "https://lbs.amap.com",
        "verified_at": "2026-08-13",
        "is_template": True,  # 待核实骨架，联系方式需人工核实后改 false 才对外发布
        "note": f"高德 POI 抓取（关键词：{keyword}），公司名/地址/经纬度直接采纳；联系方式需人工核实后改 is_template=false 发布",
    }


def next_seq_id(existing):
    """从全部品类文件中找最大编号 +1，保证 ID 全局唯一"""
    max_n = 0
    for s in existing:
        m = re.match(r"^CN-MFG-(\d{4})$", s.get("id", ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    if SUPPLIERS_DIR.exists():
        for path in SUPPLIERS_DIR.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    for s in json.load(f):
                        m = re.match(r"^CN-MFG-(\d{4})$", s.get("id", ""))
                        if m:
                            max_n = max(max_n, int(m.group(1)))
            except (json.JSONDecodeError, OSError):
                continue
    return max_n + 1


def main():
    parser = argparse.ArgumentParser(description="高德 POI 抓取框架（制造业供应商骨架）")
    parser.add_argument("--keyword", required=True, help="搜索关键词，如 CNC加工 / 注塑 / 钣金")
    parser.add_argument("--city", required=True, help="城市，如 东莞 / 深圳")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--category", default="精密机械加工", help="映射到的品类名")
    args = parser.parse_args()

    global AMAP_KEY
    import os
    AMAP_KEY = os.environ.get("AMAP_KEY", AMAP_KEY)
    if not AMAP_KEY:
        print("请先设置 API Key：export AMAP_KEY=你的key，或在 lbs.amap.com 注册获取")
        raise SystemExit(1)

    pois = fetch(args.keyword, args.city, args.limit)
    out = SUPPLIERS_DIR / f"{args.category}.json"
    existing = []
    if out.exists():
        with open(out, encoding="utf-8") as f:
            existing = json.load(f)

    # 简单去重：公司名相同则跳过
    names = {s["company"] for s in existing}
    new_pois = [p for p in pois[: args.limit] if p.get("name") not in names]
    seq_start = next_seq_id(existing)
    suppliers = [to_supplier(p, args.category, args.keyword, seq_start + i) for i, p in enumerate(new_pois)]
    if not suppliers:
        print("无新增。")
        return

    existing.extend(suppliers)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    print(f"新增 {len(suppliers)} 条（ID {seq_start:04d}~{seq_start + len(suppliers) - 1:04d}）")
    print(f"当前 {args.category} 共 {len(existing)} 条 → {out}")
    print("提醒：所有抓取数据 is_template=true，需逐条从官网核实联系方式后改 false 才对外发布")


if __name__ == "__main__":
    main()
