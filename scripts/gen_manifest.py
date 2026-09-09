#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 data/manifest.json —— 客户 Agent 的唯一入口文件

为什么要有它
------------
客户 Agent 想找供应商，最笨的办法是 clone 整个仓库（今天 4 MB，千万级约 2 GB），
而它真正要的往往只是"宁波做模具的那批"。

manifest 把"全库长什么样"压缩成一个小文件：列出每个分片的路径、条数、字节数、
内容哈希（当 ETag 用）。客户拉一次 manifest，就能精确算出自己要下哪几个分片，
其余的一个字节都不用碰。

客户端标准流程
--------------
    1. GET  data/manifest.json                    （今天约 46 KB，千万级约 0.5 MB）
    2. 本地按 gb 码筛出需要的分片（如 3525 模具制造）
    3. GET  分片文件，带请求头 If-None-Match: "<h>"
           → 服务端返回 304 就用本地缓存，只花一次往返
    4. 命中后按 id 调详情 API 取完整档案（~24 KB/次），不要提前下明细

用法:
    python scripts/gen_manifest.py            # 生成 data/manifest.json
    python scripts/gen_manifest.py --check    # 校验现有 manifest 是否过期
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gb_store  # noqa: E402

MANIFEST = ROOT / "data" / "manifest.json"
FP_DIR = ROOT / "skills" / "registry" / "fingerprint" / "gb"
EN_DIR = ROOT / "data" / "en" / "gb"
PQ_DIR = ROOT / "dist" / "parquet"

SCHEMA_VERSION = "1.0"

# 分片类型。客户端按需组合：
#   fp   L0 指纹（最小，初筛用，优先拉这个）
#   zh   中文完整档案
#   en   英文镜像
TYPE_FP = "fp"
TYPE_ZH = "zh"
TYPE_EN = "en"
TYPE_PQ = "pq"
TYPE_PHONE = "phone"      # 号码索引（id→phone），构建期产物，非国标分片


def sha1_of(path: Path) -> str:
    """内容哈希。当 ETag 用：内容没变就返回 304，省掉重传。"""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_lines(path: Path) -> int:
    n = 0
    with open(path, "rb") as f:
        for _ in f:
            n += 1
    return n


def count_json(path: Path) -> int:
    with open(path, encoding="utf-8") as f:
        return len(json.load(f))


def mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()


def _code_name(code: str) -> str:
    t = gb_store._taxonomy()
    if len(code) == 4:
        return (t["classes"].get(code) or {}).get("name", "")
    if len(code) == 3:
        return (t["groups"].get(code) or {}).get("name", "")
    return (t["divisions"].get(code) or {}).get("name", "")


def collect(directory: Path, pattern: str, stype: str, counter) -> list[dict]:
    out: list[dict] = []
    if not directory.exists():
        return out
    for f in sorted(directory.rglob(pattern)):
        rel = f.relative_to(ROOT).as_posix()
        # 分片路径 → 桶键：gb/C/34/3484.jsonl → C/34/3484
        # 注意去掉 -pN 后缀：3484-p2.json 属于同一个逻辑桶 C/34/3484，
        # 不能当成"3484-p2"这个不存在的国标码。
        stem = gb_store.base_of_stem(f.stem)
        parts = f.relative_to(directory).parts
        bucket = "/".join(parts[:-1] + (stem,)) if len(parts) > 1 else stem
        code = stem if not stem.startswith("_") else ""
        out.append({
            "p": rel,
            "b": bucket,
            "c": code,
            "n": _code_name(code) if code else "",
            "t": stype,
            "k": counter(f),
            "z": f.stat().st_size,
            "h": sha1_of(f),
            "u": mtime(f),
        })
    return out


def collect_parquet() -> list[dict]:
    """列式分片（Parquet）。没导出过就返回空——这一层是规模上来之后才启用的。"""
    if not PQ_DIR.exists():
        return []
    parquets = sorted(PQ_DIR.rglob("*.parquet"))
    if not parquets:
        return []
    try:
        import pyarrow.parquet as pq
    except ImportError:
        # 静默返回空会造出"上一版有 pq、这一版没了"的伪变更——
        # 分片数从 412 掉到 309，diff 上看只是删了一堆行，没人知道是依赖没装。
        print("  [警告] dist/parquet/ 有 %d 个分片，但 pyarrow 未安装 → "
              "本轮清单不会包含 pq 分片。\n"
              "         请改用装了 pyarrow 的解释器重跑，否则清单会静默丢一层。"
              % len(parquets), file=sys.stderr)
        return []
    out = []
    for f in parquets:
        rel = f.relative_to(ROOT).as_posix()
        stem = gb_store.base_of_stem(f.stem)
        parts = f.relative_to(PQ_DIR).parts
        bucket = "/".join(parts[:-1] + (stem,)) if len(parts) > 1 else stem
        code = stem if not stem.startswith("_") else ""
        out.append({
            "p": rel, "b": bucket, "c": code, "n": _code_name(code) if code else "",
            "t": TYPE_PQ,
            "k": pq.ParquetFile(f).metadata.num_rows,
            "z": f.stat().st_size, "h": sha1_of(f), "u": mtime(f),
        })
    return out


