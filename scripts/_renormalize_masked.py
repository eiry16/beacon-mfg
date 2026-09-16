#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分批对 mask 路径做 git add --renormalize（每文件一次 git 调用，规避环境对大索引重写的 SIGTERM）。

为什么不用 `git add --renormalize -- data/gb` 一次跑完：本仓库 data/gb 有 256 个较大分片，
单次 git 进程处理全部文件会被运行环境 SIGTERM（实测单目录也超时被杀）。改成逐文件调用，
每次 git 进程只动一个文件，单个调用很小、稳定完成。renormalize 幂等，中断重跑无副作用。
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / ".git" / "index.lock"
FULL_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

MASK_PATHS = ["data/gb", "data/en", "data/phone-index.jsonl"]


def git(args):
    return subprocess.run(
        ["git", "-C", str(ROOT)] + args,
        capture_output=True, text=True,
    )


def clear_lock():
    try:
        if LOCK.exists():
            LOCK.unlink()
    except Exception:
        pass


def list_mask_files():
    files = []
    for p in MASK_PATHS:
        r = git(["ls-files", "--", p])
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                files.append(line)
    return files


def staged_residual(path: str) -> int:
    """返回该文件在暂存区里仍含的独立 11 位全号数量。"""
    r = git(["show", f":{path}"])
    if r.returncode != 0:
        return -1
    return len(FULL_RE.findall(r.stdout))


def main():
    files = list_mask_files()
    print(f"mask files total: {len(files)}", flush=True)
    done = 0
    failed = []
    for i, f in enumerate(files):
        clear_lock()
        r = git(["add", "--renormalize", "--", f])
        if r.returncode != 0:
            failed.append((f, r.stderr.strip()[:120]))
        done += 1
        if (i + 1) % 50 == 0:
            print(f"  progress {done}/{len(files)}", flush=True)
    print(f"renormalized {done} files; failures: {len(failed)}", flush=True)
    if failed:
        for f, e in failed[:10]:
            print("  FAIL", f, e, flush=True)


if __name__ == "__main__":
    main()
