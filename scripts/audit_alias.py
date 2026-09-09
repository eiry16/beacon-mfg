# -*- coding: utf-8 -*-
"""别名覆盖审计：量出「客户能搜到多少」，而不是「别名表有多少条」。

用法：
    python scripts/audit_alias.py              # 全量审计
    python scripts/audit_alias.py --top 40     # 小类明细行数
    python scripts/audit_alias.py --gap 30     # 供给 >=30 家却无别名入口的小类

三个口径，缺一个就会被假象骗：
  1. **别名条数** ——最容易刷，加一堆冷僻词就能涨，没意义。
  2. **小类覆盖率** ——99 个有数据的小类里，多少个有采购词能指向它。
  3. **供给覆盖率** ——19559 家企业里，多少家所在的本事能被某个采购词命中。
     这个才是客户真正关心的：搜得到几家。

另外跑一组真实采购词，逐条看解析结果。落到 0 条的会退化为全库扫描
（实测 103 次请求 / 4.52MB），是必须消灭的情况。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gb_store as GB  # noqa: E402
import industry_taxonomy as TX  # noqa: E402


def supply_by_class() -> Counter:
    """每个 4 位小类实际有多少家企业。"""
    c: Counter = Counter()
    for bucket, _ in GB.iter_buckets():
        parts = bucket.split("/")
        if len(parts) != 3 or len(parts[2]) != 4:
            continue
        c[parts[2]] += len(GB.load_bucket(bucket))
    return c


def codes_referenced(alias: dict) -> set:
    return {e["code"] for entries in alias.values() for e in entries}


def battery() -> list:
    """真实客户会敲的词。覆盖主人点名的输送线，以及各主要品类。"""
    return [
        "输送线", "流水线", "传送带", "输送机", "风送线", "装配线", "自动化产线",
        "AGV", "提升机", "分拣线", "立体仓库", "起重机", "升降机",
        "非标自动化", "工业机器人", "工装夹具", "传感器", "伺服电机",
        "钣金", "焊接", "折弯", "冲压件", "钢结构", "机箱", "控制柜",
        "门窗", "断桥铝", "卷帘门",
        "喷粉", "喷漆", "镀锌", "发黑", "淬火", "抛光", "喷砂",
        "铸造", "精密铸造", "锻件",
        "车削", "铣削", "线切割", "慢走丝", "非标件", "齿轮", "减速机",
        "螺母", "铆钉", "非标螺丝",
        "密封圈", "O型圈", "油封", "橡胶件", "硅胶制品",
        "吸塑", "吹塑", "注塑件", "周转箱", "包装膜", "泡沫包装", "珍珠棉",
        "冲压模", "压铸模", "模具设计",
        "阀门", "水泵", "液压缸", "电磁阀",
        "冷库", "制冷设备", "冷凝器",
        "包装机", "灌装机", "封口机",
        "SMT", "贴片", "线束", "连接器", "电缆", "锂电池", "芯片", "LED", "灯具",
        "环保设备", "除尘设备", "污水处理",
        "汽车配件", "新能源汽车",
        "五金", "电动工具", "机电设备",
        "涂料", "胶水", "切削液",
        "木箱", "胶合板", "篷布", "窗帘",
        "刀具", "铣刀", "玻璃制品", "农机配件", "医疗器械", "矿山机械",
        "建筑五金", "消防器材",
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20, help="小类明细行数")
    ap.add_argument("--gap", type=int, default=30,
                    help="列出供给 >= N 家却仍无别名入口的小类")
    ap.add_argument("--no-battery", action="store_true", help="跳过采购词实测")
    a = ap.parse_args()

    sup = supply_by_class()
    total_co = sum(sup.values())
    n_class = len(sup)

    saved_max = GB.ALIAS_MAX_SUPPLY
    data_alias = GB.load_alias(with_curated=False)
    merged = GB.load_alias()
    curated = {k: v for k, v in merged.items() if k not in data_alias}

    data_codes = codes_referenced(data_alias)
    merged_codes = codes_referenced(merged)

    # ── 1. 别名条数 ──────────────────────────────────────────────
    print("=" * 72)
    print("1. 别名表规模")
    print("=" * 72)
    print(f"  数据推导层（gb-alias.json）        : {len(data_alias):5d} 条")
    print(f"  人工策展层（gb-alias-curated.json）: {len(curated):5d} 条")
    print(f"  合并去重后                         : {len(merged):5d} 条")
    print(f"  与数据层同词、仅补码的词           : "
          f"{len(set(data_alias) & set(curated)):5d} 条")

    # ── 2. 小类覆盖率 ────────────────────────────────────────────
    cov_before = sum(1 for c in sup if c in data_codes)
    cov_after = sum(1 for c in sup if c in merged_codes)
    print()
    print("=" * 72)
    print("2. 小类覆盖率（有数据的小类里，多少个有采购词能指到）")
    print("=" * 72)
    print(f"  有数据的小类总数        : {n_class}")
    print(f"  补词前                  : {cov_before:3d}  "
          f"({cov_before / n_class * 100:5.1f}%)")
    print(f"  补词后                  : {cov_after:3d}  "
          f"({cov_after / n_class * 100:5.1f}%)")

    # ── 3. 供给覆盖率（真正有用的口径）────────────────────────────
    reach_before = sum(n for c, n in sup.items() if c in data_codes)
    reach_after = sum(n for c, n in sup.items() if c in merged_codes)
    print()
    print("=" * 72)
    print("3. 供给覆盖率（多少家企业所在的本事能被某个采购词命中）")
    print("=" * 72)
    print(f"  全库企业数              : {total_co}")
    print(f"  补词前可命中            : {reach_before:6d}  "
          f"({reach_before / total_co * 100:5.1f}%)")
    print(f"  补词后可命中            : {reach_after:6d}  "
          f"({reach_after / total_co * 100:5.1f}%)")

    sup_idx = {}
    try:
        tree = json.load(open(ROOT / "data" / "gb-index.json", encoding="utf-8"))["tree"]
        for g in tree.values():
            for d in (g.get("divisions") or {}).values():
                for gr in (d.get("groups") or {}).values():
                    for c, cv in (gr.get("classes") or {}).items():
                        sup_idx[c] = cv.get("count", 0)
    except (OSError, KeyError, json.JSONDecodeError):
        sup_idx = {}

    # 关掉阈值重算一遍，用来对照噪音削减了多少
    GB.ALIAS_MAX_SUPPLY = None
    GB._SUPPLY_CACHE = None
    loose = GB.load_alias()
    GB.ALIAS_MAX_SUPPLY = saved_max
    GB._SUPPLY_CACHE = None

    print()
    print("=" * 72)
    print("4. 噪音对照：max_supply 阈值关掉 vs 开启")
    print("=" * 72)
    print(f"  当前阈值 ALIAS_MAX_SUPPLY = {saved_max}（环境变量 BMFG_ALIAS_MAX_SUPPLY 可覆盖）")
    print("  首位码永不淘汰，只过滤补位码——否则「钣金」这类词会掉到 0 家。")
    print()
    tot_on = tot_off = 0
    rows = []
    for w in sorted(set(loose) | set(merged)):
        off = {e["code"] for e in loose.get(w, [])}
        on = {e["code"] for e in merged.get(w, [])}
        if off == on:
            continue
        n_off = sum(sup_idx.get(c, 0) for c in off)
        n_on = sum(sup_idx.get(c, 0) for c in on)
        tot_off += n_off
        tot_on += n_on
        rows.append((w, n_off, n_on, sorted(off - on)))
    rows.sort(key=lambda r: -(r[1] - r[2]))
    print(f"  {'采购词':10s}{'关阈值':>9s}{'开阈值':>9s}  被剔除的码")
    for w, a_, b_, dropped in rows[:14]:
        print(f"  {w:10s}{a_:9d}{b_:9d}  {','.join(dropped)}")
    if len(rows) > 14:
        print(f"  … 另有 {len(rows) - 14} 个词同样被收紧")
    print()
    print(f"  受影响词合计：{tot_off} 家 → {tot_on} 家"
          f"（削减 {tot_off - tot_on} 家，{(1 - tot_on / max(tot_off, 1)) * 100:.0f}%）")
    gaps = [(c, n) for c, n in sup.most_common()
            if c not in merged_codes and n >= a.gap]
    print()
    print("=" * 72)
    print(f"5. 剩余缺口（供给 >= {a.gap} 家却仍无任何别名入口）")
    print("=" * 72)
    if not gaps:
        print("  无。所有够规模的小类都有采购词入口。")
    else:
        for c, n in gaps:
            print(f"  {c}  {TX.CLASSES.get(c, {}).get('name', '?'):26s} {n:5d} 家")

    # ── 5. 小类明细 ──────────────────────────────────────────────
    print()
    print("=" * 72)
    print(f"6. 供给 TOP{a.top} 小类：别名入口数（前 / 后）")
    print("=" * 72)
    entry_before: Counter = Counter()
    entry_after: Counter = Counter()
    for w, es in data_alias.items():
        for e in es:
            entry_before[e["code"]] += 1
    for w, es in merged.items():
        for e in es:
            entry_after[e["code"]] += 1
    print(f"  {'码':6s}{'小类名':30s}{'家数':>7s}{'前':>5s}{'后':>5s}")
    for c, n in sup.most_common(a.top):
        name = TX.CLASSES.get(c, {}).get("name", "?")[:28]
        mark = "" if entry_after[c] else "   ← 无入口"
        print(f"  {c:6s}{name:30s}{n:7d}{entry_before[c]:5d}{entry_after[c]:5d}{mark}")

    if a.no_battery:
        return 0

    # ── 6. 采购词实测 ────────────────────────────────────────────
    print()
    print("=" * 72)
    print("7. 采购词实测（解析不出 → 退化为全库扫描，103 请求 / 4.52MB）")
    print("=" * 72)
    sys.path.insert(0, str(ROOT / "scripts"))
    import query as Q  # noqa: E402

    full_scan, zero_hit, ok = [], [], []
    for w in battery():
        codes = Q.alias_codes_for(w)
        if not codes:
            full_scan.append(w)
            continue
        n = sum(sup.get(c, 0) for c in codes)
        src = next((e.get("source", "data")
                    for e in merged.get(w, [])), "fuzzy")
        (ok if n else zero_hit).append((w, sorted(codes), n, src))

    print(f"  测试词数 {len(battery())}")
    print(f"    ✅ 解析成功且有数据    : {len(ok):3d}")
    print(f"    ⚠️  解析成功但 0 家    : {len(zero_hit):3d}  {zero_hit}")
    print(f"    ❌ 解析不出（全库扫描）: {len(full_scan):3d}  {full_scan}")
    print()
    print("  抽样（前 24 条）：")
    for w, codes, n, src in ok[:24]:
        names = ",".join(TX.CLASSES.get(c, {}).get("name", "?")[:8] for c in codes[:3])
        print(f"    {w:10s} → {','.join(codes):22s} {n:5d} 家  [{src}]  {names}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
