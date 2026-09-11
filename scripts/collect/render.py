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
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
# 门类 schema 装配与码表加载都在 scripts/ 下，渲染层与采集/校验共用同一份实现
_SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_SCRIPTS_DIR))
import cap_codes  # noqa: E402
import gate_schema as gs  # noqa: E402
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
        # 询价通道的默认值按门类：制造业留邮箱，生活服务业留电话/微信 ——
        # 餐厅没人盯 sales@ 邮箱，把「电话」标成 email 会让客户 Agent 投错地方。
        _g_pre = (getattr(session, "gate", None) or "").upper() or "C"
        rfq.setdefault("protocol", "phone" if _g_pre in ("H", "R", "F", "O", "A") else "email")
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

    gate = (getattr(session, "gate", None) or "").upper() or "C"
    allowed = set(gs.build_schema(gate)["properties"])

    cap = {
        "beacon_version": "1.0",
        "supplier_id": session.supplier_id,
        "gate": gate,
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
        "limits": d.get("limits") or {},
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
    # 门类私有块：**只搬该门类 schema 认得的键**。
    # 制造业有 processes / materials / highlights，餐饮一个都没有 ——
    # 无脑全搬会被 additionalProperties:false 拒掉，而这正是「独立 schema」的价值：
    # 餐饮卡里出现 processes 应当报错，不该静默通过。
    for k in ("processes", "materials", "highlights"):
        if k in allowed:
            cap[k] = d.get(k) or []

    # caps_index：L0 指纹与检索层唯一读取的能力字段。**只能由派生函数产出**，
    # 不能从 session.data 里直接抄 —— 那样卡片内容一改就会与索引脱节且无人察觉。
    cap["caps_index"] = gs.derive_caps_index(cap, gate)

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


_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$", re.I)


def _domains(*urls) -> list[str]:
    """`safety.external_domains`：这份文档会把读取方引到哪些**外部域名**。

    判定要严：必须是长得像域名的东西（带点 + 有 TLD）。生活服务门类的
    `rfq.endpoint` 常常是一串电话/「微信同号」—— 旧实现只挡了带 `@` 的邮箱，
    于是电话号码串被当成域名写进安全声明里（2026-09-11 发现），
    而那是一条**假的安全声明**，比留空更糟。
    """
    out = []
    for u in urls:
        if not u:
            continue
        s = str(u).strip()
        if "@" in s:                       # 邮箱不是域名
            continue
        s = s.replace("https://", "").replace("http://", "").split("/")[0].strip().lower()
        if not _DOMAIN_RE.match(s):
            continue
        if s not in out:
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
    gate = gs.gate_of_card(cap) or "C"
    capp = cap.get("capability") or {}
    return {
        "id": cap["supplier_id"],
        "co": cap["company"],
        "g": gate,
        "city": (cap.get("identity") or {}).get("city"),
        # `cap` 是**跨门类**的能力键（由 caps_index 来）。L0 全量扫描时，
        # 检索层只读它就能跨门类过滤，不需要知道餐饮有菜系、制造业有工艺。
        "cap": cap.get("caps_index") or [],
        # `proc` 只对制造业有意义，其他门类恒为空数组 —— 保留键名不动，
        # 因为 App 与检索脚本都在读它。
        "proc": procs,
        # 餐饮的两个数值硬筛选条件（能不能坐下、预算够不够）。
        # 制造业卡上没有这两个键，取值为 None。
        "seat": capp.get("seats"),
        "pp": capp.get("avg_price_cny"),
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
    """按门类渲染 SKILL.md。

    段序（1 一句话 / 2 硬指标 / 3 设备场地 / 4 擅长 / 5 边界 / 6 资质 /
    7 合作须知 / 8 询价方式）全门类一致 —— 客户 Agent 的解析逻辑因此不用分支。
    **每段的内容**由门类渲染器决定：制造业讲公差与工序，餐饮讲席位与菜系。

    C 门类走 `_render_skill_md_c`，是改造前函数的原样重命名，输出逐字节不变。
    """
    gate = gs.gate_of_card(cap) or "C"
    fn = _SKILL_MD_BY_GATE.get(gate)
    if fn is None:
        return _render_skill_md_c(cap)
    return fn(cap)


def _render_skill_md_c(cap: dict) -> str:
    """制造业 SKILL.md（改造前的实现，逐字节保留）。"""
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
    # `capability.summary` / `service_items` 是 custom profile（组装、检测、设计、
    # 软件服务这类非制造主体）的**必填项**，profiles/custom.json 的 hint 也明确承诺
    # "这段会直接进 SKILL.md 的第 1 段"。以前只采不渲染 —— 企业认真写的一句话被
    # 静默丢掉，客户 Agent 看到的还是"主营 待补充"。有就放在最前面。
    _summary = _g(cap, "capability.summary")
    if isinstance(_summary, str) and _summary.strip():
        out.append(_summary.strip())
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
                     ("capability.mtc_provided", "提供材质单"),
                     # custom profile（非制造主体）的三项：同样"采了就必须渲染出来"，
                     # 否则企业在对话里说的话只剩在库里，客户 Agent 一个字也看不到。
                     ("capability.service_items", "服务项目"),
                     ("capability.equipment_or_team", "核心资源"),
                     ("capability.typical_clients", "典型客户行业")]:
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
    _declared = set((cap.get("evidence") or {}).get("self_declared") or [])
    if cap.get("exclusions"):
        for e in cap["exclusions"]:
            out.append(f"- {e}")
    elif "exclusions" in _declared:
        # 企业**明确回答过**"没有不接的活"——这是一个有信息量的肯定答复
        # （客户 Agent 据此知道"这家基本什么活都接"），不能渲染成"待补充"。
        # 「没填 ≠ 不做」这条红线是双向的：**"已确认没有"也不能显示成"没问过"**，
        # 否则采购会以为平台漏采了，从而不敢下单。
        # 判据用 evidence.self_declared：只有真正在采集会话里答过的字段才在里面，
        # 平台预填/自动整理卡都不会有它。不新增字段，避免动 Schema。
        out.append(f"- {dash}（企业已确认：没有明确不接的活）")
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


# ---------------------------------------------------------------- 餐饮（H）SKILL.md

def _declared_paths(cap: dict) -> set:
    """企业**在采集会话里真正答过**的字段路径集合。

    判据是 evidence.self_declared —— 只有答过的字段才在里面，平台预填与自动整理卡
    都不会有。「已确认没有」和「没问过」必须渲染成不同的话，靠的就是它。
    """
    return set((cap.get("evidence") or {}).get("self_declared") or [])


def _cap_names(codes, domain: str = "") -> list[str]:
    """能力码 → 中文名。渲染层只认码，名字从码表查，不在卡里冗余存。

    域从卡片的门类推导，不写死 restaurant —— 以后加娱乐/技术服务不用改这里。
    """
    if not codes:
        return []
    if not domain:
        return [str(c) for c in codes]
    out = []
    for c in codes:
        if isinstance(c, str) and "_" in c:
            try:
                out.append(cap_codes.code_name(domain, c))
            except Exception:
                out.append(c)
        else:
            out.append(str(c))
    return out


def _render_skill_md_h(cap: dict) -> str:
    """H 门类（住宿和餐饮）SKILL.md。

    段序与制造业对齐（1 一句话 / 2 硬指标 / 3 场地 / 4 擅长 / 5 边界 /
    6 资质 / 7 接待须知 / 8 预订方式），但**每一段的内容都是餐饮自己的**。
    制造业那套「公差 / 最大加工尺寸 / MOQ 件 / 打样周期」在这里一行都不出现。
    """
    L = cap.get("limits") or {}
    capp = cap.get("capability") or {}
    idt = cap.get("identity") or {}
    q = cap.get("quality") or {}
    s = cap.get("service") or {}
    rfq = cap.get("rfq") or {}
    ct = cap.get("contact") or {}
    city = f"{idt.get('province') or ''}·{idt.get('city') or ''}".strip("·")
    dash = "—"

    dom = gs.cap_domain(gs.gate_of_card(cap) or "H")
    cuis = _cap_names(capp.get("cuisines"), dom)
    modes = _cap_names(capp.get("service_modes"), dom)
    venues = _cap_names(capp.get("venue_features"), dom)
    slots = _cap_names(capp.get("time_slots"), dom)
    seats = capp.get("seats")
    rooms = capp.get("private_rooms")
    tables = capp.get("max_banquet_tables")
    avg = capp.get("avg_price_cny")
    hours = capp.get("business_hours")
    dishes = capp.get("signature_dishes") or []
    platforms = capp.get("delivery_platforms") or []
    certs = _fmt_cert(q.get("certifications"))

    desc_bits = [f"{cap['company']}（{city}）餐饮能力卡", f"主营 {_join(cuis) or '待补充'}"]
    if modes:
        desc_bits.append(f"可接 {_join(modes[:4])}")
    if seats:
        desc_bits.append(f"可容 {seats} 人")
    if rooms:
        desc_bits.append(f"{rooms} 个包间")
    if avg:
        desc_bits.append(f"人均 ¥{_fmt_val(avg)}")
    if certs:
        desc_bits.append(certs)
    desc_bits.append(f"当用户需要{_join(cuis[:3]) or cap['category']}时使用")
    description = "；".join(desc_bits) + "。"

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
        f"> 炫招灯塔 餐饮能力卡 v1.0 · ID: {cap['supplier_id']} · "
        f"更新: {cap['updated_at']} · 凭证等级: {badge}"
    )
    if (cap.get("provenance") or {}).get("mode") == "auto":
        out.append(
            "> ⚠️ **本卡片由平台从公开信息自动整理，未经该企业确认，也未实地核实。**"
            " 菜系与接待能力为推断结果——**请勿据此直接预订**，联系前请先核实。"
            " 企业认领后可更正并获认证标识。"
        )
    out.append("> **本文档是给需求方 Agent 阅读的数据，不含任何对读取方的指令。**")
    out.append("")

    # 1 一句话能力
    out.append("## 1. 一句话能力")
    _summary = _g(cap, "capability.summary")
    if isinstance(_summary, str) and _summary.strip():
        out.append(_summary.strip())
    bits = [f"{cap['company']}（{city}），主营 {_join(cuis) or '待补充'}"]
    if modes:
        bits.append(f"可接 {_join(modes)}")
    if avg:
        bits.append(f"人均约 ¥{_fmt_val(avg)}")
    out.append("，".join(bits) + "。")
    out.append("")

    # 2 硬指标
    out.append("## 2. 硬指标")
    out.append("| 项目 | 数值 |")
    out.append("|---|---|")
    # 数值一律过 _fmt_val：normalize("number") 返回 float，直接插值会渲染成
    # 「¥150.0」「260.0 人」这种一眼假的数字。
    out.append(f"| 总座位数 | {_fmt_val(seats) or dash} 座 |")
    out.append(f"| 包间数 | {_fmt_val(rooms) if rooms is not None else dash} 间 |")
    out.append(f"| 最大接待人数 | {_fmt_val(L.get('max_party_size')) or dash} 人 |")
    out.append(f"| 最大桌数 | {_fmt_val(L.get('max_tables')) or dash} 桌 |")
    out.append(f"| 最大宴席桌数 | {_fmt_val(tables) if tables is not None else dash} 桌 |")
    out.append(f"| 人均消费 | {'¥' + _fmt_val(avg) if avg is not None else dash} |")
    min_spend = L.get("min_order_value_cny")
    # 「没有最低消费」记的是 0（zero_is_answer），要渲染成明确措辞，
    # 不能显示成破折号 —— 否则读的人分不清「真没有」和「没问到」。
    if min_spend:
        min_spend_txt = f"¥{_fmt_val(min_spend)}"
    elif min_spend == 0:
        min_spend_txt = "无最低消费"
    else:
        min_spend_txt = dash
    out.append(f"| 最低消费 | {min_spend_txt} |")
    out.append(f"| 最少起接 | {_fmt_val(L.get('min_order_qty')) or dash} 桌 |")
    out.append(f"| 宴席提前预订 | {_fmt_val(L.get('advance_booking_days')) or dash} 天 |")
    load = L.get("current_load_pct")
    out.append(f"| 当前上座率 | {f'{load}%' if load is not None else dash} |")
    rush = L.get("rush_available")
    out.append(f"| 临时加桌 | {'可安排' if rush else ('不可' if rush is False else dash)} |")
    out.append(f"| 营业时间 | {hours or dash} |")
    out.append("")

    # 3 场地与服务形态
    out.append("## 3. 场地与接待能力")
    if modes:
        out.append(f"- 经营形态：{_join(modes)}")
    if venues:
        out.append(f"- 场地条件：{_join(venues)}")
    if slots:
        out.append(f"- 营业时段：{_join(slots)}")
    if platforms:
        out.append(f"- 外卖平台：{_join(platforms)}")
    elif "capability.delivery_platforms" in _declared_paths(cap):
        out.append("- 外卖平台：不做外卖")
    if idt.get("floor_area_m2"):
        out.append(f"- 营业面积：{idt['floor_area_m2']} ㎡")
    if idt.get("employee_count"):
        out.append(f"- 员工人数：{idt['employee_count']} 人")
    if out and out[-1].startswith("## 3"):
        out.append(f"- {dash}（未采集到场地信息）")
    out.append("")

    # 4 招牌菜
    out.append("## 4. 招牌菜")
    if dishes:
        for d in dishes:
            out.append(f"- {d}")
    else:
        out.append(f"- {dash}（未填写，建议补充招牌菜以提高匹配率）")
    out.append("")

    # 5 边界（强制）
    out.append("## 5. 我们不接的单（边界）")
    if cap.get("exclusions"):
        for e in cap["exclusions"]:
            out.append(f"- {e}")
    elif "exclusions" in _declared_paths(cap):
        out.append(f"- {dash}（企业已确认：没有明确不接的单）")
    else:
        out.append(f"- {dash}（**待补充**：主动写清边界可帮需求方 Agent 秒淘汰，提高成单率）")
    out.append("")

    # 6 资质与卫生
    out.append("## 6. 资质与卫生")
    out.append("| 项目 | 内容 | 凭证状态 |")
    out.append("|---|---|---|")
    for c in (q.get("certifications") or []):
        nm = c.get("name") if isinstance(c, dict) else str(c)
        vu = c.get("valid_until", dash) if isinstance(c, dict) else dash
        ev = c.get("evidence", "self_declared") if isinstance(c, dict) else "self_declared"
        ev_cn = {"self_declared": "企业自述", "platform_verified": "平台已核验",
                 "field_audited": "第三方核验"}.get(ev, ev)
        out.append(f"| 证照 | {nm}（有效期至 {vu}） | {ev_cn} |")
    if not q.get("certifications"):
        out.append(f"| 证照 | {dash} | 未核验 |")
    hg = q.get("hygiene_grade")
    out.append(f"| 卫生量化分级 | {hg if hg else dash} | — |")
    out.append("")

    # 7 接待须知
    out.append("## 7. 接待须知")
    out.append(f"- 预订渠道：{_join(s.get('booking_channels')) or dash}")
    out.append(f"- 能否开票：{'可开票' if s.get('invoice_available') else ('不提供' if s.get('invoice_available') is False else dash)}")
    dep = s.get("deposit_required")
    out.append(f"- 宴席定金：{'需付定金' if dep else ('不收定金' if dep is False else dash)}")
    out.append(f"- 付款方式：{_join(s.get('payment_terms')) or dash}")
    out.append(f"- 对接语言：{_join(s.get('languages')) or '中文'}")
    out.append("")

    # 8 预订方式
    out.append("## 8. 预订方式")
    out.append(f"- **方式**：{rfq.get('protocol', dash)}")
    out.append(f"- **渠道**：{rfq.get('endpoint', dash)}")
    out.append(f"- **RFQ 协议**：{rfq.get('schema', 'rfq/v1')}")
    if ct.get("phone"):
        out.append(f"- **电话**：{ct['phone']}")
    out.append("")
    out.append("---")
    out.append(
        "*结构化数据见同目录 `capability.json`。信息由企业自述，"
        "凭证状态以 `evidence` 字段为准；未核验字段请勿作为决策唯一依据。*"
    )
    return "\n".join(out) + "\n"


