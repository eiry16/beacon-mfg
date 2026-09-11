#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 供应商检索脚本（纯标准库，无第三方依赖）

数据源：**国标四级归档** `data/gb/{门类}/{大类}/{小类}.json`
（2026-09-08 起取代原 8 品类 `data/suppliers/`，详见 docs/ 与 data/gb-index.json）。
每条记录带 industry = {code, name, path, confidence, source}：
    - 按国标检索（--industry）最准，客户 Agent 应优先用
    - 按采购词检索（--keyword）会自动经 data/gb-alias.json 映射到国标小类，
      解决"客户说注塑、数据写的是塑料零件制造"这类词不达意的问题

用法示例:
    python scripts/query.py --keyword "CNC加工"
    python scripts/query.py --keyword "小批量" --city 深圳 --limit 5
    python scripts/query.py --province 广东 --cert 高新技术企业
    # 按国标行业检索（推荐）：小类码 / 中类前缀 / 大类 / 门类 / 中文名
    python scripts/query.py --industry 3525 --city 宁波        # 模具制造
    python scripts/query.py --industry 339 --city 苏州         # 中类「铸造及其他金属制品制造」
    python scripts/query.py --industry 29 --city 东莞          # 大类「橡胶和塑料制品业」
    python scripts/query.py --industry 模具 --limit 10
    python scripts/query.py --industry 3360 --manufacturer-only   # 排除批发/贸易商
    python scripts/query.py --list-gb                          # 看国标层级树（哪里有货）
    python scripts/query.py --list-gb --gb-level division       # 只看大类
    python scripts/query.py --list-alias                       # 看采购词→国标码映射
    python scripts/query.py --keyword 注塑 --explain            # 看关键词怎么解析的
    # 英文数据集（海外 Agent）:
    python scripts/query.py --keyword "CNC Machining" --city Shenzhen --en --limit 5
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

EN_GB_DIR = ROOT / "data" / "en" / "gb"
EN_DIR = ROOT / "data" / "en"          # 旧 8 品类目录（英文镜像重构后仅作回退）
INDUSTRY_INDEX = ROOT / "data" / "industry-index.json"

try:
    from industry_taxonomy import CODES, GATES, gate_of
except ImportError:  # 单独拷走 query.py 时也能跑，只是行业功能降级
    CODES, GATES, gate_of = {}, {}, lambda c: "C"
    # 静默降级的代价太大：CODES 为空时 --industry 会返回"没匹配到任何国标行业"，
    # 客户会以为是数据里没这家厂，实际是码表没加载。必须喊出来。
    print("[警告] 找不到 industry_taxonomy.py，国标行业过滤已失效——"
          "此时 --industry 查任何码都会返回 0 条。\n"
          "       请把 scripts/industry_taxonomy.py 与 query.py 放在一起。",
          file=sys.stderr)

try:
    import gb_store as GB
except ImportError:  # 没有访问层时退回旧目录，保证脚本不炸
    GB = None


def _read_dir(d: Path, english: bool, tag_bucket: bool):
    """读一个归档目录下的所有 JSON，产出记录列表。"""
    out = []
    for path in sorted(d.rglob("*.json")):
        rel = path.relative_to(d)
        bucket = rel.as_posix()[:-5]      # 去掉 .json
        with open(path, encoding="utf-8") as f:
            for item in json.load(f):
                if tag_bucket:
                    item["_bucket"] = bucket
                if english:
                    # 旧 --category-en 参数仍要能用（虽然 8 品类已废弃，消费方可能还在传）
                    item["_category"] = (item.get("category_en")
                                         or item.get("category"))
                item["_english"] = english
                out.append(item)
    return out


def load_all_suppliers(english=False):
    """遍历国标归档，合并为记录列表。

    中文读 data/gb/，英文读 data/en/gb/。两者结构一一对应，id 可互 join。
    英文归档不存在时回退到旧 data/en/*.json（重构前的兼容路径）。
    """
    if english:
        if EN_GB_DIR.exists() and any(EN_GB_DIR.rglob("*.json")):
            return _read_dir(EN_GB_DIR, True, True)
        suppliers = []
        for path in sorted(EN_DIR.glob("*.json")):
            with open(path, encoding="utf-8") as f:
                for item in json.load(f):
                    item["_category"] = item.get("category_en") or item.get("category")
                    item["_english"] = True
                    suppliers.append(item)
        return suppliers

    if GB is None:
        raise SystemExit("缺少 scripts/gb_store.py，无法读取 data/gb/ 归档")
    return GB.load_all(with_bucket=True)


