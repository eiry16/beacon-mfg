"""MCP 桥接层：把 beacon-mfg 指纹召回 + rfq-kernel 引擎串成「多轮对话匹配」。

设计边界（与整个 beacon-mfg MCP 只读原则一致）：
- 仅依赖同目录的 kernel / intake / beacon_adapter，零第三方依赖。
- 召回走 MCP 已有的指纹分片读取；富化走 capability 卡读取；绝不写任何数据。
- 长尾（14294 家多数只有 fingerprint、无 capability 卡）也能结构化初筛：
  fingerprint 的 proc/mat/cert/city 稀疏字段 → 标准卡，按 core 层做认证强校验与排序。

对外暴露三个无状态函数，由 MCP 服务端持有 session 状态并跨 tool 调用串联：
  build_session / answer_session / refine_session
"""
from __future__ import annotations

import math
from collections import Counter

from kernel import capacity_state
from intake import parse_free_text, narrow
from beacon_adapter import fingerprint_to_card, load_real_card, pack_of_gb, GB_TO_PACK

# 行业 pack 候选 id（含 G5 补齐的 7 类 + 原有 3 类 + material_handling）
_PACK_IDS = [
    "drinkware", "mattress", "sheet_metal",
    "machining", "injection", "die_casting", "electronics",
    "surface_treatment", "fasteners", "raw_material",
    "material_handling",
]
_PACKS: dict = {}


def _all_packs() -> dict:
    global _PACKS
    if not _PACKS:
        from kernel import load_industry
        for pid in _PACK_IDS:
            try:
                _PACKS[pid] = load_industry(pid)
            except Exception:
                pass
    return _PACKS


# 名称感知防误删：当候选供应商的厂名含本 pack 的具体产品词组时，
# 即使其国标码(gb)被 pack_of_gb 归到「另一个 pack」，也视为本行业、绝不误删。
# 这是 beacon-mfg 红线「真实企业必须被看见」的兜底：许多长尾厂把品类写在厂名里，
# 国标码却落在别的类（如输送厂 gb 常是 3360/3399/3451/3484 而非 3434）。
# 关键词只收「具体产品词组」，避免歧义单字（如 滚筒 会撞 滚筒洗衣机、物流/设备 过宽）。
_PACK_NAME_HINTS = {
    "sheet_metal": ["钣金", "冲压件", "冲压加工", "金属结构件", "机箱", "机柜", "金属外壳"],
    "machining": ["机加工", "机械加工", "数控", "精密机械", "机械零部件", "非标零件", "非标加工"],
    "injection": ["注塑", "塑料件", "塑胶", "注塑件"],
    "die_casting": ["压铸", "铸造"],
    "electronics": ["电路板", "PCBA", "线路板", "电子元件", "芯片", "SMT", "连接器", "线束"],
    "surface_treatment": ["表面处理", "阳极氧化", "电镀", "喷涂", "电泳", "热处理"],
    "fasteners": ["螺丝", "螺栓", "螺母", "紧固件", "标准件", "铆钉", "垫圈"],
    "raw_material": ["钢材", "铝材", "不锈钢", "铝型材", "铜材", "原材料"],
    "drinkware": ["保温杯", "随行杯", "真空杯", "玻璃杯", "水壶", "保温壶"],
    "mattress": ["床垫", "席梦思", "弹簧床垫", "乳胶垫", "记忆棉"],
    "material_handling": [
        "输送线", "输送机", "输送设备", "输送机械", "输送带", "传送带",
        "传输设备", "传输线", "流水线", "滚筒线", "皮带线", "链板线",
        "倍速链", "分拣线", "提升机", "连续搬运",
    ],
}


def _load_audience(aid: str) -> dict:
    from kernel import load_audience
    try:
        return load_audience(aid)
    except Exception:
        return load_audience("domestic_downstream")


# ----------------------------------------------------------------- 品类检测
def detect_industry(text: str) -> str | None:
    """跨所有 pack 的 vocab 扫描，取最长命中的 surface 对应的 pack。

    返回 pack_id；无任何命中返回 None（此时走通用澄清，仅靠卡片字段）。
    """
    best = None  # (score_tuple, pack_id)
    for pid, pack in _all_packs().items():
        for surface, meta in pack.get("vocab", {}).items():
            if surface.lower() in text.lower():
                score = (len(surface), meta.get("confidence", 0.5))
                if best is None or score > best[0]:
                    best = (score, pid)
    return best[1] if best else None