# ------------------------------------------------- 通用门类 SKILL.md（R/F/O/I/M）
#
# R/F/O/I/M 五个门类共用**一个数据驱动的渲染器**：段序与 C/H 逐段对齐（客户 Agent
# 的解析逻辑因此不必按门类分支），每段说什么由 _MD_SPECS 声明。
# 加新门类 = 往表里加一条，而不是再复制一份两百行的渲染函数 —— 复制出来的那份
# 迟早会与别处不同步（C 与 H 已经是两份了，不该再添四份）。
#
# 为什么不顺手把 C/H 也并进来：它俩的输出有逐字节回归基线（T8/T13 断言），
# 此刻重构等于拿「已上线且已逐条验证」的产物冒险。新门类没有历史包袱，
# 所以从第一天就走数据驱动。

def _na_paths(cap: dict) -> set:
    """企业**明确标注不适用**的字段路径。

    `not_applicable` 与「没问到」是两回事：不做检测的机构不该因为没填 CMA 而被
    渲染成「待补充」，它该显示成「不适用」。
    """
    return set(cap.get("not_applicable") or [])


def _metric_value(cap: dict, m: dict, dash: str = "—") -> str:
    """一条指标的值文本（含单位）。

    刻意保留 `zero_text`：`zero_is_answer` 记下的 0 是企业的**明确回答**
    （「不收上门费」「不质保」），渲染成破折号会让读的人分不清它和「没问到」。
    """
    path = m["path"]
    if path in _na_paths(cap):
        return "不适用"
    v = _g(cap, path)
    if v is None or v == "" or v == [] or v == {}:
        return dash
    is_num = isinstance(v, (int, float)) and not isinstance(v, bool)
    if m.get("money"):
        if is_num and v == 0 and m.get("zero_text"):
            return m["zero_text"]
        return f"¥{_fmt_val(v)}"
    if m.get("pct"):
        return f"{_fmt_val(v)}%"
    if m.get("bool"):
        t, f_ = m["bool"]
        if v is True:
            return t
        if v is False:
            return f_
        return dash
    # ⚠ 列表要在 text 之前判：`text: True` 只是「原样展示」，不是「str() 一把」。
    # 反了的话 `booking_channels` 会被渲染成 `['电话', '微信']` —— Python repr
    # 直接漏进给客户 Agent 读的文档里（2026-09-11 实测）。
    if isinstance(v, list):
        return _join(v) or dash
    if m.get("text"):
        return str(v)
    if is_num and v == 0 and m.get("zero_text"):
        return m["zero_text"]
    unit = m.get("unit")
    if unit is None and m.get("unit_path"):
        unit = _g(cap, m["unit_path"]) or ""
    if unit and is_num:
        return f"{_fmt_val(v)} {unit}"
    return f"{_fmt_val(v)} {unit}".strip() if unit else _fmt_val(v)


