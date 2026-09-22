"""beacon-mfg 适配器：把真实能力卡单向转换为 rfq-kernel 标准供应商卡。

关键边界（这是 rfq-kernel 与 beacon-mfg 能力卡关系的核心）：
- rfq-kernel 不重写 beacon-mfg 的能力卡，只『消费』它。能力卡是供给数据，
  rfq-kernel 是需求侧匹配引擎，二者是上下游，不是同一层。
- beacon-mfg 自动卡 limits 全 null、materials 空 —— 适配器原样保留 null，
  匹配引擎据此『静态优先，动态降级』，绝不编造硬指标。
- 认证强校验（三方验证）直接在真实卡上落地：
  quality.certifications[] 已是 {name, number, evidence} 结构，
  verified = evidence ∈ {platform_verified, field_audited} 且 number 非空。
  自报（self_declared）或无证书号 → verified=false → 不作数。
  这正是「ISO9001 需上传证书号、且平台/现场核验过才作数」。

真实卡权威位置（与 server/routers/rfq.py 一致）：
  skills/registry/capability/{sid}.json
skills/vendors/{id}/capability.json 作为兜底（两者内容同源）。
生产环境应优先 registry/capability，不要遍历 data/gb 分片。
"""
from __future__ import annotations

import json
from pathlib import Path

# skills/rfq-kernel/src -> src -> rfq-kernel -> skills -> beacon-mfg
BM_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# beacon-mfg 一级品类 -> rfq-kernel 行业 pack id
CATEGORY_TO_PACK = {
    "精密机械加工": "machining",
    "钣金冲压": "sheet_metal",
    "注塑成型": "injection",
    "压铸": "die_casting",
    "电子元器件": "electronics",
    "表面处理": "surface_treatment",
    "标准件": "fasteners",
    "原材料": "raw_material",
    "输送设备": "material_handling",
    "物流设备": "material_handling",
}

# 国标码(GB/T 4754) -> rfq-kernel 行业 pack id。
#
# 为什么需要它：beacon-mfg 在 2026-09-09 已定性「检索与分片一律走 gb_code（国标）；
# 8 品类降级为展示/采购标签」。指纹长尾记录只带 gb（无 category），gb 才是唯一的
# 行业判别信号。于是多轮匹配也用 gb 收敛行业，而非脆弱的 CJK 子串匹配。
#
# 只收录「已对照 data/gb4754-full.json 核对过名称」的码，绝不凭记忆编造：
#   - 33/34 门类内部混业严重（金属制品业含钣金/表面处理/紧固件/铸造；通用设备含机加工/紧固件），
#     一律用 4 位小类精确映射，不靠 2 位大类。
#   - 39(计算机通信电子设备)/31·32(黑色·有色冶炼压延) 门类判别清晰，可用 2 位。
# 判别规则（pack_of_gb）：gb 为空 -> None（无法归类，保留以不误删真实企业）；
# 命中下表不同 pack -> 视为跨行业可剔除；命中本 pack 或未知 -> 保留。
GB_TO_PACK = {
    # 钣金冲压（金属结构/门窗制造）
    "3311": "sheet_metal", "3312": "sheet_metal",
    # 精密机械加工（机床/金属加工机械、轴承齿轮传动、其他通用零部件、切削工具）
    # 3424 金属切割及焊接设备制造：归 machining（焊接设备属机加工装备，与钣金焊接服务区分）
    "3421": "machining", "3422": "machining", "3423": "machining", "3424": "machining",
    "3425": "machining", "3429": "machining",
    "3451": "machining", "3452": "machining", "3453": "machining", "3459": "machining",
    "3481": "machining", "3484": "machining", "3489": "machining",
    "3321": "machining", "3499": "machining",
    # 注塑成型（塑料制品业 292 全类）
    "2921": "injection", "2922": "injection", "2923": "injection", "2924": "injection",
    "2925": "injection", "2926": "injection", "2927": "injection", "2928": "injection",
    "2929": "injection",
    # 压铸（铸造/锻件及粉末冶金）
    "3391": "die_casting", "3392": "die_casting", "3393": "die_casting",
    # 表面处理（金属表面处理及热处理加工）
    "3360": "surface_treatment",
    # 标准件（紧固件制造）
    "3482": "fasteners",
    # 物料搬运设备制造（343 全类：起重/叉车/连续搬运/输送）
    # 3434 连续搬运设备制造（输送机械/装卸机械/给料机械）= 输送线语义最贴切的国标码
    "3431": "material_handling", "3432": "material_handling", "3433": "material_handling",
    "3434": "material_handling", "3439": "material_handling",
}


def pack_of_gb(gb) -> str | None:
    """国标码 -> pack id。空/未知 -> None（保守：不误删）。

    2 位清晰门类：39->electronics，31/32->raw_material；其余走 4 位精确表。
    """
    if not gb:
        return None
    code = str(gb)
    if code[:2] in ("31", "32"):
        return "raw_material"
    if code[:2] == "39":
        return "electronics"
    return GB_TO_PACK.get(code[:4])


def _card_path(supplier_id: str) -> Path | None:
    """权威路径：registry/capability（rfq.py 同款），vendors/ 兜底。"""
    reg = BM_ROOT / "skills" / "registry" / "capability" / f"{supplier_id}.json"
    if reg.exists():
        return reg
    ven = BM_ROOT / "skills" / "vendors" / supplier_id / "capability.json"
    return ven if ven.exists() else None