def pack_vocab_surfaces(pack_id: str | None) -> list:
    """返回某 pack 的产品词 surface 列表，供召回时并入检索 query 以收敛行业。"""
    if not pack_id:
        return []
    pack = _all_packs().get(pack_id)
    return list((pack or {}).get("vocab", {}).keys())


def pack_recall_terms(pack_id: str | None) -> list:
    """召回扩词：并入检索 query 的「精准」产品词组。

    与 vocab 解耦 —— vocab 承 full 词面（含 2 字泛词如「输送」「设备」），用于
    detect_industry / resolve_term；而召回侧若把全量 vocab 拼进 query，宽泛 2 字词
    （尤其「设备」「机械」作子串）会把 OR 召回池冲到 top_k 上限，反而把仅靠厂名弱匹配的
    长尾真实企业（如「青岛环球输送带洛阳轴承」只命中「输送带」）挤出候选。
    故召回扩词只取 pack 显式声明的 `recall_terms`（精准词组，不含宽泛子串）；
    未声明时回退到全量 vocab（旧行为，其他 pack 维持不变）。
    """
    if not pack_id:
        return []
    pack = _all_packs().get(pack_id)
    if not pack:
        return []
    rt = pack.get("recall_terms")
    if isinstance(rt, list) and rt:
        return list(rt)
    return list(pack.get("vocab", {}).keys())


def gbs_of_pack(pack_id: str | None) -> set:
    """某 pack 对应的全部国标码（4 位小类 + 2 位门类）。供召回侧做「GB 种子」补召。

    语义：检测到某行业时，其国标码分类下的企业（如 conveyor 的 gb=3434 连续搬运设备制造）
    即便厂名不含召回词，也应在语义上归该行业、被召回。这是「产品词→国标码语义映射」的落地。
    加法召回，绝不误删真实企业；+1000 国标命中加成自然把它们顶到前列。
    """
    out: set = set()
    if not pack_id:
        return out
    for code, pid in GB_TO_PACK.items():
        if pid == pack_id:
            out.add(code)
    if pack_id == "raw_material":      # 31 黑色 / 32 有色 冶炼压延
        out.update({"31", "32"})
    elif pack_id == "electronics":     # 39 计算机通信电子设备
        out.add("39")
    return out


# ------------------------------------------------- 区分度（复用内核思路）
def _power(values_per_candidate: list) -> tuple:
    """power = 覆盖率 × 归一化熵。返回 (power, distribution)。"""
    covered = sum(1 for v in values_per_candidate if v)
    if covered == 0:
        return 0.0, {}
    flat = [x for vs in values_per_candidate for x in (vs or [])]
    if not flat:
        return 0.0, {}
    dist = Counter(flat)
    distinct = len(dist)
    if distinct <= 1:
        return 0.0, dict(dist.most_common())
    total = sum(dist.values())
    entropy = -sum((c / total) * math.log(c / total) for c in dist.values())
    norm_entropy = entropy / math.log(distinct)
    return round((covered / len(values_per_candidate)) * norm_entropy, 3), dict(dist.most_common())


def _values_per(cards: list, getter) -> list:
    return [getter(c) for c in cards]


_MAX_CLARIFY_OPTIONS = 12   # 单个澄清维度最多给几个候选值（降低客户 agent/人的选择负荷）


def _freq_options(values_per_card: list) -> list:
    """把每卡的候选值聚合成按出现频次降序的去重列表（最高频在前，最多 _MAX_CLARIFY_OPTIONS 个）。

    澄清问题据此排序：高频（真正代表本行业的主流值）先问，
    避免 a 字母开头的跨行业噪声（anodizing/POM）浮到顶部误导客户 agent 选错过滤条件。
    长尾厂的能力字段很长，不加上限会一次抛几十个选项、反而增加使用负荷。
    """
    cnt = Counter()
    for vs in values_per_card:
        for v in (vs or []):
            if v:
                cnt[v] += 1
    return [k for k, _ in cnt.most_common(_MAX_CLARIFY_OPTIONS)]