def _md_bullet_lists(cap: dict, spec: dict, sec: int, dom: str) -> list[str]:
    """某一段里的列表字段 → markdown 条目。"""
    out: list[str] = []
    na = _na_paths(cap)
    for item in spec.get("lists", []):
        if item.get("sec") != sec:
            continue
        path, label = item["path"], item["label"]
        v = _g(cap, path)
        if path in na:
            out.append(f"- {label}：不适用")
            continue
        if not v:
            # 「明确没有」与「没问到」必须渲染成不同的话。
            if path in _declared_paths(cap):
                out.append(f"- {label}：{item.get('empty_text') or '无'}")
            continue
        if isinstance(v, list) and item.get("codes"):
            names = _cap_names(v, dom)
        elif isinstance(v, list):
            names = [str(x) for x in v]
        else:
            names = [str(v)]
        out.append(f"- {label}：{_join(names)}")
    for item in spec.get("texts", []):
        if item.get("sec") != sec:
            continue
        v = _g(cap, item["path"])
        if v:
            out.append(f"- {item['label']}：{v}")
    return out


def _render_skill_md_generic(cap: dict, gate: str) -> str:
    """R/F/O/I/M 的 SKILL.md。段序与 C/H 一致，内容全部来自 _MD_SPECS[gate]。"""
    spec = _MD_SPECS[gate]
    dom = gs.cap_domain(gate)
    capp = cap.get("capability") or {}
    idt = cap.get("identity") or {}
    q = cap.get("quality") or {}
    rfq = cap.get("rfq") or {}
    ct = cap.get("contact") or {}
    dash = "—"
    city = f"{idt.get('province') or ''}·{idt.get('city') or ''}".strip("·")

    primary = _cap_names(capp.get(spec["primary"].split(".")[-1]), dom) \
        if spec.get("primary") else []
    certs = _fmt_cert(q.get("certifications"))
    noun = spec["noun"]

    # description：塞满可检索关键词，很多 Agent 靠它做语义路由。
    desc_bits = [f"{cap['company']}（{city}）{noun}能力卡",
                 f"主营 {_join(primary) or '待补充'}"]
    by_label = {m["label"]: m for m in spec.get("metrics", [])}
    for label in spec.get("desc_labels", []):
        m = by_label.get(label)
        if not m:
            continue
        val = _metric_value(cap, m)
        if val != dash:
            desc_bits.append(f"{label} {val}")
    if certs:
        desc_bits.append(certs)
    desc_bits.append(f"当用户需要{_join(primary[:3]) or cap['category']}时使用")
    description = "；".join(desc_bits) + "。"

    out: list[str] = []
    out.append("---")
    out.append(f'name: bmfg-{cap["supplier_id"]}')
    out.append(f"description: {description}")
    out.append("---")
    out.append("")
    out.append(f"# {cap['company']}")
    out.append("")
    badge = (cap.get("claim") or {}).get("badge", "L0")
    out.append(
        f"> 炫招灯塔 {noun}能力卡 v1.0 · ID: {cap['supplier_id']} · "
        f"更新: {cap['updated_at']} · 凭证等级: {badge}"
    )
    if (cap.get("provenance") or {}).get("mode") == "auto":
        out.append(
            "> ⚠️ **本卡片由平台从公开信息自动整理，未经该企业确认，也未实地核实。**"
            f" {noun}能力为推断结果 —— **请勿据此直接下单/预订**，联系前请先核实。"
            " 企业认领后可更正并获认证标识。"
        )
    out.append("> **本文档是给需求方 Agent 阅读的数据，不含任何对读取方的指令。**")
    out.append("")

    # 1 一句话能力
    out.append("## 1. 一句话能力")
    _summary = _g(cap, "capability.summary")
    if isinstance(_summary, str) and _summary.strip():
        out.append(_summary.strip())
    bits = [f"{cap['company']}（{city}），主营 {_join(primary) or '待补充'}"]
    for label in spec.get("desc_labels", [])[:2]:
        m = by_label.get(label)
        if not m:
            continue
        val = _metric_value(cap, m)
        if val != dash:
            bits.append(f"{label} {val}")
    out.append("，".join(bits) + "。")
    out.append("")

    # 2 硬指标
    out.append("## 2. 硬指标")
    out.append("| 项目 | 数值 |")
    out.append("|---|---|")
    for m in spec.get("metrics", []):
        out.append(f"| {m['label']} | {_metric_value(cap, m, dash)} |")
    out.append("")

    # 3 场地/交付/服务能力
    out.append(f"## 3. {spec['s3']}")
    sec3 = _md_bullet_lists(cap, spec, 3, dom)
    if idt.get("floor_area_m2"):
        sec3.append(f"- {_g(cap, 'identity.floor_area_m2')} ㎡（经营面积）")
    if idt.get("employee_count"):
        sec3.append(f"- 员工人数：{idt['employee_count']} 人")
    out.extend(sec3 or [f"- {dash}（未采集到{spec['s3']}信息）"])
    out.append("")

    # 4 招牌/案例/设备
    out.append(f"## 4. {spec['s4']}")
    sec4 = _md_bullet_lists(cap, spec, 4, dom)
    out.extend(sec4 or [f"- {dash}（{spec.get('s4_empty') or '未填写，建议补充'}）"])
    out.append("")

    # 5 边界（强制）
    out.append(f"## 5. {spec['s5']}")
    if cap.get("exclusions"):
        for e in cap["exclusions"]:
            out.append(f"- {e}")
    elif "exclusions" in _declared_paths(cap):
        out.append(f"- {dash}（企业已确认：没有明确不接的）")
    else:
        out.append(f"- {dash}（**待补充**：主动写清边界可帮需求方 Agent 秒淘汰，提高成单率）")
    out.append("")

    # 6 资质
    out.append(f"## 6. {spec['s6']}")
    out.append("| 项目 | 内容 | 凭证状态 |")
    out.append("|---|---|---|")
    for c in (q.get("certifications") or []):
        nm = c.get("name") if isinstance(c, dict) else str(c)
        vu = c.get("valid_until", dash) if isinstance(c, dict) else dash
        ev = c.get("evidence", "self_declared") if isinstance(c, dict) else "self_declared"
        ev_cn = {"self_declared": "企业自述", "platform_verified": "平台已核验",
                 "field_audited": "第三方核验", "public_record": "公开记录"}.get(ev, ev)
        out.append(f"| 证照 | {nm}（有效期至 {vu}） | {ev_cn} |")
    if not q.get("certifications"):
        out.append(f"| 证照 | {dash} | 未核验 |")
    for m in spec.get("quality_extra", []):
        out.append(f"| {m['label']} | {_metric_value(cap, m, dash)} | — |")
    out.append("")

    # 7 须知
    out.append(f"## 7. {spec['s7']}")
    for m in spec.get("service_info", []):
        out.append(f"- {m['label']}：{_metric_value(cap, m, dash)}")
    out.append("")

    # 8 联系方式
    out.append(f"## 8. {spec['s8']}")
    out.append(f"- **方式**：{rfq.get('protocol', dash)}")
    out.append(f"- **渠道**：{rfq.get('endpoint', dash)}")
    out.append(f"- **RFQ 协议**：{rfq.get('schema', 'rfq/v1')}")
    if ct.get("phone"):
        out.append(f"- **电话**：{ct['phone']}")
    if ct.get("person"):
        out.append(f"- **联系人**：{ct['person']}")
    if ct.get("address"):
        out.append(f"- **地址**：{ct['address']}")
    out.append("")
    out.append("---")
    out.append(
        "*结构化数据见同目录 `capability.json`。信息由企业自述，"
        "凭证状态以 `evidence` 字段为准；未核验字段请勿作为决策唯一依据。*"
    )
    return "\n".join(out) + "\n"


