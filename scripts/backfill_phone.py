#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 contact_phone="待核实" 的企业补电话 —— 高德 POI 反查 + 同名校验。

背景
----
全库 23698 家里有 8537 家（36%）电话是「待核实」占位值。它们全部来自
source=public_directory（高德 POI 采集），入库那一刻高德就没返回 tel。
已确认本地再无可用字段：note / address / website / company / category /
source_url / keywords 全扫过，号码命中数为 0。所以只能回源重查。

为什么必须做同名校验
--------------------
高德按关键词搜会返回一堆「差不多」的 POI，直接取第一条回填，等于把 A 厂
的电话挂到 B 厂名下。**留空是「没数据」，挂错是「假数据」**——后者违反项目
红线，比不补更糟。所以只有 POI 名与库内公司名字符重合度达标才回填，
宁可漏补也不补错。

配额与节奏
----------
每条候选 1 次 API 请求（limit=5 一页）。8537 条 ≈ 8537 次，超出个人开发者
日配额，需要分多天跑。回填成功后该条不再进入候选，重跑自动续上（断点续跑）。

用法:
    python scripts/backfill_phone.py --limit 30            # 小样本验证命中率
    python scripts/backfill_phone.py --apply --limit 300   # 小批量真实回填
    python scripts/backfill_phone.py --apply               # 全量（注意配额）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GB = ROOT / "data" / "gb"
BASE = "https://restapi.amap.com/v3/place/text"

# 相似度阈值：低于这个值宁可不补。调高 = 更保守（漏补多、错补少）
SIM_THRESHOLD = 0.62


def load_key() -> str:
    key = os.environ.get("AMAP_KEY", "")
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("AMAP_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def pick_phone(raw) -> str:
    """与 APK/tools/sync_assets.py:pick_phone 同口径，别各改各的。"""
    raw = (raw or "").strip()
    if not raw or raw == "待核实":
        return ""
    if 7 <= len(re.sub(r"\D", "", raw)) <= 15:
        return raw
    for part in re.split(r"[;；,，、/|]+", raw):
        p = part.strip()
        if 7 <= len(re.sub(r"\D", "", p)) <= 15:
            return p
    return ""


def norm_name(s: str) -> str:
    """去掉括号内容与空白，只留可比的公司名字面。"""
    return re.sub(r"[（(].*?[)）]|\s+", "", s or "")


def name_sim(a: str, b: str) -> float:
    """字符级 Jaccard。中文公司名没有词边界，字符重合比编辑距离稳。"""
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def query(key: str, company: str, city: str, delay: float):
    params = {
        "key": key,
        "keywords": company,
        "city": (city or "").replace("市", ""),
        "citylimit": "true",
        "offset": 5,
        "page": 1,
        "extensions": "all",
    }
    try:
        with urllib.request.urlopen(BASE + "?" + urllib.parse.urlencode(params), timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  请求失败: {e}")
        return None
    finally:
        time.sleep(delay)
    if data.get("status") != "1":
        print(f"  API: {data.get('info')}")
        return None
    return data.get("pois", [])


def collect_candidates():
    """扫出所有电话为占位值/空的记录，返回 [(path, index, record)]。"""
    out = []
    for p in sorted(GB.rglob("*.json")):
        try:
            arr = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        for i, o in enumerate(arr):
            if isinstance(o, dict) and not pick_phone(o.get("contact_phone")):
                out.append((p, i, o))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=不限）")
    ap.add_argument("--apply", action="store_true", help="真正写回（默认只统计）")
    ap.add_argument("--random", action="store_true",
                    help="随机抽样。默认按文件顺序取——分片按国标码归档，"
                         "顺序取样会集中在少数行业，命中率会失真（实测偏差很大）")
    ap.add_argument("--delay", type=float, default=0.3, help="每次请求间隔秒")
    a = ap.parse_args()

    key = load_key()
    if not key:
        print("缺少 AMAP_KEY（放 .env 或环境变量）")
        return 1

    cands = collect_candidates()
    print(f"待补记录：{len(cands)} 条")
    if a.random:
        import random
        random.shuffle(cands)
    if a.limit:
        cands = cands[: a.limit]
        print(f"本次处理：{len(cands)} 条"
              f"（--limit，{'随机' if a.random else '按文件顺序'}）")

    found = 0            # 高德有电话且同名校验通过
    no_tel = 0           # 查到了 POI 但没电话
    no_match = 0         # 没查到够像的 POI
    patched: dict[Path, list] = {}

    for n, (path, idx, rec) in enumerate(cands, 1):
        company = (rec.get("company") or "").strip()
        city = (rec.get("region") or {}).get("city") or ""
        if not company:
            continue
        pois = query(key, company, city, a.delay)
        if pois is None:
            continue
        best, best_sim = None, 0.0
        for poi in pois:
            sim = name_sim(company, poi.get("name") or "")
            if sim > best_sim:
                best, best_sim = poi, sim
        if best is None or best_sim < SIM_THRESHOLD:
            no_match += 1
            print(f"[{n}/{len(cands)}] ✗ 无匹配  {company[:24]} (最高相似 {best_sim:.2f})")
            continue
        tel = pick_phone(best.get("tel"))
        if not tel:
            no_tel += 1
            print(f"[{n}/{len(cands)}] · POI无电话  {company[:24]} (相似 {best_sim:.2f})")
            continue
        found += 1
        print(f"[{n}/{len(cands)}] ✓ {company[:20]} → {tel} (相似 {best_sim:.2f})")
        if a.apply:
            rec["contact_phone"] = tel
            patched.setdefault(path, []).append(path)

    print("\n==== 汇总 ====")
    print(f"处理 {len(cands)} 条：可补 {found} / POI 无电话 {no_tel} / 无同名匹配 {no_match}")
    if len(cands):
        print(f"命中率 {found * 100 / len(cands):.1f}%（若能补 {found} 条，全库覆盖率将提升到 "
              f"{(15161 + found) * 100 / 23698:.1f}%）")

    if a.apply and patched:
        for path in patched:
            arr = json.loads(path.read_text(encoding="utf-8"))
            path.write_text(
                json.dumps(arr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(f"\n已写回 {len(patched)} 个分片")
    elif a.apply:
        print("\n没有任何回填。")
    else:
        print("\n（预览模式，未写回。加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