# ----------------------------------------------------- 澄清问题生成
def _build_clarifications(cards: list, pack: dict, rfq: dict, exclude: list,
                          max_n: int = 2) -> list:
    """基于真实召回分布生成澄清问题。仅保留 power>0 且未被排除的维度。

    维度来自卡片实际携带的字段（不依赖 pack.attributes，因为长尾卡片
    industry_ext 为空，结构化字段在 core 层：材料/工艺/地区 + 认证）。
    """
    dims = {}

    # 认证：优先问 pack 认证 ∩ 候选认证；按出现频次排序（高频认证先问，避免冷门认证占位）
    pack_certs = set(pack.get("certifications", []) or [])
    cert_cnt = Counter()
    for c in cards:
        for cert in c["core"].get("certifications", []):
            code = cert.get("code") if isinstance(cert, dict) else str(cert)
            if code:
                cert_cnt[code] += 1
    if pack_certs:
        cert_opts = [k for k in cert_cnt if k in pack_certs]
        cert_opts.sort(key=lambda k: -cert_cnt[k])
    else:
        cert_opts = [k for k, _ in cert_cnt.most_common()]
    cert_opts = cert_opts[:_MAX_CLARIFY_OPTIONS]
    if cert_opts:
        dims["certifications_required"] = (
            "合规认证", cert_opts,
            _values_per(cards, lambda c: [cert.get("code") if isinstance(cert, dict) else str(cert)
                                          for cert in c["core"].get("certifications", [])]))

    # 材料 / 工艺 / 地区：来自 core 层，按频次降序（高频主流值先问，抑制跨行业噪声浮顶）
    dims["material"] = (
        "材料", _freq_options(_values_per(cards, lambda c: list(c["core"].get("materials") or []))),
        _values_per(cards, lambda c: list(c["core"].get("materials") or [])))
    dims["process"] = (
        "工艺", _freq_options(_values_per(cards, lambda c: list(c["core"].get("processes") or []))),
        _values_per(cards, lambda c: list(c["core"].get("processes") or [])))
    dims["region"] = (
        "地区", _freq_options(_values_per(cards, lambda c: [c.get("region")] if c.get("region") else [])),
        _values_per(cards, lambda c: [c.get("region")] if c.get("region") else []))

    questions = []
    for field, (label, options, vpc) in dims.items():
        if field in exclude:
            continue
        if field == "certifications_required" and rfq["core"].get("certifications_required"):
            continue
        if field in ("material", "process", "region") and field in exclude:
            continue
        power, dist = _power(vpc)
        if power <= 0 or len(options) <= 1:
            continue
        questions.append({
            "field": field,
            "prompt": f"{label}有要求吗？（候选中常见：{', '.join(options[:6])}）",
            "options": options,
            "discriminative_power": power,
            "top_distribution": dict(list(dist.items())[:6]),
        })
    questions.sort(key=lambda q: -q["discriminative_power"])
    return questions[:max_n]


# ------------------------------------------------------- session 构造
_GENERIC_PACK = {"attributes": {}, "certifications": [], "vocab": {}}


def build_session(demand_text: str, recs: list, pack_id: str | None,
                  audience_id: str = "domestic_downstream") -> tuple:
    """构造第一轮 session。recs 为带 '_recall_relevance' 的指纹记录。"""
    pack = _all_packs().get(pack_id) or _GENERIC_PACK
    audience = _load_audience(audience_id)

    cards = []
    relevance = {}
    for r in recs:
        card = fingerprint_to_card(r, pack_id)
        rel = r.get("_recall_relevance", 0.0)
        card["_recall_relevance"] = rel
        relevance[card["supplier_id"]] = rel
        cards.append(card)

    # 若检测到行业，用国标码(GB)把召回池收敛到本行业，剔除跨行业噪声
    # （如注塑厂/POM、表面处理/anodizing 混入「钣金冲压」查询）。
    # 判别规则保守：仅当记录的 gb 明确映射到「另一个 pack」时才剔除；
    # gb 为空或未知的一律保留，绝不误删真实企业（beacon-mfg 红线）。
    # 若剔除后池将变空（极端情况），则放弃剔除，保留原池。
    if pack_id:
        hints = _PACK_NAME_HINTS.get(pack_id, [])
        def _cross(card):
            pg = pack_of_gb(card.get("gb"))
            if pg is not None and pg != pack_id:
                # 名称感知兜底：厂名含本 pack 具体产品词组 → 视为本行业，保住真实企业
                nm = card.get("legal_name") or ""
                if hints and any(h in nm for h in hints):
                    return False
                return True
            return False
        dropped = [c for c in cards if _cross(c)]
        if dropped and len(cards) > len(dropped):
            cards = [c for c in cards if not _cross(c)]

    rfq = parse_free_text(demand_text, pack, audience)
    # 不依赖 pack 也能抓出客户原话里点名的认证码
    lower = demand_text.lower()
    mentioned = [c for c in set(crt.get("code", "")
                              for card in cards
                              for crt in card["core"].get("certifications", []))
                 if c and c.lower() in lower]
    if mentioned:
        existing = set(rfq["core"].get("certifications_required") or [])
        rfq["core"]["certifications_required"] = sorted(existing | set(mentioned))

    state = {
        "demand_text": demand_text,
        "pack_id": pack_id,
        "audience_id": audience_id,
        "cards": cards,
        "rfq": rfq,
        "filters": {"material": None, "process": None, "region": None},
        "asked_fields": [],
        "round": 0,
        "relevance": relevance,
    }
    questions = _build_clarifications(cards, pack, rfq, exclude=[], max_n=3)
    resp = {
        "stage": "clarifying",
        "round": 1,
        "industry_detected": pack_id,
        "candidates_found": len(cards),
        "clarifying_questions": questions,
        "note": f"已为你找到约 {len(cards)} 家相关工厂，先确认几个关键点以便精准初筛。",
    }
    return state, resp


