#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参考客户端：按国标码找供应商，只为命中的分片付费

这是给客户 Agent 抄的样板实现。核心思想只有一句：
**不要让客户为它不看的那些行付费。**

一次典型查询的数据交换量：

    清单 manifest.json     ~76 KB（千万级约 1.5 MB，缓存后近乎为零）
    + 命中的小类分片       ~250 KB（fp 类型，L0 指纹）
    + 详情 API（可选）     ~24 KB / 20 家
    ─────────────────────────────────────────
    合计 ≈ 0.3 MB        对比全量 clone 4 MB（千万级 2 GB）

用法:
    # 本地模式（读仓库文件，离线可跑）
    python scripts/client_search.py --industry 3525 --city 宁波

    # 远程模式（走 HTTPS，演示真实网络开销）
    python scripts/client_search.py --industry 3525 --city 宁波 \
        --base-url https://raw.githubusercontent.com/eiry16/beacon-mfg/main

    python scripts/client_search.py --industry 3484 --proc cnc_milling --limit 10
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".workbuddy" / "cache" / "shards"
MANIFEST_REL = "data/manifest.json"

# 本次进程累计传输字节数（用来证明"只为命中的分片付费"）
TRANSFERRED = 0


def _fmt(n: float) -> str:
    if n < 1024:
        return "%d B" % n
    if n < 1048576:
        return "%.1f KB" % (n / 1024)
    return "%.2f MB" % (n / 1048576)


def fetch(rel: str, base_url: str | None) -> bytes:
    """取一个文件。本地模式直接读盘；远程模式走 HTTPS + ETag 缓存。"""
    global TRANSFERRED
    if not base_url:
        data = (ROOT / rel).read_bytes()
        TRANSFERRED += len(data)
        return data

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = rel.replace("/", "_")
    body = CACHE_DIR / key
    meta = CACHE_DIR / ("%s.meta.json" % key)
    headers = {}
    if meta.exists() and body.exists():
        headers["If-None-Match"] = json.loads(
            meta.read_text(encoding="utf-8"))["etag"]

    req = urllib.request.Request("%s/%s" % (base_url.rstrip("/"), rel),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            etag = resp.headers.get("ETag") or ""
            body.write_bytes(raw)
            meta.write_text(json.dumps({"etag": etag}), encoding="utf-8")
            TRANSFERRED += len(raw)
            return raw
    except urllib.error.HTTPError as e:
        if e.code == 304 and body.exists():
            return body.read_bytes()   # 命中缓存，零传输
        raise


def load_manifest(base_url: str | None) -> dict:
    return json.loads(fetch(MANIFEST_REL, base_url).decode("utf-8"))


def pick_shards(manifest: dict, code: str | None, stype: str = "fp") -> list[dict]:
    """按国标码挑分片。支持前缀：3525 精确、34 大类下全部。"""
    shards = [s for s in manifest["shards"] if s["t"] == stype]
    if not code:
        return shards
    return [s for s in shards if s["c"].startswith(code)]


def load_rows(shard: dict, base_url: str | None) -> list[dict]:
    raw = fetch(shard["p"], base_url).decode("utf-8")
    if shard["p"].endswith(".jsonl"):
        return [json.loads(l) for l in raw.splitlines() if l.strip()]
    return json.loads(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description="参考客户端：按国标码找供应商")
    ap.add_argument("--industry", help="国标码（支持前缀）：3525 模具 / 34 通用设备大类")
    ap.add_argument("--city", help="城市")
    ap.add_argument("--proc", help="工艺码，逗号分隔任一命中")
    ap.add_argument("--mat", help="材料关键词")
    ap.add_argument("--type", default="fp", choices=["fp", "zh", "en"],
                    help="分片类型：fp 指纹（默认，最小）/ zh 中文档案 / en 英文")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--base-url", help="远程基址，不给则读本地仓库")
    a = ap.parse_args()

    manifest = load_manifest(a.base_url)
    shards = pick_shards(manifest, a.industry, a.type)
    if not shards:
        print("清单里没有匹配的分片：industry=%s type=%s" % (a.industry, a.type))
        return 1

    print("清单：%d 个分片，本次命中 %d 个（%s）"
          % (manifest["metadata"]["total_shards"], len(shards),
             ", ".join("%s %s" % (s["c"], s["n"]) for s in shards[:3])))

    rows: list[dict] = []
    for s in shards:
        rows.extend(load_rows(s, a.base_url))
    print("载入 %d 条记录" % len(rows))

    procs = [p.strip() for p in a.proc.split(",")] if a.proc else []
    hits = []
    for r in rows:
        if a.city and r.get("city") != a.city:
            continue
        if procs and not (set(r.get("proc") or []) & set(procs)):
            continue
        if a.mat and not any(a.mat in m for m in (r.get("mat") or [])):
            continue
        hits.append(r)

    hits.sort(key=lambda r: (-(r.get("sc") or 0), -(r.get("tel") or 0)))
    print("\n命中 %d 家（显示前 %d）：\n" % (len(hits), a.limit))
    for r in hits[:a.limit]:
        print("  %s  %s  · %s  · 工艺 %s%s"
              % (r["id"], r.get("co"), r.get("city") or "-",
                 ",".join(r.get("proc") or []) or "-",
                 " · 有电话" if r.get("tel") else ""))

    print("\n本次传输 %s（全量 clone 需要 %s）"
          % (_fmt(TRANSFERRED),
             _fmt(manifest["metadata"]["total_bytes"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
