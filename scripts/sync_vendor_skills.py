# -*- coding: utf-8 -*-
"""把供应商 Skill 同步进检索链路（CI 流程的第 ⑤⑥ 步）

PROPOSAL_VENDOR_SKILL.md §12 里写的提交流程是：

    capability.json 校验 → 生成/更新 fingerprint 行 → 回写 data/ 的 agent 字段

但这两步**历史上从未实现**，结果是：
  - `skills/registry/capability/` 是空目录
    → SKILL.md 教客户 Agent「按 id 读 capability/{id}.json」，实际读不到任何东西
  - 名录 `agent` 字段 0/20264
    → 客户 Agent 检索命中后拿不到 skill_url，链路断在这里

本脚本补上这两步。三件事，全部幂等：

  1. `skills/vendors/{id}/capability.json` → `skills/registry/capability/{id}.json`
     （L1 能力卡的常读入口，按 id 单文件读）
  2. 更新 `skills/registry/fingerprint/{品类}.jsonl`
     （L0 指纹行，按 id 覆盖而非追加，避免重跑产生幽灵数据）
  3. 名录记录的 `agent` 字段经 supplier_loader.persist_supplier 写回 data/gb/
     （skill_url / protocol / capabilities / verified）

用法：
    python scripts/sync_vendor_skills.py --ids CN-MFG-0002786,CN-MFG-0002908
    python scripts/sync_vendor_skills.py --all
    python scripts/sync_vendor_skills.py --all --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "server"
VENDOR_DIR = REPO_ROOT / "skills" / "vendors"
REGISTRY_DIR = REPO_ROOT / "skills" / "registry"
CAPABILITY_DIR = REGISTRY_DIR / "capability"
FINGERPRINT_DIR = REGISTRY_DIR / "fingerprint"
REGISTRY_INDEX = REGISTRY_DIR / "index.json"
CONFLICT_REPORT = REGISTRY_DIR / "category-conflicts.json"

# 每次 --all 收集到的「名录品类 vs 能力卡品类」冲突，收尾写进 CONFLICT_REPORT
CONFLICTS: list[dict] = []

sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(SERVER_DIR))

from collect.render import render_fingerprint, append_fingerprint  # noqa: E402
from collect.batch_auto_profile import completeness  # noqa: E402
from loaders import supplier_loader  # noqa: E402
# 地址 → (省, 市) 的口径与认证回写共用一份，不各写一套（certify_writeback 模块级无副作用）
from certify_writeback import split_region  # noqa: E402

GATES_DIR = REPO_ROOT / "skills" / "schema" / "gates"


def _gate_name(gate: str) -> str:
    """门类字母 → 门类中文名（CN-I → 信息传输、软件和信息技术服务业）。"""
    try:
        d = json.loads((GATES_DIR / f"{gate}.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    return d.get("gate_name") or ""


def record_from_card(cap: dict) -> dict:
    """能力卡 → 归档主记录。**只用于「归档里没有这家企业」的新建场景。**

    为什么必须建档：`data/gb/` 是名录的权威来源 —— `gen_fingerprint.py` 全量重建
    指纹时只读它，而手机端检索只走指纹。注册新建的企业（非采集来源，例如
    CN-I-0000001 赤兔智能工业）若只落 L1 卡、不进归档，那么每次采集跑 postfetch
    重建，它的指纹都会被冲掉 —— 客户在 App 里搜公司名永远搜不到
    （2026-09-12 实测复现）。

    字段口径对齐 `certify_writeback.build_record`（认证建档），差异只在两处：
    - source 用 "certification"：SOURCE_ENUM 内唯一表示「企业自主提交、平台核验」
      的值。不新造枚举值 —— 那要同步改 SPEC/SKILL/validate 三处，超出本次修复范围。
    - category：C 门类用能力卡里的品类名（validate 认那 8 个值）；
      非 C 门类用**门类名**。绝不套 `category_of()` 的制造业默认值 ——
      那会把一家 IT 服务公司标成「精密机械加工」，属错标。
    """
    sid = cap["supplier_id"]
    gate = cap.get("gate") or ""
    ident = cap.get("identity") or {}
    contact = cap.get("contact") or {}
    claim = cap.get("claim") or {}

    address = contact.get("address") or ident.get("address") or ""
    province = ident.get("province") or ""
    city = ident.get("city") or ""
    if address and not (province and city):
        p, c = split_region(address)
        province = province or p
        city = city or c

    category = cap.get("category") or ""
    if gate != "C":
        category = _gate_name(gate) or category

    return {
        "id": sid,
        "company": cap.get("company") or "",
        "category": category or "未分类",
        # 检索词只用有据可依的真实串（这里就是它的品类/门类），不编造工艺材料
        "keywords": [category] if category else [],
        "region": {"province": province, "city": city},
        "address": address,
        # 电话只取 card.contact.phone：卡上没有就留空，绝不拿 rfq.endpoint
        # 或"待核实"之类占位串顶上（本项目红线）
        "contact_phone": contact.get("phone") or None,
        "source": "certification",
        "source_url": None,
        "verified_at": (claim.get("verified_at") or "")[:10] or None,
        "is_template": False,
        "note": "",
        "status": "verified",
        # 国标码留空：门类 I 的服务型企业没有对应小类，不编造
        "industry": None,
        "is_manufacturer": gate == "C",
        # 灯牌从卡的 claim 读（规则算出来的），本函数不参与评级
        "cl": claim.get("badge") or "L0",
    }


def capabilities_of(cap: dict) -> list[str]:
    """从能力卡推断 agent.capabilities（SPEC 2.3 取值域：catalog/rfq/live_chat/quote）。"""
    caps = ["catalog"]
    rfq = cap.get("rfq") or {}
    if rfq.get("endpoint"):
        caps.append("rfq")
        if rfq.get("auto_quote"):
            caps.append("quote")
    return caps


def content_reviewed(cap: dict) -> bool:
    """agent.verified：卡内容是否经过平台审核。

    SPEC §2.3 定义 agent.verified = 「平台内容审核通过」。
    认证流程里的卡带有 claim.status + claim.verified_at（审核日期），
    满足即视为已审核；自动整理卡这两个字段为空 → False。
    """
    claim = cap.get("claim") or {}
    return claim.get("status") in ("claimed", "verified", "audited") and bool(
        claim.get("verified_at")
    )


def sync_one(sid: str, dry_run: bool = False) -> tuple[bool, str]:
    vdir = VENDOR_DIR / sid
    cfile = vdir / "capability.json"
    if not cfile.exists():
        return False, "缺少 capability.json"

    try:
        cap = json.loads(cfile.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"capability.json 解析失败: {exc}"

    cat = cap.get("category")
    mode = (cap.get("provenance") or {}).get("mode", "?")
    detail = []

    if dry_run:
        return True, f"[{mode}] {cat} （dry-run，未写入）"

    # ── 1. L1 能力卡入口 ────────────────────────────────────────────────
    CAPABILITY_DIR.mkdir(parents=True, exist_ok=True)
    (CAPABILITY_DIR / f"{sid}.json").write_text(
        json.dumps(cap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    detail.append("capability ✓")

    # ── 2. L0 指纹行（append_fingerprint 内部按 id 去重，重复跑不会产生幽灵数据）
    append_fingerprint(cap, completeness(cap))
    detail.append(f"fingerprint ✓(sc={completeness(cap)})")

    # ── 3. 回写名录 agent 字段（复用已验证的原子落盘逻辑）────────────────
    rec = supplier_loader.get_supplier(sid)
    if rec is None:
        # 归档里没有这家企业 —— 注册新建的场景（不是认领已有名录）。
        # 以前这里直接 return「跳过 agent 回写」，结果归档永远没有这条记录，
        # 采集重建后连指纹一起被冲掉（2026-09-12 赤兔实例）。改为先建档。
        new_rec = record_from_card(cap)
        if not supplier_loader.insert_supplier(new_rec):
            return False, (f"[{mode}] {cat} {' '.join(detail)} "
                           f"归档建档失败（capability/fingerprint 已写入）")
        rec = supplier_loader.get_supplier(sid)
        if rec is None:          # 理论上不会发生；真发生就是内存同步漏了
            return False, f"[{mode}] {cat} 建档后内存索引仍取不到 {sid}"
        detail.append("archive ✓(新建档)")
    if rec.get("category") != cat:
        # 国标迁移后名录按国标码重划了品类，能力卡还挂着旧的 8 品类标签。
        # 这是归类口径变化，不是身份错配（id 精确匹配），用它阻断 agent 回写
        # 会让 15% 的卡永远进不了检索链路。所以只记冲突、不阻断。
        CONFLICTS.append({"id": sid, "directory": rec.get("category"),
                          "capability": cat, "profile": cap.get("profile")})
        detail.append(f"⚠品类冲突(名录 {rec.get('category')} ≠ 卡 {cat})")

    updated = dict(rec)
    updated["agent"] = {
        "skill_url": f"skills/vendors/{sid}/SKILL.md",
        "protocol": "skill",
        "capabilities": capabilities_of(cap),
        # SPEC §2.3：agent.verified = **平台内容审核通过**。
        # 原实现恒写 False，对已通过认证审核的企业是明显低估（其卡内容逐项核过）。
        # 判定：卡已认领/核验（status ∈ claimed|verified|audited）**且**有审核日期。
        # 自动整理卡（status=unclaimed / verified_at=null）仍为 False——
        # 未经审核的内容不能对外宣称已审核。
        "verified": content_reviewed(cap),
    }
    if not supplier_loader.persist_supplier(sid, updated):
        return False, f"agent 字段落盘失败（前面 capability/fingerprint 已写入）"
    detail.append("agent ✓")

    return True, f"[{mode}] {cat} {' '.join(detail)}"


def rebuild_registry_index(dry_run: bool = False) -> None:
    """index.json 的权威入口是 scripts/gen_fingerprint.py，本脚本不再写它。

    原来这里按扁平的 8 品类 `fingerprint/*.jsonl` 统计，而现行指纹是国标四级
    `fingerprint/gb/{门类}/{大类}/{小类}.jsonl` —— glob("*.jsonl") 恒为空，
    跑一次 --all 就会把 index.json 覆盖成 total=0 的空壳，而且不报错。
    gen_fingerprint.py 写的那份带 field_spec / by_gate / shards，两边各写一份
    只会互相覆盖，所以这里改成只提示。
    """
    print("\n提示：registry/index.json 由 `python scripts/gen_fingerprint.py --apply` 重建"
          "（本脚本不再写，避免两份实现互相覆盖）")


def main() -> int:
    ap = argparse.ArgumentParser(description="同步供应商 Skill 到检索链路")
    ap.add_argument("--ids", help="逗号分隔的 supplier_id 列表")
    ap.add_argument("--all", action="store_true", help="同步全部供应商 Skill")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写入")
    args = ap.parse_args()

    if not args.ids and not args.all:
        ap.error("需要 --ids 或 --all")

    if args.all:
        targets = sorted(p.name for p in VENDOR_DIR.iterdir()
                         if p.is_dir() and (p / "capability.json").exists())
    else:
        targets = [s.strip() for s in args.ids.split(",") if s.strip()]

    if not args.dry_run:
        supplier_loader.load()

    failed = 0
    for sid in targets:
        ok, msg = sync_one(sid, dry_run=args.dry_run)
        if not ok:
            failed += 1
            print(f"[FAIL] {sid}: {msg}")
        else:
            print(f"[ OK ] {sid}: {msg}")

    rebuild_registry_index(dry_run=args.dry_run)

    if CONFLICTS and not args.dry_run:
        REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        # 与既有报告**合并**，不是覆盖：本脚本常以 --ids 单跑（注册一个企业就走一次），
        # 直接覆写会把 --all 攒下的历史冲突全抹掉 —— 2026-09-12 实测把 625 条冲成 1 条。
        merged: dict[str, dict] = {}
        try:
            old = json.loads(CONFLICT_REPORT.read_text(encoding="utf-8"))
            for it in (old.get("items") or []):
                if it.get("id"):
                    merged[it["id"]] = it
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        for it in CONFLICTS:
            merged[it["id"]] = it          # 本次的覆盖同 id 旧记录
        items = [merged[k] for k in sorted(merged)]
        CONFLICT_REPORT.write_text(
            json.dumps({"updated_at": date.today().isoformat(),
                        "total": len(items),
                        "note": "名录按国标码重划后的品类 ≠ 能力卡的旧 8 品类标签；"
                                "id 精确匹配，属口径差异不是错配，待人工决定是否重划",
                        "items": items}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"品类冲突 {len(CONFLICTS)} 家已并入报告（共 {len(items)} 条）"
              f" → {CONFLICT_REPORT.relative_to(REPO_ROOT)}")

    print(f"\n同步 {len(targets)} 家，失败 {failed} 家"
          + ("（dry-run，未写入任何文件）" if args.dry_run else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
