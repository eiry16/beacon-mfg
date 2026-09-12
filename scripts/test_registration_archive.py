#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""注册建档回归：**注册新建的企业必须落在归档里，否则重建后从指纹层消失。**

锁的是 2026-09-12 实测到的缺陷：
    注册（collect.confirm）→ sync_one 写 L1 卡 + append 指纹行 + 尝试回写 agent，
    但企业**不在归档 data/gb/** 时 `get_supplier()` 返回 None，agent 回写被跳过 ——
    归档永远没有这条记录。此后任何一次采集跑 postfetch（→ gen_fingerprint.py）
    全量重建指纹（只读归档），这条企业的指纹就被冲掉。
    表现：手机 App 搜公司名永远搜不到，且**不报任何错**（赤兔智能工业实例）。

判定方式（全部只读，不改任何数据）：
    [1] 反向一致性：每张 L1 能力卡的 id 都必须能在归档里找到。
        这是本次缺陷的直接探测器 —— 有卡无归档 = 重建后会消失。
    [2] 有 agent 的归档记录必须在指纹层里有对应行（客户检索只走指纹层）。
    [3] 指纹行数 == 归档条数（覆盖率 100%，gen_fingerprint 的不变式）。
    [4] record_from_card 的字段口径：不编造电话 / 非 C 门类用门类名当 category /
        region 解析符合归档口径（不带「省」「市」后缀）。
    [5] split_region 回归：「江苏省苏州市…」必须解析成 ("江苏", "苏州")，
        不能是 ("江苏省", "江苏省苏州")。

用法：python scripts/test_registration_archive.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gb_store  # noqa: E402

FAILS: list[str] = []

VENDOR_DIR = ROOT / "skills" / "vendors"
FP_GB_DIR = ROOT / "skills" / "registry" / "fingerprint" / "gb"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def load_gate_names() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted((ROOT / "skills" / "schema" / "gates").glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out[d.get("gate")] = d.get("gate_name") or ""
    return out


def main() -> int:
    rows = gb_store.load_all()
    archive_ids = {r.get("id") for r in rows}
    print(f"归档：{len(rows)} 条 / {len(archive_ids)} 个 id")

    # ── [1] 反向一致性：每张 L1 卡都要在归档里 ────────────────────────────
    cards = sorted(VENDOR_DIR.glob("*/capability.json"))
    card_ids = {p.parent.name for p in cards}
    orphan = sorted(card_ids - archive_ids)
    check("每张 L1 能力卡都有对应归档记录（否则重建后从指纹消失）",
          not orphan,
          f"缺 {len(orphan)} 张: {orphan[:5]}" if orphan else f"{len(card_ids)} 张卡全部有归档")

    # ── [2] 有 agent 的归档记录必须在指纹层里 ────────────────────────────
    fp_ids: set[str] = set()
    for f in FP_GB_DIR.rglob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                fp_ids.add(json.loads(line).get("id"))
            except json.JSONDecodeError:
                continue
    print(f"指纹层：{len(fp_ids)} 个 id / {len(list(FP_GB_DIR.rglob('*.jsonl')))} 个分片")

    agent_ids = {r.get("id") for r in rows if r.get("agent")}
    missing_fp = sorted(agent_ids - fp_ids)
    check("带 agent 的归档记录都在指纹层里（客户检索只认指纹）",
          not missing_fp,
          f"缺 {len(missing_fp)} 条: {missing_fp[:5]}" if missing_fp else f"{len(agent_ids)} 条全在")

    # ── [3] 覆盖率 100%：指纹行数 == 归档条数 ────────────────────────────
    check("指纹覆盖率 100%（gen_fingerprint 不变式）",
          len(fp_ids) == len(archive_ids),
          f"指纹 {len(fp_ids)} vs 归档 {len(archive_ids)}")

    # ── [4] record_from_card 字段口径 ────────────────────────────────────
    from sync_vendor_skills import record_from_card  # noqa: E402

    gates = load_gate_names()
    fake = {
        "supplier_id": "CN-I-0009999",
        "gate": "I",
        "company": "某某信息技术服务有限公司",
        "category": "其他信息服务",
        "claim": {"status": "claimed", "verified_at": "2026-09-12", "badge": "L1"},
        "contact": {"phone": None, "address": "江苏省苏州市虎丘区某某路1号"},
        "identity": {},
    }
    rec = record_from_card(fake)
    check("非 C 门类用门类名当 category（不套制造业默认值）",
          rec["category"] == gates.get("I") and rec["category"] != "精密机械加工",
          f"category={rec['category']!r}")
    check("region 解析符合归档口径（不带省/市后缀）",
          rec["region"] == {"province": "江苏", "city": "苏州"},
          f"region={rec['region']}")
    check("电话缺失时留空，不拿占位串顶上",
          rec["contact_phone"] is None, f"contact_phone={rec['contact_phone']!r}")
    check("is_template=false 与 status=verified 一致（validate 硬约束）",
          rec["is_template"] is False and rec["status"] == "verified")
    check("非 C 门类 is_manufacturer=False",
          rec["is_manufacturer"] is False)
    check("灯牌从卡里读，不在这里评级", rec["cl"] == "L1")
    check("keywords 非空（validate 硬约束）", bool(rec["keywords"]))

    # ── [5] split_region 回归 ────────────────────────────────────────────
    from certify_writeback import split_region  # noqa: E402

    cases = {
        "江苏省苏州市虎丘区苏州工业园星湖街青创港二期": ("江苏", "苏州"),
        "广东省深圳市宝安区沙井街道": ("广东", "深圳"),
        "上海市浦东新区张江路1号": ("上海", "上海"),
        "深圳市南山区科技园": ("", "深圳"),
        "浙江省宁波市鄞州区": ("浙江", "宁波"),
    }
    bad = {k: split_region(k) for k, want in cases.items() if split_region(k) != want}
    check("split_region 输出与归档口径一致（不带「省」「市」后缀）",
          not bad, str(bad) if bad else f"{len(cases)} 个样例全对")

    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
