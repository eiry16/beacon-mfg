# -*- coding: utf-8 -*-
"""
BeaconMFG Skill 渲染器
======================

把采集会话的结果渲染成三份产物：
1. capability.json  机器可读能力卡（客户 Agent 做确定性比对）
2. SKILL.md         供应商自述（标准 8 段，供客户 Agent 精读）
3. fingerprint      一行能力指纹（L0 层，全量扫描用）

关键约定：
- 数据缺失一律渲染为 null / "—"，**禁止编造**
- SKILL.md 第 5 段「我们不做的」为强制段落，缺失时显式标注
- frontmatter description 必须塞满可检索关键词，很多 Agent 靠它做语义路由
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import gb_store  # noqa: E402

BADGE_BY_CLAIM = {"unclaimed": "L0", "claimed": "L1", "verified": "L2", "audited": "L3"}


def _g(d: dict, path: str, default=None):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _join(v, sep="、"):
    if not v:
        return None
    if isinstance(v, list):
        return sep.join(str(x) for x in v)
    return str(v)


def _fmt_cert(certs):
    if not certs:
        return None
    out = []
    for c in certs:
        if isinstance(c, dict):
            s = c.get("name", "")
            if c.get("valid_until"):
                s += f"（有效期至 {c['valid_until']}）"
            out.append(s)
        else:
            out.append(str(c))
    return "、".join(out)


def _fmt_val(v: Any, unit: str = "") -> str:
    """数值/区间/尺寸列表的展示格式。尺寸用 ×，区间用 ~。"""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (list, tuple)):
        nums = [x for x in v if isinstance(x, (int, float))]
        if len(nums) == len(v) and v:
            sep = "×" if len(v) >= 3 else "~"
            s = sep.join(str(int(x)) if float(x).is_integer() else str(x) for x in v)
            return f"{s}{unit}" if unit else s
        return "、".join(str(x) for x in v)
    if isinstance(v, (int, float)):
        s = str(int(v)) if float(v).is_integer() else str(v)
        return f"{s}{unit}" if unit else s
    return str(v)


# 品类字段的单位后缀
_UNITS = {
    "capability.axis_max": " 轴",
    "capability.turning_max_dia_mm": " mm",
    "capability.milling_stroke_mm": " mm",
    "capability.min_wall_thickness_mm": " mm",
    "capability.laser_bed_mm": " mm",
    "capability.bend_length_mm": " mm",
    "capability.sheet_thickness_mm": " mm",
    "capability.press_tonnage_t": " 吨",
    "capability.clamping_force_t": " 吨",
    "capability.machine_tonnage_t": " 吨",
    "capability.shot_weight_g": " g",
    "capability.part_weight_g": " g",
    "capability.coating_thickness_um": " μm",
    "capability.salt_spray_hours": " 小时",
    "capability.max_part_size_mm": " mm",
    "capability.stock_tons": " 吨",
    "capability.smt_lines": " 条",
    "capability.smt_capacity_pts_day": " 点/天",
    "capability.min_cut_length_mm": " mm",
}


def _fmt_equip(items):
    if not items:
        return None
    out = []
    for e in items:
        if isinstance(e, dict):
            s = e.get("name", "")
            if e.get("qty"):
                s += f" ×{e['qty']}"
            if e.get("spec"):
                s += f"（{e['spec']}）"
            out.append(s)
        else:
            out.append(str(e))
    return "、".join(out)


# ---------------------------------------------------------------- capability.json

def render_capability(session, base: Optional[dict] = None,
                      declared: Optional[dict] = None, claim: Optional[dict] = None) -> dict:
    """生成能力卡。

    - `base`：data/ 名录里的原始 POI 记录，用于继承已知字段。
    - `declared`：**企业自己在认证流程里申报**的联系方式（`contact_phone` /
      `claimed_address` / `contact_name`）。名录里没有这家（新注册）时，这是唯一
      的联系方式来源——企业既然自己认证过，这份自述就该进卡，而不是留个空。
      ⚠ 来源必须标出来：并入 `evidence.self_declared`，**不能挂在公开记录名下**。
    - `claim`：灯牌块。由调用方从认证档案实算（`certification.claim_block`）。
      不传就如实标 L0/未认领——能力卡渲染器**没有**判定灯牌资格的输入。
    """
    base = base or {}
    declared = declared or {}
    d = session.data
    today = date.today().isoformat()

    region = base.get("region", {}) or {}
    idt = dict(d.get("identity", {}) or {})
    idt.setdefault("province", region.get("province"))
    idt.setdefault("city", region.get("city"))
    idt.setdefault("address", base.get("address"))
    idt.setdefault("lat", base.get("lat"))
    idt.setdefault("lng", base.get("lng"))
    idt.setdefault("website", base.get("website"))

    rfq = dict(d.get("rfq", {}) or {})
    if rfq.get("endpoint"):
        rfq.setdefault("protocol", "email")
        rfq.setdefault("schema", "rfq/v1")
        rfq.setdefault("callback_supported", True)
        rfq.setdefault("auto_quote", False)

    # 联系方式：名录（公开可查）优先，名录没有才用企业自报。
    # 混在一起的代价是下游分不清"我上网一查就能对上"和"只有他自己这么说"，
    # 所以哪个字段来自自报，就写进 evidence.self_declared。
    contact = {
        "phone": base.get("contact_phone"),
        "email": None,
        "website": base.get("website"),
        "address": base.get("address"),
        "person": None,
    }
    from_declared: list[str] = []
    if not contact["phone"] and declared.get("phone"):
        contact["phone"] = declared["phone"]
        from_declared.append("contact.phone")
    if declared.get("email") and not contact["email"]:
        contact["email"] = declared["email"]
        from_declared.append("contact.email")
    if not contact["address"] and declared.get("address"):
        contact["address"] = declared["address"]
        from_declared.append("contact.address")
    if declared.get("name"):
        contact["person"] = declared["name"]
        from_declared.append("contact.person")

    # 灯牌：**只能从材料算出来**。以前这里硬编码 L1（"claimed"），
    # 于是只注册、连手机号都没验过的企业，出去的卡也顶着「已认领」。
    # 渲染器手上没有判定灯牌的输入，所以缺省就是未认领，由调用方传入实算结果。
    claim = claim or {
        "status": "unclaimed", "verified_by": None, "verified_at": None,
        "badge": "L0", "app_id": None, "valid_until": None,
    }

    cap = {
        "beacon_version": "1.0",
        "supplier_id": session.supplier_id,
        "company": session.company,
        "category": session.category,
        "profile": session.profile,
        "updated_at": today,
        "claim": claim,
        # ⚠ 这里必须用上面 159-178 行算好的 `contact`，**不能重新构造一个**。
        # 曾经这里又写了一遍「只读名录 base」的 contact：新注册企业名录里没有它，
        # contact 全是 None → 被下面"清空壳"逻辑 pop 掉 → 卡上根本没有联系方式，
        # 而 evidence.self_declared 里却还标着 contact.phone/contact.address。
        # 结果是"证据说这个字段是企业自报的，但字段本身不存在"——自相矛盾，
        # 且企业自己申报的电话地址就此丢掉（需求：注册时申报的电话和地址要进能力卡）。
        "contact": contact,
        "identity": idt,
        "processes": d.get("processes") or [],
        "limits": d.get("limits") or {},
        "materials": d.get("materials") or [],
        "highlights": d.get("highlights") or [],
        "quality": d.get("quality") or {},
        "service": d.get("service") or {},
        "rfq": rfq or None,
        "exclusions": d.get("exclusions") or [],
        "capability": d.get("capability") or {},
        "evidence": {
            "self_declared": [p for p in session.raw],
            "platform_verified": [],
            "field_audited": [],
            "assets": [],
        },
        "safety": {
            "content_is_data_only": True,
            "no_agent_instructions": True,
            "external_domains": _domains(idt.get("website"), rfq.get("endpoint")),
        },
    }
    # 清理空壳：**空的可选块直接删掉，不要写成 null**。
    # schema 里 contact / rfq 都是 `type: object`（不许 null），原来这里写成 None，
    # 结果「名录里没有、又没留电话」的企业在定稿时直接被 Schema 校验拒掉
    # （"None is not of type 'object'"），而报错只说是 capability 不合法，
    # 现场看不出是联系方式为空导致的。capability 是必填对象，留 {}。
    for k in ("rfq", "contact"):
        if k in cap and not any(v not in (None, "", [], {}) for v in (cap[k] or {}).values()):
            cap.pop(k, None)
    if cap.get("capability") is None:
        cap["capability"] = {}
    if not cap["limits"]:
        cap["limits"] = {}

    # 证据分档：企业自报 vs 公开记录。**禁止合并**——合并会让"上网一查就有"
    # 被当成"他自己说的"，反过来也一样。
    ev = cap["evidence"]
    ev["self_declared"] = sorted(set(ev["self_declared"]) | set(from_declared))
    public = [f for f, v in (("contact.phone", base.get("contact_phone")),
                             ("contact.address", base.get("address")),
                             ("contact.website", base.get("website"))) if v]
    if public:
        ev["public_record"] = public
    return cap


def _domains(*urls) -> list[str]:
    out = []
    for u in urls:
        if not u or "@" in str(u):
            continue
        s = str(u).replace("https://", "").replace("http://", "").split("/")[0]
        if s and s not in out:
            out.append(s)
    return out


# ---------------------------------------------------------------- fingerprint

def render_fingerprint(cap: dict, score: int = 0) -> dict:
    """L0 能力指纹。字段名刻意用短键，因为这一层要全量扫描。"""
    lt = cap.get("limits", {}) or {}
    ltd = lt.get("lead_time_days", {}) or {}
    procs = [p["code"] for p in (cap.get("processes") or []) if isinstance(p, dict)]
    certs = [c.get("name") if isinstance(c, dict) else str(c)
             for c in ((cap.get("quality") or {}).get("certifications") or [])]
    return {
        "id": cap["supplier_id"],
        "co": cap["company"],
        "city": (cap.get("identity") or {}).get("city"),
        "proc": procs,
        "mat": cap.get("materials") or [],
        "tol": lt.get("tolerance_mm"),
        "size": lt.get("max_part_size_mm"),
        "moq": lt.get("min_order_qty"),
        "lt": [ltd.get("sample"), ltd.get("batch_100")],
        "cert": certs,
        "rt": (cap.get("service") or {}).get("quote_response_hours"),
        "cl": (cap.get("claim") or {}).get("badge", "L0"),
        "pv": (cap.get("provenance") or {}).get("mode", "vendor_claimed"),
        "sc": score,
    }


# ---------------------------------------------------------------- SKILL.md

def render_skill_md(cap: dict) -> str:
    """渲染标准 8 段 SKILL.md。"""
    L = _g(cap, "limits", {}) or {}
    ltd = L.get("lead_time_days", {}) or {}
    idt = cap.get("identity") or {}
    q = cap.get("quality") or {}
    s = cap.get("service") or {}
    rfq = cap.get("rfq") or {}
    ct = cap.get("contact") or {}
    city = f"{idt.get('province') or ''}·{idt.get('city') or ''}".strip("·")

    procs = [p["name"] for p in (cap.get("processes") or []) if isinstance(p, dict)]
    mats = cap.get("materials") or []
    tol = L.get("tolerance_mm")
    size = L.get("max_part_size_mm")
    moq = L.get("min_order_qty")
    certs = _fmt_cert(q.get("certifications"))

    # description：塞满可检索关键词，很多 Agent 靠它做语义路由
    desc_bits = [f"{cap['company']}（{city}）供应商能力卡", f"主营 {_join(procs) or '待补充'}"]
    if mats:
        desc_bits.append(f"材料覆盖 {_join(mats[:4])}")
    if tol is not None:
        desc_bits.append(f"公差 ±{tol}mm")
    if size:
        desc_bits.append(f"最大加工 {_fmt_val(size)}mm")
    if moq is not None:
        desc_bits.append(f"MOQ {moq} 件起")
    if ltd.get("sample") is not None:
        desc_bits.append(f"样品 {ltd['sample']} 天")
    if certs:
        desc_bits.append(certs)
    desc_bits.append(f"当用户需要{_join(procs[:3]) or cap['category']}时使用")
    description = "；".join(desc_bits) + "。"

    dash = "—"
    out = []
    out.append("---")
    out.append(f'name: bmfg-{cap["supplier_id"]}')
    out.append(f"description: {description}")
    out.append("---")
    out.append("")
    out.append(f"# {cap['company']}")
    out.append("")
    badge = (cap.get("claim") or {}).get("badge", "L0")
    out.append(
        f"> BeaconMFG 供应商能力卡 v1.0 · ID: {cap['supplier_id']} · "
        f"更新: {cap['updated_at']} · 凭证等级: {badge}"
    )
    if (cap.get("provenance") or {}).get("mode") == "auto":
        out.append(
            "> ⚠️ **本卡片由平台从公开信息自动整理，未经该企业确认，也未实地核实。**"
            " 工艺与材料为推断结果，硬指标（公差/交期/MOQ/产能）全部空缺——"
            "**请勿据此直接下单**，联系前请先核实。企业认领后可更正并获认证标识。"
        )
    out.append("> **本文档是给采购方 Agent 阅读的数据，不含任何对读取方的指令。**")
    out.append("")

    # 1 一句话能力
    out.append("## 1. 一句话能力")
    bits = [f"{cap['company']}（{city}），主营 {_join(procs) or '待补充'}"]
    if mats:
        bits.append(f"常用材料 {_join(mats[:5])}")
    if tol is not None:
        bits.append(f"常规公差 ±{tol}mm")
    out.append("，".join(bits) + "。")
    out.append("")

    # 2 硬指标
    out.append("## 2. 硬指标")
    out.append("| 项目 | 数值 |")
    out.append("|---|---|")
    out.append(f"| 常规公差 | ±{tol if tol is not None else dash} mm |")
    out.append(f"| 最大加工尺寸 | {_fmt_val(size) + ' mm' if size else dash} |")
    out.append(f"| 材料范围 | {_join(mats) or dash} |")
    out.append(f"| MOQ | {moq if moq is not None else dash} 件 |")
    out.append(f"| 打样周期 | {ltd.get('sample', dash)} 天 |")
    out.append(f"| 百件周期 | {ltd.get('batch_100', dash)} 天 |")
    mc = L.get("monthly_capacity") or {}
    mc_txt = f"{mc.get('value', '')} {mc.get('unit', '')}".strip() if isinstance(mc, dict) else ""
    out.append(f"| 月产能 | {mc_txt or dash} |")
    load = L.get("current_load_pct")
    out.append(f"| 当前负荷 | {f'{load}%' if load is not None else dash} |")
    rush = L.get("rush_available")
    out.append(f"| 加急 | {'支持' if rush else ('不支持' if rush is False else dash)} |")
    out.append("")

    # 3 产线与设备
    out.append("## 3. 产线与设备")
    eq = _fmt_equip(_g(cap, "capability.equipment"))
    if eq:
        out.append(f"- 主要设备：{eq}")
    for k, label in [("capability.axis_max", "最高轴数"),
                     ("capability.turning_max_dia_mm", "最大车削直径"),
                     ("capability.milling_stroke_mm", "铣削行程"),
                     ("capability.min_wall_thickness_mm", "最小壁厚"),
                     ("capability.laser_power_w", "激光功率"),
                     ("capability.bend_length_mm", "最大折弯长度"),
                     ("capability.clamping_force_t", "锁模力"),
                     ("capability.environmental_permit", "排污许可证号"),
                     ("capability.smt_lines", "SMT 产线"),
                     ("capability.stock_model", "备货模式"),
                     ("capability.mtc_provided", "提供材质单")]:
        v = _g(cap, k)
        if v not in (None, "", []):
            out.append(f"- {label}：{_fmt_val(v, _UNITS.get(k, '').strip())}")
    if q.get("inspection_equipment"):
        out.append(f"- 检测设备：{_join(q['inspection_equipment'])}")
    if idt.get("floor_area_m2"):
        out.append(f"- 厂房面积：{idt['floor_area_m2']} ㎡")
    if idt.get("employee_count"):
        out.append(f"- 员工规模：{idt['employee_count']} 人")
    if len(out) > 0 and out[-1].startswith("## 3"):
        out.append(f"- {dash}（未采集到设备信息）")
    out.append("")

    # 4 擅长
    out.append("## 4. 我们特别擅长")
    if cap.get("highlights"):
        for h in cap["highlights"]:
            out.append(f"- {h}")
    else:
        out.append(f"- {dash}（未填写，建议补充具体场景以提高匹配率）")
    out.append("")

    # 5 边界（强制）
    out.append("## 5. 我们不做的（边界）")
    if cap.get("exclusions"):
        for e in cap["exclusions"]:
            out.append(f"- {e}")
    else:
        out.append(f"- {dash}（**待补充**：主动声明边界可帮客户 Agent 秒淘汰，提高成交率）")
    out.append("")

    # 6 质量与凭证
    out.append("## 6. 质量与凭证")
    out.append("| 项目 | 内容 | 凭证状态 |")
    out.append("|---|---|---|")
    for c in (q.get("certifications") or []):
        nm = c.get("name") if isinstance(c, dict) else str(c)
        vu = c.get("valid_until", dash) if isinstance(c, dict) else dash
        ev = c.get("evidence", "self_declared") if isinstance(c, dict) else "self_declared"
        ev_cn = {"self_declared": "企业自述", "platform_verified": "平台已核验",
                 "field_audited": "第三方核验"}.get(ev, ev)
        out.append(f"| 认证 | {nm}（有效期至 {vu}） | {ev_cn} |")
    if not q.get("certifications"):
        out.append(f"| 认证 | {dash} | 未核验 |")
    out.append(f"| 首件报告 | {'提供' if q.get('first_article_report') else dash} | — |")
    out.append(f"| 材质单 | {'提供' if q.get('material_certificate') else dash} | — |")
    out.append("")

    # 7 合作须知
    out.append("## 7. 合作须知")
    out.append(f"- 询价需提供：{_join(s.get('quote_inputs_required')) or dash}")
    out.append(f"- 报价响应：{s.get('quote_response_hours', dash)} 小时内")
    out.append(f"- 打样政策：{s.get('sample_policy') or dash}")
    out.append(f"- 付款方式：{_join(s.get('payment_terms')) or dash}")
    out.append(f"- 贸易条款：{_join(s.get('trade_terms')) or dash}")
    out.append(f"- NDA：{'接受' if s.get('nda_accepted') else dash}")
    out.append(f"- 对接语言：{_join(s.get('languages')) or '中文'}")
    out.append("")

    # 8 询价方式
    out.append("## 8. 询价方式")
    out.append(f"- **方式**：{rfq.get('protocol', dash)}")
    out.append(f"- **地址**：{rfq.get('endpoint', dash)}")
    out.append(f"- **RFQ 协议**：{rfq.get('schema', 'rfq/v1')}")
    out.append(f"- **自动回价**：{'支持' if rfq.get('auto_quote') else '不支持'}")
    if ct.get("phone"):
        out.append(f"- **电话**：{ct['phone']}")
    out.append("")
    out.append("---")
    out.append(
        "*结构化数据见同目录 `capability.json`。信息由企业自述，"
        "凭证状态以 `evidence` 字段为准；未核验字段请勿作为决策唯一依据。*"
    )
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 落盘

def write_vendor_skill(cap: dict, skill_md: str, root: Path = None) -> dict:
    """
    写入 skills/vendors/{id}/（capability.json + SKILL.md）。

    注意：这里**不再**往 skills/registry/capability/ 写第二份。
    那份与 vendors/{id}/capability.json 字节级重复，2700 家下来白白占 12MB，
    且两处容易不同步。vendors/ 是唯一真源。
    """
    root = Path(root) if root else REPO_ROOT
    vid = cap["supplier_id"]
    vdir = root / "skills" / "vendors" / vid
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "capability.json").write_text(
        json.dumps(cap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (vdir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    return {"vendor_dir": vdir, "capability": vdir / "capability.json",
            "skill": vdir / "SKILL.md"}


def append_fingerprint(cap: dict, score: int = 0, root: Path = None) -> Path:
    """写入 L0 指纹层，落位到国标分片（与 data/gb/ 同构）。

    分片由记录的国标码决定，不是旧的采购品类 —— 客户按国标码查，
    指纹层也按国标码切，两边口径必须对得上，否则初筛命中了还得多绕一层。
    """
    root = Path(root) if root else REPO_ROOT
    fp = render_fingerprint(cap, score)
    sid = cap["supplier_id"]
    # 该厂商在归档里属于哪个小类，就写进哪个分片；查不到归 _unclassified
    bucket = None
    try:
        gb_store.GB_DIR = root / "data" / "gb"
        bucket = gb_store.locate(sid)
    except Exception:
        bucket = None
    fp["gb"] = _industry_code_of(sid, root) if "gb" not in fp else fp["gb"]
    reg = root / "skills" / "registry" / "fingerprint" / "gb"
    reg.mkdir(parents=True, exist_ok=True)
    f = reg / ("%s.jsonl" % (bucket or gb_store.UNCLASSIFIED))
    f.parent.mkdir(parents=True, exist_ok=True)
    old_row = None
    lines = []
    if f.exists():
        for l in f.read_text(encoding="utf-8").splitlines():
            if not l.strip():
                continue
            try:
                r = json.loads(l)
            except json.JSONDecodeError:
                continue
            if r.get("id") == sid:
                # 旧行留底但不写回，循环结束后用 merge 后的新行替代。
                # 注意顺序：先解析再判断，不能先用字符串包含过滤——
                # 那样目标行会被 continue 掉，old_row 永远是 None（血泪）。
                old_row = r
                continue
            lines.append(l)

    if old_row:
        # 能力卡渲染出的行只有能力字段（proc/mat/tol/...），缺名录字段 gb/mf/tel。
        # 直接整体替换会让「是否制造商 / 有无电话 / 国标码」这些筛选键静默消失，
        # 所以先以旧行为底，再让能力字段覆盖；名录侧字段一律以旧行为准。
        merged = dict(old_row)
        merged.update(fp)
        for k in ("co", "city", "province", "gb", "mf", "tel"):
            if k in old_row:
                merged[k] = old_row[k]
        fp = merged

    lines.append(json.dumps(fp, ensure_ascii=False, separators=(",", ":")))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def _industry_code_of(supplier_id: str, root: Path) -> str | None:
    """取某厂商的国标码（用于给指纹行补 gb 字段）。"""
    try:
        gb_store.GB_DIR = root / "data" / "gb"
        bucket = gb_store.locate(supplier_id)
        if bucket and "/" in bucket:
            code = bucket.rsplit("/", 1)[-1]
            return None if code.startswith("_") else code
    except Exception:
        pass
    return None
