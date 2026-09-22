"""需求侧流程：宽召回 → 分布驱动澄清 → 精筛 → 标准 RFQ。

设计要点：
- 澄清问题的选项来自真实供给分布，不依赖预设枚举。
- 推断值必须回显确认，不接受静默填充。
- 精筛保留不淘汰：未填字段降权，不直接出局。
- 认证强校验：只有 verified 且有证书号（三方可验证）才作数。
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone

from kernel import (cert_satisfied, default, envelope, inferred, load_audience,
                    load_industry, load_suppliers, resolve_term, stated, unknown,
                    capacity_state)

# 澄清问题的三档
REQUIRED, INFERRABLE, LATE = "required", "inferrable", "late"


# ------------------------------------------------------------------ 1. 解析

def parse_free_text(text: str, pack: dict, audience: dict) -> dict:
    """从客户原话里抽取已知信息。抽不出的留空，绝不填猜测值。"""
    rfq: dict = {"core": {}, "industry_ext": {}, "provenance": {}}

    canonical, conf = resolve_term(text, pack)
    rfq["core"]["product"] = {"raw": text, "normalized": canonical,
                              "confidence": round(conf, 2)}
    rfq["provenance"]["core.product.normalized"] = stated("取自客户原话归一化")

    # 数量：中文数字与阿拉伯数字都取，能推断的标 inferred 等回显
    qty = None
    for token in text.replace("，", ",").split(","):
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits and any(k in token for k in ("个", "件", "只", "pcs", "条", "张", "张", "批", "套")):
            qty = int(digits)
            break
    if qty is None and any(k in text for k in ("样品", "打样", "先来一个", "先来一批")):
        qty = 3
        rfq["provenance"]["core.quantity.value"] = inferred(
            0.6, "客户提到样品/打样，推断为 1~5 件")
    elif qty is not None:
        rfq["provenance"]["core.quantity.value"] = stated()
    else:
        rfq["provenance"]["core.quantity.value"] = unknown()

    rfq["core"]["quantity"] = {"value": qty, "unit": "pcs"}

    # 认证码是行业概念，标准码来自 pack；用标准码比对，不用人话整串比对。
    certs = [c for c in pack.get("certifications", []) if c.lower() in text.lower()]
    rfq["core"]["certifications_required"] = certs
    rfq["provenance"]["core.certifications_required"] = (
        stated("从原话中识别到标准认证码") if certs else unknown("客户未提合规要求"))

    # 资格词属客户视图范畴（描述交易条件，不是产品本身），不参与工厂过滤。
    quals = [t for t in audience.get("qualification_terms", [])
             if any(k.lower() in text.lower() for k in t.split())]
    rfq["core"]["qualification_terms"] = quals
    rfq["provenance"]["core.qualification_terms"] = (
        stated("命中客户视图资格词") if quals else unknown("未提及交易资格要求"))

    rfq["core"]["incoterm"] = audience["default_incoterm"]
    rfq["provenance"]["core.incoterm"] = default(
        f"按 {audience['display_name']} 习惯默认 {audience['default_incoterm']}")

    return rfq


# ------------------------------------------------------------------ 2. 宽召回

def wide_recall(suppliers: list[dict], pack_id: str) -> list[dict]:
    """只做宽松语义匹配，不做任何硬过滤。目的是让澄清选项有真实来源。"""
    return [s for s in suppliers if pack_id in s.get("industry", [])]


# --------------------------------------------------- 3. 分布驱动的澄清问题

# RFQ 字段名 → 工厂能力卡字段名
FIELD_ALIAS = {"certifications_required": "certifications"}


def _tokens(supplier: dict, field: str) -> list[str]:
    field = FIELD_ALIAS.get(field, field)
    for scope in ("core", "industry_ext"):
        node = supplier.get(scope, {})
        if field in node:
            return _discretize(node[field])
    return []


def _discretize(value) -> list[str]:
    if isinstance(value, list):
        out = []
        for v in value:
            if isinstance(v, dict) and "code" in v:
                # 认证对象：verified 且有证书号才标「(已核验)」，否则「(未核验)」
                mark = "" if (v.get("verified") and v.get("cert_no")) else " (未核验)"
                out.append(str(v["code"]) + mark)
            else:
                out.append(str(v))
        return out
    if isinstance(value, dict) and "min" in value:
        return [f"{value['min']}-{value['max']}"]
    return [str(value)]


def discriminative_power(field: str, candidates: list[dict]) -> dict:
    """这个问题能把候选集切得多开？

    power = 覆盖率 × 归一化熵。
    全部供应商取值相同的字段（例如人人都有的认证）power=0，问了纯浪费轮次。
    """
    per_supplier = [_tokens(s, field) for s in candidates]
    covered = sum(1 for t in per_supplier if t)
    if covered == 0:
        return {"power": 0.0, "distinct": 0, "coverage": 0.0, "hint": "无人声明"}

    flat = [t for toks in per_supplier for t in toks]
    dist = Counter(flat)
    distinct = len(dist)
    if distinct <= 1:
        return {"power": 0.0, "distinct": distinct, "coverage": covered / len(candidates),
                "hint": "所有候选一致，问了不改变结果"}

    total = sum(dist.values())
    entropy = -sum((c / total) * math.log(c / total) for c in dist.values())
    norm_entropy = entropy / math.log(distinct)

    return {
        "power": round((covered / len(candidates)) * norm_entropy, 3),
        "distinct": distinct,
        "coverage": round(covered / len(candidates), 2),
        "distribution": dict(dist.most_common()),
        "hint": "分布分散，优先澄清",
    }


def suggest_clarifications(rfq: dict, pack: dict, audience: dict,
                           candidates: list[dict], top_n: int = 3) -> list[dict]:
    """澄清问题的排序 = 区分度 × 客户视图权重。

    选项集直接来自召回结果的真实取值，避免空选项与过时枚举。
    """
    known = set()
    for path, prov in rfq["provenance"].items():
        if prov["source"] in ("stated", "inferred") and prov.get("confidence", 0) > 0:
            known.add(path.split(".")[-1])

    fields = {k: v for k, v in pack["attributes"].items() if k not in known}
    if "certifications_required" not in known:
        fields["certifications_required"] = {
            "label": "合规认证", "type": "list", "tier": REQUIRED}

    weights = audience.get("field_weights", {})
    questions = []
    for field, spec in fields.items():
        info = discriminative_power(field, candidates)
        if info["power"] <= 0:
            continue
        options = sorted(info.get("distribution", {}).keys())
        questions.append({
            "field": field,
            "prompt": f"{spec['label']}？（候选集中共 {info['distinct']} 种）",
            "tier": spec.get("tier", LATE),
            "options": options,
            "default": None,
            "discriminative_power": info["power"],
            "audience_weight": weights.get(field, 0.5),
            "score": round(info["power"] * weights.get(field, 0.5), 3),
            "coverage": info["coverage"],
            "hint": info["hint"],
        })

    # required 档优先，late 档后置；同档内按得分降序
    tier_rank = {REQUIRED: 0, INFERRABLE: 1, LATE: 2}
    questions.sort(key=lambda q: (tier_rank.get(q["tier"], 3), -q["score"]))
    return questions[:top_n]


def apply_answers(rfq: dict, answers: dict[str, str], pack: dict,
                  confirmed: bool = True) -> None:
    """把澄清答案写回 RFQ，并记录出处。"""
    for field, value in answers.items():
        if field == "certifications_required":
            rfq["core"][field] = [value] if isinstance(value, str) else value
        else:
            rfq["industry_ext"][field] = value
        rfq["provenance"][field] = {
            "source": "stated", "confidence": 1.0,
            "confirmed": confirmed, "note": "客户澄清轮次提供",
        }


# ------------------------------------------------------------------ 4. 精筛

def _reason(field: str) -> str:
    return {
        "quantity": "CAP.MOQ",
        "certifications_required": "CAP.CERT_MISSING",
        "liner_material": "CAP.MATERIAL",
        "size": "CAP.OVERSIZE",
        "fire_retardant": "CAP.CERT_MISSING",
    }.get(field, "CAP.TOLERANCE")


def narrow(candidates: list[dict], rfq: dict, pack: dict,
           audience: dict) -> tuple[list[dict], list[dict]]:
    """硬过滤。同时给出结构化拒单原因码，让客户 Agent 能直接分支处理。"""
    matched, rejected = [], []

    for s in candidates:
        core, ext = s.get("core", {}), s.get("industry_ext", {})
        reasons: list[dict] = []

        qty = rfq["core"].get("quantity", {}).get("value")
        if qty and qty < (core.get("moq") or 0):
            reasons.append({
                "reason_code": "CAP.MOQ",
                "field": "moq",
                "detail": f"起订量 {core['moq']}，来单 {qty}",
                "actionable": f"补足到 {core['moq']} 件可承接",
            })

        # 认证强校验：只接受 verified 且有证书号的认证
        need_certs = rfq["core"].get("certifications_required") or []
        if need_certs:
            missing, unverified = cert_satisfied(need_certs, core.get("certifications", []))
            for c in missing:
                reasons.append({
                    "reason_code": "CAP.CERT_MISSING",
                    "field": "certifications_required",
                    "detail": f"缺 {c}",
                    "actionable": "该工厂无此认证，换厂或放宽要求",
                })
            for c in unverified:
                reasons.append({
                    "reason_code": "CAP.CERT_UNVERIFIED",
                    "field": "certifications_required",
                    "detail": f"{c} 仅有自报、无三方可验证证书号",
                    "actionable": f"向工厂索要 {c} 证书号核验，或换厂",
                })

        for field, attr in pack["attributes"].items():
            want = rfq["industry_ext"].get(field)
            if want is None:
                continue
            have = ext.get(field)
            if have is None:
                continue
            if attr["type"] == "enum" and isinstance(have, list):
                if want not in have:
                    reasons.append({
                        "reason_code": _reason(field),
                        "field": field,
                        "detail": f"可做 {', '.join(have)}，不含 {want}",
                        "actionable": f"改选 {have[0]} 或换厂",
                    })
            elif attr["type"] == "number" and isinstance(have, dict):
                if not (have["min"] <= want <= have["max"]):
                    reasons.append({
                        "reason_code": _reason(field),
                        "field": field,
                        "detail": f"能力区间 {have['min']}-{have['max']}{attr.get('unit','')}，来单 {want}",
                        "actionable": "超出该厂能力上限，换厂",
                    })

        cap = capacity_state(core.get("capacity", {}))
        # must_surface 用 RFQ 字段名，工厂卡用能力卡字段名，须经别名映射
        weights = audience.get("field_weights", {})
        total_w = sum(weights.values()) or 1.0
        present_w = sum(w for f, w in weights.items()
                        if core.get(FIELD_ALIAS.get(f, f)) not in (None, [], False))
        base = present_w / total_w
        bonus = 0.0
        if cap["fresh"]:
            bonus += {"light": 0.10, "moderate": 0.05, "heavy": 0.0}.get(
                cap["load_level"], 0.0)
        bonus += max(0.0, (45 - (core.get("lead_time_days") or 45)) / 100)
        score = base * 0.75 + bonus

        record = {
            "supplier_id": s["supplier_id"],
            "legal_name": s["legal_name"],
            "region": s["region"],
            "score": round(min(score, 1.0), 2),
            "moq": core.get("moq"),
            "lead_time_days": core.get("lead_time_days"),
            "certifications": core.get("certifications"),
            "capacity": cap,
            "matched_fields": [f for f in rfq["industry_ext"]
                               if f in ext],
            "negative_capability": s.get("negative_capability", []),
            "evidence": s.get("evidence", []),
        }

        if reasons:
            low = [r for r in reasons if r["reason_code"] == "CAP.MOQ"]
            # 未填不淘汰：只因批量不足出局的，降权保留在备选里
            if len(low) == len(reasons):
                record["reasons"] = reasons
                record["tier"] = "degraded"
                matched.append(record)
            else:
                record["reasons"] = reasons
                record["tier"] = "rejected"
                rejected.append(record)
        else:
            record["tier"] = "matched"
            record["reasons"] = []
            matched.append(record)

    matched.sort(key=lambda r: (r["tier"] != "matched", -r["score"]))
    return matched, rejected


# --------------------------------------------------------- 5. 客户视图投影

def project_view(record: dict, supplier: dict, profile: dict) -> dict:
    """同一份数据，换个镜头看。投影不复制。"""
    term_map = profile.get("term_map", {})
    suppress = set(profile.get("suppress_fields", []))
    core = supplier["core"]

    view = {
        "supplier": record["legal_name"],
        "region": record["region"],
        "price_basis": profile["price_basis"],
        "currency": profile["currency"],
    }
    for field in profile["must_surface"]:
        if field in suppress:
            continue
        if field == "certifications_required":
            view[term_map.get(field, field)] = _summarize_certs(core.get("certifications"))
        else:
            view[term_map.get(field, field)] = core.get(field)

    cap = record["capacity"]
    view["capacity_note"] = (
        f"负荷 {cap['load_level']}（{cap.get('age_days', '?')} 天前采样）"
        if cap["fresh"] else cap["message"]
    )
    supported: list[str] = []
    pay = str(core.get("payment_terms", ""))
    for t in profile.get("qualification_terms", []):
        if core.get("spot_stock") and ("现货" in t or "当天" in t):
            supported.append(t)
        elif ("账期" in t or "月结" in t) and ("月结" in pay or "账期" in pay):
            supported.append(t)
        elif ("小批量" in t or "MOQ" in t or "ODM" in t) and (core.get("moq") or 9999) <= 500:
            supported.append(t)
    view["qualification_terms"] = supported or ["—"]
    return view


def _summarize_certs(certs: list) -> str:
    if not certs:
        return "无"
    parts = []
    for c in certs:
        if isinstance(c, dict):
            ok = c.get("verified") and c.get("cert_no")
            parts.append(f"{c.get('code')}{' ✓' if ok else ' (未核验)'}")
    return "、".join(parts) if parts else "无"
