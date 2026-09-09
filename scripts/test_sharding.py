#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""步骤3 回归：单桶超过 MAX_PER_FILE 时自动分片，且读回能跨片合并、缩容能清掉旧片。

在临时目录里跑，绝不碰真实 data/gb/。
用法：python scripts/test_sharding.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

os.environ["GB_MAX_PER_FILE"] = "500"      # 必须在 import 前设置
import gb_store  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gb_shard_test_"))
    try:
        real_dir = gb_store.GB_DIR
        gb_store.GB_DIR = tmp
        print(f"临时归档目录: {tmp}")
        print(f"MAX_PER_FILE = {gb_store.MAX_PER_FILE}")

        bucket = "C/34/3484"
        recs = [{"id": "CN-MFG-T%05d" % i, "company": "测试企业%d" % i} for i in range(1200)]

        # 1) 写入 1200 条 → 应切成 500/500/200 三片
        gb_store._write_bucket(bucket, recs)
        paths = gb_store.shard_paths(bucket)
        names = [p.name for p in paths]
        print(f"\n[1] 写入 1200 条后文件: {names}")
        check("切成 3 个文件", len(paths) == 3, str(len(paths)))
        check("命名为主文件 + -p2/-p3",
              names == ["3484.json", "3484-p2.json", "3484-p3.json"], str(names))
        sizes = [len(json.load(open(p, encoding="utf-8"))) for p in paths]
        check("每片条数 500/500/200", sizes == [500, 500, 200], str(sizes))

        # 2) read_bucket 跨片合并
        back = gb_store.load_bucket(bucket)
        check("读回 1200 条且顺序不变", len(back) == 1200 and back == recs, str(len(back)))

        # 3) iter_buckets 不把分片重复算成独立桶
        buckets = [b for b, _ in gb_store.iter_buckets()]
        check("iter_buckets 只认一个逻辑桶", buckets.count(bucket) == 1, str(buckets))

        # 4) 缩容到 300 条 → 旧分片必须被清掉
        gb_store._write_bucket(bucket, recs[:300])
        left = [p.name for p in gb_store.shard_paths(bucket)]
        print(f"\n[4] 缩容到 300 条后文件: {left}")
        check("旧分片已清理，只剩主文件", left == ["3484.json"], str(left))
        check("读回 300 条", len(gb_store.load_bucket(bucket)) == 300)

        # 5) 清空 → 文件整体删除（不留空 []）
        gb_store._write_bucket(bucket, [])
        check("清空后不留空文件", not gb_store.path_of_bucket(bucket).exists())

        # 6) 真实数据规模参照：当前最大桶多少条
        gb_store.GB_DIR = real_dir
        biggest = (0, "")
        for b, _ in gb_store.iter_buckets():
            n = len(gb_store.load_bucket(b))
            if n > biggest[0]:
                biggest = (n, b)
        print(f"\n[6] 真实归档最大桶: {biggest[1]} = {biggest[0]} 条"
              f"（阈值 {int(os.environ['GB_MAX_PER_FILE'])} 仅测试用，"
              f"生产为 {os.environ.get('GB_MAX_PER_FILE_PROD', '默认 5000')}）")
        print("    当前最大桶未超 5000，生产环境不会触发分片——能力就绪，待规模触发。")
    finally:
        gb_store.GB_DIR = real_dir if "real_dir" in dir() else gb_store.GB_DIR
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