def _load_level(pct) -> str:
    if pct is None:
        return "light"
    if pct >= 85:
        return "heavy"
    if pct >= 50:
        return "moderate"
    return "light"


def _adapt_certs(quality: dict | None) -> list[dict]:
    """真实卡 quality.certifications[] -> 标准 cert 对象。

    verified 当且仅当 evidence 是平台/现场核验 且 证书号(number)非空。
    """
    quality = quality or {}
    out = []
    for c in (quality.get("certifications") or []):
        if not isinstance(c, dict):
            # 极少数旧裸字符串（理论不该出现）：按未核验处理
            out.append({"code": str(c), "cert_no": None,
                        "issuer": None, "valid_until": None, "verified": False})
            continue
        name = c.get("name")
        number = c.get("number")
        evidence = c.get("evidence")
        verified = evidence in ("platform_verified", "field_audited") and bool(number)
        out.append({
            "code": name,
            "cert_no": number,
            "issuer": None,
            "valid_until": c.get("valid_until"),
            "verified": verified,
        })
    return out


def adapt_card(l1: dict) -> dict:
    """L1 capability.json -> rfq-kernel 标准供应商卡。"""
    identity = l1.get("identity", {}) or {}
    region = " ".join(filter(None, [identity.get("province"), identity.get("city")])).strip()

    limits = l1.get("limits", {}) or {}
    moq = limits.get("min_order_qty")
    lead = limits.get("lead_time_days")
    load_pct = limits.get("current_load_pct")

    # 产能：beacon-mfg 自动卡无采样时间 -> fresh=False 降级
    capacity = {"load_level": _load_level(load_pct), "as_of": None, "ttl_days": 30}

    category = l1.get("category", "")
    pack_id = CATEGORY_TO_PACK.get(category)

    # RFQ 可达性：卡上的 rfq 块（schema=rfq/v1）即生效的 RFQ 能力位
    rfq = l1.get("rfq") or {}
    rfq_block = None
    if rfq.get("schema") == "rfq/v1" and rfq.get("endpoint"):
        rfq_block = {
            "protocol": rfq.get("protocol"),
            "endpoint": rfq.get("endpoint"),
            "schema": "rfq/v1",
            "auto_quote": bool(rfq.get("auto_quote", False)),
        }

    # 认领状态：信任等级锚点
    claim = l1.get("claim") or {}
    claim_status = claim.get("status", "unclaimed")

    return {
        "supplier_id": l1.get("supplier_id"),
        "legal_name": l1.get("company", ""),
        "industry": [pack_id] if pack_id else [],
        "region": region,
        "core": {
            "processes": [p.get("code") for p in l1.get("processes", [])],
            "moq": moq,
            "lead_time_days": lead,
            "payment_terms": None,
            "spot_stock": False,
            "certifications": _adapt_certs(l1.get("quality")),
            "capacity": capacity,
        },
        # 真实卡 limits 全 null -> 行业特有属性留空，匹配时宽匹配
        "industry_ext": {},
        "negative_capability": l1.get("exclusions") or [],
        "evidence": [],
        "rfq": rfq_block,
        "claim_status": claim_status,
        "_source": "beacon-mfg L1 adapter",
    }


def load_real_card(supplier_id: str) -> dict:
    """按 id 从 beacon-mfg 权威路径取真实能力卡并适配。

    生产环境直接读 skills/registry/capability/{sid}.json（与 rfq.py 投递读取同源），
    不再 grep data/gb 分片。找不到抛 FileNotFoundError。
    """
    path = _card_path(supplier_id)
    if path is None:
        raise FileNotFoundError(
            f"找不到 {supplier_id} 的能力卡（registry/capability 与 vendors/ 均未命中）")
    with open(path, encoding="utf-8") as f:
        l1 = json.load(f)
    return adapt_card(l1)


def fingerprint_to_card(rec: dict, pack_id: str | None = None) -> dict:
    """长尾桥：beacon-mfg 指纹记录 → rfq-kernel 标准供应商卡。

    14294 家多数只有 fingerprint（无 capability 卡）。指纹携带 proc/mat/cert/city
    稀疏字段，足以支撑核心层的结构化初筛（材料/工艺/认证/地区）；
    limits 与行业特有属性在指纹里没有 → 留 None/空，匹配时按『静态优先、动态降级』，
    绝不编造硬指标。认证裸字符串默认 verified=false（G6：无证书号即未核验）。
    """
    certs = [
        {"code": str(c), "cert_no": None, "issuer": None,
         "valid_until": None, "verified": False}
        for c in (rec.get("cert") or [])
    ]
    return {
        "supplier_id": rec.get("id"),
        "legal_name": rec.get("co") or rec.get("company") or "",
        "industry": [pack_id] if pack_id else [],
        "region": rec.get("city") or "",
        "gb": rec.get("gb"),
        "core": {
            "processes": list(rec.get("proc") or []),
            "materials": list(rec.get("mat") or []),
            "moq": None,
            "lead_time_days": None,
            "payment_terms": None,
            "spot_stock": False,
            "certifications": certs,
            "capacity": {"load_level": "light", "as_of": None, "ttl_days": 30},
        },
        "industry_ext": {},
        "negative_capability": [],
        "evidence": [],
        "rfq": None,
        "claim_status": "unclaimed",
        "_source": "fingerprint bridge",
        "_recall_relevance": rec.get("_recall_relevance", 0.0),
    }
