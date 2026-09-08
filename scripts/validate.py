#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 数据校验脚本（纯标准库，供 CI 与本地 PR 前使用）

用法:
    python scripts/validate.py              # 默认：核心检查（向后兼容，快速）
    python scripts/validate.py --strict     # 全量严格检查 + 生成统计 + README 数字一致性

默认模式（核心检查）:
    1. data/index.json 是合法 JSON，品类文件路径存在且可解析
    2. 每条中文记录核心必填字段齐全（id/company/category/keywords/region/contact_phone/source）
    3. id 格式合法（CN-MFG-XXXXXXX）且全局唯一
    4. 国标行业标签（industry）：代码必须在 GB/T 4754 代码表内、confidence 枚举合法、
       is_manufacturer 与代码门类自洽；industry=null 是合法值（不硬贴标签）
    5. data/industry-index.json 是否与主库同步（不同步 → 新企业按行业搜不到，静默缺陷）

--strict 模式（在核心检查之上追加）:
    4. status 枚举校验（若存在）；is_template 与 status 一致性
       （SPEC §2.4：is_template=true ⇔ status="unverified_poi"，真实企业 POI 电话待核实；
        仅 company 含「示例」才是 status="template" 真占位；is_template=false ⇔ "verified"）
    5. claim.status / claim.verified_by / agent.protocol / agent.capabilities 枚举校验
    6. 日期格式校验：imported_at / last_verified_at / verified_at（YYYY-MM-DD）
    7. 真实数据约束：source 合法且不得为 template；联系方式不得为占位符（verified 不得为「待核实」）
    8. index.json count 与实际文件条数一致性；category 与 index 一致性
    9. 英文镜像校验（文件存在、内部 id 唯一、company_en 非空）
    10. 调用 scripts/stats.py 重新生成 data/DATA_STATS.md
    11. README.md 数字与 DATA_STATS.md 一致性（不一致 → ERROR）

退出码 0 = 通过，1 = 有错误
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
INDEX = ROOT / "data" / "index.json"
SUPPLIERS_DIR = ROOT / "data" / "suppliers"
EN_DIR = ROOT / "data" / "en"
README = ROOT / "README.md"