def _apply_filters(cards: list, filters: dict) -> list:
    out = []
    for c in cards:
        if filters["material"] and filters["material"] not in (c["core"].get("materials") or []):
            continue
        if filters["process"] and filters["process"] not in (c["core"].get("processes") or []):
            continue
        if filters["region"] and filters["region"] != c.get("region"):
            continue
        out.append(c)
    return out


# ----------------------------------------------------- 认证评估（G6 在长尾生效）
def _eval_certs(card: dict | None, required: list) -> dict:
    """评估一张卡是否满足 RFQ 要求的认证。

    返回 missing / unverified / verified_ok。长尾 fingerprint 卡的认证只有裸字符串
    → verified=false、cert_no=None → 归入 unverified（G6：无证书号即未核验）。
    仅在能力卡经平台/现场核验且带证书号时才算 verified。
    """
    if not required:
        return {"required": [], "missing": [], "unverified": [], "verified_ok": True}
    by_code: dict = {}
    for c in (card or {}).get("core", {}).get("certifications", []):
        code = c.get("code") if isinstance(c, dict) else str(c)
        by_code.setdefault(code, []).append(c)
    missing, unverified = [], []
    for code in required:
        entries = by_code.get(code, [])
        if not entries:
            missing.append(code)
        else:
            ok = any((e.get("verified") if isinstance(e, dict) else False)
                     and (e.get("cert_no") if isinstance(e, dict) else None)
                     for e in entries)
            if not ok:
                unverified.append(code)
    verified_ok = not missing and not unverified
    return {"required": list(required), "missing": missing,
            "unverified": unverified, "verified_ok": verified_ok}


def _run_narrow(state: dict, pack: dict, audience: dict, filtered: list) -> tuple:
    """精筛 + 认证降级处理。

    关键：narrow 只做结构/材料/工艺/批量的硬过滤；认证单独评估——
    长尾指纹数据无证书号，认证问题降级标注（tier=degraded）而非硬淘汰；
    只有材料/工艺等硬不匹配才保持 rejected。认证强校验留给真正投递 RFQ 时执行。
    """
    required = list(state["rfq"]["core"].get("certifications_required") or [])
    saved = state["rfq"]["core"].get("certifications_required")
    state["rfq"]["core"]["certifications_required"] = []  # 先屏蔽，narrow 不做认证拒单
    matched, rejected = narrow(filtered, state["rfq"], pack, audience)
    state["rfq"]["core"]["certifications_required"] = saved

    for rec in matched + rejected:
        card = next((c for c in filtered if c["supplier_id"] == rec["supplier_id"]), None)
        ce = _eval_certs(card, required) if card else {
            "required": required, "missing": list(required), "unverified": [], "verified_ok": False}
        rec["cert_eval"] = ce
        rec["cert_score"] = (1.0 if not required else
                             round((len(required) - len(ce["missing"]) - len(ce["unverified"])) / len(required), 2))
        hard = [r for r in rec.get("reasons", []) if r["reason_code"] not in
                ("CAP.CERT_MISSING", "CAP.CERT_UNVERIFIED")]
        if ce["missing"] or ce["unverified"]:
            if not hard:  # 仅认证问题 → 降级标注，不淘汰
                rec["tier"] = "degraded"
            for code in ce["missing"]:
                rec.setdefault("reasons", []).append({
                    "reason_code": "CAP.CERT_MISSING", "field": "certifications_required",
                    "detail": f"未声明 {code}", "actionable": "向工厂确认是否具备该认证"})
            for code in ce["unverified"]:
                rec.setdefault("reasons", []).append({
                    "reason_code": "CAP.CERT_UNVERIFIED", "field": "certifications_required",
                    "detail": f"{code} 仅自报/指纹记录、无三方可验证证书号",
                    "actionable": f"向工厂索要 {code} 证书号核验"})
    return matched, rejected