def alias_codes_for(keyword: str, exact_top: int = 3, fuzzy_top: int = 1):
    """采购词 → 国标小类码集合（经 data/gb-alias.json）。

    匹配方式（别名词表已按命中次数降序）：
      - **精确命中**（输入 == 别名）：取前 exact_top 个小类。
        比如「注塑加工」确实同时出现在 2929 和少数其他小类里，都算有效。
      - **子串命中**（输入 ⊂ 别名 或 别名 ⊂ 输入）：只取命中最多的那 1 个。
        这里必须收紧 —— 实测「注塑」若把别名表的全部候选都放进来，
        会连带命中「弹簧制造」「金属结构制造」等无关小类（因为它们也标过注塑），
        搜一次注塑出来一堆弹簧厂，比搜不到更糟。

    有精确命中时不再用子串命中，避免宽窄两套结果混在一起。
    """
    if GB is None:
        return set()
    alias = GB.load_alias()
    if not alias:
        return set()
    kw = (keyword or "").strip()
    if not kw:
        return set()
    low = kw.lower()
    exact: list[str] = []
    fuzzy: list[str] = []
    for word, entries in alias.items():
        wl = word.lower()
        if wl == low:
            exact.extend(e["code"] for e in entries[:exact_top])
        elif wl in low or low in wl:
            fuzzy.extend(e["code"] for e in entries[:fuzzy_top])
    return set(exact) if exact else set(fuzzy)


def alias_rank_for(keyword: str, exact_top: int = 3, fuzzy_top: int = 1) -> dict[str, int]:
    """采购词 → {国标码: 证据等级}，等级越小越强。

      0 = 企业自身关键词里就有这个词（最强，由调用方判定，不在这里）
      1 = 别名表的**首位**小类 —— 语义最贴近采购词
      2 = 别名表的**其余**小类 —— 按行业推断，企业没说过自己能做

    为什么必须分级：别名扩展是"整类扩展"而不是"同义词扩展"。搜「齿轮」
    命中 3453 齿轮制造（全国 6 家）的同时，也会把 3484 机械零部件加工
    的 3415 家一起带进来——实测放大 3421 倍。不分级的话客户 Agent 分不清
    「这家真做齿轮」和「这家只是被归在这一类」，只能全盘接受或全部放弃。

    分级后按等级排序，前面是强命中，Agent 想截断随时能截。
    """
    if GB is None:
        return {}
    alias = GB.load_alias()
    if not alias:
        return {}
    kw = (keyword or "").strip()
    if not kw:
        return {}
    low = kw.lower()
    exact: dict[str, int] = {}
    fuzzy: dict[str, int] = {}
    for word, entries in alias.items():
        wl = word.lower()
        if wl == low:
            target, top = exact, exact_top
        elif wl in low or low in wl:
            target, top = fuzzy, fuzzy_top
        else:
            continue
        for i, e in enumerate(entries[:top]):
            rank = 1 if i == 0 else 2
            code = e["code"]
            if code not in target or rank < target[code]:
                target[code] = rank
    return exact if exact else fuzzy


def cert_names(record):
    """认证名称列表。兼容两种写法：
    - POI 抓取的记录：["高新技术企业"]（字符串）
    - 认证回流写入的：[{"name": "ISO9001", "evidence": "self_declared"}]（对象）
    两种混在一个库里，不统一处理会在打印/过滤时炸。
    """
    out = []
    for c in (record.get("certifications") or []):
        if isinstance(c, dict):
            n = c.get("name")
        else:
            n = c
        if n:
            out.append(str(n))
    return out


def match(record, keyword):
    """关键词匹配：对 keywords 列表做子串匹配（忽略大小写）。英文数据同时匹配 keywords_en"""
    kw = keyword.strip().lower()
    if not kw:
        return False
    pools = list(record.get("keywords", [])) + list(record.get("keywords_en", []))
    return any(kw in k.lower() for k in pools)


