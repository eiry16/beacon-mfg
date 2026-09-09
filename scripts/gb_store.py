#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国标四级归档的统一访问层（data/gb/）

为什么要有这一层
----------------
2026-09-08 归档结构从「8 个品类文件」切到「国标四级目录」后，
原先散落在 query.py / validate.py / fetch_batch.py / supplier_loader /
certify_writeback / en_backfill 等十几个脚本里的路径逻辑
（`data/suppliers/{品类}.json`）全部失效。

如果逐个脚本各写一遍目录推算，将来再改结构会重演一遍同样的灾难。
所以路径与落位规则只在这里定义一次，业务脚本一律调本模块。

归档布局
--------
    data/gb/{gate}/{div}/{class}.json     4 位小类码，如 C/34/3434.json
    data/gb/{gate}/{div}/{group}.json     3 位中类码（与小类同目录，位数区分）
    data/gb/{gate}/{div}/_partial.json    2 位大类码，未细分
    data/gb/_unclassified.json            无国标码，一条不丢
    data/gb-index.json                    四级层级树 + 各级计数
    data/gb-alias.json                    采购词 → 国标码（由真实数据推导）

设计原则
--------
- **精确到哪一级就放哪一级**：宁可粗（放到大类 _partial）不可错（乱猜小类）。
- **无码不猜**：解析不出国标码就进 _unclassified，绝不按关键词硬贴。
- **写操作先找旧位置**：upsert 时若记录的国标码变了，必须把它从旧桶里删掉，
  否则同一 id 会在两个小类下各留一份（归档重构最容易出现幽灵数据）。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

ROOT = Path(__file__).resolve().parent.parent
GB_DIR = ROOT / "data" / "gb"
GB_INDEX = ROOT / "data" / "gb-index.json"
GB_ALIAS = ROOT / "data" / "gb-alias.json"
GB_FULL = ROOT / "data" / "gb4754-full.json"

UNCLASSIFIED = "_unclassified"
PARTIAL = "_partial"

# 单个归档文件的最大条数。超过就自动切成 {小类}-p2.json / -p3.json ...
# 为什么要限：3484 机械零部件加工现在已经 3415 条 / 4 MB，到千万级会是 2 GB 的
# 单个 JSON —— 客户端无法按需下载，git pack/diff/PR 也全部失效。
# 分片后"小类"这个逻辑桶不变（仍是 C/34/3484），只是落在多个文件里，
# 上层调用方通过 load_bucket() 无感知。
MAX_PER_FILE = int(os.environ.get("GB_MAX_PER_FILE", "5000"))
_SHARD_RE = re.compile(r"-p\d+$")

# 桶键 → 落盘路径。桶键形如 "C/34/3434" / "C/34/343" / "C/34/_partial" / "_unclassified"


# ─── 国标码表（懒加载，读一次缓存） ──────────────────────────────────────────
_TAX: dict[str, dict] | None = None


def _taxonomy() -> dict[str, dict]:
    """读 data/gb4754-full.json 的四级码表。失败时返回空表（调用方降级）。"""
    global _TAX
    if _TAX is None:
        try:
            with open(GB_FULL, encoding="utf-8") as f:
                d = json.load(f)
            _TAX = {
                "gates": d.get("gates") or {},
                "divisions": d.get("divisions") or {},
                "groups": d.get("groups") or {},
                "classes": d.get("classes") or {},
            }
        except Exception:
            _TAX = {"gates": {}, "divisions": {}, "groups": {}, "classes": {}}
    return _TAX


def resolve(code: str | None) -> tuple[str, str, str | None, str] | None:
    """国标码 → (gate, div, group, level)。按码长分级，精确到哪级算哪级。

    4 位 → class（小类）   3 位 → group（中类）   2 位 → division（大类）
    解析不出返回 None —— 调用方应落 _unclassified，不要猜。
    """
    if not code:
        return None
    t = _taxonomy()
    classes, groups, divisions = t["classes"], t["groups"], t["divisions"]
    code = str(code).strip()

    if len(code) >= 4 and code in classes:
        c = classes[code]
        g = groups.get(c.get("group"))
        dv = divisions.get(g.get("div")) if g else None
        if dv:
            return dv["gate"], g["div"], c["group"], "class"
    if len(code) == 3 and code in groups:
        g = groups[code]
        dv = divisions.get(g.get("div"))
        if dv:
            return dv["gate"], g["div"], code, "group"
    if len(code) == 2 and code in divisions:
        dv = divisions[code]
        return dv["gate"], code, None, "division"
    return None


