#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存量去重清洗 —— 合并 data/gb 里「同一家企业被分配了多个编号」的记录。

背景（2026-09-19 数据库质量检查）
--------------------------------
全库 127,738 条里确认：
  · 同一 amap.poi_id 有多个 CN-MFG 编号：851 组 / 1,707 条 → 冗余 856 条
  · 归一化名称 + 地址完全相同（含 poi_id 不同的情形）：1,265 组 → 冗余 1,299 条
  · 另有 4,873 组同名不同址是**连锁门店**，合法不同实体，绝不合并

成因是 `save_suppliers()` 在分配 id 之前没有登记判重索引（已在 P0 修复），
但**已入库的存量重复不会自己消失**，本脚本负责清洗它们。

安全设计
--------
- 默认 `--dry-run`：只报告不落盘，先看清单再决定。
- `--apply` 前自动把 data/gb 整体备份到 .backup_dedup_<时间戳>/。
- 合并是「只增不减」：保留信息最全的那条，把同组其它记录的关键词与缺失字段并进去，
  已有字段不被覆盖同其余组的较弱证据改写。
- 被删掉的 id 会写进 `data/_dedup_removed_ids.json`，供 `--clean-en` 同步清理
  英文镜像里的孤儿记录（英文按 id 镜像，不同步删会留下孤儿）。

用法
----
    python scripts/dedup_gb.py                    # 干跑，看清单
    python scripts/dedup_gb.py --apply            # 备份 + 真合并
    python scripts/dedup_gb.py --clean-en         # 清理英文孤儿（配合上面）
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gb_store  # noqa: E402


# ────────────────────────────────────────────── 分组（并查集，两个键可传递）
class DSU:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _real_phone(rec) -> bool:
    p = re.sub(r"\D", "", str(rec.get("contact_phone") or ""))
    return len(p) >= 7 and "待核实" not in str(rec.get("contact_phone") or "")


def _richness(rec) -> tuple:
    """信息完整度打分，用于挑选「留下哪条」。越大越该留。"""
    return (
        1 if _real_phone(rec) else 0,
        1 if (rec.get("lat") and rec.get("lng")) else 0,
        1 if (rec.get("amap") or {}).get("type") else 0,
        1 if (rec.get("amap") or {}).get("poi_id") else 0,
        1 if rec.get("source_url") else 0,
        1 if rec.get("status") == "verified" else 0,
        len(rec.get("keywords") or []),
    )


def _idnum(rec) -> int:
    m = re.match(r"CN-MFG-(\d+)", rec.get("id") or "")
    return int(m.group(1)) if m else 1 << 30


def _merge_group(rows: list[dict]) -> tuple[dict, list[dict]]:
    """把一组重复记录合并成一条。返回 (留下的记录, 被移除的记录)。

    留下：信息最完整的一条；同分则取编号最小（历史最久、被引用最多）的那条。
    """
    ordered = sorted(rows, key=lambda r: (-_richness(r)[0], -_richness(r)[1],
                                          -_richness(r)[2], -_richness(r)[3],
                                          -_richness(r)[4], -_richness(r)[5],
                                          -_richness(r)[6], _idnum(r)))
    keep = ordered[0]
    dropped = ordered[1:]

    # 关键词并集（保序）
    kws = list(keep.get("keywords") or [])
    for d in dropped:
        for k in (d.get("keywords") or []):
            if k not in kws:
                kws.append(k)
    keep["keywords"] = kws

    for d in dropped:
        # 电话：只填空，不覆盖
        if not _real_phone(keep) and _real_phone(d):
            keep["contact_phone"] = d.get("contact_phone")
            keep["status"] = d.get("status", keep.get("status"))
            keep["is_template"] = keep.get("status") != "verified"
        # 经纬/地址：只补缺失
        if not keep.get("lat") and d.get("lat"):
            keep["lat"], keep["lng"] = d.get("lat"), d.get("lng")
        if not keep.get("address") and d.get("address"):
            keep["address"] = d.get("address")
        # amap 扩展字段：逐字段补全
        ka = keep.setdefault("amap", {}) or {}
        keep["amap"] = ka
        for k, v in (d.get("amap") or {}).items():
            if not ka.get(k) and v:
                ka[k] = v
        if not keep.get("source_url") and d.get("source_url"):
            keep["source_url"] = d.get("source_url")
    return keep, dropped


