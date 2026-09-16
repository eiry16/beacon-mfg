#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高效地核对暂存区 mask 路径里「未认领」记录是否仍含独立 11 位全号（安全闸门离线版）。

实现：用 `git ls-files -s` 取全部暂存 blob 的 sha+路径，再用单次 `git cat-file --batch`
把所有 blob 内容一次性读出，避免每文件一次 git 子进程（511 文件 × N 次会被环境 SIGTERM）。
与 scripts/postfetch.py 的 _staged_phone_masked_ok 同口径：claim.status=claimed/verified 允许全号，
未认领记录检查 contact_phone/address/address_en 是否仍含 1[3-9]\d{9}。
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STANDALONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
PHONE_FIELDS = ("contact_phone", "address", "address_en")
MASK_PATHS = ["data/gb", "data/en", "data/phone-index.jsonl"]


def is_claimed(rec):
    claim = rec.get("claim") if isinstance(rec, dict) else None
    return isinstance(claim, dict) and claim.get("status") in ("claimed", "verified")


def parse_records(content):
    content = (content or "").strip()
    if not content:
        return []
    try:
        d = json.loads(content)
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return [d]
    except Exception:
        pass
    recs = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if isinstance(o, dict):
            recs.append(o)
    return recs


def main():
    # 1) 取暂存 blob 的 sha 与路径
    r = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-s", "--"] + MASK_PATHS,
        capture_output=True, text=True,
    )
    entries = []  # (sha, path)
    for line in r.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        meta, path = parts
        sha = meta.split()[1]
        entries.append((sha, path))
    print(f"staged mask blobs: {len(entries)}", flush=True)
    if not entries:
        print("nothing staged -> 无需校验")
        return

    # 2) 单次 cat-file --batch 读出全部内容
    p = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "--batch"],
        input="\n".join(s for s, _ in entries) + "\n",
        capture_output=True,
    )
    raw = p.stdout
    # 解析 batch 输出：每个 blob = "<sha> blob <size>\n<content>\n"
    pos = 0
    blobs = []
    for sha, path in entries:
        # 头部行
        nl = raw.find(b"\n", pos)
        header = raw[pos:nl].decode("utf-8", "replace")
        pos = nl + 1
        if header.endswith("missing"):
            blobs.append((path, ""))
            pos += 1  # 跳过末尾换行
            continue
        # header: "<sha> blob <size>"
        size = int(header.split()[2])
        content = raw[pos:pos + size].decode("utf-8", "replace")
        pos += size
        if raw[pos:pos + 1] == b"\n":
            pos += 1
        blobs.append((path, content))

    # 3) 逐 blob 核对
    offenders = []
    total = 0
    for path, content in blobs:
        for rec in parse_records(content):
            if not isinstance(rec, dict) or is_claimed(rec):
                continue
            for field in PHONE_FIELDS:
                v = rec.get(field)
                if isinstance(v, str) and STANDALONE.search(v):
                    total += 1
                    offenders.append((path, field, v[:40]))
                    break
    print(f"RESIDUAL full-mobile count (unclaimed): {total}", flush=True)
    for path, field, v in offenders[:40]:
        print(f"  OFFENDER {path} [{field}] {v!r}", flush=True)
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