_MD_SPECS: dict[str, dict] = {

    # ------------------------------------------------ R 文化、体育和娱乐业
    "R": {
        "noun": "娱乐场所",
        "primary": "capability.venue_types",
        "s3": "场地与接待能力",
        "s4": "招牌项目",
        "s4_empty": "未填写，建议补充招牌项目（如 VR 赛车、主题密室）以提高匹配率",
        "s5": "我们不接的单（边界）",
        "s6": "资质与卫生",
        "s7": "预订须知",
        "s8": "预订方式",
        "desc_labels": ["同时可容纳", "人均消费"],
        "lists": [
            {"path": "capability.venue_types", "label": "场所类型", "sec": 3, "codes": True},
            {"path": "capability.service_modes", "label": "经营形态", "sec": 3, "codes": True},
            {"path": "capability.venue_features", "label": "场地设施", "sec": 3, "codes": True,
             "empty_text": "无特别设施"},
            {"path": "capability.time_slots", "label": "营业时段", "sec": 3, "codes": True},
            {"path": "capability.signature_items", "label": "招牌项目", "sec": 4},
        ],
        "metrics": [
            {"label": "同时可容纳", "path": "capability.capacity", "unit": "人"},
            {"label": "包间数", "path": "capability.private_rooms", "unit": "间"},
            {"label": "人均消费", "path": "capability.price_per_person_cny", "money": True},
            {"label": "单次最短时长", "path": "capability.min_duration_hours", "unit": "小时"},
            {"label": "最大接待人数", "path": "limits.max_party_size", "unit": "人"},
            {"label": "最少起接", "path": "limits.min_order_qty", "unit": "人"},
            {"label": "最低消费", "path": "limits.min_order_value_cny", "money": True,
             "zero_text": "无最低消费"},
            {"label": "包场提前预订", "path": "limits.advance_booking_days", "unit": "天"},
            {"label": "当前场地使用率", "path": "limits.current_load_pct", "pct": True},
            {"label": "临时加场", "path": "limits.rush_available", "bool": ("可安排", "不可")},
            {"label": "营业时间", "path": "capability.business_hours", "text": True},
        ],
        "quality_extra": [
            {"label": "公共场所卫生等级", "path": "quality.hygiene_grade", "text": True},
        ],
        "service_info": [
            {"label": "预订渠道", "path": "service.booking_channels", "text": True},
            {"label": "能否开票", "path": "service.invoice_available",
             "bool": ("可开票", "不提供")},
            {"label": "包场定金", "path": "service.deposit_required",
             "bool": ("需付定金", "不收定金")},
            {"label": "取消政策", "path": "service.cancellation_policy", "text": True},
            {"label": "付款方式", "path": "service.payment_terms", "text": True},
        ],
    },

    # ------------------------------------------------ F 批发和零售业
    "F": {
        "noun": "零售批发",
        "primary": "capability.retail_categories",
        "s3": "经营与交付",
        "s4": "主营品牌",
        "s4_empty": "未填写（无品牌货可留空）",
        "s5": "我们不接的活（边界）",
        "s6": "资质与授权",
        "s7": "交易须知",
        "s8": "联系方式",
        "desc_labels": ["在售品类数", "最少起订量", "是否有现货"],
        "lists": [
            {"path": "capability.retail_categories", "label": "经营品类", "sec": 3, "codes": True},
            {"path": "capability.business_modes", "label": "经营方式", "sec": 3, "codes": True},
            {"path": "capability.delivery_modes", "label": "交付方式", "sec": 3, "codes": True},
            {"path": "capability.store_features", "label": "门店条件", "sec": 3, "codes": True,
             "empty_text": "无特别条件"},
            {"path": "capability.brands", "label": "主营品牌", "sec": 4},
        ],
        "metrics": [
            {"label": "在售品类数", "path": "capability.sku_count", "unit": "种"},
            {"label": "最少起订量", "path": "limits.min_order_qty",
             "unit_path": "capability.purchase_unit"},
            {"label": "是否有现货", "path": "limits.stock_available",
             "bool": ("有现货", "需订货")},
            {"label": "发货/到货", "path": "limits.delivery_days", "unit": "天"},
            {"label": "配送半径", "path": "limits.delivery_radius_km", "unit": "公里"},
            {"label": "日最大接单", "path": "limits.max_daily_orders", "unit": "单"},
            {"label": "最低订单金额", "path": "limits.min_order_value_cny", "money": True,
             "zero_text": "无门槛"},
            {"label": "当前负荷", "path": "limits.current_load_pct", "pct": True},
            {"label": "加急", "path": "limits.rush_available", "bool": ("可加急", "不可")},
            {"label": "营业时间", "path": "capability.business_hours", "text": True},
        ],
        "quality_extra": [
            {"label": "品牌授权", "path": "quality.brand_authorized",
             "bool": ("有正规授权", "无授权（杂牌/自有渠道）")},
            {"label": "货源说明", "path": "quality.source_note", "text": True},
        ],
        "service_info": [
            {"label": "报价需提供", "path": "service.quote_inputs_required", "text": True},
            {"label": "报价响应", "path": "service.quote_response_hours", "unit": "小时"},
            {"label": "是否支持批发", "path": "service.wholesale_available",
             "bool": ("支持", "仅零售")},
            {"label": "能否开票", "path": "service.invoice_available",
             "bool": ("可开票", "不提供")},
            {"label": "退换货政策", "path": "service.return_policy", "text": True},
            {"label": "付款方式", "path": "service.payment_terms", "text": True},
        ],
    },

    # ------------------------------------------------ O 居民服务、修理和其他服务业
    "O": {
        "noun": "居民服务",
        "primary": "capability.service_items",
        "s3": "服务项目与保障",
        "s4": "服务保障与特色",
        "s4_empty": "未填写，建议补充服务保障（如不满意返工、用原厂配件）",
        "s5": "我们不接的活（边界）",
        "s6": "资质与人员",
        "s7": "服务须知",
        "s8": "联系方式",
        "desc_labels": ["服务半径", "上门响应", "起步价"],
        "lists": [
            {"path": "capability.service_items", "label": "服务项目", "sec": 3, "codes": True},
            {"path": "capability.business_modes", "label": "经营方式", "sec": 3, "codes": True},
            {"path": "capability.delivery_modes", "label": "交付方式", "sec": 3, "codes": True},
            {"path": "capability.store_features", "label": "门店条件", "sec": 3, "codes": True,
             "empty_text": "无特别条件"},
        ],
        "texts": [
            {"path": "capability.service_guarantee", "label": "服务保障", "sec": 4},
        ],
        "metrics": [
            {"label": "可出动师傅", "path": "capability.technician_count", "unit": "人"},
            {"label": "服务半径", "path": "limits.service_radius_km", "unit": "公里"},
            {"label": "起步价", "path": "limits.min_order_value_cny", "money": True,
             "zero_text": "不收上门费"},
            {"label": "上门响应", "path": "limits.response_hours", "unit": "小时"},
            {"label": "最少起接", "path": "limits.min_order_qty", "unit": "单"},
            {"label": "日最大接单", "path": "limits.max_daily_orders", "unit": "单"},
            {"label": "提前预约", "path": "limits.advance_booking_days", "unit": "天"},
            {"label": "当前负荷", "path": "limits.current_load_pct", "pct": True},
            {"label": "加急", "path": "limits.rush_available", "bool": ("可加急", "不可")},
            {"label": "接单时间", "path": "capability.business_hours", "text": True},
        ],
        "quality_extra": [
            {"label": "师傅持证", "path": "quality.technician_certified",
             "bool": ("持职业资格证", "未持证")},
            {"label": "公共场所卫生等级", "path": "quality.hygiene_grade", "text": True},
        ],
        "service_info": [
            {"label": "接单渠道", "path": "service.booking_channels", "text": True},
            {"label": "报价需提供", "path": "service.quote_inputs_required", "text": True},
            {"label": "报价响应", "path": "service.quote_response_hours", "unit": "小时"},
            {"label": "质保", "path": "service.warranty_days", "unit": "天",
             "zero_text": "不质保"},
            {"label": "是否有价目表", "path": "service.price_list_available",
             "bool": ("有标准价目表", "无价目表")},
            {"label": "能否开票", "path": "service.invoice_available",
             "bool": ("可开票", "不提供")},
            {"label": "付款方式", "path": "service.payment_terms", "text": True},
        ],
    },

    # ------------------------------------------------ I 信息传输、软件和信息技术服务业
    "I": {
        "noun": "信息技术服务",
        "primary": "capability.tech_directions",
        "s3": "技术方向与交付",
        "s4": "代表案例",
        "s4_empty": "未填写，建议补充具体项目（写「给谁做了什么」比写「经验丰富」有用得多）",
        "s5": "我们不接的活（边界）",
        "s6": "资质与可查成果",
        "s7": "合作须知",
        "s8": "联系方式",
        "desc_labels": ["技术团队", "需求响应", "最小接单"],
        "lists": [
            {"path": "capability.tech_directions", "label": "技术方向", "sec": 3, "codes": True},
            {"path": "capability.tech_stacks", "label": "技术栈", "sec": 3, "codes": True,
             "empty_text": "不限技术栈"},
            {"path": "capability.service_modes", "label": "服务模式", "sec": 3, "codes": True},
            {"path": "capability.deliverables", "label": "交付成果", "sec": 3, "codes": True},
            {"path": "capability.team_roles", "label": "团队构成", "sec": 3, "codes": True},
            {"path": "capability.portfolio", "label": "代表案例", "sec": 4},
        ],
        "metrics": [
            {"label": "技术团队", "path": "capability.team_size", "unit": "人"},
            {"label": "最小接单", "path": "limits.min_order_days", "unit": "人天"},
            {"label": "需求响应", "path": "limits.response_hours", "unit": "小时"},
            {"label": "常规交付周期", "path": "limits.delivery_days", "unit": "天"},
            {"label": "同时在制项目", "path": "limits.max_concurrent_projects", "unit": "个"},
            {"label": "最低项目金额", "path": "limits.min_order_value_cny", "money": True,
             "zero_text": "无门槛"},
            {"label": "当前负荷", "path": "limits.current_load_pct", "pct": True},
            {"label": "加急", "path": "limits.rush_available", "bool": ("可加急", "不可")},
            {"label": "技术支持时段", "path": "capability.support_hours", "text": True},
        ],
        "quality_extra": [
            {"label": "开源作品", "path": "capability.open_source",
             "bool": ("有公开开源作品", "无公开开源作品")},
            {"label": "软件著作权", "path": "quality.software_copyright_count", "unit": "项",
             "zero_text": "无软著"},
        ],
        "service_info": [
            {"label": "报价需提供", "path": "service.quote_inputs_required", "text": True},
            {"label": "报价响应", "path": "service.quote_response_hours", "unit": "小时"},
            {"label": "保密协议", "path": "service.nda_accepted",
             "bool": ("可签 NDA", "不签 NDA")},
            {"label": "免费维保", "path": "service.maintenance_months", "unit": "个月",
             "zero_text": "不提供免费维保"},
            {"label": "能否开票", "path": "service.invoice_available",
             "bool": ("可开票", "不提供")},
            {"label": "付款方式", "path": "service.payment_terms", "text": True},
        ],
    },

    # ------------------------------------------------ M 科学研究和技术服务业
    "M": {
        "noun": "科研技术服务",
        "primary": "capability.tech_directions",
        "s3": "技术方向与能力",
        "s4": "代表案例",
        "s4_empty": "未填写，建议补充具体项目与委托方类型",
        "s5": "我们不接的委托（边界）",
        "s6": "资质与认可",
        "s7": "委托须知",
        "s8": "联系方式",
        "desc_labels": ["出报告天数", "响应时效"],
        "lists": [
            {"path": "capability.tech_directions", "label": "技术方向", "sec": 3, "codes": True},
            {"path": "capability.service_modes", "label": "服务模式", "sec": 3, "codes": True},
            {"path": "capability.deliverables", "label": "交付成果", "sec": 3, "codes": True},
            {"path": "capability.team_roles", "label": "团队构成", "sec": 3, "codes": True},
            {"path": "capability.key_instruments", "label": "关键仪器设备", "sec": 3,
             "empty_text": "未登记仪器"},
            {"path": "capability.portfolio", "label": "代表案例", "sec": 4},
        ],
        "metrics": [
            {"label": "团队人数", "path": "capability.team_size", "unit": "人"},
            {"label": "研发/技术人员", "path": "capability.researcher_count", "unit": "人",
             "zero_text": "无专职研发人员"},
            {"label": "出报告天数", "path": "limits.report_days", "unit": "天"},
            {"label": "最少起做量", "path": "limits.min_order_qty", "unit": "件"},
            {"label": "最低委托金额", "path": "limits.min_order_value_cny", "money": True},
            {"label": "响应时效", "path": "limits.response_hours", "unit": "小时"},
            {"label": "同时在制项目", "path": "limits.max_concurrent_projects", "unit": "个"},
            {"label": "当前负荷", "path": "limits.current_load_pct", "pct": True},
            {"label": "加急出报告", "path": "limits.rush_available", "bool": ("可加急", "不可")},
            {"label": "服务时段", "path": "capability.support_hours", "text": True},
        ],
        "quality_extra": [
            # 这两个是本门类最硬的两行 —— 没有 CMA，出的报告不能用于司法/行政/社会公证。
            {"label": "CMA 资质认定", "path": "quality.cma_accredited",
             "bool": ("有（报告可用于司法/行政/社会公证）", "无（报告不能用于公证用途）")},
            {"label": "CNAS 实验室认可", "path": "quality.cnas_accredited",
             "bool": ("有（国际互认）", "无")},
        ],
        "service_info": [
            {"label": "报价需提供", "path": "service.quote_inputs_required", "text": True},
            {"label": "报价响应", "path": "service.quote_response_hours", "unit": "小时"},
            {"label": "保密协议", "path": "service.nda_accepted",
             "bool": ("可签保密协议", "不签保密协议")},
            {"label": "样品退还", "path": "service.sample_return",
             "bool": ("退还", "不退还（破坏性检测）")},
            {"label": "能否开票", "path": "service.invoice_available",
             "bool": ("可开票", "不提供")},
            {"label": "付款方式", "path": "service.payment_terms", "text": True},
        ],
    },
}


# ---------------------------------------------------------------- 门类分派

_SKILL_MD_BY_GATE = {
    "C": _render_skill_md_c,
    "H": _render_skill_md_h,
    **{g: (lambda cap, _g=g: _render_skill_md_generic(cap, _g)) for g in _MD_SPECS},
}


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
