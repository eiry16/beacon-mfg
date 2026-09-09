#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_gb_code.py —— 给 L1 能力卡回写国标码，让 625 家「品类冲突」自然消解。

## 为什么要这么改（而不是「以名录为准批量重划」）

名录的 `category` 和能力卡的 `category` **是同一套旧 8 品类标签**
（精密机械加工 / 钣金冲压 / 注塑成型 / 表面处理 / 标准件 / 电子元器件 /
原材料 / 压铸），不是「国标装不下 8 品类」。625 家冲突 = 同一套标签、
两个来源给了不同答案（占 4136 张卡的 15.1%）。

以公司名为裁判（项目红线：工艺只信公司名）实测：

    能力卡更准   164 家  26.2%
    名录更准     106 家  17.0%
    两边都命中   132 家  21.1%
    公司名无信号  223 家  35.7%

→ 以名录为准重划，会在 26.2% 上用更差的标签覆盖更好的，只改善 17.0%，
  **净负收益**。典型会改错的：毅泰模具压铸（名录=原材料 / 卡=压铸）。

## 做法：不二选一

1. 能力卡新增 `gb_code` / `gb_name` / `gb_path`，从名录 `industry` 同步。
   **`category` / `profile` 一个字都不动** —— 它是采购侧的展示标签，
   不是归档依据。
2. 检索、分片、CDN 路径一律走 `gb_code`（本来就已经这么做了，
   这里只是让能力卡自包含，下游 Agent 不用再 join 名录）。
3. 8 品类降级为「展示 / 采购标签」，不再承担归档职责。冲突自然消解。
4. 名录缺 `industry` 的（能力卡里 11 家）：`gb_code` 留 null，
   **不猜、不填兜底值**（项目红线：数据宁可留空也不编造）。

## 用法

    python scripts/sync_gb_code.py            # 预览，不落盘
    python scripts/sync_gb_code.py --apply    # 写回能力卡 + 更新冲突清单

改的是 `skills/registry/capability/*.json`（在 git 里，可 `git diff` 复核、
可 `git checkout` 回滚）。
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gb_store  # noqa: E402

CAP_DIR = ROOT / "skills" / "registry" / "capability"
CONFLICT_FILE = ROOT / "skills" / "registry" / "category-conflicts.json"

# 能力卡里 gb_* 字段的插入位置：紧跟 updated_at，读起来是一组元信息
ANCHOR = "updated_at"


def load_directory():
    """名录 id -> (gb_code, gb_name, gb_path, category)"""
    out = {}
    for rec in gb_store.load_all(with_bucket=True):
        ind = rec.get("industry") or {}
        out[rec["id"]] = (
            ind.get("code") or None,
            ind.get("name") or None,
            ind.get("path") or None,
            rec.get("category"),
        )
    return out


def read_card(p: Path):
    """读能力卡。损坏/空文件返回 None 而不是抛异常——4136 个文件里
    只要有 1 个坏文件就让整轮迁移崩掉，代价太大。"""
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_card(p: Path, d: dict):
    """原子写：先写 .tmp 再 os.replace。

    直接 write_text 时若进程被中断（Ctrl-C / 超时 SIGTERM），会留下半截
    甚至空文件，下一轮 load 直接崩。os.replace 在同分区是原子操作，
    要么旧文件、要么新文件，不会出现中间态。"""
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def inject_gb(card: dict, gb: tuple) -> bool:
    """把 gb_code/gb_name/gb_path 插进能力卡。返回是否发生变化。"""
    code, name, path = gb[0], gb[1], gb[2]
    if card.get("gb_code") == code and card.get("gb_name") == name:
        return False
    # 按 ANCHOR 顺序重建字典，保持字段可读性（JSON 是有序的）
    rebuilt = {}
    for k, v in card.items():
        rebuilt[k] = v
        if k == ANCHOR:
            rebuilt["gb_code"] = code
            rebuilt["gb_name"] = name
            rebuilt["gb_path"] = path
    if "gb_code" not in rebuilt:  # 没有 anchor，追加到末尾
        rebuilt["gb_code"] = code
        rebuilt["gb_name"] = name
        rebuilt["gb_path"] = path
    card.clear()
    card.update(rebuilt)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写回能力卡（默认只预览）")
    args = ap.parse_args()

    directory = load_directory()
    cards = {}
    broken = []
    for p in sorted(CAP_DIR.glob("*.json")):
        d = read_card(p)
        if d is None:
            broken.append(p.name)
            continue
        cards[d["supplier_id"]] = (p, d)

    print(f"名录 {len(directory)} 家 · 能力卡 {len(cards)} 张")
    if broken:
        print(f"\n⚠ 跳过 {len(broken)} 个损坏文件（需先从 CDN 恢复）：")
        for b in broken[:10]:
            print(f"    {b}")
        print("  恢复：从 https://beacon-mfg.pages.dev/manifest.json 的分片里找回")
        print("  （能力卡目录被 .gitignore 排除，git 里没有备份）")
    print()

    changed = 0
    no_gb = []
    conflict_gb_hit = 0
    for sid, (p, d) in cards.items():
        gb = directory.get(sid)
        if gb is None:
            continue
        code = gb[0]
        if code is None:
            no_gb.append((sid, d.get("company", "")))
        # 只读判断，绝不在这里改 d：inject_gb 会原地修改对象，
        # 一旦预览阶段改了，--apply 阶段再判断就成了「无变化」，整轮静默跳过写盘。
        if d.get("gb_code") != code or d.get("gb_name") != gb[1]:
            changed += 1

    # 冲突清单：补上国标码，并改写定性说明
    conflict = json.loads(CONFLICT_FILE.read_text(encoding="utf-8"))
    for item in conflict["items"]:
        gb = directory.get(item["id"])
        if gb and gb[0]:
            item["gb_code"] = gb[0]
            item["gb_name"] = gb[1]
            conflict_gb_hit += 1

    print(f"将变更能力卡：{changed} 张")
    print(f"名录缺 industry.code（gb_code 留 null）：{len(no_gb)} 家")
    for sid, name in no_gb[:5]:
        print(f"    {sid}  {name[:30]}")
    print(f"\n冲突清单 {len(conflict['items'])} 条，补上国标码：{conflict_gb_hit} 条")

    if not args.apply:
        print("\n（预览模式，未落盘。加 --apply 执行）")
        return

    for sid, (p, d) in cards.items():
        if inject_gb(d, directory.get(sid) or (None, None, None, None)):
            write_card(p, d)

    conflict["note"] = (
        "已定性（2026-09-09）：名录 category 与能力卡 category 是同一套旧 8 品类标签，"
        "两边口径不同，不是错配，也不是国标装不下。"
        "决定：8 品类降级为展示/采购标签，检索与分片一律走 gb_code（国标）；"
        "能力卡 category 保持不动，仅新增 gb_code/gb_name/gb_path。"
        "以公司名为裁判实测「以名录为准重划」净负收益（改错 26.2% vs 改对 17.0%），故不重划。"
        "本清单保留供人工复核，不阻断任何检索路径。"
    )
    conflict["resolved_by"] = "gb_code"
    CONFLICT_FILE.write_text(
        json.dumps(conflict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\n已写回 {changed} 张能力卡，冲突清单已更新。")
    print(f"下一步：python scripts/gen_capability_shards.py --apply && "
          f"python scripts/deploy_pages.py --apply")


if __name__ == "__main__":
    main()