ID_RE = re.compile(r"^CN-MFG-\d{4,7}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PLACEHOLDER_MARKERS = ["XXXX", "xxxx", "占位", "example", "示例"]

STATUS_ENUM = ("verified", "unverified_poi", "template")
SOURCE_ENUM = ("public_directory", "gov_list", "company_website", "exhibition", "template")
CLAIM_STATUS_ENUM = ("unclaimed", "claimed", "verified")
VERIFIED_BY_ENUM = ("wechat", "email", "manual")
PROTOCOL_ENUM = ("mcp", "skill", "a2a", "native")
CAPABILITY_ENUM = ("catalog", "rfq", "live_chat", "quote")

CORE_FIELDS = ("id", "company", "category", "keywords", "region", "contact_phone", "source")

EN_FILES = {
    "精密机械加工": "precision-machining.json",
    "钣金冲压": "sheet-metal.json",
    "注塑成型": "injection-molding.json",
    "压铸": "die-casting.json",
    "电子元器件": "electronic-components.json",
    "表面处理": "surface-treatment.json",
    "标准件": "standard-parts.json",
    "原材料": "raw-materials.json",
}

sys.path.insert(0, str(SCRIPTS_DIR))  # 保证可 import scripts/stats.py（resolve_status / 统计复用）
try:
    from stats import resolve_status as _resolve_status
except ImportError:  # stats.py 缺失时降级为内置推导，不阻断校验
    _resolve_status = None

try:
    from industry_taxonomy import (
        CODES as _GB_CODES, CLASSES as _GB_CLASSES, GROUPS as _GB_GROUPS,
        DIVISIONS as _GB_DIVISIONS, name_of as _gb_name, is_manufacturer as _gb_is_mfr,
        level_of as _gb_level,
    )
except ImportError:
    _GB_CODES, _GB_CLASSES, _GB_GROUPS, _GB_DIVISIONS = {}, {}, {}, {}
    _gb_name = lambda c: ""
    _gb_is_mfr = lambda c: True
    _gb_level = lambda c: ""


def _gb_valid(code):
    """分层落地后代码可能是 4 位小类 / 3 位中类 / 2 位大类，按长度分别查表。"""
    code = str(code)
    if code[:1].isalpha():
        return False
    return {2: _GB_DIVISIONS, 3: _GB_GROUPS, 4: _GB_CLASSES}.get(len(code), {}).__contains__(code)

GB_CONFIDENCE_ENUM = ("high", "medium", "low")
INDUSTRY_INDEX = ROOT / "data" / "industry-index.json"

errors = []
warnings = []


def _resolved_status(item):
    """状态解析：优先 stats.resolve_status，缺失时按 SPEC §2.4 内置推导。"""
    if _resolve_status is not None:
        try:
            return _resolve_status(item)
        except Exception:
            pass
    s = item.get("status")
    if s in STATUS_ENUM:
        return s
    if item.get("is_template") is False:
        return "verified"
    if item.get("is_template") is True:
        company = item.get("company") or ""
        if any(m in company for m in ("示例", "测试")):
            return "template"
        return "unverified_poi"
    return None


def err(msg):
    errors.append(msg)
    print(f"[ERROR] {msg}")


def warn(msg):
    warnings.append(msg)
    print(f"[WARN] {msg}")


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        err(f"{path.relative_to(ROOT)} 无法解析: {e}")
        return None


# ---------------------------------------------------------------- 默认核心检查

def check_core_record(item, path, seen_ids):
    """默认模式：核心字段齐全 + id 格式/唯一。"""
    ok = True
    for f in CORE_FIELDS:
        if f not in item:
            err(f"{path}: 缺少必填核心字段 '{f}'")
            ok = False
    if "keywords" in item and not item.get("keywords"):
        err(f"{path} ({item.get('id', '?')}): keywords 不能为空")
        ok = False
    if "id" in item:
        if not ID_RE.match(item["id"]):
            err(f"{path} ({item['id']}): id 格式应为 CN-MFG-XXXXXXX（4-7位数字）")
            ok = False
        if item["id"] in seen_ids:
            err(f"重复 id: {item['id']}")
            ok = False
        seen_ids.add(item["id"])
    return ok


# ---------------------------------------------------------------- 严格模式追加检查

def check_status_consistency(item, path):
    """status 枚举、is_template↔status 一致性。"""
    ok = True
    status = item.get("status")
    is_tpl = item.get("is_template")
    if status is not None and status not in STATUS_ENUM:
        err(f"{path} ({item.get('id')}): status 枚举非法 '{status}'（应为 {list(STATUS_ENUM)}）")
        ok = False
    if is_tpl is True and status not in (None, "unverified_poi", "template"):
        err(f"{path} ({item.get('id')}): is_template=true 与 status='{status}' 不一致"
            f"（SPEC §2.4：is_template=true 表示电话待核实，应对应 "
            f"status=\"unverified_poi\"；真占位才用 \"template\"）")
        ok = False
    if is_tpl is False and status not in (None, "verified"):
        err(f"{path} ({item.get('id')}): is_template=false 与 status='{status}' 冲突"
            f"（电话已核实的记录才应为 is_template=false / status=\"verified\"）")
        ok = False
    if status is None and is_tpl is None:
        err(f"{path} ({item.get('id')}): 缺少数据状态字段（新数据请用 status，旧数据保留 is_template）")
        ok = False
    return ok


def check_industry(item, path):
    """国标行业标签校验（GB/T 4754-2017 子集）。

    industry=null 是**合法值**（公司名无行业信号 / 越界行业，不硬贴标签）。
    只要打了标签，代码、置信度、is_manufacturer 三者必须自洽。
    """
    if "industry" not in item:
        # 不打逐条警告——缺标签的新抓数据可能有几千条，汇总一条更有用
        return False, True
    ind = item.get("industry")
    if ind is None:
        return False, False
    if not isinstance(ind, dict) or "code" not in ind:
        err(f"{path} ({item.get('id')}): industry 结构非法（应为对象且含 code）")
        return False, False
    code = ind["code"]
    if not _GB_CODES:
        return True, False  # 代码表缺失时不做内容校验
    if not _gb_valid(code):
        err(f"{path} ({item.get('id')}): industry.code '{code}' 不在 GB/T 4754 代码表里"
            f"（小类4位/中类3位/大类2位）")
        return False, False
    ok = True
    if ind.get("name") != _gb_name(code):
        warn(f"{path} ({item.get('id')}): industry.name 与代码表不一致"
             f"（{ind.get('name')} ≠ {_gb_name(code)}）")
        ok = False
    if ind.get("level") != _gb_level(code):
        err(f"{path} ({item.get('id')}): industry.level '{ind.get('level')}' 与码长 {len(code)} 位不符"
            f"（应为 {_gb_level(code)}）")
        ok = False
    if ind.get("confidence") not in GB_CONFIDENCE_ENUM:
        err(f"{path} ({item.get('id')}): industry.confidence 非法 '{ind.get('confidence')}'"
            f"（应为 {list(GB_CONFIDENCE_ENUM)}）")
        ok = False
    want_mfr = _gb_is_mfr(code)
    if item.get("is_manufacturer") is not want_mfr:
        err(f"{path} ({item.get('id')}): is_manufacturer={item.get('is_manufacturer')} 与 "
            f"行业 {code}（{'批发业' if not want_mfr else '制造业'}）矛盾")
        ok = False
    return ok, False


def check_industry_index(total, classified, unclassified):
    """行业索引是否过期。

    为什么必须查：SKILL.md 要求 Agent「按行业检索先读 industry-index.json」。
    索引里的 ids 是快照，抓完新数据不重建 → 新企业按行业永远搜不到，且不报错
    （region-index.json 就曾这样静默失效过）。
    """
    if not INDUSTRY_INDEX.exists():
        err("缺少 data/industry-index.json（跑 scripts/gen_industry_index.py 生成）")
        return
    try:
        data = json.loads(INDUSTRY_INDEX.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        err(f"data/industry-index.json 无法解析: {e}")
        return
    meta = data.get("metadata", {})
    if meta.get("total_suppliers") != total:
        warn("data/industry-index.json 已过期：索引 %s 家 ≠ 实际 %d 家，"
             "新数据按行业检索会搜不到 → 跑 scripts/gen_industry_index.py"
             % (meta.get("total_suppliers"), total))
    elif meta.get("classified") != classified:
        warn("data/industry-index.json 归类数与主库不一致（索引 %s vs 实际 %d）→ 重建索引"
             % (meta.get("classified"), classified))
    for code, v in data["index"].items():
        if not _GB_CODES:
            break
        if not _gb_valid(code):
            err(f"industry-index.json 含非法代码 {code}")
        elif len(v.get("ids", [])) != v.get("count"):
            err(f"industry-index.json: {code} 的 ids 条数 {len(v.get('ids', []))} ≠ count {v.get('count')}")


def check_dates(item, path):
    ok = True
    for f in ("verified_at", "imported_at", "last_verified_at"):
        v = item.get(f)
        if v is None:
            continue
        if not isinstance(v, str) or not DATE_RE.match(v):
            err(f"{path} ({item.get('id')}): {f} 格式应为 YYYY-MM-DD，实际 '{v}'")
            ok = False
    return ok


def check_claim_agent(item, path):
    ok = True
    claim = item.get("claim")
    if claim is not None and not isinstance(claim, dict):
        err(f"{path} ({item.get('id')}): claim 应为 object 或 null")
        ok = False
    elif isinstance(claim, dict):
        cs = claim.get("status")
        if cs is not None and cs not in CLAIM_STATUS_ENUM:
            err(f"{path} ({item.get('id')}): claim.status 枚举非法 '{cs}'（应为 {list(CLAIM_STATUS_ENUM)}）")
            ok = False
        vb = claim.get("verified_by")
        if vb is not None and vb not in VERIFIED_BY_ENUM:
            err(f"{path} ({item.get('id')}): claim.verified_by 枚举非法 '{vb}'（应为 {list(VERIFIED_BY_ENUM)}）")
            ok = False
        ca = claim.get("claimed_at")
        if isinstance(ca, str) and not DATE_RE.match(ca):
            err(f"{path} ({item.get('id')}): claim.claimed_at 格式应为 YYYY-MM-DD")
            ok = False

    agent = item.get("agent")
    if agent is not None and not isinstance(agent, dict):
        err(f"{path} ({item.get('id')}): agent 应为 object 或 null")
        ok = False
    elif isinstance(agent, dict):
        proto = agent.get("protocol")
        if proto is not None and proto not in PROTOCOL_ENUM:
            err(f"{path} ({item.get('id')}): agent.protocol 枚举非法 '{proto}'（应为 {list(PROTOCOL_ENUM)}）")
            ok = False
        caps = agent.get("capabilities")
        if caps is not None:
            if not isinstance(caps, list):
                err(f"{path} ({item.get('id')}): agent.capabilities 应为数组")
                ok = False
            else:
                for c in caps:
                    if c not in CAPABILITY_ENUM:
                        err(f"{path} ({item.get('id')}): agent.capabilities 含非法值 '{c}'（应为 {list(CAPABILITY_ENUM)}）")
                        ok = False
    return ok


def check_phone_and_source(item, path, status):
    """按解析后的 status 校验 source 与联系方式真实性约束。
    规则（SPEC §2.5）：真实数据 source 不能为空/template；联系方式不得含占位符
    （XXXX/占位/example/示例）；「待核实」是缺失电话的规范表示，允许。"""
    ok = True
    src = item.get("source")
    if src is not None and src not in SOURCE_ENUM:
        err(f"{path} ({item.get('id')}): source 枚举非法 '{src}'（应为 {list(SOURCE_ENUM)}）")
        ok = False
    if status in ("verified", "unverified_poi"):
        # 真实数据（SPEC §5.1）：source 不能为空或 template
        if src in (None, "", "template"):
            err(f"{path} ({item.get('id')}): 真实数据（status={status}）的 source 不能为空或 template")
            ok = False
        phone = item.get("contact_phone") or ""
        for seg in re.split(r"[;；,，]", phone):
            seg = seg.strip()
            if not seg or seg == "待核实":
                continue
            if any(m in seg for m in PLACEHOLDER_MARKERS):
                err(f"{path} ({item.get('id')}): 真实数据联系方式疑似占位符 '{seg}'")
                ok = False
    return ok


def check_strict_record(item, path):
    ok = True
    for fn in (check_status_consistency, check_dates, check_claim_agent):
        if not fn(item, path):
            ok = False
    if not check_phone_and_source(item, path, _resolved_status(item)):
        ok = False
    return ok


def check_en_mirrors(valid_categories):
    """英文镜像：存在性 + 内部 id 唯一 + company_en 非空（与中文主库不交叉去重，同 id 是设计）。"""
    ok = True
    total = 0
    for cat_name, fn in EN_FILES.items():
        path = EN_DIR / fn
        if not path.exists():
            err(f"英文镜像缺失: data/en/{fn}")
            ok = False
            continue
        items = load_json(path)
        if items is None:
            ok = False
            continue
        seen = set()
        for it in items:
            iid = it.get("id")
            if not iid:
                err(f"data/en/{fn}: 缺少 id 字段")
                ok = False
                continue
            if iid in seen:
                err(f"data/en/{fn}: 英文镜像内部重复 id: {iid}")
                ok = False
            seen.add(iid)
            if not it.get("company_en"):
                warn(f"data/en/{fn} ({iid}): company_en 为空")
            if it.get("category") not in valid_categories:
                err(f"data/en/{fn} ({iid}): 未知 category '{it.get('category')}'")
                ok = False
        total += len(items)
    if ok and total:
        print(f"英文镜像校验：{total} 条")
    return ok


def warn_verified_rows_with_pending_phone(items_by_cat):
    """聚合提示：is_template=false（≈verified）但电话仍为「待核实」的记录。
    非错误——「待核实」是缺失电话的规范表示（SPEC §2.5）；仅提示迁移 status 时的归属。"""
    per_cat = {}
    for cat_name, items in items_by_cat.items():
        n = 0
        for it in items:
            if it.get("is_template") is False and "待核实" in (it.get("contact_phone") or ""):
                n += 1
        if n:
            per_cat[cat_name] = n
    for cat_name, n in per_cat.items():
        warn(f"{cat_name}: {n} 条 is_template=false 记录电话仍为「待核实」（迁移到 status 时建议标 unverified_poi 或补全电话）")


def check_index_counts(items_by_cat, index):
    """index.json 的 count / total_suppliers 与实际文件条数一致性。"""
    ok = True
    real_sum = 0
    for cat in index["categories"]:
        real_sum += len(items_by_cat.get(cat["name"], []))
        declared = cat.get("count")
        actual = len(items_by_cat.get(cat["name"], []))
        if declared != actual:
            err(f"index.json: 品类 '{cat['name']}' count={declared} 与实际文件条数 {actual} 不一致")
            ok = False
    declared_total = index.get("total_suppliers")
    if declared_total is not None and declared_total != real_sum:
        err(f"index.json: total_suppliers={declared_total} 与各品类实际合计 {real_sum} 不一致")
        ok = False
    return ok


# ---------------------------------------------------------------- 统计 & README 一致性

def ensure_stats_file():
    """调用 scripts/stats.py 重新生成 data/DATA_STATS.md；返回统计 dict 或 None。"""
    try:
        import stats as stats_mod
        st = stats_mod.compute_stats()
        md = stats_mod.render_markdown(st)
        path = stats_mod.write_stats_file(md)
        print(f"已生成 {path.relative_to(ROOT)}")
        return st
    except Exception as e:  # noqa: BLE001 —— 统计失败不应中断其余校验输出
        err(f"生成 data/DATA_STATS.md 失败: {e}")
        return None


def _read_num(m):
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def check_readme_consistency(st):
    """README.md 数字与 DATA_STATS.md（stats dict）一致性。不一致 → ERROR。"""
    if not README.exists():
        warn("README.md 不存在，跳过数字一致性检查")
        return True
    text = README.read_text(encoding="utf-8")
    ok = True

    def cmp_metric(label, found, expected):
        nonlocal ok
        if found is None:
            warn(f"README 未找到指标「{label}」的表述，无法比对（保持数字同步请按现有格式书写）")
            return
        if found != expected:
            err(f"README 数字一致性：{label} README={found}，DATA_STATS.md={expected}")
            ok = False

    # 徽章（全文）
    cmp_metric("records 徽章", _read_num(re.search(r"records-(\d[\d,]*)", text)), st["cn_total"])
    cmp_metric("categories 徽章", _read_num(re.search(r"categories-(\d[\d,]*)", text)), st["n_categories"])

    # 「数据现状」章节
    sec = None
    m = re.search(r"^##\s*数据现状\s*$.*?(?=^##\s)", text, re.M | re.S)
    if m:
        sec = m.group(0)
    else:
        warn("README 未找到「## 数据现状」章节，只能比对徽章数字")
        sec = text

    cmp_metric("中文记录总数", _read_num(re.search(r"(\d[\d,]*)\s*条记录", sec)), st["cn_total"])
    cmp_metric("品类数", _read_num(re.search(r"(\d[\d,]*)\s*个品类", sec)), st["n_categories"])
    cmp_metric("已核实数", _read_num(re.search(r"(\d[\d,]*)\s*条(?:电话)?已核实", sec)), st["verified_total"])
    # README 的「待核实」= 电话待核实，对应 status="unverified_poi"。
    # （2026-09-08 已按 SPEC §2.4 重跑迁移，此前 3170 家真实企业被误标 status="template"
    #   的坑已消除，template 桶现在只收真占位，故不再并入比对。）
    cmp_metric("待核实数", _read_num(re.search(r"(\d[\d,]*)\s*条待核实", sec)), st["unverified_total"])
    cmp_metric("英文镜像数", _read_num(re.search(r"(\d[\d,]*)\s*条英文镜像", sec)), st["en_total"])
    cmp_metric("覆盖城市数", _read_num(re.search(r"(\d[\d,]*)\s*个城市", sec)), st["cities"])

    # 分品类表：| 品类 | 总数 | 已核实 | 待核实 |（与 DATA_STATS 各品类明细一致）
    row_hits = 0
    for r in st["categories"]:
        name = r["name"]
        pat = re.compile(
            r"\|\s*" + re.escape(name) + r"\s*\|\s*(\d[\d,]*)\s*\|\s*(\d[\d,]*)\s*\|\s*(\d[\d,]*)\s*\|")
        rm = pat.search(sec)
        if not rm:
            continue
        row_hits += 1
        total_n = int(rm.group(1).replace(",", ""))
        verified_n = int(rm.group(2).replace(",", ""))
        unverified_n = int(rm.group(3).replace(",", ""))
        cmp_metric(f"品类「{name}」总数", total_n, r["total"])
        cmp_metric(f"品类「{name}」已核实", verified_n, r["verified"])
        # 同上：README 待核实列对应 status="unverified_poi"（电话待核实）
        cmp_metric(f"品类「{name}」待核实", unverified_n, r["unverified_poi"])
    if row_hits == 0:
        warn("README「数据现状」章节未匹配到任何分品类表格行，跳过逐品类比对")
    return ok


# ---------------------------------------------------------------- 主流程

def main():
    # Windows 控制台默认 GBK，强制 UTF-8 输出避免 UnicodeEncodeError（CI/Linux 不受影响）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="BeaconMFG 数据校验（--strict 为全量严格模式）")
    parser.add_argument("--strict", action="store_true", help="开启全部严格检查")
    args = parser.parse_args()

    # 1. index.json
    index = load_json(INDEX)
    if index is None:
        print("校验终止：index.json 无法解析。")
        sys.exit(1)
    categories = index.get("categories") or []
    if not categories:
        err("index.json: categories 为空")
        sys.exit(1)
    valid_categories = {c["name"] for c in categories}

    # 2. 逐品类校验
    seen_ids = set()
    total = 0
    classified = unclassified = missing_key_n = 0
    items_by_cat = {}
    for cat in categories:
        path = SUPPLIERS_DIR / cat["file"]
        if not path.exists():
            err(f"品类 '{cat['name']}' 文件不存在: {cat['file']}")
            continue
        items = load_json(path)
        if items is None:
            continue
        items_by_cat[cat["name"]] = items
        total += len(items)
        for item in items:
            check_core_record(item, cat["file"], seen_ids)
            has_ind, missing_key = check_industry(item, cat["file"])
            if has_ind:
                classified += 1
            else:
                unclassified += 1
                if missing_key:
                    missing_key_n += 1
            if args.strict:
                check_strict_record(item, cat["file"])
                if item.get("category") not in valid_categories:
                    err(f"{cat['file']}: 未知 category '{item.get('category')}'")
    if total == 0:
        err("没有任何可校验的记录")

    check_industry_index(total, classified, unclassified)
    if total:
        print("国标行业覆盖：%d/%d 条已归类（%.1f%%），未归类 %d 条"
              % (classified, total, 100 * classified / total, unclassified))
    if missing_key_n:
        warn("%d 条记录连 industry 字段都没有（新抓未打标签）→ 跑 "
             "scripts/classify_industry.py --backfill" % missing_key_n)

    # 3. 严格模式附加检查
    if args.strict:
        check_index_counts(items_by_cat, index)
        check_en_mirrors(valid_categories)
        warn_verified_rows_with_pending_phone(items_by_cat)
        # 10. 生成 data/DATA_STATS.md（复用 scripts/stats.py）
        st = ensure_stats_file()
        # 11. README 数字一致性
        if st is not None:
            check_readme_consistency(st)

    # 汇总
    if errors:
        print(f"\n校验未通过：{len(errors)} 个错误，{len(warnings)} 个警告。请修复后重试。")
        sys.exit(1)
    mode = "严格模式" if args.strict else "核心模式"
    print(f"校验通过（{mode}）：{len(categories)} 个品类，{total} 条中文记录（{len(warnings)} 个警告）。")
    sys.exit(0)


if __name__ == "__main__":
    main()
