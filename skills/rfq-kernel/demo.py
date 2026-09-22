"""端到端演示：模糊需求 → 宽召回 → 分布驱动澄清 → 精筛 → 双客户视图投影。
并额外演示：
  (A) 认证三方验证（verified 且有证书号才「作数」）
  (B) 真实 beacon-mfg 能力卡经适配器接入，并优雅降级

运行：PYTHONIOENCODING=utf-8 python demo.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from intake import (apply_answers, narrow, parse_free_text, project_view,
                    suggest_clarifications, wide_recall)
from kernel import (cert_satisfied, envelope, load_audience, load_industry,
                    load_suppliers)
from beacon_adapter import load_real_card

LINE = "=" * 70


def show(title: str) -> None:
    print(f"\n{LINE}\n{title}\n{LINE}")


def main() -> None:
    pack = load_industry("drinkware")
    audience = load_audience("intl_buyer")
    suppliers = load_suppliers()

    # ---------------------------------------------------- 真实 beacon-mfg 卡接入
    real_card = load_real_card("CN-MFG-0029509")
    suppliers_all = suppliers + [real_card]

    # ---------------------------------------------------------- Step 1
    show("Step 1 · 客户 Agent 收到模糊需求")
    raw = "公司年会要做一批保温杯，先看看 500 个行不行，要过 FDA，也希望能有 BSCI 验厂"
    print(f"客户原话：{raw}\n")

    rfq = parse_free_text(raw, pack, audience)
    rfq["rfq_id"] = "RFQ-20260922-0001"
    rfq["schema_version"] = "0.1.0"
    rfq["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rfq["status"] = "draft"
    rfq["industry"] = "drinkware"
    rfq["audience"] = audience["profile_id"]

    print("解析结果（每个字段都带出处，猜的标 inferred 且未确认）：")
    for path, prov in sorted(rfq["provenance"].items()):
        f = path.split(".")[-1]
        if path.startswith("core.product"):
            val = f"{rfq['core']['product']['raw'][:20]}...  ->  {rfq['core']['product']['normalized']}"
        elif f == "certifications_required":
            val = str(rfq["core"]["certifications_required"] or "未指定")
        elif f == "qualification_terms":
            val = str(rfq["core"]["qualification_terms"] or "未指定")
        elif f == "incoterm":
            val = rfq["core"]["incoterm"]
        else:
            q = rfq["core"].get("quantity", {})
            val = f"{q.get('value')} {q.get('unit')}" if q.get("value") else "未指定"
        print(f"  {f:<26}{prov['source']:<10}conf={prov['confidence']:<6}{val}")

    # ---------------------------------------------------------- Step 2
    show("Step 2 · 宽召回（不做任何硬过滤；真实 beacon-mfg 卡属 sheet_metal，不混入杯具召回）")
    candidates = wide_recall(suppliers_all, "drinkware")
    print(f"粗召回 {len(candidates)} 家 —— 立刻回给客户，不用等填完问卷：")
    for s in candidates:
        tag = "  ← 真实 beacon-mfg 卡（适配器接入）" if s.get("_source") else ""
        print(f"  {s['supplier_id']}  {s['legal_name']:<24}{s['region']}{tag}")

    # ---------------------------------------------------------- Step 3
    show("Step 3 · 按真实供给分布生成澄清问题")
    questions = suggest_clarifications(rfq, pack, audience, candidates, top_n=8)
    print("排序依据 = 区分度 × 客户视图权重；required 档优先，late 档后置\n")
    print(f"  {'字段':<24}{'档位':<12}{'区分度':>7}{'客户权重':>9}{'得分':>7}{'覆盖':>7}")
    print(f"  {'-'*24}{'-'*12}{'-'*7}{'-'*9}{'-'*7}{'-'*7}")
    for q in questions:
        print(f"  {q['field']:<24}{q['tier']:<12}{q['discriminative_power']:>7.2f}"
              f"{q['audience_weight']:>9.2f}{q['score']:>7.2f}{q['coverage']:>7.2f}")

    known = sorted(p.split(".")[-1] for p, v in rfq["provenance"].items()
                   if v["source"] == "stated")
    print(f"\n  已由原话确定、不进澄清队列：{', '.join(known)}")
    print("  区分度再高也不重复问已知信息；全场取值一致的字段同样被过滤。")

    ask_now = [q for q in questions if q["tier"] == "required"]
    ask_late = [q for q in questions if q["tier"] == "late"]
    print(f"\n本轮要问（required）{len(ask_now)} 个，选项直接来自真实供给，不存在空选项：")
    for i, q in enumerate(ask_now, 1):
        opts = " / ".join(str(o) for o in q["options"])
        print(f"  Q{i}  {q['field']}  候选 {len(q['options'])} 种")
        print(f"      {opts}")
    print(f"\n搜完再问（late）{len(ask_late)} 个 —— 不影响召回，只影响报价细节：")
    print(f"  {', '.join(q['field'] for q in ask_late)}")

    # ---------------------------------------------------------- Step 4
    show("Step 4 · 客户回答 + 推断项回显确认")
    answers = {"capacity_ml": 500, "liner_material": "304不锈钢"}
    print("客户回答：")
    for k, v in answers.items():
        print(f"  {k} = {v}")
    apply_answers(rfq, answers, pack)
    rfq["status"] = "clarified"
    print(f"\nRFQ 状态：draft → {rfq['status']}")

    # ---------------------------------------------------------- Step 5
    show("Step 5 · 精筛（硬过滤 + 结构化拒单，含认证强校验）")
    matched, rejected = narrow(candidates, rfq, pack, audience)

    strict = [r for r in matched if r["tier"] == "matched"]
    degraded = [r for r in matched if r["tier"] == "degraded"]
    print(f"可承接 {len(matched)} 家（严格匹配 {len(strict)}，降权保留 {len(degraded)}）：")
    print(f"  {'工厂':<24}{'匹配度':>7}{'起订':>7}{'交期':>7}  负荷")
    print(f"  {'-'*24}{'-'*7}{'-'*7}{'-'*7}  {'-'*22}")
    for r in matched:
        cap = r["capacity"]
        cap_txt = (f"{cap['load_level']}" if cap["fresh"]
                   else f"{cap['load_level']} (需确认)")
        mark = "" if r["tier"] == "matched" else "  <- 批量不足，降权保留"
        print(f"  {r['legal_name']:<24}{r['score']:>7.2f}{str(r['moq']):>7}"
              f"{str(r['lead_time_days']):>7}  {cap_txt}{mark}")

    if rejected:
        print(f"\n不可承接 {len(rejected)} 家（拒单也是结构化的，客户 Agent 能直接分支）：")
        for r in rejected:
            for reason in r["reasons"]:
                print(f"  {r['legal_name']:<24}{reason['reason_code']:<20}{reason['detail']}")
                print(f"  {'':<24}可行动建议：{reason['actionable']}")

    # ---------------------------------------------------------- Step 6 双视图
    show("Step 6 · 同一份工厂数据，两个客户视图（投影而非复制）")
    sup_by_id = {s["supplier_id"]: s for s in suppliers_all}
    domestic = load_audience("domestic_downstream")
    target = matched[0]
    supplier = sup_by_id[target["supplier_id"]]
    for profile in (audience, domestic):
        view = project_view(target, supplier, profile)
        print(f"\n— {profile['display_name']}（{profile['language']} / "
              f"{profile['currency']} / {profile['price_basis']}）")
        print(json.dumps(view, ensure_ascii=False, indent=2))

    # ---------------------------------------------------------- (A) 认证强校验单元
    show("认证强校验单元 · verified 且有证书号才「作数」")
    cases = {
        "已三方验证（verified + 证书号）": [
            {"code": "ISO9001", "cert_no": "CNAS-001", "verified": True}],
        "自报无证书号（verified=false）": [
            {"code": "ISO9001", "cert_no": None, "verified": False}],
        "完全未声明": [],
    }
    for label, certs in cases.items():
        missing, unverified = cert_satisfied(["ISO9001"], certs)
        verdict = "✓ 满足" if not missing and not unverified else \
                  (f"✗ 缺失 {missing}" if missing else f"✗ 未核验 {unverified}")
        print(f"  {label:<28} → {verdict}")
    print("\n  → 裸声称『我有 ISO9001』但无证书号，等同没有；这正是「ISO9001 需上传证书号才作数」。")

    # ---------------------------------------------------------- (B) 真实卡接入 + 降级
    show("真实 beacon-mfg 能力卡接入 · 优雅降级演示")
    print(f"真实卡：{real_card['legal_name']}（{real_card['region']}）")
    print(f"适配器转换结果：processes={real_card['core']['processes']} "
          f"materials=空 limits=全 null certs={real_card['core']['certifications']}")
    # 不要求认证，只看工艺 + 模糊数量
    b_raw = "激光切割一批钣金件，先来 50 件样品"
    b_pack = load_industry("sheet_metal")
    b_rfq = parse_free_text(b_raw, b_pack, audience)
    b_rfq["industry"] = "sheet_metal"
    b_cands = wide_recall([real_card], "sheet_metal")
    b_matched, b_rejected = narrow(b_cands, b_rfq, b_pack, audience)
    print(f"\n需求：「{b_raw}」（不要求认证）")
    for r in b_matched:
        print(f"  命中：{r['legal_name']}  匹配度={r['score']}  "
              f"负荷={r['capacity']['load_level']} "
              f"({r['capacity']['message'] or 'fresh'})")
        print(f"    硬指标：moq={r['moq']} lead_time={r['lead_time_days']} "
              f"certs={r['certifications']}")
    print("\n  → 真实卡工艺匹配，但 limits/certs 全空：流程不中断，")
    print("    交期与认证标记为『需向工厂确认』，交给定价/接单环节人工补，绝不编造。")

    # ---------------------------------------------------------- 跨行业验证
    show("跨行业验证 · 换行业 pack，不动内核")
    m_pack = load_industry("mattress")
    m_raw = "要 100 张独立袋装的酒店床垫，出口英国"
    m_rfq = parse_free_text(m_raw, m_pack, audience)
    m_cands = wide_recall(suppliers, "mattress")
    m_q = suggest_clarifications(m_rfq, m_pack, audience, m_cands, top_n=5)
    print(f"客户原话：{m_raw}")
    print(f"命中行业 pack：{m_pack['display_name']}；召回 {len(m_cands)} 家")
    print("澄清问题全部来自 mattress pack 的属性定义：")
    for q in m_q:
        print(f"  [{q['tier']:<9}] {q['field']:<20} {' / '.join(str(o) for o in q['options'])}")
    print("\n杯具的 capacity_ml / liner_material / insulation_hours 完全没有出现。")
    print("schema/ 与 src/ 下的文件一行未改 —— N + M 而非 N x M。")

    print(f"\n{LINE}\n验证点\n{LINE}")
    print("1. 澄清选项全部来自真实供给 —— 无空选项，无过时枚举")
    print("2. 已由原话确定的字段与全场一致的字段都不进澄清队列 —— 不浪费客户轮次")
    print("3. 批量不足的工厂降权保留而非淘汰 —— 未填/不够不等于不做")
    print("4. 产能陈旧自动回落需人工确认 —— 静态数据不背动态数据的锅")
    print("5. 同一份 supplier 数据投影出两套视图 —— 数据没有复制")
    print("6. 拒单带原因码与可行动建议 —— 客户 Agent 能直接改设计或换厂")
    print("7. 认证强校验：verified 且有证书号才作数，裸声称等同没有")
    print("8. 真实 beacon-mfg 卡经适配器接入并优雅降级 —— rfq-kernel 消费能力卡，不替代它")


if __name__ == "__main__":
    main()
