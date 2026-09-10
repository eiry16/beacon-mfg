#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L1 能力卡国标分片：产出可直接上传 Pages / 内置进 App 的静态分片

为什么要有这个脚本
------------------
能力卡（L1）目前是 `skills/registry/capability/{id}.json`，**4136 个独立小文件**：

1. **不进 Git**（.gitignore 排除）→ GitHub CDN 上不存在 → "云端按需拉取"没有云端。
2. **App 读不到**：`SupplierDetail` 里没有工艺/材料/硬指标字段，手机上完全看不到能力卡。
3. **4136 次 HTTP 请求**才拿完全量，任何一个 Agent 都不会这么干。

本脚本按国标小类把能力卡打包成 ~107 个分片，客户端拿国标码一次就能拉到
该小类的全部能力卡，或者整包内置进 App。

两种产物（一次生成，用途不同）
------------------------------
- `full/gb/{门类}/{大类}/{小类}.json`
  完整能力卡（原字段全保留），用于**上传到 Pages/R2 做按需拉取**。
- `slim/gb/{门类}/{大类}/{小类}.json`
  精简版，只留工艺位需要的字段，体积约为 full 的 1/7，
  用于**内置进 APK assets**（App 不依赖 CDN 就能显示工艺位）。

⚠ 必须知道的填充率真相（2026-09-09 实测，别按理想值设计 UI）
------------------------------------------------------------
    processes  100.0%  (4136/4136)  由企业名称推断，33 种工艺码
    materials   15.3%  (631/4136)
    limits       0.1%  (6/4136)     其余 4130 张是全 null 空壳
    badge  L0 4134 / L1 2（只有 2 家认领过）

**硬指标几乎全空是诚实结果，不是 bug。** 自动整理的卡不编造公差/MOQ/交期，
所以 slim 版在 limits 全空时**直接省略该字段**，UI 侧据此显示"未填报"，
不要拿 0 充数——那会让客户以为这家厂公差能做到 0。

字段：slim 版
-------------
    id / co 公司名 / gb 国标码 / gn 国标名 / city / prov
    proc 工艺 [{c 码, n 名, l 级别 primary|secondary|outsourced}]
    mat  材料（无则省略）
    lim  硬指标（全空则省略，只写有值的键）
    cl   认证等级 L0/L1/L2        pv  provenance.mode（auto/vendor_claimed…）
    tel  有可用电话 1/0           sk  厂商 skill {u 路径, v 是否已核实}

用法:
    python scripts/gen_capability_shards.py             # 预览（不写盘）
    python scripts/gen_capability_shards.py --apply     # 写盘
    python scripts/gen_capability_shards.py --apply --only-claimed   # 只打包认领过的
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gb_store  # noqa: E402

CAP_DIR = ROOT / "skills" / "registry" / "capability"
DEFAULT_OUT = ROOT / "dist" / "capability"
UNCLASSIFIED = "_unclassified"

# slim 版保留的 limits 键（顺序即展示顺序）
LIMIT_KEYS = [
    ("tolerance_mm", "tol"),
    ("max_part_size_mm", "size"),
    ("min_order_qty", "moq"),
    ("lead_time_days", "lt"),
    ("current_load_pct", "load"),
    ("rush_available", "rush"),
]


def _unlink_with_retry(path: Path, retries: int = 3) -> None:
    """删文件，失败按 0.2s / 0.4s 退避重试。

    为什么：流水线里清理旧分片时，Windows 的文件占用会让 unlink 抛 WinError 32。
    一次抖动不该让整 db 的重建失败（更糟的是只删一半，留下残缺分片）。
    """
    for i in range(retries):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            if i == retries - 1:
                raise
            time.sleep(0.2 * (i + 1))


def has_phone(rec: dict) -> int:
    p = rec.get("contact_phone")
    if not p:
        return 0
    s = str(p).strip()
    if not s or "待核实" in s:
        return 0
    return 1 if any(ch.isdigit() for ch in s) else 0


def load_directory() -> dict[str, dict]:
    """名录主记录：用来补国标码 / 城市 / 电话（能力卡自己没有国标码）。"""
    out: dict[str, dict] = {}
    for r in gb_store.load_all(with_bucket=True):
        sid = r.get("id")
        if sid:
            out[sid] = r
    return out