def resolve_industry(selector):
    """把 --industry 参数解析成国标小类代码集合。

    支持四种写法：
        3525      小类码          → 模具制造
        339 / 29  中类/大类前缀   → 该层下所有小类
        C / F     门类            → 制造业 / 批发和零售业
        模具 / 铸造  中文名子串    → 名称含它的所有小类
    返回 (代码集合, 匹配到的可读说明列表)。
    """
    sel = (selector or "").strip()
    if not sel:
        return None, []
    if sel.upper() in GATES:
        codes = {c for c in CODES if gate_of(c) == sel.upper()}
        return codes, ["%s %s（%d 个小类）" % (sel.upper(), GATES[sel.upper()], len(codes))]
    if sel in CODES:
        return {sel}, ["%s %s" % (sel, CODES[sel]["name"])]
    codes = {c for c in CODES if c.startswith(sel)}
    if codes:
        lvl = {2: "大类", 3: "中类"}.get(len(sel), "前缀")
        return codes, ["%s %s（%s，%d 个小类）" % (sel, lvl, "、".join(sorted(codes)), len(codes))]
    codes = {c for c in CODES if sel in (CODES[c].get("name") or "") or sel in (CODES[c].get("en") or "")}
    return (codes or set()), ["%s %s" % (c, CODES[c]["name"]) for c in sorted(codes)]


def _norm(s):
    """去掉省市后缀做兼容匹配"""
    return (s or "").replace("省", "").replace("市", "").replace("自治区", "")


def industry_of(record):
    """兼容中英文两种字段：industry（中文库）/ industry_en（英文镜像）。"""
    return record.get("industry") or record.get("industry_en") or {}


def search(suppliers, args, industry_codes=None, alias_cache=None):
    results = []
    alias_cache = alias_cache if alias_cache is not None else {}
    for s in suppliers:
        # 只有真占位（status="template"）默认排除。
        # status="unverified_poi" 是真实企业 POI（电话待核实），SPEC §2.4 要求保留展示——
        # 这里曾经按 is_template 一刀切过滤，导致 40% 名录默认搜不到。
        if s.get("status") == "template" and not args.include_template:
            continue
        # --category 是 8 品类时代的遗留参数。归档已改走国标，但为了不打断
        # 现有调用方，仍按记录里的 category 字段匹配（它是"采购标签"而非归档依据）。
        if args.category and (s.get("category") or s.get("_category")) != args.category:
            continue
        if args.category_en and s.get("_category") != args.category_en:
            continue
        if industry_codes is not None:
            ind = industry_of(s)
            code = ind.get("code")
            # 没归类的（industry=null）在按行业检索时一律不返回：
            # 按行业找供应商时，拿「不知道是什么行业」的记录充数是误导。
            if code not in industry_codes:
                continue
        if args.manufacturer_only and s.get("is_manufacturer") is False:
            continue
        if args.city and _norm(s.get("region", {}).get("city")) != _norm(args.city):
            continue
        if args.province and _norm(s.get("region", {}).get("province")) != _norm(args.province):
            continue
        if args.cert and args.cert not in cert_names(s):
            continue
        if args.keyword:
            # 支持空格分隔的多关键词 AND；每个词命中"关键词"或"别名映射到的国标码"之一即可
            kws = [k for k in args.keyword.split() if k]
            ind_code = (industry_of(s) or {}).get("code")
            ok = True
            worst = 0
            for k in kws:
                hit = match(s, k)
                rank = 0  # 0 = 企业自己就写了这个词
                if not hit and not args.no_alias:
                    ranks = alias_cache.get(k)
                    if ranks is None:
                        ranks = alias_rank_for(k)
                        alias_cache[k] = ranks
                    rank = ranks.get(ind_code, 0) if ind_code else 0
                    hit = rank > 0
                if not hit:
                    ok = False
                    break
                # 多词 AND 时取最弱的一环——整条结果的可信度由短板决定
                worst = max(worst, rank)
            if not ok:
                continue
            s["_alias_rank"] = worst
        results.append(s)
    # 强命中排前面：字面 > 别名首位码 > 别名其余码。
    # 稳定排序，同档内保持原顺序（分片内按 ID）。
    results.sort(key=lambda r: r.get("_alias_rank", 0))
    return results


