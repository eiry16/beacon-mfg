#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重建 L0 指纹层：国标分片 + 100% 覆盖

为什么重写
----------
原指纹层有两个硬伤：

1. **只有 4136 条**（20265 家里的 20%），因为指纹是从能力卡（L1）渲染出来的，
   而能力卡只有被采集/认领过的厂商才有。结果就是：客户 Agent 扫指纹层做初筛时，
   **80% 的厂商根本不在索引里** —— 最省流量的那一层形同虚设。
2. **按旧 8 品类分文件**（`精密机械加工.jsonl`），而归档已经是国标四级了。
   两套体系并存，客户得先按品类找文件、再按国标筛，白绕一圈。

改造后
------
- 指纹**直接从归档主记录生成**，覆盖率 100%（有卡用卡的富字段，无卡用国标码推导）。
- 分片与归档同构：`skills/registry/fingerprint/gb/{门类}/{大类}/{小类}.jsonl`，
  客户拿国标码就能直接定位文件，不用再过一层映射。
- 单条控制在 ~230 B（短键 + 空字段省略），1000 万家全量指纹约 2.3 GB，
  但按小类分片后单次只需拉 1~2 MB。

字段（刻意用短键，这一层要被全量扫描）
--------------------------------------
    id    供应商 ID
    co    公司名
    city  城市
    gb    国标码（小类 4 位 / 中类 3 位，无码为 null）
    mf    是否制造商（1/0）—— 批发商不是我们要的供应商
    proc  工艺码列表
    mat   材料列表
    cert  证书名列表
    cl    认证等级 L0/L1/L2
    pv    来源：auto（能力卡自动画像）/ vendor_claimed（厂商自述）/
          derived（无卡，由国标码推导）/ fixture（测试夹具）
    sc    能力画像分（无卡为 0，不用 0 分冒充真实评分）
    tel   是否有可用电话（1/0）—— 电话都没有的厂商初筛就该淘汰

用法:
    python scripts/gen_fingerprint.py            # 预览（不写盘）
    python scripts/gen_fingerprint.py --apply    # 写盘并重建 index.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gb_store  # noqa: E402

FP_DIR = ROOT / "skills" / "registry" / "fingerprint"
FP_GB_DIR = FP_DIR / "gb"
REGISTRY_INDEX = ROOT / "skills" / "registry" / "index.json"
PROC_MAP = ROOT / "skills" / "registry" / "gb-proc-map.json"

# 国标码推导工艺的最小样本数：某小类下至少 min_hits 家被能力卡确认过该工艺，
# 才写进映射表。避免拿一两条噪声给整个小类贴标签。
MIN_HITS = 3
# 推导材料用的公司名关键词（只在公司名里找，不猜）
MAT_KEYWORDS = [
    ("不锈钢", "不锈钢"), ("锌合金", "锌合金"), ("镁合金", "镁合金"),
    ("钛合金", "钛合金"), ("铝合金", "铝合金"), ("铝型材", "铝合金"),
    ("铜材", "铜材"), ("黄铜", "铜材"), ("工程塑料", "工程塑料"),
    ("塑胶", "工程塑料"), ("塑料", "工程塑料"), ("橡胶", "橡胶/硅胶"),
    ("硅胶", "橡胶/硅胶"),
]


def _read_jsonl(path: Path, out: dict[str, dict]) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("id"):
            out[r["id"]] = r


def load_old_fingerprints() -> dict[str, dict]:
    """读已有指纹，按 id 去重保留富字段行（能力卡带来的 tol/moq/proc…）。

    2026-09-09 修正：这里原来**只读旧的 8 品类目录** `fingerprint/*.jsonl`。
    而 08:14 那次国标化重建已经把那批文件删了（备份在 .workbuddy/backup）。
    于是再跑一次 `gen_fingerprint.py --apply` 时 old={} → 23698 条全部退化成
    `pv=derived / sc=0`，gb-proc-map 被清空——**能力卡沉淀的工艺与硬指标静默全丢**，
    而且不报错。现在改为：优先读现行国标分片 `fingerprint/gb/**/*.jsonl`
    （这样重跑是幂等的，只补新增记录），读不到再退回旧的 8 品类目录。
    """
    out: dict[str, dict] = {}
    if FP_GB_DIR.exists():
        for f in sorted(FP_GB_DIR.rglob("*.jsonl")):
            _read_jsonl(f, out)
    if out:
        return out
    if FP_DIR.exists():
        for f in sorted(FP_DIR.glob("*.jsonl")):
            _read_jsonl(f, out)
    return out