def scan_plan():
    """扫全库，产出去重计划。返回 (记录, 重复组, 幽灵id, poi判定对数)。

    全库存在两类重复，必须分开处理：
      A. **幽灵数据** —— 同一个 id 同时躺在 `_unclassified.json` 和已归类桶里
         （GB 码补出来了却没从 _unclassified 摘掉）。实测 169 条。
         它会让下游按文件遍历时把一条记录数两遍（127738 vs 唯一 id 127569）。
      B. **真重复** —— 同一家企业两个不同编号（球UnionFind 判得出来的那些）。
    """
    raw: list[tuple[str, dict]] = []
    for bucket, _ in gb_store.iter_buckets():
        for r in gb_store.load_bucket(bucket):
            raw.append((bucket, r))

    # ---- A. 幽灵数据：同一 id 跨桶重复，按「已归类优先」裁决保留哪个位置
    by_id: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for item in raw:
        sid = item[1].get("id")
        if sid:
            by_id[sid].append(item)

    ghost_plan: list[tuple[str, str, str]] = []   # (id, 保留桶, 删除桶)
    keep_of: dict[str, str] = {}
    for sid, items in by_id.items():
        if len(items) < 2:
            continue

        def _specificity(b):
            # 归到具体小类 > 具体中类 > _partial > _unclassified
            if b == "_unclassified":
                return 0
            if "_partial" in b:
                return 1
            return 2 + len(b.split("/"))

        items_sorted = sorted(items, key=lambda it: -_specificity(it[0]))
        keep_bucket = items_sorted[0][0]
        keep_of[sid] = keep_bucket
        for b, _ in items_sorted:
            if b != keep_bucket:
                ghost_plan.append((sid, keep_bucket, b))

    all_rows = [it for it in raw if keep_of.get(it[1].get("id"), it[0]) == it[0]]

    # ---- B. 真重复：poi_id 与 (城市, 归一化名, 地址) 双键并查集
    dsu = DSU()
    by_poi: dict[str, int] = {}
    by_key: dict[tuple, int] = {}
    dup_pairs = 0
    for i, (_, r) in enumerate(all_rows):
        poi = (r.get("amap") or {}).get("poi_id") or ""
        city = (r.get("region") or {}).get("city") or ""
        n = gb_store.norm_name(r.get("company"))
        a = gb_store.norm_addr(r.get("address"))
        if poi:
            if poi in by_poi:
                dsu.union(by_poi[poi], i)
                dup_pairs += 1
            else:
                by_poi[poi] = i
        if n and a:
            k = (city, n, a)
            if k in by_key:
                dsu.union(by_key[k], i)
            else:
                by_key[k] = i

    groups: dict[int, list[tuple[str, dict]]] = defaultdict(list)
    for i, item in enumerate(all_rows):
        groups[dsu.find(i)].append(item)

    dup_groups = {g: items for g, items in groups.items() if len(items) > 1}
    return all_rows, dup_groups, dup_pairs, ghost_plan