def bucket_of(record: dict[str, Any]) -> str:
    """一条记录 → 桶键。归档与检索都用它，保证口径一致。"""
    ind = record.get("industry") or record.get("industry_en") or {}
    return bucket_of_code(ind.get("code"))


def bucket_of_code(code: str | None) -> str:
    loc = resolve(code)
    if not loc:
        return UNCLASSIFIED
    gate, div, grp, level = loc
    if level == "class":
        return "%s/%s/%s" % (gate, div, code)
    if level == "group":
        return "%s/%s/%s" % (gate, div, code)
    return "%s/%s/%s" % (gate, div, PARTIAL)


def path_of_bucket(bucket: str) -> Path:
    parts = bucket.split("/")
    if len(parts) == 3:
        return GB_DIR / parts[0] / parts[1] / ("%s.json" % parts[2])
    return GB_DIR / ("%s.json" % bucket)


def path_of_code(code: str | None) -> Path:
    return path_of_bucket(bucket_of_code(code))


def base_of_stem(stem: str) -> str:
    """文件名（不含扩展）→ 逻辑桶基名：3484-p2 → 3484。"""
    return _SHARD_RE.sub("", stem)


def shard_paths(bucket: str) -> list[Path]:
    """一个逻辑桶当前占用的全部文件（主文件 + -pN 分片），按序排列。"""
    primary = path_of_bucket(bucket)
    out: list[Path] = []
    if primary.exists():
        out.append(primary)
    for f in sorted(primary.parent.glob("%s-p*.json" % primary.stem)):
        if f != primary:
            out.append(f)
    return out


# ─── 读 ────────────────────────────────────────────────────────────────────
def iter_buckets() -> Iterator[tuple[str, Path]]:
    """遍历所有归档文件，产出 (桶键, 路径)。按目录序稳定输出。"""
    if not GB_DIR.exists():
        return
    p = GB_DIR / ("%s.json" % UNCLASSIFIED)
    if p.exists():
        yield UNCLASSIFIED, p
    seen: set[str] = set()
    for gate_dir in sorted(d for d in GB_DIR.iterdir() if d.is_dir()):
        for div_dir in sorted(d for d in gate_dir.iterdir() if d.is_dir()):
            for f in sorted(div_dir.glob("*.json")):
                stem = base_of_stem(f.stem)
                bucket = "%s/%s/%s" % (gate_dir.name, div_dir.name, stem)
                if bucket in seen:
                    continue      # 同一逻辑桶的 -pN 分片不重复产出
                seen.add(bucket)
                yield bucket, path_of_bucket(bucket)


def load_bucket(bucket: str) -> list[dict[str, Any]]:
    """读一个逻辑桶。跨 -pN 分片自动合并，调用方无需知道分了几片。"""
    out: list[dict[str, Any]] = []
    for p in shard_paths(bucket):
        with open(p, encoding="utf-8") as f:
            out.extend(json.load(f))
    return out


def load_all(with_bucket: bool = False) -> list[dict[str, Any]]:
    """读全库。with_bucket=True 时给每条记录挂 `_bucket`（所属归档桶）。"""
    out: list[dict[str, Any]] = []
    for bucket, _ in iter_buckets():
        for r in load_bucket(bucket):
            if with_bucket:
                r["_bucket"] = bucket
            out.append(r)
    return out


def stats() -> dict[str, Any]:
    """全库统计：总条数、各层计数。用于守恒校验。"""
    total = 0
    per_bucket: dict[str, int] = {}
    for bucket, _ in iter_buckets():
        n = len(load_bucket(bucket))
        per_bucket[bucket] = n
        total += n
    return {"total": total, "buckets": len(per_bucket), "per_bucket": per_bucket}


