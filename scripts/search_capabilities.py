# -*- coding: utf-8 -*-
"""
BeaconMFG 能力检索（客户 Agent 侧 · L0 指纹层）
===============================================

演示「客户 Agent 如何用能力指纹做确定性筛选」——这是整个链路的最后一环：
供应商口述 → 归一化 → 能力卡 → 指纹行 → **客户按需求命中**。

设计要点：
- 只读 .jsonl 指纹行，不加载 SKILL.md（避免上下文爆炸）
- 全部用数值/枚举做确定性比对，不靠 LLM 猜
- 逐条给出命中/淘汰原因，便于人工复核

用法：
    python scripts/search_capabilities.py --city 深圳 --proc cnc_milling \
        --mat 铝合金6061 --tol 0.05 --moq 10 --size 300,200,100
    python scripts/search_capabilities.py --industry 3525 --city 宁波   # 只看模具制造
    python scripts/search_capabilities.py --industry 29 --proc injection_molding
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import gb_store  # noqa: E402

FP_DIR = REPO_ROOT / "skills" / "registry" / "fingerprint" / "gb"
INDUSTRY_INDEX = REPO_ROOT / "data" / "industry-index.json"

try:
    from industry_taxonomy import CODES, GATES, gate_of
except ImportError:
    CODES, GATES, gate_of = {}, {}, lambda c: "C"


def resolve_industry_ids(selector):
    """按国标代码/前缀/门类/中文名筛出属于该行业的供应商 ID 集合。

    行业口径来自 data/industry-index.json（每次抓完要重建，否则新数据查不到）。
    指纹行本身不带行业字段，靠 id 关联，避免重复维护一份分类。
    """
    sel = (selector or "").strip()
    if not sel:
        return None, []
    want = set()
    if sel.upper() in GATES:
        want = {c for c in CODES if gate_of(c) == sel.upper()}
    elif sel in CODES:
        want = {sel}
    elif any(c.startswith(sel) for c in CODES):
        want = {c for c in CODES if c.startswith(sel)}
    else:
        want = {c for c in CODES if sel in CODES[c]["name"] or sel in CODES[c]["en"]}
    if not want:
        return set(), []

    if not INDUSTRY_INDEX.exists():
        print("[WARN] 缺少 data/industry-index.json，先跑 scripts/gen_industry_index.py")
        return set(), []
    data = json.loads(INDUSTRY_INDEX.read_text(encoding="utf-8"))
    ids, labels = set(), []
    for code in want:
        item = data["index"].get(code)
        if item:
            ids.update(item["ids"])
            labels.append("%s %s（%d 家）" % (code, item["name"], item["count"]))
    return ids, labels


def load_fingerprints(code: str | None = None) -> tuple[list[dict], list[Path]]:
    """读 L0 指纹，返回 (行列表, 实际读取的分片文件)。

    **给国标码时只加载命中的分片** —— 这是省流量的关键：
    查 3525 模具制造就读 3525.jsonl（约 250 KB），
    而不是把 2 万家全量指纹（4.5 MB）都读进来。
    千万级之后这个差别就是 1 MB 与 2 GB 的差别。
    """
    out: list[dict] = []
    if not FP_DIR.exists():
        return out, []
    if code:
        bucket = gb_store.bucket_of_code(code)
        files = [FP_DIR / ("%s.jsonl" % bucket)]
    else:
        files = sorted(FP_DIR.rglob("*.jsonl"))
    used: list[Path] = []
    for f in files:
        if not f.exists():
            continue
        used.append(f)
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out, used


def match(fp: dict, req: dict) -> tuple[bool, list[str], int]:
    """返回 (是否命中, 原因列表, 得分 0-100)。"""
    reasons: list[str] = []
    score = 0

    # proc 支持逗号分隔的多工艺 OR 查询。
    # 为什么需要：采购需求常是「塑料件」，但甲方说不清该走机加工还是注塑 ——
    # 实际上塑料零件大多走注塑，机加工只用于治具/绝缘件/小批量板材件。
    # 只按 cnc_milling 查会把真正的塑料厂全漏掉，必须允许任一命中。
    if req.get("proc"):
        want = req["proc"]
        have = fp.get("proc") or []
        hit = [p for p in want if p in have]
        if hit:
            score += 30
            reasons.append(f"工艺命中 {'/'.join(hit)}")
        else:
            return False, [f"工艺不符（需要 {'/'.join(want)}，实际 {have}）"], 0

    if req.get("city"):
        if fp.get("city") == req["city"]:
            score += 15
            reasons.append(f"同城 {req['city']}")
        else:
            return False, [f"城市不符（{fp.get('city')}）"], 0

    if req.get("mat"):
        mats = fp.get("mat") or []
        hit = [m for m in mats if req["mat"] in m or m in req["mat"]]
        if hit:
            score += 20
            reasons.append(f"材料命中 {hit[0]}")
        elif not mats:
            # 关键：没填材料 ≠ 不做这个材料。自动整理卡的材料填写率只有 ~13%，
            # 若在此硬淘汰，任何带材料约束的查询都会 0 命中。
            # 正确语义：无证据时不淘汰，降权并标记为待确认。
            score -= 25
            reasons.append(f"材料未填（无法确认是否做 {req['mat']}，需询价时确认）")
        else:
            return False, [f"材料不符（需 {req['mat']}，实际 {mats}）"], 0

    if req.get("tol") is not None:
        t = fp.get("tol")
        if t is None:
            reasons.append("未填公差（无法确定性筛选）")
        elif t <= req["tol"]:
            score += 15
            reasons.append(f"公差达标 ±{t} ≤ ±{req['tol']}")
        else:
            return False, [f"公差不足（±{t} > ±{req['tol']}）"], 0

    if req.get("moq") is not None:
        m = fp.get("moq")
        if m is None:
            reasons.append("未填 MOQ")
        elif m <= req["moq"]:
            score += 10
            reasons.append(f"MOQ 可接受 {m} ≤ {req['moq']}")
        else:
            return False, [f"MOQ 过高（{m} > {req['moq']}）"], 0

    if req.get("size"):
        s = fp.get("size")
        need = req["size"]
        if s and len(s) >= len(need):
            if all(float(s[i]) >= float(need[i]) for i in range(len(need))):
                score += 10
                reasons.append(f"尺寸可容纳 {'×'.join(str(x) for x in s)}")
            else:
                return False, [f"尺寸不够（机床 {s} < 需求 {need}）"], 0
        else:
            reasons.append("未填尺寸（无法确定性筛选）")

    return True, reasons, score


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city")
    ap.add_argument("--proc", help="工艺码，逗号分隔表示「任一命中即可」，"
                                   "如 cnc_milling,injection_molding")
    ap.add_argument("--mat", help="材料关键词")
    ap.add_argument("--tol", type=float, help="公差上限 mm")
    ap.add_argument("--moq", type=int, help="可接受的最大起订量")
    ap.add_argument("--size", help="工件尺寸 长,宽,高 mm")
    ap.add_argument("--industry", help="国标行业：小类码 3525 / 中类 339 / 大类 29 / 中文名 模具。"
                                       "给小类码时只读该分片，不扫全量")
    ap.add_argument("--show-rejected", action="store_true")
    args = ap.parse_args()

    # 4 位小类码 → 直接定位到分片文件，不加载全量
    shard_code = args.industry if (args.industry and args.industry.isdigit()
                                   and len(args.industry) == 4) else None
    industry_ids, industry_labels = (None, []) if shard_code else resolve_industry_ids(args.industry)
    if args.industry and not shard_code and not industry_ids:
        print("该行业在索引里没有记录：%s" % args.industry)
        return 0
    if industry_labels:
        print("行业范围：%s" % "；".join(industry_labels[:8]))

    req = {
        "city": args.city,
        "proc": [p.strip() for p in args.proc.split(",") if p.strip()] if args.proc else None,
        "mat": args.mat,
        "tol": args.tol, "moq": args.moq,
        "size": [float(x) for x in args.size.split(",")] if args.size else None,
    }
    if req["proc"]:
        print(f"工艺条件（任一命中）：{' / '.join(req['proc'])}")
    fps, shard_files = load_fingerprints(shard_code)
    if shard_files:
        mb = sum(f.stat().st_size for f in shard_files) / 1048576
        print("读取分片：%s（%.2f MB）" % (
            ", ".join(str(f.relative_to(REPO_ROOT)) for f in shard_files[:3]), mb))
    if not fps:
        print("[ERROR] registry/fingerprint 为空，请先跑采集：")
        print("        python scripts/collect/cli.py --demo CN-MFG-0000005")
        return 1

    hits, misses = [], []
    skipped_industry = 0
    for fp in fps:
        if industry_ids is not None and fp.get("id") not in industry_ids:
            skipped_industry += 1
            continue
        ok, why, sc = match(fp, req)
        (hits if ok else misses).append((sc, fp, why))

    hits.sort(key=lambda x: (-x[0], -(x[1].get("sc") or 0)))

    print(f"\n扫描 {len(fps)} 条能力指纹"
          f"{f'，其中 {len(fps) - skipped_industry} 条属于目标行业' if industry_ids is not None else ''}"
          f"，命中 {len(hits)} 家\n")
    print("=" * 64)
    for sc, fp, why in hits:
        # 硬指标为空时显示「未填」，别把 None 拼进字符串
        def f(v, unit=""):
            return f"{v}{unit}" if v is not None else "未填"
        auto_tag = "  ⚠ 自动整理" if fp.get("pv") == "auto" else ""
        # 有硬指标未填 → 结果不可直接采信，必须标记为待确认
        if any("未填" in w or "无法" in w for w in why):
            auto_tag += "  ⚑待确认"
        print(f"\n[{fp['id']}] {fp['co']}  ·  {fp.get('city')}  ·  匹配分 {sc}  ·  凭证 {fp.get('cl')}{auto_tag}")
        print(f"   工艺 {fp.get('proc')}   材料 {fp.get('mat') or '未填'}")
        print(f"   公差 ±{f(fp.get('tol'),'mm')}   尺寸 {f(fp.get('size'))}   MOQ {f(fp.get('moq'),' 件')}")
        print(f"   交期 样品 {f(fp.get('lt', [None, None])[0],' 天')} / "
              f"百件 {f(fp.get('lt', [None, None])[1],' 天')}   "
              f"回价 {f(fp.get('rt'),'h')}   认证 {fp.get('cert') or '未填'}")
        print(f"   命中原因：{'；'.join(why)}")

    if args.show_rejected and misses:
        # 只展示「最接近」的淘汰项：同城或同工艺至少占一个，否则是纯噪音
        # （查上海却列出嘉兴的激光切割厂毫无意义）
        def near(m: tuple) -> int:
            fp = m[1]
            return (1 if req.get("city") and fp.get("city") == req["city"] else 0) + \
                   (1 if req.get("proc") and (set(req["proc"]) & set(fp.get("proc") or [])) else 0)
        near_misses = [m for m in misses if near(m) > 0]
        near_misses.sort(key=lambda m: (-near(m), m[1].get("id")))
        print("\n" + "=" * 64)
        print(f"淘汰清单（最接近的 {min(len(near_misses), 15)} 条"
              f"{'' if near_misses else '，无同城或同工艺项'}）：")
        for _, fp, why in near_misses[:15]:
            print(f"  [{fp['id']}] {fp['co']} — {why[0]}")

    if not hits:
        print("\n没有匹配的供应商。可放宽：公差 / MOQ / 地区，或换个工艺码。")
        return 0

    # 结果可信度总览：客户 Agent 必须知道这批结果能不能直接采信，
    # 否则会把「未经确认的自动整理卡」当成「确认能做」去下单。
    unconfirmed = [h for h in hits if any("未填" in w or "无法" in w for w in h[2])]
    claimed = [h for h in hits if h[1].get("pv") != "auto"]
    print("\n" + "=" * 64)
    print(f"结果可信度：{len(claimed)} 家已认主（硬指标经企业确认） / "
          f"{len(hits) - len(claimed)} 家平台自动整理")
    if unconfirmed:
        print(f"⚠ 其中 {len(unconfirmed)} 家含未填硬指标，已标 ⚑待确认：")
        print("  这些卡片仅证明「该企业在名录中存在且名义上做此工艺」，")
        print("  不能证明它做你要的材料 / 公差 / 批量。询价前须确认。")
    if not claimed and hits:
        print("\n▶ 建议：本轮只能做「存在性发现」。若要真正的匹配，")
        print("  需等企业认主补全公差 / MOQ / 交期，或放宽到已认主企业较多的城市。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