def collect_phone() -> list[dict]:
    """号码索引（id→phone，jsonl）。

    它不按国标归档，是构建期从 data/gb 完整档案抽出来的横向索引，
    所以单独占一项 t=phone。App 靠它让卡片直接显示号码而不必现拉 2.4 万份档案。
    文件由 APK/tools/sync_assets.py --apply 生成；没生成过就跳过，不阻塞清单。
    """
    f = ROOT / "data" / "phone-index.jsonl"
    if not f.exists():
        print("  [跳过] data/phone-index.jsonl 不存在 —— "
              "先跑 python APK/tools/sync_assets.py --apply", file=sys.stderr)
        return []
    return [{
        "p": f.relative_to(ROOT).as_posix(),
        "b": "phone-index",
        "c": "",
        "n": "号码索引",
        "t": TYPE_PHONE,
        "k": count_lines(f),
        "z": f.stat().st_size,
        "h": sha1_of(f),
        "u": mtime(f),
    }]


def build() -> dict:
    shards: list[dict] = []
    shards += collect(FP_DIR, "*.jsonl", TYPE_FP, count_lines)
    shards += collect(gb_store.GB_DIR, "*.json", TYPE_ZH, count_json)
    shards += collect(EN_DIR, "*.json", TYPE_EN, count_json)
    shards += collect_phone()
    shards += collect_parquet()

    total_bytes = sum(s["z"] for s in shards)
    by_type: dict[str, dict] = {}
    for s in shards:
        d = by_type.setdefault(s["t"], {"shards": 0, "bytes": 0, "records": 0})
        d["shards"] += 1
        d["bytes"] += s["z"]
        d["records"] += s["k"]

    return {
        "metadata": {
            "schema": SCHEMA_VERSION,
            "description": "分片清单。客户 Agent 只需拉本文件，即可定位并按需下载"
                           "命中的国标小类分片，无需 clone 整个仓库。",
            "usage": "1) 拉本文件 2) 按 c（国标码）筛分片 3) 缓存本次响应返回的 ETag，"
                     "下次带 If-None-Match 拉分片（命中返回 304，零传输）"
                     " 4) 命中后按 id 调详情 API 取完整档案",
            # 血泪教训（2026-09-09 实测）：h 是**内容** SHA1，不是服务端 ETag。
            # GitHub raw 返回的 ETag 是另一套哈希（形如 W/"sha256..."），
            # 拿 h 去当 If-None-Match 永远不命中 304，会静默退化成每次全量重下。
            # h 的正确用途：下载后校验分片内容是否被篡改/传错。
            "field_note": "h = 分片内容 SHA1，用于下载后校验完整性；"
                          "做增量更新请用服务端响应头里的 ETag，不要用 h。",
            "generated_at": date.today().isoformat(),
            "total_shards": len(shards),
            "total_bytes": total_bytes,
            "by_type": by_type,
            "field_spec": {
                "p": "分片路径（相对仓库根）", "b": "归档桶键", "c": "国标码",
                "n": "国标名称",
                "t": "类型 fp 指纹 / zh 中文 / en 英文 / pq 列式 / phone 号码索引",
                "k": "记录条数", "z": "字节数", "h": "内容 SHA1（作 ETag）",
                "u": "最后更新",
            },
        },
        "shards": shards,
    }


def check() -> int:
    """校验磁盘内容与 manifest 是否一致（CI 可跑，防止改了数据忘了重建）。"""
    if not MANIFEST.exists():
        print("[ERROR] 缺少 data/manifest.json，先跑 scripts/gen_manifest.py")
        return 1
    old = json.loads(MANIFEST.read_text(encoding="utf-8"))
    old_map = {s["p"]: s for s in old["shards"]}
    new = build()
    new_map = {s["p"]: s for s in new["shards"]}

    stale = [p for p, s in new_map.items()
             if p not in old_map or old_map[p]["h"] != s["h"]]
    missing = [p for p in old_map if p not in new_map]
    if not stale and not missing:
        print("manifest 校验通过：%d 个分片全部一致" % len(new_map))
        return 0
    for p in stale[:10]:
        print("  已变化：%s" % p)
    for p in missing[:10]:
        print("  已删除：%s" % p)
    print("[ERROR] manifest 过期（%+d 变化 / %d 删除），请重跑 gen_manifest.py"
          % (len(stale), len(missing)))
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="生成/校验 data/manifest.json")
    ap.add_argument("--check", action="store_true", help="校验现有 manifest 是否过期")
    a = ap.parse_args()
    if a.check:
        return check()

    m = build()
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    md = m["metadata"]
    print("manifest 已生成：%s" % MANIFEST.relative_to(ROOT))
    print("  分片 %d 个，全库 %.2f MB" % (md["total_shards"], md["total_bytes"] / 1048576))
    for t, d in sorted(md["by_type"].items()):
        print("  %-3s %4d 分片 / %6d 条 / %7.2f MB"
              % (t, d["shards"], d["records"], d["bytes"] / 1048576))
    print("  清单自身 %.1f KB —— 客户拉这个就能决定下什么"
          % (MANIFEST.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