def answer_session(state: dict, answers: dict) -> tuple:
    """应用澄清答案；若还需澄清（<2 轮）返回下一轮问题，否则进入精筛推荐。"""
    state["round"] += 1
    for field, val in (answers or {}).items():
        if field == "certifications_required":
            state["rfq"]["core"]["certifications_required"] = val if isinstance(val, list) else [val]
        elif field in ("material", "process", "region"):
            state["filters"][field] = val
        state["asked_fields"].append(field)

    pack = _all_packs().get(state["pack_id"]) or _GENERIC_PACK
    audience = _load_audience(state["audience_id"])
    filtered = _apply_filters(state["cards"], state["filters"])

    # 还有未问且区分度足够的维度，再来一轮（最多 2 轮澄清）
    if state["round"] < 2:
        more = _build_clarifications(filtered, pack, state["rfq"],
                                     exclude=state["asked_fields"], max_n=2)
        if more:
            return state, {
                "stage": "clarifying",
                "round": state["round"] + 1,
                "candidates_after_filter": len(filtered),
                "clarifying_questions": more,
                "note": "再确认一下，就可以给你初选推荐了。",
            }

    # 进入精筛 + 推荐
    matched, rejected = _run_narrow(state, pack, audience, filtered)
    recs = _rank(state, matched)
    resp = _recommend_response(state, recs, rejected, filtered)
    return state, resp


def _rank(state: dict, matched: list) -> list:
    maxrel = max((state["relevance"].get(m["supplier_id"], 0) for m in matched), default=1) or 1
    for m in matched:
        rel = state["relevance"].get(m["supplier_id"], 0) / maxrel
        m["recall_relevance"] = round(rel, 2)
        # 召回相关度 0.5 + 内核结构化分 0.3 + 认证满足度 0.2：认证已核验者优先
        m["blended_score"] = round(0.5 * rel + 0.3 * (m.get("score") or 0)
                                   + 0.2 * (m.get("cert_score") or 1.0), 2)
    matched.sort(key=lambda m: (m.get("tier") != "matched", -m["blended_score"]))
    return matched


def _summarize_certs(certs) -> str:
    if not certs:
        return "无"
    parts = []
    for c in certs:
        if isinstance(c, dict):
            ok = c.get("verified") and c.get("cert_no")
            parts.append(f"{c.get('code')}{' ✓' if ok else ' (未核验)'}")
        else:
            parts.append(f"{c} (未核验)")
    return "、".join(parts) if parts else "无"


def _enrich(rec: dict) -> None:
    """富化：有能力卡则带入三方可验证认证 + RFQ 可达性；否则按 fingerprint 未核验处理。"""
    sid = rec["supplier_id"]
    try:
        real = load_real_card(sid)
        rec["verified_certs"] = [c["code"] for c in real["core"]["certifications"]
                                 if c.get("verified") and c.get("cert_no")]
        rec["rfq_reachable"] = bool(real.get("rfq"))
        rec["rfq_endpoint"] = (real.get("rfq") or {}).get("endpoint")
        rec["claim_status"] = real.get("claim_status")
        rec["has_card"] = True
    except FileNotFoundError:
        rec["verified_certs"] = []
        rec["rfq_reachable"] = False
        rec["rfq_endpoint"] = None
        rec["claim_status"] = "unclaimed"
        rec["has_card"] = False
    rec["cert_summary"] = _summarize_certs(rec.get("certifications"))