def build_proc_map(old: dict[str, dict], code_of: dict[str, str | None]) -> dict[str, list[str]]:
    """从已有能力卡反推「国标码 → 工艺码」。

    **不手写映射**：统计每个国标码下，被能力卡确认过的工艺各出现多少次，
    达到 MIN_HITS 次的才收录。这样每条映射都有真实数据支撑。
    """
    by_code: dict[str, Counter] = defaultdict(Counter)
    for sid, row in old.items():
        code = code_of.get(sid)
        if not code:
            continue
        for p in row.get("proc") or []:
            by_code[code][p] += 1
    mapping: dict[str, list[str]] = {}
    for code, ctr in by_code.items():
        hit = [p for p, n in ctr.most_common() if n >= MIN_HITS]
        if hit:
            mapping[code] = hit
    return dict(sorted(mapping.items()))


def derive_mat(company: str) -> list[str]:
    name = company or ""
    seen: list[str] = []
    for kw, mat in MAT_KEYWORDS:
        if kw in name and mat not in seen:
            seen.append(mat)
    return seen


def has_phone(rec: dict) -> int:
    p = rec.get("contact_phone")
    if not p:
        return 0
    s = str(p).strip()
    if not s or "待核实" in s:
        return 0
    return 1 if any(c.isdigit() for c in s) else 0


def cert_names(rec: dict) -> list[str]:
    """证书名。库里同时存在字符串列表与 dict 列表两种写法，都兼容。"""
    out: list[str] = []
    for c in rec.get("certifications") or []:
        if isinstance(c, dict):
            n = c.get("name")
        else:
            n = c
        if n and n not in out:
            out.append(str(n))
    return out


