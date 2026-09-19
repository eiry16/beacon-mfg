#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填 region.district（区县/县级市），让「昆山」这类目标城市能被检索到。

问题
----
JOBS 里 昆山 / 海盐 / 莫干山 是目标城市，但高德 POI 对县级市返回的是**地级市名**：
昆山的 POI 的 `cityname` 是「苏州」、`adname` 才是「昆山市」。
结果这些记录全部以 `region.city = 苏州` 落库 —— 数据抓到了，但**按「昆山」检索返回 0**
（实测：地址含"昆山"的 190 条记录，city 字段全是 苏州）。

做法
----
记录里存了 `amap.adcode`，而 `data/district_cache.json` 有「父城市 → [(adcode, 区县名)]」，
两者一拼就能还原区县。写入 `region.district`（去掉 市/县/区 后缀，便于精确匹配"昆山"）。

- `--apply` 才落盘，默认干跑。
- 一个 adcode 在同一父城市下对应多个名字时（如东莞 441900 对一堆镇）判定为有歧义，跳过。

用法
----
    python scripts/backfill_district.py          # 干跑，看能回填多少
    python scripts/backfill_district.py --apply  # 真写
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gb_store  # noqa: E402

SUFFIX = re.compile(r"(市|县|区|旗|自治州|自治县)$")


def build_adcode_map() -> tuple[dict[tuple[str, str], str], dict[str, int]]:
    """(父城市, adcode) → 区县名。返回 (映射, 歧义统计)。"""
    cache = json.load(open(ROOT / "data" / "district_cache.json", encoding="utf-8"))
    raw: dict[tuple[str, str], set[str]] = defaultdict(set)
    for city, blob in cache.items():
        # 少数键是字符串哨兵（如 莫干山 -> "none"，表示查不到行政区划），直接跳过
        if not isinstance(blob, dict):
            continue
        for item in blob.get("items", []):
            if len(item) >= 2 and item[0] and item[1]:
                raw[(city, str(item[0]))].add(str(item[1]))
    out: dict[tuple[str, str], str] = {}
    ambiguous: dict[str, int] = defaultdict(int)
    for k, names in raw.items():
        if len(names) == 1:
            out[k] = SUFFIX.sub("", next(iter(names)))
        else:
            ambiguous[k[0]] += 1
    return out, dict(ambiguous)


def main() -> int:
    apply = "--apply" in sys.argv
    admap, ambiguous = build_adcode_map()
    print("=" * 74)
    print("区县回填%s" % ("（落盘）" if apply else "（干跑）"))
    print("=" * 74)
    print("adcode→区县 映射条目 : %d" % len(admap))
    if ambiguous:
        print("有歧义已跳过（同 adcode 多名，多为东莞/中山的镇）: %s"
              % ", ".join("%s×%d" % (c, n) for c, n in
                          sorted(ambiguous.items(), key=lambda kv: -kv[1])[:6]))

    targets = ["昆山", "海盐", "莫干山", "大理", "丽江"]
    hit_targets: dict[str, int] = defaultdict(int)
    total = filled = already = 0
    dirty: dict[str, list] = {}

    for bucket, _ in gb_store.iter_buckets():
        rows = gb_store.load_bucket(bucket)
        changed = False
        for r in rows:
            total += 1
            region = r.get("region") or {}
            adcode = str((r.get("amap") or {}).get("adcode") or "")
            city = region.get("city") or ""
            dist = region.get("district")
            if dist:
                already += 1
                hit_targets[dist] += 1
                continue
            name = admap.get((city, adcode)) if adcode else None
            if not name:
                # 兜底：地址里直接写明了（如「昆山市玉山镇…」）
                addr = r.get("address") or ""
                for t in targets:
                    if t in addr:
                        name = t
                        break
            if not name or name == city:
                continue
            region["district"] = name
            r["region"] = region
            changed = True
            filled += 1
            hit_targets[name] += 1
        if changed and apply:
            dirty[bucket] = rows

    print("扫描记录            : %d" % total)
    print("新填区县            : %d" % filled)
    print("已有区县（跳过）    : %d" % already)
    print("\n--- 目标城市命中 ---")
    for t in targets:
        print("  %-6s %d 条" % (t, hit_targets.get(t, 0)))
    top = sorted((k, v) for k, v in hit_targets.items() if k not in targets)
    if top:
        print("\n--- 其它区县 Top10 ---")
        for k, v in sorted(top, key=lambda kv: -kv[1])[:10]:
            print("  %-10s %d 条" % (k, v))

    if not apply:
        print("\n干跑结束。加 --apply 写入。")
        return 0
    for bucket, rows in dirty.items():
        gb_store.save_bucket(bucket, rows)
    gb_store.invalidate_cache()
    print("\n已写入 %d 个桶。" % len(dirty))
    return 0


if __name__ == "__main__":
    sys.exit(main())
