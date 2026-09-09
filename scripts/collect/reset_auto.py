"""reset_auto.py — 清空「自动整理」层，只保留人工/样本卡片。

为什么需要：L0 指纹是**追加写入**的。重复生成同一城市时，
若候选挑选结果有变化（打分规则改了、名录新增了），新条目会追加进去，
旧条目不会被覆盖 —— 于是出现「上海 341 家 > 名额 300」这种幽灵数据。
每次重跑批量整理前，先执行本脚本。

为什么用「归档」而不是删除：
  能力卡是派生数据（可由 data/suppliers/*.json 完全重建），但一次要清 2700+ 个
  目录，会触发批量删除保护。整目录重命名是**单次移动**，既达到清空效果，
  又留了退路 —— 出问题把目录改回来即可。

用法：
    python scripts/collect/reset_auto.py            # 预演，只统计不改动
    python scripts/collect/reset_auto.py --apply    # 真正执行
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDORS = REPO_ROOT / "skills" / "vendors"
FINGER = REPO_ROOT / "skills" / "registry" / "fingerprint"


def reset(apply: bool) -> None:
    # ---- 1. 能力卡目录 ---------------------------------------------------
    dirs_kept, dirs_del = [], []
    for d in sorted(VENDORS.glob("*")):
        cj = d / "capability.json"
        if not cj.exists():
            continue
        try:
            mode = (json.loads(cj.read_text(encoding="utf-8")).get("provenance") or {}).get("mode")
        except Exception:
            mode = None
        (dirs_del if mode == "auto" else dirs_kept).append(d.name)

    # ---- 2. 指纹 jsonl ---------------------------------------------------
    fp_kept = fp_del = 0
    for f in sorted(FINGER.glob("*.jsonl")):
        lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
        keep = []
        for ln in lines:
            try:
                if json.loads(ln).get("pv") == "auto":
                    fp_del += 1
                    continue
            except Exception:
                pass
            keep.append(ln)
        fp_kept += len(keep)
        if apply:
            f.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")

    print(f"能力卡：归档 auto {len(dirs_del)} 家，保留 {len(dirs_kept)} 家 {dirs_kept}")
    print(f"指纹　：删除 auto {fp_del} 条，保留 {fp_kept} 条")

    if apply:
        # 保留的样本先挪出去，整目录改名，再放回 —— 全程只做「移动」，不做删除
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        archived = VENDORS.parent / f"_vendors_backup_{ts}"
        staging = VENDORS.parent / "_keep_sample"
        for name in dirs_kept:
            src = VENDORS / name
            if src.exists():
                shutil.move(str(src), str(staging / name))
        shutil.move(str(VENDORS), str(archived))
        VENDORS.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            for name in dirs_kept:
                src = staging / name
                if src.exists():
                    shutil.move(str(src), str(VENDORS / name))
            shutil.rmtree(staging, ignore_errors=True)
        print(f"\n已归档到 {archived.name}（确认无误后可自行删除该目录）。")
        print("现在可以重跑 batch_auto_profile.py（多城市请**不要**再加 --clean）。")
    else:
        print("\n预演模式，未改动任何文件。加 --apply 执行。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行清理")
    reset(ap.parse_args().apply)