def slim_card(cap: dict, rec: dict | None) -> dict:
    """精简一张能力卡：只留工艺位相关字段，空字段一律不占位。"""
    rec = rec or {}
    ind = rec.get("industry") or {}
    region = rec.get("region") or {}
    claim = cap.get("claim") or {}

    row: dict = {
        "id": cap.get("supplier_id"),
        "co": cap.get("company") or rec.get("company"),
    }
    code = ind.get("code")
    if code:
        row["gb"] = code
        if ind.get("name"):
            row["gn"] = ind["name"]
    city = region.get("city")
    if city:
        row["city"] = city
    prov = region.get("province")
    if prov:
        row["prov"] = prov

    # 工艺位：这一层的核心价值
    proc = []
    for p in cap.get("processes") or []:
        if not isinstance(p, dict):
            continue
        item = {"c": p.get("code")}
        if p.get("name"):
            item["n"] = p["name"]
        if p.get("level"):
            item["l"] = p["level"]
        proc.append(item)
    row["proc"] = proc

    mats = [m for m in (cap.get("materials") or []) if m]
    if mats:
        row["mat"] = mats

    # 硬指标：全空就不写这个键。写 {} 等于骗客户"这家填了但没有值"
    lim_src = cap.get("limits") or {}
    lim = {}
    for src_key, out_key in LIMIT_KEYS:
        v = lim_src.get(src_key)
        if v not in (None, [], "", {}):
            lim[out_key] = v
    if lim:
        row["lim"] = lim

    row["cl"] = claim.get("badge") or "L0"
    prov_mode = (cap.get("provenance") or {}).get("mode")
    row["pv"] = prov_mode or ("vendor_claimed" if claim.get("status") == "claimed" else "auto")
    row["tel"] = has_phone(rec)

    # 厂商 skill 入口：L2 自述层。CDN 未部署时这个路径不可达，
    # 客户端应先判空，不要拿它当"这家一定有 skill"的证据。
    if rec.get("agent") or cap.get("supplier_id"):
        row["sk"] = {
            "u": "skills/vendors/%s/SKILL.md" % cap.get("supplier_id"),
            "v": bool((rec.get("agent") or {}).get("verified")),
        }
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="L1 能力卡国标分片（Pages / App 内置两用）")
    ap.add_argument("--apply", action="store_true", help="写盘（默认只预览）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--only-claimed", action="store_true",
                    help="只打包已认领（L1/L2）的卡，默认全部")
    ap.add_argument("--full-only", action="store_true", help="只产 full 版")
    ap.add_argument("--slim-only", action="store_true", help="只产 slim 版")
    a = ap.parse_args()

    if not CAP_DIR.exists():
        print("能力卡目录不存在：%s" % CAP_DIR.relative_to(ROOT))
        print("先跑 scripts/sync_vendor_skills.py --all 生成 L1 层")
        return 1

    cards: list[dict] = []
    for f in sorted(CAP_DIR.glob("*.json")):
        try:
            cards.append(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            print("跳过解析失败的能力卡 %s：%s" % (f.name, exc))
    print("读到能力卡 %d 张" % len(cards))

    if a.only_claimed:
        cards = [c for c in cards
                 if (c.get("claim") or {}).get("status") == "claimed"]
        print("按 --only-claimed 过滤后 %d 张" % len(cards))
    if not cards:
        print("没有可打包的能力卡，退出")
        return 1

    directory = load_directory()

    # 按国标小类分桶。能力卡自己不带国标码，从名录取。
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    by_bucket_slim: dict[str, list[dict]] = defaultdict(list)
    no_rec = 0
    for cap in cards:
        sid = cap.get("supplier_id")
        if not sid:
            continue
        rec = directory.get(sid)
        if rec is None:
            no_rec += 1
        bucket = (rec or {}).get("_bucket") or UNCLASSIFIED
        by_bucket[bucket].append(cap)
        by_bucket_slim[bucket].append(slim_card(cap, rec))

    total = sum(len(v) for v in by_bucket.values())
    print("国标小类分片：%d 个（能力卡 %d 张）" % (len(by_bucket), total))
    if no_rec:
        print("⚠ %d 张卡在名录里找不到对应记录（国标码为空）" % no_rec)
    top = sorted(((k, len(v)) for k, v in by_bucket.items()),
                 key=lambda x: -x[1])[:8]
    for k, n in top:
        print("   %-14s %5d" % (k, n))

    if not a.apply:
        print("\n（预览模式，未写盘。加 --apply 执行）")
        return 0

    out = Path(a.out)
    do_full = not a.slim_only
    do_slim = not a.full_only

    def write_layer(name: str, data: dict[str, list[dict]]) -> tuple[int, int, dict]:
        """写一层（full / slim）。

        以前是「先删掉整层再重写」，两个毛病：
          1. 删除 42+ 个文件，任何批量删除保护/杀软都会把整条流水线红掉；
          2. 删完到写完之间文件不存在，正好在读的那个 App/Agent 会拿 404。
        改成**原子替换 + 只清理孤儿**：
          - 每次往 .tmp 写完再 os.replace，读者看到的永远是完整文件；
          - 只有「这次没产出、但旧版里有」的分片才需要删（通常是 0 个）。
        """
        base = out / name
        n = 0
        shards = {}
        written: set[Path] = set()
        for bucket, rows in data.items():
            rows = sorted(rows, key=lambda r: (r.get("id") or r.get("supplier_id") or ""))
            if bucket == UNCLASSIFIED:
                p = base / ("%s.json" % UNCLASSIFIED)
            else:
                p = base / "gb" / ("%s.json" % bucket)
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, p)
            written.add(p)
            n += len(rows)
            rel = p.relative_to(out).as_posix()
            shards[rel] = {
                "p": rel,
                "c": bucket,
                "k": len(rows),
                "z": p.stat().st_size,
                "h": hashlib.sha1(payload.encode("utf-8")).hexdigest(),
            }

        # 清理孤儿：这批没产出的旧分片（如某小类一张卡都没有了）
        orphan = 0
        if base.exists():
            for f in base.rglob("*.json"):
                if f in written:
                    continue
                _unlink_with_retry(f)
                orphan += 1
        if orphan:
            print("   清理旧分片 %d 个（该目录下已无对应能力卡）" % orphan)
        total_bytes = sum(s["z"] for s in shards.values())
        return n, total_bytes, shards

    manifest: dict = {
        "metadata": {
            "description": "L1 能力卡国标分片。full/ 完整卡用于 CDN 按需拉取，"
                           "slim/ 精简版用于 App 内置（只留工艺位）。",
            "generated_at": date.today().isoformat(),
            "source": "skills/registry/capability/{id}.json",
            "git_tracked": False,
            "note_fields": {
                "proc": "工艺位，100% 有值（由企业名称推断，未获企业确认）",
                "lim": "硬指标，仅 0.1% 有值；字段缺失 = 未填报，不是 0",
                "pv": "auto 自动整理 / vendor_claimed 厂商自述",
                "sk": "厂商 skill 路径，CDN 未部署时不可达，客户端需判空",
            },
        },
        "usage": {
            "by_gb_code": "查国标码 3525 → 拉 gb/C/35/3525.json（一次请求拿到该小类全部能力卡）",
            "cache": "用分片 shards[].h（SHA1）做 ETag；注意 GitHub Pages 的 ETag 是另一套哈希，"
                     "照搬 h 做 If-None-Match 不会命中 304（2026-09-09 实测教训）",
            "app_bundle": "slim/ 整包可内置进 APK assets，App 不联网也能显示工艺位",
        },
        "counts": {
            "cards": total,
            "unclassified": len(by_bucket.get(UNCLASSIFIED, [])),
            "no_directory_record": no_rec,
        },
        "shards": {},
    }

    if do_full:
        n, tb, sh = write_layer("full", by_bucket)
        manifest["shards"]["full"] = sorted(sh.values(), key=lambda x: x["p"])
        print("\nfull/：%d 张 · %d 片 · %.2f MB（%.0f B/张）"
              % (n, len(sh), tb / 1048576, tb / n if n else 0))
    if do_slim:
        n, tb, sh = write_layer("slim", by_bucket_slim)
        manifest["shards"]["slim"] = sorted(sh.values(), key=lambda x: x["p"])
        print("slim/：%d 张 · %d 片 · %.2f MB（%.0f B/张）"
              % (n, len(sh), tb / 1048576, tb / n if n else 0))

    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\n清单 → %s" % (out / "manifest.json").relative_to(ROOT))
    print("上传 Pages 直接把这个目录传上去即可：%s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
