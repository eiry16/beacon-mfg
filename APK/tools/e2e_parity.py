#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""APK 检索内核 E2E 对拍用例生成器。

用途：把「Python 版 query.py」在同样参数下的结果固化为期望值，
供安卓端 instrumented test（SearchEngineParityTest）比对，
保证 Kotlin 移植没有偏离 Python 语义。

设计要点：
  * 期望值**由真实数据算出**，不是手写常量 —— 数据更新后重跑即可，不会过期。
  * 只比对「总数 + 三档构成 + 前 N 个 id 的顺序」三个维度。
    不比对排序细节：Python 与 Kotlin 的 stable sort 在并列时的次序可能不同，
    而并列次序对用户体验没有影响，硬要比对只会制造假失败。
  * 用例刻意覆盖了三条红线的边界：
      - 收敛砍成 0 的分支（放宽计数）
      - 行业推断档（企业未确认）
      - 制造业过滤 / 城市归一化（省市区后缀）

用法：
    python APK/tools/e2e_parity.py                 # 打印表格
    python APK/tools/e2e_parity.py --write         # 同时写出 e2e_expect.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import query  # noqa: E402  （必须在 sys.path 调整后导入）

OUT_JSON = Path(__file__).resolve().parent / "e2e_expect.json"

# ---------------------------------------------------------------- 用例定义
# 每个用例对应安卓端 SearchParams 的一组入参。
# 字段名与 cn.beaconmfg.app.data.SearchParams 保持一致，便于两侧对照。
CASES: list[dict] = [
    {
        "name": "齿轮·上海（经典用例，混合三档）",
        "params": {"keyword": "齿轮", "city": "上海", "limit": 10},
    },
    {
        "name": "焊接·上海（验证 max_supply 收敛后的量级）",
        "params": {"keyword": "焊接", "city": "上海", "limit": 10},
    },
    {
        "name": "输送线·上海（策展别名；预期收敛到耐特斯 1 家）",
        "params": {"keyword": "输送线", "city": "上海", "limit": 10},
    },
    {
        "name": "输送线（全国，策展别名覆盖率验证）",
        "params": {"keyword": "输送线", "limit": 10},
    },
    {
        "name": "数控加工·深圳",
        "params": {"keyword": "数控加工", "city": "深圳", "limit": 10},
    },
    {
        "name": "钣金·苏州·只看制造商",
        "params": {"keyword": "钣金", "city": "苏州", "manufacturerOnly": True, "limit": 10},
    },
    {
        "name": "注塑·东莞",
        "params": {"keyword": "注塑", "city": "东莞", "limit": 10},
    },
    {
        "name": "齿轮 焊接（多词 AND，验证取最弱档）",
        "params": {"keyword": "齿轮 焊接", "limit": 10},
    },
    {
        "name": "行业码 3453（国标小类，不带关键词）",
        "params": {"industryCode": "3453", "limit": 10},
    },
    {
        "name": "不存在的词（验证不静默放宽，必须有 relaxed 计数）",
        "params": {"keyword": "zzz不存在的采购词zzz", "city": "上海", "limit": 10},
    },
]

TOP_N = 5


def to_args(p: dict) -> SimpleNamespace:
    """SearchParams(dict) → query.py 的 argparse 命名空间。"""
    return SimpleNamespace(
        keyword=p.get("keyword"),
        category=None,
        category_en=None,
        city=p.get("city"),
        province=None,
        cert=p.get("cert"),
        industry=p.get("industryCode"),
        manufacturer_only=bool(p.get("manufacturerOnly")),
        no_alias=False,
        alias_broad=False,
        include_template=False,
        en=False,
    )


def run_case(suppliers, case: dict, gb) -> dict:
    p = case["params"]
    args = to_args(p)
    industry_codes, _labels = query.resolve_industry(args.industry) if args.industry else (None, [])

    alias_cache: dict = {}
    if args.keyword:
        for k in [x for x in args.keyword.split() if x]:
            alias_cache[k] = query.alias_rank_for(k)

    results = query.search(suppliers, args, industry_codes, alias_cache)

    literal = sum(1 for r in results if r.get("_alias_rank", 0) == 0)
    primary = sum(1 for r in results if r.get("_alias_rank", 0) == 1)
    secondary = sum(1 for r in results if r.get("_alias_rank", 0) == 2)

    # 收敛砍成 0 的分支：Python 端不静默放宽，只是告知放宽能拿多少。
    relaxed = None
    if not results and args.keyword and gb is not None:
        saved = gb.ALIAS_MAX_SUPPLY
        gb.ALIAS_MAX_SUPPLY = None
        gb._SUPPLY_CACHE = None
        broad = {k: query.alias_rank_for(k) for k in args.keyword.split() if k}
        relaxed = len(query.search(suppliers, args, industry_codes, broad))
        gb.ALIAS_MAX_SUPPLY = saved
        gb._SUPPLY_CACHE = None

    return {
        "name": case["name"],
        "params": p,
        "total": len(results),
        "literal": literal,
        "primary": primary,
        "secondary": secondary,
        "relaxed": relaxed,
        "topIds": [r.get("id") for r in results[:TOP_N]],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 APK 检索内核对拍期望值")
    ap.add_argument("--write", action="store_true", help="写出 e2e_expect.json")
    a = ap.parse_args()

    print("加载全量供应商归档（首次较慢）…")
    suppliers = query.load_all_suppliers()
    print(f"已加载 {len(suppliers)} 条\n")

    gb = query.GB
    out = [run_case(suppliers, c, gb) for c in CASES]

    hdr = f"{'用例':<40}{'总数':>8}{'字面':>7}{'首位码':>8}{'推断':>7}{'放宽':>8}"
    print(hdr)
    print("-" * len(hdr.expandtabs()) + "-" * 20)
    for r in out:
        print(
            f"{r['name']:<40}{r['total']:>8}{r['literal']:>7}"
            f"{r['primary']:>8}{r['secondary']:>7}"
            f"{('-' if r['relaxed'] is None else r['relaxed']):>8}"
        )

    print("\n前 %d 个 id（顺序需与安卓端一致）：" % TOP_N)
    for r in out:
        print(f"  {r['name']}：{r['topIds']}")

    payload = {
        "generated_by": "APK/tools/e2e_parity.py",
        "source": "scripts/query.py（Python 参考实现）",
        "supplier_count": len(suppliers),
        "top_n": TOP_N,
        "cases": out,
    }
    if a.write:
        OUT_JSON.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写出 {OUT_JSON}")


if __name__ == "__main__":
    main()