def main():
    ap = argparse.ArgumentParser(description="存量去重清洗（合并同一企业的多个编号）")
    ap.add_argument("--apply", action="store_true", help="真正落盘（默认只报告）")
    ap.add_argument("--clean-en", action="store_true",
                    help="按 _dedup_removed_ids.json 清理英文镜像里的孤儿记录")
    ap.add_argument("--dedupe-en", action="store_true",
                    help="修复英文镜像自身的跨分片重复 id（合并后按 MAX_PER_FILE 重写分片）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 组（调试用）")
    args = ap.parse_args()

    if args.clean_en:
        return clean_en()
    if args.dedupe_en:
        return dedupe_en()

    all_rows, dup_groups, dup_pairs, ghost_plan = scan_plan()
    print("=" * 76)
    print("存量去重%s" % ("（落盘模式）" if args.apply else "（干跑模式，未改动任何文件）"))
    print("=" * 76)
    print("磁盘条目总数      : %d" % (len(all_rows) + len(ghost_plan)))
    print("A. 幽灵数据        : %d 条（同一 id 同时躺在 _unclassified 与已归类桶）"
          % len(ghost_plan))
    print("去重后唯一记录    : %d（与 DATA_STATS 的「中文记录总数」口径一致）"
          % len(all_rows))
    print("-" * 76)
    print("B. 重复组数        : %d" % len(dup_groups))
    dup_recs = sum(len(v) for v in dup_groups.values())
    print("涉及记录          : %d" % dup_recs)
    print("冗余记录（将删除）: %d  (%.2f%%)" % (dup_recs - len(dup_groups),
                                              100.0 * (dup_recs - len(dup_groups)) / max(1, len(all_rows))))
    print("其中按 poi_id 判定: %d 对" % dup_pairs)

    print("\n--- 重复最多 Top15 ---")
    for g, items in sorted(dup_groups.items(), key=lambda kv: -len(kv[1]))[:15]:
        city = (items[0][1].get("region") or {}).get("city") or ""
        name = items[0][1].get("company") or ""
        ids = " ".join(sorted(r.get("id", "?") for _, r in items)[:5])
        print("  [%s] %-26s x%-3d ids: %s" % (city, name[:26], len(items), ids))

    plan = list(dup_groups.items())
    if args.limit:
        plan = plan[:args.limit]

    # 逐组合并，收集「即将被移除的 id」与「受影响的桶」
    removed_ids: list[str] = []
    affected: set[str] = set()
    for _g, items in plan:
        _keep, dropped = _merge_group([r for _, r in items])
        removed_ids.extend(d.get("id") for d in dropped if d.get("id"))
        affected.update(bucket for bucket, _r in items)

    if not args.apply:
        print("\n干跑结束。加 --apply 执行（会先备份 data/gb）。")
        return 0

    # ── 步骤 1：删掉幽灵副本（同一 id 在次要位置的那份）
    ghost_map: dict[str, set[str]] = defaultdict(set)   # 桶 -> 要删的 id
    for _sid, _keep_bucket, drop_bucket in ghost_plan:
        ghost_map[drop_bucket].add(_sid)

    drop_set = set(removed_ids)
    touched_buckets: dict[str, list[dict]] = {}
    before_counts: dict[str, int] = {}
    for bucket in affected | set(ghost_map):
        rows = gb_store.load_bucket(bucket)
        before_counts[bucket] = len(rows)
        new_rows = [r for r in rows
                    if r.get("id") not in drop_set
                    and r.get("id") not in ghost_map.get(bucket, set())]
        touched_buckets[bucket] = new_rows

    # ── 步骤 2：把合并结果写回保留下来的那条记录
    for g, items in plan:
        rows = [r for _, r in items]
        keep, _dropped = _merge_group(rows)
        for bucket, _r in items:
            lst = touched_buckets.get(bucket)
            if not lst:
                continue
            for idx, x in enumerate(lst):
                if x.get("id") == keep.get("id"):
                    lst[idx] = keep
                    break

    for bucket, rows in touched_buckets.items():
        if rows is not None:
            gb_store.save_bucket(bucket, rows)

    gb_store.invalidate_cache()
    out = ROOT / "data" / "_dedup_removed_ids.json"
    out.write_text(json.dumps(sorted(set(removed_ids)), ensure_ascii=False, indent=0),
                   encoding="utf-8")
    changed = sum(1 for b, rows in touched_buckets.items()
                  if len(rows) != before_counts.get(b, len(rows)))
    print("\n落盘完成：")
    print("  · 移除幽灵副本 %d 条" % len(ghost_plan))
    print("  · 合并重复记录，移除冗余 %d 条" % len(set(removed_ids)))
    print("  · 涉及 %d 个桶" % changed)
    print("被合并掉的 id 已写入 %s" % out.relative_to(ROOT))
    print("英文镜像孤儿请再跑：python scripts/dedup_gb.py --clean-en")
    return 0