def build_row(rec: dict, bucket: str, old_row: dict | None,
              proc_map: dict[str, list[str]]) -> dict:
    """生成一条指纹。有卡的用卡（富字段），无卡的用国标码推导（最小字段）。"""
    ind = rec.get("industry") or {}
    code = ind.get("code")
    region = rec.get("region") or {}

    if old_row:
        row = {
            "id": old_row["id"],
            "co": old_row.get("co") or rec.get("company"),
            "city": old_row.get("city") or region.get("city"),
            # 2026-09-19：区县/县级市。昆山的数据 city=苏州，只有带上 dist 才能
            # 按「昆山」被检索到（详见 scripts/backfill_district.py）。无值不占位。
            "gb": code,
            "mf": 1 if rec.get("is_manufacturer", True) else 0,
            "proc": old_row.get("proc") or [],
            "mat": old_row.get("mat") or [],
            "cert": old_row.get("cert") or cert_names(rec),
            "cl": old_row.get("cl") or rec.get("cl") or "L0",
            "pv": old_row.get("pv") or "auto",
            "sc": old_row.get("sc", 0),
            "tel": has_phone(rec),
        }
        # 富字段：有值才写，没值不占位（这一层按字节计费）
        for k in ("tol", "size", "moq", "lt", "rt"):
            v = old_row.get(k)
            if v not in (None, [], [None, None]):
                row[k] = v
    else:
        row = {
            "id": rec.get("id"),
            "co": rec.get("company"),
            "city": region.get("city"),
            "gb": code,
            "mf": 1 if rec.get("is_manufacturer", True) else 0,
            "proc": proc_map.get(code, []) if code else [],
            "mat": derive_mat(rec.get("company") or ""),
            "cert": cert_names(rec),
            "cl": rec.get("cl") or "L0",
            "pv": "derived",
            "sc": 0,
            "tel": has_phone(rec),
        }
    # 区县：只有真有值时才写（指纹层按字节计费，空字段不占位）。
    # 有了它，city=苏州 但 district=昆山 的记录才能被「昆山」检索命中。
    dist = region.get("district")
    if dist:
        row["dist"] = dist
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="重建 L0 指纹层（国标分片 + 100% 覆盖）")
    ap.add_argument("--apply", action="store_true", help="写盘（默认只预览）")
    ap.add_argument("--keep-old", action="store_true",
                    help="保留旧 8 品类指纹文件（默认迁移后删除）")
    a = ap.parse_args()

    recs = gb_store.load_all(with_bucket=True)
    old = load_old_fingerprints()
    code_of = {r["id"]: (r.get("industry") or {}).get("code")
               for r in recs if r.get("id")}
    proc_map = build_proc_map(old, code_of)

    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        sid = r.get("id")
        if not sid:
            continue
        bucket = r.get("_bucket") or gb_store.UNCLASSIFIED
        row = build_row(r, bucket, old.get(sid), proc_map)
        row.pop("_bucket", None)
        by_bucket[bucket].append(row)

    total = sum(len(v) for v in by_bucket.values())
    with_card = sum(1 for v in by_bucket.values() for r in v
                    if r["pv"] in ("auto", "vendor_claimed", "fixture"))
    print("国标小类分片：%d 个" % len(by_bucket))
    print("指纹总数：%d（覆盖率 %.1f%%），其中能力卡富字段 %d 条、国标码推导 %d 条"
          % (total, total / len(recs) * 100 if recs else 0, with_card, total - with_card))
    top = sorted(((k, len(v)) for k, v in by_bucket.items()),
                 key=lambda x: -x[1])[:8]
    for k, n in top:
        print("   %-14s %5d" % (k, n))

    if not a.apply:
        print("\n（预览模式，未写盘。加 --apply 执行）")
        return 0

    # 旧文件备份后清理：它们是按 8 品类切的，留着会和国标分片打架
    if FP_DIR.exists() and not a.keep_old:
        backup = ROOT / ".workbuddy" / "backup"
        backup.mkdir(parents=True, exist_ok=True)
        stamp = date.today().isoformat()
        dst = backup / ("fingerprint-8cat-%s" % stamp)
        if not dst.exists():
            shutil.copytree(FP_DIR, dst)
        print("\n旧指纹已备份到 %s" % dst.relative_to(ROOT))
        for f in FP_DIR.glob("*.jsonl"):
            f.unlink()

    FP_GB_DIR.mkdir(parents=True, exist_ok=True)
    for bucket, rows in by_bucket.items():
        rows.sort(key=lambda r: r["id"])
        p = FP_GB_DIR / ("%s.jsonl" % bucket)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":"))
                      for r in rows) + "\n",
            encoding="utf-8")

    with open(PROC_MAP, "w", encoding="utf-8") as f:
        json.dump({
            "metadata": {
                "description": "国标码 → 工艺码映射。**由能力卡真实数据反推**："
                               "统计每个国标小类下被能力卡确认过的工艺，"
                               "出现 >= %d 次才收录。仅用于无能力卡厂商的推导，"
                               "有卡的以卡为准。" % MIN_HITS,
                "generated_at": date.today().isoformat(),
                "min_hits": MIN_HITS,
                "codes": len(proc_map),
            },
            "map": proc_map,
        }, f, ensure_ascii=False, indent=2)

    total_bytes = sum(p.stat().st_size
                      for p in FP_GB_DIR.rglob("*.jsonl"))
    registry = {
        "updated_at": date.today().isoformat(),
        "total": total,
        "coverage": round(total / len(recs) * 100, 1) if recs else 0,
        "layout": "国标四级分片，与 data/gb/ 同构",
        "layers": {
            "L0_fingerprint": "skills/registry/fingerprint/gb/{门类}/{大类}/{小类}.jsonl",
            "L0_manifest": "data/manifest.json（分片清单 + ETag，客户端入口）",
            "L1_capability": "skills/registry/capability/{id}.json（按需，不进 Git）",
            "L2_self_report": "skills/vendors/{id}/SKILL.md（按需，不进 Git）",
        },
        "field_spec": {
            "id": "供应商 ID", "co": "公司名", "city": "城市",
            "gb": "国标码（小类 4 位 / 中类 3 位）", "mf": "是否制造商 1/0",
            "proc": "工艺码列表", "mat": "材料列表", "cert": "证书列表",
            "cl": "认证等级 L0/L1/L2",
            "pv": "auto 能力卡 / vendor_claimed 自述 / derived 国标推导 / fixture 夹具",
            "sc": "能力画像分（无卡为 0）", "tel": "有可用电话 1/0",
        },
        "bytes_total": total_bytes,
        "bytes_per_record": round(total_bytes / total, 1) if total else 0,
        "by_gate": dict(sorted(Counter(
            b.split("/")[0] if "/" in b else "_unclassified"
            for b in by_bucket).items())),
        "shards": {b: len(v) for b, v in sorted(by_bucket.items())},
    }
    with open(REGISTRY_INDEX, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)

    print("\n已写入 %d 个分片，共 %.2f MB（%.0f B/条）"
          % (len(by_bucket), total_bytes / 1048576,
             total_bytes / total if total else 0))
    print("工艺映射 %d 个国标码 → %s" % (len(proc_map), PROC_MAP.relative_to(ROOT)))
    print("注册表 → %s" % REGISTRY_INDEX.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