# ─── 写 ────────────────────────────────────────────────────────────────────
def _write_file(path: Path, records: list[dict[str, Any]]) -> None:
    """原子写单个文件：先写临时文件再替换，避免中途崩溃留下半截 JSON。"""
    if not records:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    try:
        # newline 默认 → Windows 下 \n 转 \r\n，与既有文件保持一致
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _write_bucket(bucket: str, records: list[dict[str, Any]]) -> None:
    """写一个逻辑桶。超过 MAX_PER_FILE 自动分片，分不完的旧分片自动删除。

    桶空了就把文件全删掉 —— 跨类迁移后原小类可能一条不剩，
    留一堆 `[]` 会让归档目录慢慢堆满空文件，也会让"这个小类有货"产生误判。
    """
    primary = path_of_bucket(bucket)
    if not records:
        for f in shard_paths(bucket):
            f.unlink()
        if primary.exists():
            primary.unlink()
        return

    primary.parent.mkdir(parents=True, exist_ok=True)
    chunks = [records[i:i + MAX_PER_FILE]
              for i in range(0, len(records), MAX_PER_FILE)]
    written: set[Path] = set()
    for i, chunk in enumerate(chunks):
        target = primary if i == 0 else primary.parent / (
            "%s-p%d.json" % (primary.stem, i + 1))
        _write_file(target, chunk)
        written.add(target)
    for f in shard_paths(bucket):
        if f not in written:
            f.unlink()


def upsert(records: Iterable[dict[str, Any]],
           on_move: Callable[[str, str, str], None] | None = None) -> dict[str, int]:
    """把记录按当前国标码写回归档。已存在同 id 的就地更新，跨桶移动的自动从旧桶删除。

    返回 {"added": n, "updated": n, "moved": n}。
    on_move(id, old_bucket, new_bucket) 可选回调，用于记录迁移日志。
    """
    incoming = list(records)
    if not incoming:
        return {"added": 0, "updated": 0, "moved": 0}

    # 1. 扫全库定位每条 id 当前在哪（一次遍历，避免逐条 grep 文件）
    old_loc: dict[str, tuple[str, int]] = {}
    buckets: dict[str, list[dict[str, Any]]] = {}
    for bucket, _ in iter_buckets():
        rows = load_bucket(bucket)
        buckets[bucket] = rows
        for i, r in enumerate(rows):
            sid = r.get("id")
            if sid:
                old_loc[sid] = (bucket, i)

    # 2. 计算新归属
    added = updated = moved = 0
    touched: set[str] = set()
    for rec in incoming:
        sid = rec.get("id")
        new_bucket = bucket_of(rec)
        touched.add(new_bucket)
        if not sid:
            buckets.setdefault(new_bucket, []).append(rec)
            added += 1
            continue

        prev = old_loc.get(sid)
        if prev is None:
            buckets.setdefault(new_bucket, []).append(rec)
            added += 1
            continue

        old_bucket, idx = prev
        if old_bucket == new_bucket:
            buckets[old_bucket][idx] = rec          # 原地更新
            updated += 1
        else:
            buckets[old_bucket][idx] = None         # 标记删除，稍后清理
            buckets.setdefault(new_bucket, []).append(rec)
            moved += 1
            touched.add(old_bucket)
            if on_move:
                on_move(sid, old_bucket, new_bucket)

    # 3. 清理被移走的空位并落盘
    for bucket in touched:
        rows = [r for r in buckets.get(bucket, []) if r is not None]
        _write_bucket(bucket, rows)

    return {"added": added, "updated": updated, "moved": moved}


