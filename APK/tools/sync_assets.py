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
import re
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
GB_SRC = ROOT / "data" / "gb"          # 完整档案（含 contact_phone）
CAP_SRC = ROOT / "dist" / "capability" / "slim"   # L1 能力卡精简分片（含根目录的未归类片）
CAP_DST = ASSETS / "capability"
UNCLASSIFIED = "_unclassified"


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


def sync_capability(apply: bool, clean: bool) -> dict:
    """同步 L1 能力卡精简分片（App 工艺位的数据源）。

    为什么要内置而不是联网拉
    ------------------------
    工艺位是详情页的核心信息，而能力卡只有 4136 张、精简后整包 1.19MB，
    内置进 APK 完全没压力。若改成联网按需拉取，在没有 CDN 的当下
    （server 未部署、L1 不进 Git）App 上就是一片空白——等于做了个看不见的功能。

    内置后断网也能显示工艺位；等 Pages 部署好了，再把完整版（full/，
    含 service/rfq/quality）改成按需拉取，两者不冲突。

    ⚠ 体积与填充率的真相（别按理想值设计 UI）
      - processes 100% 有值，但**由企业名称推断**，企业没确认过
      - limits 只有 0.1%（6/4136）有实质值，其余是全 null 空壳
      - 所以 App 侧硬指标为空必须显示「未填报」，不能显示 0
    """
    if not CAP_SRC.exists():
        print(f"[缺失] {CAP_SRC} —— 能力卡跳过（详情页将不显示工艺位）")
        print("       先跑 python scripts/gen_capability_shards.py --apply")
        return {"shards": 0, "bytes": 0, "cards": 0, "map": {}}
    if clean and apply and CAP_DST.exists():
        shutil.rmtree(CAP_DST)

    total = 0
    cards = 0
    cmap: dict[str, str] = {}
    for p in sorted(CAP_SRC.rglob("*.json")):
        size = p.stat().st_size
        total += size
        # 未归类的一片在 slim/ 根目录，其余在 gb/{门类}/{大类}/ 下。
        # 两个位置都要扫——只扫 gb/ 会静默丢掉未归类那批卡（App 里查得到企业、
        # 却没有工艺位，且不报错）。
        rel = p.relative_to(CAP_SRC).as_posix()
        # 文件名就是国标小类码：3399.json → "3399"。未归类用 "_" 兜底。
        key = "_" if p.stem == UNCLASSIFIED else p.stem
        cmap[key] = rel
        try:
            cards += len(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
        if apply:
            dst = CAP_DST / p.relative_to(CAP_SRC)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)

    if apply:
        (ASSETS / "capability").mkdir(parents=True, exist_ok=True)
        (ASSETS / "capability" / "_map.json").write_text(
            json.dumps(cmap, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    return {
        "shards": len(cmap),
        "bytes": total,
        "cards": cards,
        "map": cmap,
    }


def pick_phone(raw: str) -> str:
    """从 contact_phone 里挑出一个可用号码；挑不出就返回空串。

    源数据里三种情况都得处理，且**不能用占位值冒充号码**：
      - 单号码：18938530580
      - 多号码：'13630046699; 18566403616'、'0510-86230800; 0510-86230825' → 取第一个
      - 占位/垃圾：'待核实' → 判为无效，宁可留空（卡片显示「号码待核实」）
    判定口径：去掉非数字后长度 7–15 位。这样 400 热线、带区号固话、手机号都能过。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if 7 <= len(re.sub(r"\D", "", raw)) <= 15:
        return raw
    for part in re.split(r"[;；,，、/|]+", raw):
        p = part.strip()
        if 7 <= len(re.sub(r"\D", "", p)) <= 15:
            return p
    return ""


def sync_phone_index(apply: bool) -> dict:
    """从完整档案里抽出 id → 电话，生成 App 内置的号码索引。

    为什么不放进指纹层
    ------------------
    指纹（L0）是给 Agent 生态全量扫描的公开数据层，刻意保持最小：一条 ~120 字节，
    2.4 万条才 4.7MB。电话号码放进去，既撑大每次全量扫描的成本，也让「最小可用字段集」
    这个契约破功。而 App 卡片要直接显示号码，若靠联网现拉 2.4 万份完整档案，
    离线时又变成「有电话」——等于白改。

    所以：构建期抽一份 id→phone 的映射（jsonl，约 1MB）塞进 assets，
    App 启动时和指纹一起装载。离线也能直接显示号码，且不动 L0 的数据契约。
    """
    if not GB_SRC.exists():
        print(f"[缺失] {GB_SRC} —— 号码索引跳过（卡片将只显示『有电话』）")
        return {"entries": 0, "bytes": 0}

    rows: dict[str, str] = {}
    total = 0
    for p in sorted(GB_SRC.rglob("*.json")):
        try:
            arr = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        for o in arr:
            if not isinstance(o, dict):
                continue
            rid = (o.get("id") or "").strip()
            if not rid:
                continue
            total += 1
            phone = pick_phone(o.get("contact_phone"))
            if phone:
                rows[rid] = phone          # 后出现的覆盖前面的

    dst = ASSETS / "index" / "phone-index.jsonl"
    # 同一份内容也要落到仓库 data/ 下：manifest.json 会给它登记一条 t=phone，
    # App 就能像指纹分片一样增量拉取更新（否则新企业永远没号码）。
    repo_dst = ROOT / "data" / "phone-index.jsonl"
    payload = "".join(f"{k},{v}\n" for k, v in sorted(rows.items()))
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(payload, encoding="utf-8")
        repo_dst.parent.mkdir(parents=True, exist_ok=True)
        repo_dst.write_text(payload, encoding="utf-8")
    return {
        "entries": len(rows),
        "total": total,
        "missing": total - len(rows),
        "bytes": len(payload.encode("utf-8")),
        "sha1": hashlib.sha1(payload.encode("utf-8")).hexdigest(),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="同步仓库数据到 App assets")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    ap.add_argument("--clean", action="store_true", help="写入前清空内置指纹目录")
    a = ap.parse_args()

    files = sync_index(a.apply)
    fp = sync_fingerprint(a.apply, a.clean)
    cap = sync_capability(a.apply, a.clean)
    phones = sync_phone_index(a.apply)

    builtin = {
        "builtin_at": date.today().isoformat(),
        "source_repo": "beacon-mfg",
        "fingerprint": fp,
        "phone_index": phones,
        "capability": cap,
        "files": files,
        "notes": "本文件由 APK/tools/sync_assets.py 生成，请勿手改。"
                 "字段 sha1 用于核对内置副本与仓库数据是否一致。",
    }
    print("内置索引：")
    for rel, meta in files.items():
        print("  %-32s %8d B  %s" % (rel, meta["bytes"], meta["sha1"][:12]))
    print("内置指纹：%d 片 / %.2f MB" % (fp["shards"], fp["bytes"] / 1048576))
    print("号码索引：%d/%d 条有号码（%d 条源数据为占位值，留空）/ %.2f MB"
          % (phones["entries"], phones["total"], phones["missing"],
             phones["bytes"] / 1048576))
    print("能力卡：%d 张 / %d 片 / %.2f MB（工艺位，内置离线可用）"
          % (cap["cards"], cap["shards"], cap["bytes"] / 1048576))
    print("合计：%.2f MB" % ((fp["bytes"] + phones["bytes"] + cap["bytes"]
                            + sum(m["bytes"] for m in files.values()))
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
