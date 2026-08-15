#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 批量抓取 —— 一键更新全品类骨架数据（合规绿区：高德开放 API）

按「品类 × 关键词 × 城市」矩阵循环抓取，增量去重后写入对应品类文件。
高频维护用：每次跑一遍，把新出现的供应商补进库。

用法:
    export AMAP_KEY=你的key
    python scripts/fetch_batch.py                # 全矩阵更新
    python scripts/fetch_batch.py --limit 20     # 每任务限量 20 条（默认 60）
    python scripts/fetch_batch.py --dry-run      # 只打印任务计划，不请求

注意：
- 尊重配额：任务间默认 1s 延时；高德个人开发者每日有请求上限
- 数据入库即脱敏（完整手机号不进仓库），is_template 由人工核实后翻转
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_gaode_poi as fetcher  # noqa: E402

# 抓取矩阵：品类 → (关键词列表, 目标城市列表)
# 城市按产业集聚分配（长三角 + 珠三角）：
#   珠三角：广州/深圳/东莞/佛山/中山/珠海/惠州
#   长三角：上海/苏州/无锡/常州/南京/南通（江苏）、杭州/宁波/嘉兴/绍兴/温州/台州（浙江）、合肥（安徽）
JOBS = {
    "精密机械加工": (["CNC加工", "数控加工", "精密机械"], ["深圳", "东莞", "广州", "苏州", "无锡", "常州", "上海", "宁波", "杭州", "嘉兴"]),
    "钣金冲压": (["钣金加工", "激光切割", "冲压"], ["东莞", "深圳", "佛山", "中山", "广州", "苏州", "无锡", "常州", "上海"]),
    "注塑成型": (["注塑加工", "注塑成型"], ["深圳", "东莞", "广州", "佛山", "苏州", "昆山", "无锡", "嘉兴", "宁波", "上海"]),
    "压铸": (["压铸"], ["东莞", "佛山", "深圳", "宁波", "苏州", "无锡", "常州", "上海"]),
    "电子元器件": (["PCB", "PCBA", "连接器"], ["深圳", "东莞", "惠州", "珠海", "广州", "苏州", "无锡", "上海", "杭州"]),
    "表面处理": (["阳极氧化", "电镀", "喷涂"], ["东莞", "深圳", "佛山", "惠州", "苏州", "无锡", "宁波", "上海"]),
    "标准件": (["紧固件", "轴承", "弹簧"], ["宁波", "嘉兴", "温州", "上海", "苏州", "无锡", "常州", "东莞", "深圳"]),
    "原材料": (["铝合金", "不锈钢", "铜材"], ["佛山", "无锡", "苏州", "上海", "宁波", "广州", "温州"]),
}


def plan():
    tasks = []
    for cat, (kws, cities) in JOBS.items():
        for kw in kws:
            for city in cities:
                tasks.append((cat, kw, city))
    return tasks


def main():
    parser = argparse.ArgumentParser(description="BeaconMFG 批量抓取（高德 POI）")
    parser.add_argument("--limit", type=int, default=60, help="每个任务抓取上限")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    parser.add_argument("--category", help="只跑指定品类（可选）")
    args = parser.parse_args()

    tasks = [t for t in plan() if not args.category or t[0] == args.category]
    if args.dry_run:
        print(f"计划任务 {len(tasks)} 个：")
        for cat, kw, city in tasks:
            print(f"  {cat:>8} × {kw:<6} × {city}")
        return

    if not fetcher.AMAP_KEY:
        import os
        fetcher.AMAP_KEY = os.environ.get("AMAP_KEY", "")
    if not fetcher.AMAP_KEY:
        print("请先设置 API Key：export AMAP_KEY=你的key")
        raise SystemExit(1)

    done = 0
    total_new = 0
    for cat, kw, city in tasks:
        print(f"\n[{done + 1}/{len(tasks)}] {cat} × {kw} × {city}")
        try:
            pois = fetcher.fetch(kw, city, args.limit)
            added = fetcher.save_suppliers(pois, cat, kw)
            total_new += added
        except Exception as e:
            print(f"  失败: {e}")
        done += 1
        time.sleep(1)

    print(f"\n完成：{done} 个任务，新增 {total_new} 条。")
    print("提示：跑 scripts/validate.py 校验；scripts/audit_contacts.py 看核实清单。")


if __name__ == "__main__":
    main()