def rebuild_index() -> dict[str, Any]:
    """重建 data/gb-index.json（四级树 + 各级计数）。写完返回 metadata。"""
    t = _taxonomy()
    gates, divisions, groups, classes = (
        t["gates"], t["divisions"], t["groups"], t["classes"])
    tree: dict[str, Any] = {}
    total = classified = 0

    for bucket, _ in iter_buckets():
        rows = load_bucket(bucket)
        n = len(rows)
        total += n
        if bucket == UNCLASSIFIED:
            continue

        parts = bucket.split("/")
        gate, div = parts[0], parts[1]
        classified += n
        g_node = tree.setdefault(
            gate, {"name": gates.get(gate, ""), "count": 0, "divisions": {}})
        d_node = g_node["divisions"].setdefault(
            div, {"name": divisions.get(div, {}).get("name", ""),
                  "count": 0, "direct": 0, "groups": {}})
        g_node["count"] += n
        d_node["count"] += n

        if len(parts) == 3 and parts[2] == PARTIAL:
            d_node["direct"] += n
            continue

        code = parts[2]
        if len(code) == 4:
            c = classes.get(code) or {}
            grp = c.get("group") or code[:3]
            m_node = d_node["groups"].setdefault(
                grp, {"name": groups.get(grp, {}).get("name", ""),
                      "count": 0, "direct": 0, "classes": {}})
            m_node["count"] += n
            m_node["classes"][code] = {"name": c.get("name", ""), "count": n}
        else:  # 3 位中类
            m_node = d_node["groups"].setdefault(
                code, {"name": groups.get(code, {}).get("name", ""),
                       "count": 0, "direct": 0, "classes": {}})
            m_node["count"] += n
            m_node["direct"] += n

    meta = {
        "standard": "GB/T 4754-2017 国民经济行业分类（含 2019 年第 1 号修改单）",
        "description": "厂商数据库按国标四级归档索引。门类 1 位字母 / 大类 2 位 / "
                       "中类 3 位 / 小类 4 位。客户 Agent 可按任意层级检索。",
        "generated_at": _today(),
        "counts": {
            "gates_in_standard": 20, "divisions_in_standard": 97,
            "groups_in_standard": 473, "classes_in_standard": 1382,
            "gates_used": len(tree),
            "divisions_used": sum(len(v["divisions"]) for v in tree.values()),
            "groups_used": sum(len(d["groups"])
                               for v in tree.values() for d in v["divisions"].values()),
            "classes_used": sum(len(m["classes"])
                                for v in tree.values()
                                for d in v["divisions"].values()
                                for m in d["groups"].values()),
        },
        "total_suppliers": total,
        "classified": classified,
        "unclassified": total - classified,
        "layout": "data/gb/{gate}/{division}/{class}.json，无码记录见 _unclassified.json",
    }
    with open(GB_INDEX, "w", encoding="utf-8") as f:
        json.dump({"metadata": meta, "tree": tree}, f, ensure_ascii=False, indent=2)
    return meta


