#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 端到端演示：模拟一个采购 Agent 的完整检索流程

用法: python examples/demo_agent.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import query  # noqa: E402


def agent_flow():
    print("=" * 60)
    print("场景：下游自动化设备厂商的采购 Agent 找供应商")
    print("用户需求：深圳能接小批量 CNC 件、有 ISO 认证的厂")
    print("=" * 60)

    class Args:
        keyword = "CNC加工 小批量"
        category = None
        city = "深圳"
        province = None
        cert = "ISO9001"
        limit = 3
        include_template = True  # MVP 阶段仅模板数据，演示用

    results = query.search(query.load_all_suppliers(), Args())
    if not results:
        print("（模板数据中没有同时满足 深圳+CNC+小批量+ISO9001 的示例，演示放宽条件：）")
        Args.cert = None
        results = query.search(query.load_all_suppliers(), Args())

    print(f"匹配 {len(results)} 家：\n")
    for r in results:
        print(query.fmt(r))
        print("-" * 46)

    print("\n→ Agent 将以上述格式向采购人员呈现，并引导直接联系核实。")


if __name__ == "__main__":
    agent_flow()
