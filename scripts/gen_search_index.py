#!/usr/bin/env python
"""生成 MCP 检索用的预构建倒排索引（发布侧产物，MCP 只读消费）。

背景：MCP 早期实现每次冷启动都要把全部 L0 指纹分片（fp）拉下来在客户端建索引，
复杂度 O(数据总量) —— 11.8 万条约 5.8s，外推千万级约 8 分钟、2GB 内存，不可接受。

本脚本把「建索引」这件事挪到发布侧一次性做完，产出随数据一同发布的只读产物：

    skills/registry/index/
        meta.json            桶数 / 记录数 / 国标码数 / 来源 HEAD（供 MCP 判新鲜度）
        city.json            {城市: {国标码: 命中条数}}
        terms/bXXXX.json     {词:   {国标码: 命中条数}}   按 sha1(词) % BUCKETS 分桶

查询时 MCP 只拉：meta + 城市表 + 命中的 1~N 个词桶 → 求交得到候选国标码 →
只拉这些国标码对应的分片做精确过滤。复杂度从 O(总量) 降为 O(命中量)。

词表口径（必须与 mcp/server.py 的 _tokenize 完全一致，否则会误判为空结果）：
    - CJK：滑窗 1-gram 与 2-gram（2-gram 支撑「精密加工」这类多字词，1-gram 支撑单字查询）
    - 拉丁/数字：整词 + 所有长度 >= 2 的前缀（支撑 "cnc"、"mach" 这类前缀式子串）
    - 覆盖字段与 MCP 的检索口径一致：企业名 / 城市 / 国标码 / 工艺 / 材料 / 认证

安全边界：只读 fp 分片，不写数据、不碰工作树里的 data/、不触碰任何密钥。

用法：
    python scripts/gen_search_index.py                # 写入 skills/registry/index/
    python scripts/gen_search_index.py --dry-run      # 只统计不写盘
    python scripts/gen_search_index.py --buckets 512  # 指定词桶数量
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FP_DIR = ROOT / "skills" / "registry" / "fingerprint"
OUT_DIR = ROOT / "skills" / "registry" / "index"

DEFAULT_BUCKETS = 512

# CJK 统一表意文字区（含扩展 A），够用且不会把标点卷进来
CJK = r"\u4e00-\u9fff\u3400-\u4dbf"
RE_CJK = re.compile(f"[{CJK}]+")
RE_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list:
    """把一段文本切成可索引的词。必须与 mcp/server.py 的 _tokenize 保持一致。"""
    t = (text or "").lower()
    out = set()
    for run in RE_CJK.findall(t):
        for i, ch in enumerate(run):
            out.add(ch)                      # 1-gram：支撑单字查询
            if i + 2 <= len(run):
                out.add(run[i:i + 2])        # 2-gram：支撑多字词
    for w in RE_WORD.findall(t):
        if len(w) >= 2:
            out.add(w)                                    # 整词
            for n in range(2, min(len(w), 12)):           # 前缀：支撑前缀式子串
                out.add(w[:n])
        elif w:
            out.add(w)
    return sorted(out)


def hay(rec: dict) -> str:
    """与 mcp/server.py 的 _hay 对齐：检索口径必须一致，否则索引会漏召回。"""
    return " ".join(str(rec.get(k, "")) for k in ("co", "city", "gb")) + " " + \
           " ".join(rec.get("proc", []) or []) + " " + \
           " ".join(rec.get("mat", []) or []) + " " + \
           " ".join(rec.get("cert", []) or [])


def head_sha() -> str:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def iter_fp_records():
    for p in sorted(FP_DIR.rglob("*.jsonl")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
        except Exception:
            continue


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 MCP 检索倒排索引")
    ap.add_argument("--buckets", type=int, default=DEFAULT_BUCKETS,
                    help=f"词桶数量（默认 {DEFAULT_BUCKETS}）")
    ap.add_argument("--out", default=str(OUT_DIR), help="输出目录")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写盘")
    args = ap.parse_args()

    postings = defaultdict(lambda: defaultdict(int))   # term -> gb -> count
    city_postings = defaultdict(lambda: defaultdict(int))

    n = 0
    for rec in iter_fp_records():
        n += 1
        gb = str(rec.get("gb", "") or "")
        if not gb:
            continue
        city = str(rec.get("city", "") or "")
        if city:
            city_postings[city][gb] += 1
        for term in tokenize(hay(rec)):
            postings[term][gb] += 1

    if not n:
        print("没有读到任何 fp 分片记录，索引未生成（检查 skills/registry/fingerprint/）")
        return 1

    buckets = defaultdict(dict)
    for term, gbmap in postings.items():
        b = int(hashlib.sha1(term.encode("utf-8")).hexdigest(), 16) % args.buckets
        buckets[b][term] = dict(gbmap)

    total_bytes = 0
    if not args.dry_run:
        out = Path(args.out)
        (out / "terms").mkdir(parents=True, exist_ok=True)
        for b, payload in buckets.items():
            fp = out / "terms" / f"b{b:04d}.json"
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            fp.write_text(data, encoding="utf-8")
            total_bytes += len(data.encode("utf-8"))
        city_path = out / "city.json"
        city_data = json.dumps({c: dict(m) for c, m in city_postings.items()},
                               ensure_ascii=False, separators=(",", ":"))
        city_path.write_text(city_data, encoding="utf-8")
        total_bytes += len(city_data.encode("utf-8"))
        meta = {
            "version": 2,
            "buckets": args.buckets,
            "records": n,
            "gb_count": len({g for m in postings.values() for g in m}),
            "terms": len(postings),
            "cities": len(city_postings),
            "source_head": head_sha(),
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (out / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"记录数 {n} | 词条 {len(postings)} | 城市 {len(city_postings)} | 桶 {args.buckets}")
    if not args.dry_run:
        print(f"索引输出 {args.out} | 体积约 {total_bytes / 1024 / 1024:.2f} MB "
              f"（平均每桶 {total_bytes / max(len(buckets), 1) / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