def fmt(record):
    region = record.get("region", {})
    if record.get("_english"):
        lines = [
            f"Company: {record.get('company_en', record.get('company', ''))} "
            f"({region.get('province', '')} · {region.get('city', '')})",
            f"Products: {' / '.join(record.get('keywords_en') or record.get('keywords', []))}",
            f"Category: {record.get('category_en', record.get('category', ''))}",
        ]
        if record.get("contact_phone"):
            lines.append(f"Phone: {record['contact_phone']}")
        if record.get("address_en"):
            lines.append(f"Address: {record['address_en']}")
        if record.get("note_en"):
            lines.append(f"Note: {record['note_en']}")
        lines.append(f"Source: public directory, verified {record.get('verified_at', '?')}")
        return "\n".join(lines)

    certs = "、".join(cert_names(record)) or "无"
    lines = [
        f"公司：{record['company']}（{region.get('province', '')}·{region.get('city', '')}）",
        f"主营：{' / '.join(record.get('keywords', []))}",
    ]
    ind = industry_of(record)
    if ind.get("code"):
        conf = {"high": "高", "medium": "中", "low": "低"}.get(ind.get("confidence"), "?")
        lines.append(f"行业：{ind['code']} {ind.get('name', '')}（置信度{conf}）")
        if isinstance(ind.get("path"), str) and ind["path"]:
            lines.append(f"       {ind['path']}")
    lines.append(f"认证：{certs}")
    if record.get("website"):
        lines.append(f"官网：{record['website']}")
    if record.get("contact_phone"):
        lines.append(f"电话：{record['contact_phone']}")
    if record.get("contact_email"):
        lines.append(f"邮箱：{record['contact_email']}")
    if record.get("note"):
        lines.append(f"备注：{record['note']}")
    st = record.get("status")
    if st == "template":
        lines.append("⚠ 示例占位数据，真实数据填充中")
    elif st == "unverified_poi":
        lines.append(
            f"数据来源：公开渠道，导入于 {record.get('verified_at', '?')}"
            f"（真实企业，电话待核实）"
        )
    else:
        lines.append(f"数据来源：公开渠道，核实于 {record.get('verified_at', '?')}")
    if record.get("is_manufacturer") is False:
        lines.append("⚠ 批发/贸易类（F51 批发业），非生产企业，采购时需自行确认是否代工")
    return "\n".join(lines)