def _recommend_response(state: dict, matched: list, rejected: list, filtered: list) -> dict:
    top = matched[:12]
    for r in top:
        _enrich(r)
    recommendable = [m for m in matched if m.get("tier") in ("matched", "degraded")]
    return {
        "stage": "recommending",
        "round": 1,
        "candidates_considered": len(filtered),
        "matched_count": len(recommendable),
        "rejected_count": len(rejected),
        "top_recommendations": [
            {
                "supplier_id": r["supplier_id"],
                "legal_name": r["legal_name"],
                "region": r["region"],
                "blended_score": r.get("blended_score"),
                "tier": r.get("tier"),
                "matched_fields": r.get("matched_fields"),
                "cert_flag": _cert_flag(r.get("cert_eval")),
                "reasons": r.get("reasons"),
                "cert_summary": r.get("cert_summary"),
                "verified_certs": r.get("verified_certs"),
                "rfq_reachable": r.get("rfq_reachable"),
                "claim_status": r.get("claim_status"),
                "has_card": r.get("has_card"),
            }
            for r in top
        ],
        "note": "以上为按需求匹配度初选的供应商（cert_flag=verified 表示三方可验证；"
                "unverified/missing 表示需向工厂确认）。发起询价请调用 refine_sourcing(details)。",
    }


def _cert_flag(ce: dict | None) -> str:
    if not ce or not ce.get("required"):
        return "na"
    if ce["verified_ok"]:
        return "verified"
    if ce["unverified"] and not ce["missing"]:
        return "unverified"
    return "missing"


def refine_session(state: dict, action: str, value: str | None = None) -> dict:
    """推荐轮次的交互：details / more / best。"""
    # refine 阶段基于 state 重新精筛排序（state 未存 matched，重算）
    pack = _all_packs().get(state["pack_id"]) or _GENERIC_PACK
    audience = _load_audience(state["audience_id"])
    filtered = _apply_filters(state["cards"], state["filters"])
    matched, rejected = _run_narrow(state, pack, audience, filtered)
    ranked = _rank(state, matched)

    if action == "details" and value:
        for r in ranked:
            if r["supplier_id"] == value:
                _enrich(r)
                return {
                    "stage": "recommending",
                    "action": "details",
                    "supplier_id": value,
                    "legal_name": r["legal_name"],
                    "region": r["region"],
                    "blended_score": r.get("blended_score"),
                    "cert_flag": _cert_flag(r.get("cert_eval")),
                    "cert_summary": r.get("cert_summary"),
                    "verified_certs": r.get("verified_certs"),
                    "rfq_reachable": r.get("rfq_reachable"),
                    "rfq_endpoint": r.get("rfq_endpoint"),
                    "claim_status": r.get("claim_status"),
                    "has_card": r.get("has_card"),
                    "reasons": r.get("reasons"),
                    "note": ("该厂已声明 RFQ 可达，可直接经 /v1/rfq 投递询价。"
                             if r.get("rfq_reachable")
                             else "该厂暂无在线 RFQ 入口，建议先认领/补全能力卡。"),
                }
        return {"stage": "recommending", "action": "details",
                "error": f"未在上次匹配结果中找到 {value}"}

    if action in ("more", "top") and value:
        n = int(value) if str(value).isdigit() else 12
        top = ranked[:n]
        recs = []
        for r in top:
            _enrich(r)
            recs.append({"supplier_id": r["supplier_id"], "legal_name": r["legal_name"],
                         "region": r["region"], "blended_score": r.get("blended_score"),
                         "tier": r.get("tier"), "cert_flag": _cert_flag(r.get("cert_eval")),
                         "cert_summary": r.get("cert_summary"),
                         "rfq_reachable": r.get("rfq_reachable")})
        return {"stage": "recommending", "action": action, "top_recommendations": recs}

    if action == "best":
        if not ranked:
            return {"stage": "recommending", "action": "best", "error": "无匹配结果"}
        r = ranked[0]
        _enrich(r)
        return {
            "stage": "recommending",
            "action": "best",
            "supplier_id": r["supplier_id"],
            "legal_name": r["legal_name"],
            "region": r["region"],
            "blended_score": r.get("blended_score"),
            "tier": r.get("tier"),
            "cert_flag": _cert_flag(r.get("cert_eval")),
            "why": "召回相关度 + 结构化匹配度综合最高，且认证/材料/工艺与需求一致。",
            "cert_summary": r.get("cert_summary"),
            "rfq_reachable": r.get("rfq_reachable"),
            "has_card": r.get("has_card"),
            "claim_status": r.get("claim_status"),
        }

    return {"stage": "recommending", "action": action or "unknown",
            "error": "支持的 action：details(带 supplier_id) / more(带 N) / best"}
