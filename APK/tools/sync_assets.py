#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把仓库里的数据同步进安卓工程的 assets/。

为什么要有这个脚本
------------------
App 内置的是「全量指纹 + 索引」的**副本**。副本和源数据一旦靠手工拷贝维护，
迟早会出现「App 里还是上个月的分片清单」这种情况——而用户手机上是看不出来的，
只会表现为「搜得到 A 厂，搜不到 B 厂」。所以：

- 拷贝只由本脚本执行，任何人（含 CI）跑一次就能对齐；
- 每次同步都写 builtin.json，记录源文件的 SHA1 与生成时间，
  App 设置页直接显示「内置数据版本」，可当场核对是不是最新；
- 分片路径与 manifest.json 的 p 字段严格对应（去掉 skills/registry/fingerprint/ 前缀），
  App 端才能用同一套相对路径做「内置 vs 已更新」的覆盖判定。

用法:
    python APK/tools/sync_assets.py            # 预览（只打印将写入什么）
    python APK/tools/sync_assets.py --apply    # 真正写入
    python APK/tools/sync_assets.py --apply --clean   # 先清空 assets 再写
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent          # 仓库根
APK = ROOT / "APK"
ASSETS = APK / "app" / "src" / "main" / "assets"

# 源 → 目标（相对路径）。指纹前缀要剥掉，否则 assets 里多三层无用目录
INDEX_FILES = {
    "data/manifest.json": "index/manifest.json",
    "data/gb-index.json": "index/gb-index.json",
    "data/gb-alias.json": "index/gb-alias.json",
    "data/gb-alias-curated.json": "index/gb-alias-curated.json",
}
FP_SRC = ROOT / "skills" / "registry" / "fingerprint" / "gb"
FP_DST = ASSETS / "fingerprint" / "gb"


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sync_index(apply: bool) -> dict:
    out = {}
    for src_rel, dst_rel in INDEX_FILES.items():
        src = ROOT / src_rel
        if not src.exists():
            print(f"[缺失] {src_rel} —— 跳过（App 里该索引会为空，功能降级）")
            continue
        out[dst_rel] = {
            "source": src_rel,
            "bytes": src.stat().st_size,
            "sha1": sha1(src),
        }
        if apply:
            dst = ASSETS / dst_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return out


def sync_fingerprint(apply: bool, clean: bool) -> dict:
    """同步指纹分片，并记录每个分片的 SHA1。

    SHA1 是给 App 端做增量更新用的：manifest.json 里每个分片都有 h（内容 SHA1），
    App 用它和内置副本的 SHA1 比对，只下载真正变了的那些。没有这份清单，
    App 要么每次全量重下 4.7MB，要么永远用内置数据无法更新。
    """
    if not FP_SRC.exists():
        print(f"[缺失] {FP_SRC} —— 指纹层不存在，先跑 scripts/gen_fingerprint.py --apply")
        return {"shards": 0, "bytes": 0}
    if clean and apply and FP_DST.exists():
        shutil.rmtree(FP_DST)

    shards = sorted(FP_SRC.rglob("*.jsonl"))
    total = 0
    detail = {}
    for p in shards:
        size = p.stat().st_size
        total += size
        rel = p.relative_to(FP_SRC).as_posix()
        detail[rel] = {"bytes": size, "sha1": sha1(p)}
        if apply:
            dst = FP_DST / p.relative_to(FP_SRC)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
    return {"shards": len(shards), "bytes": total, "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser(description="同步仓库数据到 App assets")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    ap.add_argument("--clean", action="store_true", help="写入前清空内置指纹目录")
    a = ap.parse_args()

    files = sync_index(a.apply)
    fp = sync_fingerprint(a.apply, a.clean)

    builtin = {
        "builtin_at": date.today().isoformat(),
        "source_repo": "beacon-mfg",
        "fingerprint": fp,
        "files": files,
        "notes": "本文件由 APK/tools/sync_assets.py 生成，请勿手改。"
                 "字段 sha1 用于核对内置副本与仓库数据是否一致。",
    }
    print("内置索引：")
    for rel, meta in files.items():
        print("  %-32s %8d B  %s" % (rel, meta["bytes"], meta["sha1"][:12]))
    print("内置指纹：%d 片 / %.2f MB" % (fp["shards"], fp["bytes"] / 1048576))
    print("合计：%.2f MB" % ((fp["bytes"] + sum(m["bytes"] for m in files.values()))
                            / 1048576))

    if a.apply:
        ASSETS.mkdir(parents=True, exist_ok=True)
        with open(ASSETS / "index" / "builtin.json", "w", encoding="utf-8") as f:
            json.dump(builtin, f, ensure_ascii=False, indent=2)
        print("\n已写入 %s" % (ASSETS / "index" / "builtin.json"))
    else:
        print("\n（预览模式，未写入。加 --apply 执行）")


if __name__ == "__main__":
    sys.exit(main())