def list_gb(level="division", top=30):
    """打印国标层级树里「有货」的节点，帮客户 Agent 先看清能按什么层级查。"""
    if GB is None or not GB.GB_INDEX.exists():
        print("还没有归档索引，先跑：python scripts/migrate_to_gb.py --apply")
        return
    with open(GB.GB_INDEX, encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get("metadata", {})
    c = meta.get("counts", {})
    print("%s" % meta.get("standard", ""))
    print("归档 %d 条（已归类 %d / 无码 %d）；占用 门类 %d、大类 %d、中类 %d、小类 %d\n"
          % (meta.get("total_suppliers", 0), meta.get("classified", 0),
             meta.get("unclassified", 0), c.get("gates_used", 0),
             c.get("divisions_used", 0), c.get("groups_used", 0),
             c.get("classes_used", 0)))

    rows = []
    for gate, g in data["tree"].items():
        if level == "gate":
            rows.append((gate, g["name"], g["count"], ""))
            continue
        for div, d in g["divisions"].items():
            if level == "division":
                rows.append(("%s/%s" % (gate, div), d["name"], d["count"], g["name"]))
                continue
            for grp, m in d["groups"].items():
                if level == "group":
                    rows.append(("%s/%s/%s" % (gate, div, grp), m["name"],
                                 m["count"], d["name"]))
                    continue
                for cls, v in m["classes"].items():
                    rows.append(("%s/%s/%s" % (gate, div, cls), v["name"],
                                 v["count"], m["name"]))
    rows.sort(key=lambda x: -x[2])
    label = {"gate": "门类", "division": "大类", "group": "中类", "class": "小类"}[level]
    print("  %-14s %-30s %7s  %s" % ("路径", "%s名称" % label, "家数", "上级"))
    for path, name, cnt, parent in rows[:top]:
        print("  %-14s %-30s %7d  %s" % (path, name[:30], cnt, parent[:20]))
    print("\n用法：python scripts/query.py --industry <路径或代码> [--city 城市]")
    print("      python scripts/query.py --list-gb --gb-level class   # 看小类")


def list_alias(top=50):
    """打印采购词 → 国标码映射表。"""
    if GB is None:
        return
    alias = GB.load_alias()
    if not alias:
        print("还没有别名表，先跑：python scripts/gb_store.py --realias")
        return
    n_cur = sum(1 for v in alias.values() if v[0].get("source") == "curated")
    print("采购词 → 国标小类（共 %d 条：数据推导 %d，人工策展 %d）"
          % (len(alias), len(alias) - n_cur, n_cur))
    print("data=由真实数据推导（括号为命中次数）；curated=人工策展（无命中数，不可编）\n")
    # 策展条目 hits 为 None，按 0 排（有数据的排前面）
    items = sorted(alias.items(),
                   key=lambda x: -(x[1][0].get("hits") or 0))[:top]
    for word, entries in items:
        parts = []
        for e in entries[:3]:
            hit = e.get("hits")
            tail = "(%d)" % hit if hit is not None else "(curated)"
            parts.append("%s %s%s" % (e["code"], e["name"][:12], tail))
        print("  %-16s → %s" % (word, "、".join(parts)))
    print("\n共 %d 条。用法：python scripts/query.py --keyword %s"
          % (len(alias), items[0][0] if items else "模具"))


def list_industries(top=40):
    """打印 data/industry-index.json 里「有货」的行业，帮 Agent 先看清有哪些可查。"""
    if not INDUSTRY_INDEX.exists():
        print("还没有行业索引，先跑：python scripts/gen_industry_index.py")
        return
    with open(INDUSTRY_INDEX, encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get("metadata", {})
    print("行业索引：%s" % meta.get("standard", ""))
    print("覆盖 %d/%d 家（未归类 %d），共 %d 个国标小类，生成于 %s\n"
          % (meta.get("classified", 0), meta.get("total_suppliers", 0),
             meta.get("unclassified", 0), meta.get("total_codes", 0),
             meta.get("generated_at", "?")))
    print("  %-6s %-26s %6s  %-16s %s" % ("代码", "行业小类", "家数", "TOP 城市", "置信度高/中/低"))
    for code, v in list(data["index"].items())[:top]:
        c = v["confidence"]
        cities = "、".join("%s%d" % (x["city"], x["count"]) for x in v.get("top_cities", [])[:2])
        print("  %-6s %-26s %6d  %-16s %d/%d/%d"
              % (code, v["name"][:26], v["count"], cities[:16],
                 c["high"], c["medium"], c["low"]))
    print("\n用法：python scripts/query.py --industry <代码> [--city 城市]")


def main():
    parser = argparse.ArgumentParser(description="BeaconMFG 供应商检索")
    parser.add_argument("--keyword", help="产品关键词（空格分隔多词 AND）")
    parser.add_argument("--category", help="品类名（见 data/index.json）")
    parser.add_argument("--category-en", dest="category_en", help="英文品类名（--en 模式）")
    parser.add_argument("--city", help="城市过滤，如 深圳 / Shenzhen")
    parser.add_argument("--province", help="省份过滤，如 广东 / Guangdong")
    parser.add_argument("--cert", help="认证标签过滤，如 高新技术企业")
    parser.add_argument("--industry", help="国标行业：小类码 3525 / 中类 339 / 大类 29 / 门类 C / 中文名 模具")
    parser.add_argument("--manufacturer-only", action="store_true",
                        help="排除批发贸易类（is_manufacturer=false，F51 批发业）")
    parser.add_argument("--list-industries", action="store_true", help="列出行业索引里有货的国标小类")
    parser.add_argument("--list-gb", action="store_true", help="列出国标归档层级树（按有货量排序）")
    parser.add_argument("--gb-level", default="division",
                        choices=["gate", "division", "group", "class"],
                        help="--list-gb 的展示层级（默认 division 大类）")
    parser.add_argument("--list-alias", action="store_true", help="列出采购词→国标码映射")
    parser.add_argument("--no-alias", action="store_true",
                        help="关闭关键词→国标的别名扩展（只做字面匹配，便于对比）")
    parser.add_argument("--alias-broad", action="store_true",
                        help="别名扩展不做 max_supply 收敛：召回更全但噪音更大。"
                             "默认会被收敛——目标小类家数超过阈值的补位码会被剔除，"
                             "实测削减 70%% 结果量（见 scripts/audit_alias.py 第 4 节）")
    parser.add_argument("--explain", action="store_true", help="打印检索条件是怎么解析的")
    parser.add_argument("--limit", type=int, default=5, help="返回条数上限")
    parser.add_argument("--include-template", action="store_true", help="包含模板示例数据")
    parser.add_argument("--en", action="store_true", help="检索英文数据集（data/en/gb）")
    args = parser.parse_args()

    if args.list_industries:
        list_industries()
        return
    if args.list_gb:
        list_gb(args.gb_level)
        return
    if args.list_alias:
        list_alias()
        return

    if not any([args.keyword, args.category, args.category_en, args.city,
                args.province, args.cert, args.industry]):
        parser.print_help()
        sys.exit(1)

    industry_codes, labels = resolve_industry(args.industry)
    if args.industry:
        if not industry_codes:
            print("没匹配到任何国标行业：%s" % args.industry)
            print("提示：用 --list-gb 看有货的国标层级，或换中文名（如 模具 / 铸造 / 橡胶）")
            sys.exit(0)
        print("行业范围：%s" % "；".join(labels[:6]))

    alias_cache: dict[str, dict] = {}
    if args.keyword and not args.no_alias:
        if args.alias_broad and GB is not None:
            # 放宽收敛。必须清缓存，否则 load_alias 用的是上一次的合并结果
            GB.ALIAS_MAX_SUPPLY = None
            GB._SUPPLY_CACHE = None
        for k in [x for x in args.keyword.split() if x]:
            ranks = alias_rank_for(k)
            alias_cache[k] = ranks
            if args.explain and ranks:
                strong = [c for c, r in ranks.items() if r == 1]
                weak = [c for c, r in ranks.items() if r == 2]
                print("采购词「%s」→ 强相关 %s%s"
                      % (k, "、".join(sorted(strong)) or "（无）",
                         ("；行业推断 " + "、".join(sorted(weak))) if weak else ""))

    suppliers = load_all_suppliers(english=args.en)
    if not suppliers and args.en:
        print("英文数据集为空：请先运行 scripts/translate_en.py 生成 data/en/")
        sys.exit(0)
    results = search(suppliers, args, industry_codes, alias_cache)

    if not results:
        if args.industry:
            print("未找到匹配供应商。该行业在目标条件下暂无记录——"
                  "可用 --list-industries 看哪些行业有货，或去掉 --city 再试。")
            sys.exit(0)
        print("未找到匹配供应商。")
        # 收敛把结果砍成 0 时，告诉用户放宽能拿到多少——但**不静默放宽**。
        # 直接返回 0 会让客户误以为「上海没有齿轮厂」，
        # 而真相是「没有一家被归类为齿轮制造，只有 216 家行业推断」。
        if (args.keyword and not args.no_alias and not args.alias_broad
                and GB is not None):
            saved = GB.ALIAS_MAX_SUPPLY
            GB.ALIAS_MAX_SUPPLY = None
            GB._SUPPLY_CACHE = None
            broad = {k: alias_rank_for(k) for k in args.keyword.split() if k}
            n = len(search(suppliers, args, industry_codes, broad))
            GB.ALIAS_MAX_SUPPLY = saved
            GB._SUPPLY_CACHE = None
            if n:
                print("放宽别名收敛（--alias-broad）可得到 %d 家，"
                      "但全部是按国标行业推断的，企业未确认。" % n)
                sys.exit(0)
        print("提示：换关键词（如 CNC加工→数控加工），或放宽地区/认证条件。")
        sys.exit(0)

    # 分层说明：别名扩展是"整类扩展"，弱档结果企业没说过自己能做，必须说清楚
    if args.keyword and not args.no_alias:
        n_strong = sum(1 for r in results if r.get("_alias_rank", 0) == 0)
        n_top = sum(1 for r in results if r.get("_alias_rank", 0) == 1)
        n_weak = sum(1 for r in results if r.get("_alias_rank", 0) == 2)
        if n_top or n_weak:
            print("命中构成：字面关键词 %d 家 · 别名首位码 %d 家 · 行业推断 %d 家"
                  % (n_strong, n_top, n_weak))
            print("（后两档是按国标行业推断的，企业未确认；已按强度排序，前 %d 家为最强档）"
                  % max(n_strong, 1))
            print()

    print(f"共匹配 {len(results)} 家，展示前 {min(args.limit, len(results))} 家：\n")
    for r in results[: args.limit]:
        if r.get("_alias_rank", 0) == 2:
            print("[行业推断·未确认]")
        print(fmt(r))
        print("-" * 46)


if __name__ == "__main__":
    main()