def rebuild_alias(min_hits: int = 3, max_codes: int = 6) -> dict[str, Any]:
    """重建 data/gb-alias.json：采购词 → 国标小类。

    **不手写**：统计每个关键词实际出现在哪些小类下，出现 >= min_hits 次才收录。
    这样每条别名都有数据支撑，不是拍脑袋编的映射。
    """
    kw_by_code: dict[str, Counter] = {}
    for bucket, path in iter_buckets():
        if bucket == UNCLASSIFIED:
            continue
        parts = bucket.split("/")
        if len(parts) != 3 or len(parts[2]) != 4:
            continue
        code = parts[2]
        rows = load_bucket(bucket)
        c = kw_by_code.setdefault(code, Counter())
        for r in rows:
            for kw in (r.get("keywords") or []):
                kw = str(kw).strip()
                # 跳过内部结构化标记（工艺:welding / 材料:stainless_steel）。
                # 它们不是采购词，是给能力卡推断用的元信息，进别名表只会污染检索。
                if not kw or ":" in kw or "：" in kw:
                    continue
                c[kw] += 1

    classes = _taxonomy()["classes"]
    alias: dict[str, list[dict[str, Any]]] = {}
    for code, ctr in kw_by_code.items():
        for kw, hits in ctr.items():
            if hits >= min_hits:
                alias.setdefault(kw, []).append(
                    {"code": code, "name": classes.get(code, {}).get("name", ""),
                     "hits": hits})
    for kw in alias:
        alias[kw].sort(key=lambda x: -x["hits"])
        alias[kw] = alias[kw][:max_codes]

    payload = {
        "metadata": {
            "description": "采购词 → 国标小类映射。**由真实数据推导**：统计某关键词"
                           "实际出现在哪些小类下，出现 >= %d 次才收录，最多保留 %d 个小类。"
                           "替代原 8 个采购品类的检索入口。" % (min_hits, max_codes),
            "generated_at": _today(),
            "alias_count": len(alias),
        },
        "alias": dict(sorted(alias.items())),
    }
    with open(GB_ALIAS, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload["metadata"]


def load_alias() -> dict[str, list[dict[str, Any]]]:
    if not GB_ALIAS.exists():
        return {}
    with open(GB_ALIAS, encoding="utf-8") as f:
        return json.load(f).get("alias") or {}


def lookup_alias(word: str) -> list[str]:
    """采购词 → 国标小类码列表。找不到返回空列表（调用方应回退到关键词匹配）。"""
    a = load_alias()
    w = (word or "").strip()
    if not w:
        return []
    if w in a:
        return [x["code"] for x in a[w]]
    low = w.lower()
    for k, v in a.items():
        if k.lower() == low:
            return [x["code"] for x in v]
    return []


def save_bucket(bucket: str, records: list[dict[str, Any]]) -> None:
    """整桶覆盖写回（原子 + 自动分片）。空桶会删掉全部文件。"""
    _write_bucket(bucket, records)


# ─── 增量入库用：全库扫描缓存 ───────────────────────────────────────────────
# 抓取是「每次几十条、跑几百个任务」，如果每个任务都全量扫一遍 2 万条，
# 光 IO 就把时间吃光了。这里扫一次缓存住，进程内复用。
_CACHE: dict[str, Any] = {"names": None, "max_id": 0}


def scan_cache(rebuild: bool = False) -> dict[str, Any]:
    """一次全库扫描，缓存 公司名 → (桶, id) 与当前最大 ID。

    跨桶去重必须靠它：国标归档后同一家公司在多个小类文件里各存一份
    是很容易发生的（比如既标"机械加工"又标"模具"），只在本桶内去重拦不住。
    """
    if _CACHE["names"] is not None and not rebuild:
        return _CACHE
    names: dict[str, tuple[str, str]] = {}
    max_id = 0
    for bucket, _ in iter_buckets():
        for r in load_bucket(bucket):
            n = r.get("company")
            sid = r.get("id") or ""
            if n and n not in names:
                names[n] = (bucket, sid)
            m = re.match(r"CN-MFG-(\d+)", sid)
            if m:
                max_id = max(max_id, int(m.group(1)))
    _CACHE["names"] = names
    _CACHE["max_id"] = max_id
    return _CACHE


def invalidate_cache() -> None:
    """写过数据后调用，下次 scan_cache 重新扫描。"""
    _CACHE["names"] = None
    _CACHE["max_id"] = 0
    _CACHE["ids"] = None


_ID_INDEX_MISS = object()


def locate(sid: str) -> str | None:
    """按供应商 ID 找到它当前所在的桶。找不到返回 None。

    用于"增量更新单条记录"的场景（如能力卡写回指纹层）：
    需要先知道这条记录归在哪个国标小类下，才能落到正确的分片。
    """
    idx = _CACHE.get("ids")
    if idx is None:
        idx = {}
        for bucket in iter_buckets():
            for r in load_bucket(bucket[0]):
                if r.get("id"):
                    idx[r["id"]] = bucket[0]
        _CACHE["ids"] = idx
    return idx.get(sid)


def next_seq_id() -> int:
    return scan_cache()["max_id"] + 1


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="data/gb 归档访问层自检")
    ap.add_argument("--reindex", action="store_true", help="重建 gb-index.json")
    ap.add_argument("--realias", action="store_true", help="重建 gb-alias.json")
    ap.add_argument("--reshard", action="store_true",
                    help="按 MAX_PER_FILE（默认 5000，环境变量 GB_MAX_PER_FILE 可调）"
                         "重切所有桶，用于验证分片逻辑与容量治理")
    a = ap.parse_args()
    if a.reindex:
        m = rebuild_index()
        print("gb-index 已重建：", json.dumps(m["counts"], ensure_ascii=False))
    if a.realias:
        m = rebuild_alias()
        print("gb-alias 已重建：", m["alias_count"], "条")
    if a.reshard:
        before = stats()
        for bucket, _ in list(iter_buckets()):
            save_bucket(bucket, load_bucket(bucket))
        rebuild_index()
        after = stats()
        assert before["total"] == after["total"], "分片后条数不守恒！"
        print("重分片完成：%d 条守恒，逻辑桶 %d 个，物理文件 %d 个"
              % (after["total"], after["buckets"],
                 sum(1 for _ in GB_DIR.rglob("*.json"))))
    if not (a.reindex or a.realias or a.reshard):
        s = stats()
        print("总条数 %d / %d 个文件" % (s["total"], s["buckets"]))
        for k, v in sorted(s["per_bucket"].items(), key=lambda x: -x[1])[:10]:
            print("  %-16s %d" % (k, v))
