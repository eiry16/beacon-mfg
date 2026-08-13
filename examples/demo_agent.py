#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 端到端演示：模拟一个采购 Agent 的完整检索流程

演示 SKILL.md 描述的三步流程：
1. 解析用户需求 → 映射品类关键词（读 data/index.json 词典）
2. 调用 scripts/query.py 检索（地区/关键词/认证过滤）
3. 按标准格式呈现结果 + 合规提示

用法: python examples/demo_agent.py
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import query  # noqa: E402


def map_keywords(user_need, index):
    """Step 1: 用户需求 → 品类 + 关键词 映射"""
    print(f"用户需求：{user_need}")
    hits = {}
    for cat in index["categories"]:
        matched = [k for k in cat["keywords"] if k in user_need]
        if matched:
            hits[cat["name"]] = matched
    if hits:
        print(f"→ 映射到品类：{', '.join(hits.keys())}，关键词：{hits}")
        return hits
    print("→ 未命中品类词典，提示用户补充产品描述")
    return {}


def search(hits, user_city=None):
    """Step 2: 检索"""
    if not hits:
        return []
    # 取第一个命中品类的第一个关键词做检索示例
    category = next(iter(hits))
    keyword = hits[category][0]

    args = SimpleNamespace(
        keyword=keyword, category=None, city=user_city, province=None,
        cert=None, limit=5, include_template=True,  # 数据核实前为模板，演示完整格式
    )
    results = query.search(query.load_all_suppliers(), args)
    return results, category, keyword


def main():
    print("=" * 62)
    print("场景：下游自动化设备厂商的采购 Agent 帮用户找供应商")
    print("=" * 62)

    index = json.load(open(ROOT / "data" / "index.json", encoding="utf-8"))

    # 场景 1：完整命中
    user_need = "找深圳能接小批量 CNC 加工的厂"
    hits = map_keywords(user_need, index)
    results, category, keyword = search(hits, user_city="深圳")
    print(f"\n检索条件：品类={category} 关键词={keyword} 城市=深圳\n")
    print(f"匹配 {len(results)} 家，展示前 {min(5, len(results))} 家：\n")
    for r in results[:5]:
        print(query.fmt(r))
        print("-" * 46)
    print("→ 呈现格式如上。合规提醒：真实数据发布前需核实联系方式（is_template=false）。\n")

    # 场景 2：无命中提示
    print("=" * 62)
    user_need2 = "找做模具的厂"
    hits2 = map_keywords(user_need2, index)
    if not hits2:
        print("→ Agent 应回复：'暂时没有'模具'品类，换个说法试试，比如 注塑/冲压/压铸'")

    # 场景 3：数据状态说明
    real_count = sum(1 for s in query.load_all_suppliers() if not s.get("is_template"))
    total = len(query.load_all_suppliers())
    print(f"\n数据状态：共 {total} 条骨架记录，已核实可发布 {real_count} 条。")
    print("提示：跑 scripts/audit_contacts.py 生成核实清单，逐条核实后改 is_template=false。")


if __name__ == "__main__":
    main()
