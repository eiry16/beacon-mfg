#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BeaconMFG 数据统计（生成 data/DATA_STATS.md）

用法:
    python scripts/stats.py

说明:
    - 统计 data/suppliers/*.json 中文主库与 data/en/*.json 英文镜像，输出 markdown。
    - 状态分类（SPEC §2.4）优先读取 status 字段；旧数据无 status 时按 is_template 兼容推导：
        is_template=false                    → verified（电话完整）
        is_template=true 且公司名含「示例/测试」 → template（示例占位）
        is_template=true                     → unverified_poi（真实企业 POI，电话待核实）
    - 直接生成 data/DATA_STATS.md（validate.py --strict 亦会调用本模块重新生成）。
"""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "data" / "index.json"
SUPPLIERS_DIR = ROOT / "data" / "suppliers"
EN_DIR = ROOT / "data" / "en"
STATS_FILE = ROOT / "data" / "DATA_STATS.md"

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

STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified_poi"
STATUS_TEMPLATE = "template"
ALL_STATUSES = (STATUS_VERIFIED, STATUS_UNVERIFIED, STATUS_TEMPLATE)

SOURCE_LABELS = {
    "public_directory": "公开地图/工商 POI 名录",
    "gov_list": "政府公开名单（专精特新/高企等）",
    "company_website": "企业官网",
    "exhibition": "展会/协会名录",
    "template": "示例占位数据",
}
SOURCE_NOTE = {
    "public_directory": "当前数据 100% 为此来源",
    "gov_list": "尚未引入",
    "company_website": "尚未引入",
    "exhibition": "尚未引入",
    "template": "仅 status=template 标识，不作 source 值",
}

# 字段完整性统计关注的字段
FIELD_LABELS = [
    ("core", ["id", "company", "category", "keywords", "region", "contact_phone"]),
    ("address", ["address"]),
    ("lat", ["lat"]),
    ("lng", ["lng"]),
    ("source", ["source"]),
    ("verified_at", ["verified_at"]),
    ("source_url", ["source_url"]),
    ("note", ["note"]),
    ("website", ["website"]),
    ("contact_email", ["contact_email"]),
    ("founded_scale_cert", ["founded", "scale", "certifications"]),
    ("status", ["status"]),
    ("imported_at", ["imported_at", "last_verified_at"]),
    ("claim", ["claim"]),
    ("agent", ["agent"]),
]
FIELD_REMARKS = {
    "core": "核心必填字段",
    "address": "公开地址",
    "lat": "纬度（LBS 检索）",
    "lng": "经度（LBS 检索）",
    "source": "来源渠道（当前全为 public_directory）",
    "verified_at": "旧字段，实际为导入日期（非核验日期）",
    "source_url": "去出处策略：当前为空串（字段保留，供未来溯源）",
    "note": "备注（当前未填写）",
    "website": "增值字段：认领后解锁（当前未收录）",
    "contact_email": "增值字段：认领后解锁（当前未收录）",
    "founded_scale_cert": "增值字段：认领后解锁（当前未收录）",
    "status": "待迁移（schema 已支持；当前按 is_template 兼容推导）",
    "imported_at": "Phase 0 拆分启用（schema 已支持）",
    "claim": "Phase 1 认主系统预留",
    "agent": "Phase 1 供应商官方 Agent 预留",
}


def _is_empty(v):
    return v is None or v == "" or v == [] or v == {}


def load_json(path):
    """读取 JSON；失败返回 None（不抛出，由调用方决定降级策略）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def resolve_status(item):
    """把记录归一到 status 枚举（SPEC §2.4）：优先 status 字段，否则按 is_template 推导。"""
    s = item.get("status")
    if s in ALL_STATUSES:
        return s
    if item.get("is_template") is False:
        return STATUS_VERIFIED
    company = item.get("company") or ""
    if item.get("is_template") is True:
        if any(m in company for m in ("示例", "测试")):
            return STATUS_TEMPLATE
        return STATUS_UNVERIFIED
    return None  # 既无 status 也无 is_template（非法，由 validate.py 报错）


def _count_cn(index):
    """中文主库分品类计数（总数 / verified / unverified_poi / template / 其他）。"""
    rows = []
    for cat in index["categories"]:
        path = SUPPLIERS_DIR / cat["file"]
        items = load_json(path)
        if items is None:
            items = []
        buckets = {s: 0 for s in ALL_STATUSES}
        buckets["unknown"] = 0
        for it in items:
            st = resolve_status(it)
            key = st if st in ALL_STATUSES else "unknown"
            buckets[key] += 1
        rows.append({
            "name": cat["name"],
            "total": len(items),
            STATUS_VERIFIED: buckets[STATUS_VERIFIED],
            STATUS_UNVERIFIED: buckets[STATUS_UNVERIFIED],
            STATUS_TEMPLATE: buckets[STATUS_TEMPLATE],
            "unknown": buckets["unknown"],
        })
    return rows


def _count_en():
    """英文镜像分品类计数（仅统计 EN_FILES 映射内的 8 个镜像文件）。"""
    total = 0
    per_cat = {}
    problems = []
    for cat_name, fn in EN_FILES.items():
        path = EN_DIR / fn
        items = load_json(path)
        if items is None:
            problems.append(fn)
            continue
        per_cat[cat_name] = len(items)
        total += len(items)
    return total, per_cat, problems


def _count_cities(index):
    """按中文主库 region.city 统计覆盖城市数（去重）。"""
    cities = set()
    for cat in index["categories"]:
        path = SUPPLIERS_DIR / cat["file"]
        items = load_json(path)
        if items is None:
            continue
        for it in items:
            r = it.get("region") or {}
            if isinstance(r, dict) and r.get("city"):
                cities.add(r["city"])
    return len(cities)


def _field_rates(index):
    """真实数据（verified，is_template=false）的字段非空率。"""
    real_n = 0
    counts = {name: [0, 0] for name, _ in FIELD_LABELS}  # [非空, 样本]
    verified_at_values = set()
    for cat in index["categories"]:
        path = SUPPLIERS_DIR / cat["file"]
        items = load_json(path)
        if items is None:
            continue
        for it in items:
            if resolve_status(it) != STATUS_VERIFIED:
                continue
            real_n += 1
            for name, fields in FIELD_LABELS:
                counts[name][1] += 1
                if all(not _is_empty(it.get(f)) for f in fields):
                    counts[name][0] += 1
            v = it.get("verified_at")
            if isinstance(v, str) and v:
                verified_at_values.add(v)
    rates = {}
    for name, _ in FIELD_LABELS:
        nonempty, sample = counts[name]
        rates[name] = (round(nonempty / sample * 100, 1) if sample else 0.0, nonempty, sample)
    return real_n, rates, sorted(verified_at_values)


def compute_stats():
    """汇总全部统计，返回 dict（供 render_markdown 与 validate.py 复用）。"""
    index = load_json(INDEX)
    if index is None or not index.get("categories"):
        raise RuntimeError(f"{INDEX} 无法解析或 categories 为空")

    rows = _count_cn(index)
    totals = {s: sum(r[s] for r in rows) for s in ALL_STATUSES}
    cn_total = sum(r["total"] for r in rows)

    en_total, en_per_cat, en_problems = _count_en()

    # 来源分布（全部中文记录）
    source_counter = {}
    for cat in index["categories"]:
        path = SUPPLIERS_DIR / cat["file"]
        items = load_json(path) or []
        for it in items:
            src = it.get("source")
            source_counter[src] = source_counter.get(src, 0) + 1

    cities = _count_cities(index)
    real_n, field_rates, verified_at_values = _field_rates(index)

    return {
        "generated_on": date.today().isoformat(),
        "n_categories": len(rows),
        "cn_total": cn_total,
        "verified_total": totals[STATUS_VERIFIED],
        "unverified_total": totals[STATUS_UNVERIFIED],
        "template_total": totals[STATUS_TEMPLATE],
        "categories": rows,
        "en_total": en_total,
        "en_per_cat": en_per_cat,
        "en_problems": en_problems,
        "source_counter": source_counter,
        "cities": cities,
        "real_count": real_n,
        "field_rates": field_rates,
        "verified_at_values": verified_at_values,
    }


def _pct(n, d):
    """百分比：整数不带小数（100%），否则保留 1 位（53.8%）。"""
    p = round(n / d * 100, 1) if d else 0.0
    return f"{p:g}%"


def _fmt_pct(n, d, ndigits=1):
    return _pct(n, d)


def render_markdown(st):
    """把统计 dict 渲染为 data/DATA_STATS.md 的 markdown 正文。"""
    src_total = st["cn_total"]
    src_lines = []
    for src, count in sorted(st["source_counter"].items(), key=lambda kv: -kv[1]):
        label = SOURCE_LABELS.get(src, src or "(空)")
        note = SOURCE_NOTE.get(src, "")
        pct = _pct(count, src_total)
        src_lines.append(f"| `{src}` | {count} | {pct} | {label}（{note}） |" if src else
                         f"| (空) | {count} | {pct} | 缺少 source 字段 |")
    for src in SOURCE_LABELS:
        if src not in st["source_counter"]:
            src_lines.append(f"| `{src}` | 0 | 0% | {SOURCE_LABELS[src]}（{SOURCE_NOTE[src]}） |")

    vdates = st["verified_at_values"]
    if len(vdates) == 1:
        vdate_note = f"真实数据全部为 {vdates[0]}"
    elif vdates:
        vdate_note = f"真实数据共 {len(vdates)} 个日期值：{'、'.join(vdates)}"
    else:
        vdate_note = "无"

    lines = []
    lines.append("# BeaconMFG 数据统计")
    lines.append("")
    lines.append("> ⚠️ **本文件由 `scripts/stats.py` 自动生成（`validate.py --strict` 触发），请勿手动编辑。**")
    lines.append("> 数字与 README/SKILL 不一致时，以本文件为准。")
    lines.append("")
    lines.append(f"**更新日期：** {st['generated_on']}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 总体")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 品类数 | **{st['n_categories']}** |")
    lines.append(f"| 中文记录总数 | **{st['cn_total']}** |")
    lines.append(f"| 已核实（verified） | **{st['verified_total']}** |")
    lines.append(f"| 待核实（unverified_poi） | **{st['unverified_total']}** |")
    lines.append(f"| 模板（template，示例占位） | **{st['template_total']}** |")
    lines.append(f"| 英文镜像 | **{st['en_total']}** |")
    lines.append(f"| 覆盖城市 | **{st['cities']}** |")
    src_txt = "、".join(
        f"{SOURCE_LABELS.get(s, s)}（{_pct(c, src_total)} {s}）"
        for s, c in sorted(st["source_counter"].items(), key=lambda kv: -kv[1])
    ) or "(空)"
    lines.append(f"| 数据来源（当前） | {src_txt} |")
    lines.append("")
    lines.append("> **说明：** `is_template=true` 的记录并非「模板占位」，而是真实企业 POI 电话待核实的")
    lines.append("> 地图数据（对应 `status: \"unverified_poi\"`），检索时应保留。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 各品类明细")
    lines.append("")
    lines.append("| 品类 | 总数 | 已核实（verified） | 待核实（unverified_poi） | 模板（template） | 待核实占比 |")
    lines.append("|---|---|---|---|---|---|")
    for r in st["categories"]:
        lines.append(
            f"| {r['name']} | {r['total']} | {r[STATUS_VERIFIED]} | {r[STATUS_UNVERIFIED]} | "
            f"{r[STATUS_TEMPLATE]} | {_fmt_pct(r[STATUS_UNVERIFIED], r['total'])} |"
        )
    if st["en_per_cat"]:
        lines.append("")
        lines.append("| 品类（英文镜像） | 条数 |")
        lines.append("|---|---|")
        for r in st["categories"]:
            n = st["en_per_cat"].get(r["name"], 0)
            lines.append(f"| {r['name']} | {n} |")
    if st["en_problems"]:
        lines.append("")
        lines.append(f"> ⚠️ 英文镜像文件缺失或损坏：{', '.join(st['en_problems'])}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 字段完整性")
    lines.append("")
    lines.append(f"> 样本：真实数据（verified，is_template=false）共 {st['real_count']} 条；非空率按字段是否缺失/为空串/空数组统计。")
    lines.append("")
    lines.append("| 字段 | 非空率 | 备注 |")
    lines.append("|---|---|---|")
    fr = st["field_rates"]
    remarks = dict(FIELD_REMARKS)
    remarks["verified_at"] = f"旧字段，实际为导入日期（非核验日期）；{vdate_note}"
    for name, fields in FIELD_LABELS:
        rate, nonempty, sample = fr[name]
        pct = f"{rate:g}%" if rate == int(rate) else f"{rate}%"
        lines.append(f"| {' / '.join(fields)} | {pct}（{nonempty}/{sample}） | {remarks[name]} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 来源说明")
    lines.append("")
    lines.append("| source | 记录数 | 占比 | 说明 |")
    lines.append("|---|---|---|---|")
    lines.extend(src_lines)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*生成命令：`python scripts/validate.py --strict`（CI 自动执行，底层调用 `scripts/stats.py`）*")
    lines.append("")
    return "\n".join(lines)


def write_stats_file(markdown):
    """写入 data/DATA_STATS.md（utf-8，末尾换行）。"""
    STATS_FILE.write_text(markdown + "\n", encoding="utf-8")
    return STATS_FILE


def main():
    # Windows 控制台默认 GBK，强制 UTF-8 输出避免 UnicodeEncodeError（CI/Linux 不受影响）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    st = compute_stats()
    md = render_markdown(st)
    path = write_stats_file(md)
    print(md)
    print(f"\n[stats] 已生成 {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