def backup_gb() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = ROOT / (".backup_dedup_%s" % ts)
    src = ROOT / "data" / "gb"
    shutil.copytree(src, dst / "gb")
    print("已备份 data/gb → %s" % dst.relative_to(ROOT))
    return dst


def dedupe_en() -> int:
    """修英文镜像自身的重复 id。

    根因：`en_backfill.flush()` 曾把整桶写进主文件后删 -pN 分片，而流水线其余环节
    （gb_store / en_sync_industry）都按 MAX_PER_FILE 分片。两套布局并存 →
    主文件与 -p2 各持有一批相同 id，实测 15,989 个重复 id。

    修复后仍可能复发 —— 复发的前提是「两个写入方布局不一致」，所以同时必须改
    `en_backfill.flush()` 用同一套分片（已改，2026-09-19）。本函数负责清存量。
    """
    import en_backfill as EB

    en_root = ROOT / "data" / "en" / "gb"
    buckets: dict[str, list[Path]] = defaultdict(list)
    for f in sorted(en_root.rglob("*.json")):
        if f.suffix != ".json":
            continue
        rel = f.relative_to(en_root).with_suffix("").as_posix()
        # 连续分片后缀（历史上出现过 6210-p2-p2）要一次性剥干净，
        # 否则会被当成另一个独立桶，同一批 id 分散在两组里照样去重不掉。
        buckets[re.sub(r"(-p\d+)+$", "", rel)].append(f)

    total_before = processed = fixed = 0
    for bucket, files in sorted(buckets.items()):
        merged: dict[str, dict] = {}
        for f in files:
            try:
                rows = json.load(open(f, encoding="utf-8"))
            except Exception as e:
                print("  !! %s 解析失败，跳过：%r" % (f.name, e))
                continue
            if not isinstance(rows, list):
                continue
            total_before += len(rows)
            for r in rows:
                if isinstance(r, dict) and r.get("id") and r["id"] not in merged:
                    merged[r["id"]] = r
        before = 0
        for f in files:
            if f.exists():
                try:
                    before += len(json.load(open(f, encoding="utf-8")))
                except Exception:
                    pass
        if before == len(merged):
            continue
        processed += 1
        fixed += before - len(merged)
        recs = sorted(merged.values(), key=lambda r: r.get("id") or "")
        EB._write_en_sharded(en_root / (bucket + ".json"), recs)

    print("英文去重：%d 个桶存在跨分片重复，合并掉冗余 %d 条" % (processed, fixed))
    return 0


def clean_en() -> int:
    ids_file = ROOT / "data" / "_dedup_removed_ids.json"
    if not ids_file.exists():
        print("找不到 %s，请先跑 --apply" % ids_file.name)
        return 1
    drop = set(json.loads(ids_file.read_text(encoding="utf-8")))
    en_root = ROOT / "data" / "en" / "gb"
    removed = scanned = 0
    for f in sorted(en_root.rglob("*.json")):
        rows = json.load(open(f, encoding="utf-8"))
        if not isinstance(rows, list):
            continue
        scanned += len(rows)
        new_rows = [r for r in rows if r.get("id") not in drop]
        if len(new_rows) != len(rows):
            f.write_text(json.dumps(new_rows, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            removed += len(rows) - len(new_rows)
    print("英文镜像：扫描 %d 条，移除孤儿 %d 条" % (scanned, removed))
    return 0


if __name__ == "__main__":
    if "--apply" in sys.argv:
        backup_gb()
    sys.exit(main())
