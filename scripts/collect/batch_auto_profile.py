"""
batch_auto_profile.py — 批量自动整理（冷启动弹药生成器）

从薄名录中挑选信息量最足的 N 家，生成「平台自动整理 · 未认证」能力卡。

用法：
    python scripts/collect/batch_auto_profile.py --city 嘉兴 --limit 300
    python scripts/collect/batch_auto_profile.py --city 嘉兴 --limit 300 --dry-run
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collect.auto_profile import screen, build_capability, quality_score, clean_name
from collect.render import render_skill_md, write_vendor_skill, append_fingerprint

REPO_ROOT = Path(__file__).resolve().parents[2]

# 低于此分的候选不生成：名称推断不出可靠能力，生成即是垃圾
MIN_SCORE = 3
# 每个品类保底名额，确保「所有类别都做一些」
MIN_PER_CATEGORY = 10


def completeness(cap: dict) -> int:
    """
    资料完整度（0-100）。自动整理的卡片天然很低——这正是激励认主的钩子。
    """
    srv = cap.get("service") or {}
    idt = cap.get("identity") or {}
    ct = cap.get("contact") or {}
    lim = cap.get("limits") or {}
    checks = [
        bool(cap.get("processes")),
        bool(cap.get("materials")),
        any(v is not None for v in lim.values()),
        bool((cap.get("quality") or {}).get("certifications")),
        bool(srv.get("quote_response_hours")),
        bool(srv.get("payment_terms")),
        bool(idt.get("employee_count")),
        bool(idt.get("founded")),
        bool(ct.get("email")),
        bool(cap.get("highlights")),
    ]
    return round(sum(checks) / len(checks) * 100)


def select(cands: list[dict], limit: int) -> list[dict]:
    """
    挑选：按各品类的合格候选数**等比例分配**，再按分数取。

    为什么不是全局按分数取 top N：标准件在名录里占绝对多数，
    全局排序会让 300 家里 126 家都是标准件、原材料只剩 8 家。
    用户要的是「所有类别都做一些」，所以按品类配额。
    """
    by_cat: dict[str, list[dict]] = collections.defaultdict(list)
    for c in cands:
        by_cat[c["record"]["category"]].append(c)
    for items in by_cat.values():
        items.sort(key=lambda x: (-x["score"], x["record"]["id"]))

    total = len(cands)
    quota = {cat: min(max(MIN_PER_CATEGORY, round(len(it) / total * limit)), len(it))
             for cat, it in by_cat.items()}

    # 总量超出：从配额最大的品类削，但不得低于保底
    while sum(quota.values()) > limit:
        cat = max((c for c in quota if quota[c] > MIN_PER_CATEGORY),
                  key=lambda c: (quota[c], -len(by_cat[c])), default=None)
        if cat is None:
            break
        quota[cat] -= 1

    # 总量不足：把余额给还有余量、候选最多的品类
    while sum(quota.values()) < limit:
        cat = max((c for c in quota if quota[c] < len(by_cat[c])),
                  key=lambda c: (len(by_cat[c]) - quota[c], -quota[c]), default=None)
        if cat is None:
            break
        quota[cat] += 1

    out: list[dict] = []
    for cat, q in quota.items():
        out.extend(by_cat[cat][:q])
    out.sort(key=lambda x: (-x["score"], x["record"]["id"]))
    return out


def run_city(city: str, limit: int, min_score: int, dry_run: bool,
             today: str) -> tuple[int, int]:
    """为单个城市挑选并生成能力卡，返回 (成功数, 失败数)。"""
    kept, stats = screen(city)
    cands = [k for k in kept if k["score"] >= min_score]

    print(f"\n[{city}] 原始 {stats['total']} 家")
    print(f"  硬噪声剔除 {stats['noise_hard']} 家（农场/门窗店/营销中心/楼栋等）")
    print(f"  推断不出工艺 {stats['no_process']} 家")
    print(f"  通过筛选 {stats['kept']} 家，其中分数 ≥{min_score} 的 {len(cands)} 家")

    picks = select(cands, limit)
    print(f"  实际挑选 {len(picks)} 家")

    if dry_run:
        cc = collections.Counter(p["record"]["category"] for p in picks)
        for k, v in sorted(cc.items(), key=lambda x: -x[1]):
            print(f"    {k:10} {v}")
        return 0, 0

    ok, failed = 0, []
    cat_cnt: dict[str, int] = collections.Counter()
    conf_cnt: dict[str, int] = collections.Counter()
    scores: list[int] = []

    for p in picks:
        cap, inf = build_capability(p["record"], p["cleaned"], today)
        if not cap:
            failed.append((p["record"]["id"], inf.get("reason", "?")))
            continue
        try:
            sc = completeness(cap)
            md = render_skill_md(cap)
            write_vendor_skill(cap, md)
            append_fingerprint(cap, sc)
            ok += 1
            cat_cnt[cap["category"]] += 1
            conf_cnt[inf["confidence"]] += 1
            scores.append(sc)
        except Exception as e:
            failed.append((p["record"]["id"], f"{type(e).__name__}: {e}"))

    print(f"生成成功 {ok} 家，失败 {len(failed)} 家")
    if failed:
        for i, r in failed[:10]:
            print(f"    ✗ {i}: {r}")

    print("\n按品类：")
    for k, v in sorted(cat_cnt.items(), key=lambda x: -x[1]):
        print(f"    {k:10} {v}")
    print("\n推断置信度：")
    for k, v in sorted(conf_cnt.items(), key=lambda x: -x[1]):
        print(f"    {k:8} {v}")
    print(f"\n资料完整度：均 {sum(scores)/max(len(scores),1):.0f}%（自动整理卡片必然很低，这是设计）")

    # 报告
    rep = REPO_ROOT / "docs" / "AUTO_PROFILE_REPORT.md"
    lines = [
        f"# 自动整理报告 · {city}",
        "",
        f"生成时间：{today}　引擎：`scripts/collect/auto_profile.py`",
        "",
        "## 筛选漏斗",
        "",
        f"| 阶段 | 数量 |",
        f"|---|---|",
        f"| 名录原始记录 | {stats['total']} |",
        f"| 剔除 POI 噪声 | -{stats['noise_hard']} |",
        f"| 推断不出工艺 | -{stats['no_process']} |",
        f"| 通过筛选 | {stats['kept']} |",
        f"| 分数 ≥ {min_score} | {len(cands)} |",
        f"| **最终生成** | **{ok}** |",
        "",
        "## 品类分布",
        "",
        "| 品类 | 数量 |",
        "|---|---|",
    ]
    for k, v in sorted(cat_cnt.items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        "## 推断置信度",
        "",
        "| 置信度 | 数量 | 含义 |",
        "|---|---|---|",
    ]
    meaning = {
        "high": "名称含明确工艺词（如「轴承」「压铸」「电镀」）",
        "medium": "名称含通用能力词（如「精密机械」「钣金」）",
        "low": "仅按名录品类兜底推断，可信度最低",
    }
    for k, v in sorted(conf_cnt.items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} | {meaning.get(k, '')} |")
    lines += [
        "",
        "## 重要声明",
        "",
        "这些卡片**未经供应商确认**。引擎只推断「名义能力」（做什么工艺、用什么材料），",
        "**硬指标（公差/MOQ/交期/产能/认证）全部留空** —— 平台不替供应商编造数字。",
        "",
        "客户 Agent 可用于「按城市 + 工艺 + 材料」初筛，**不可直接据此下单**。",
        "企业认领后可更正信息并获得认证标识。",
    ]
    rep.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  报告：{rep.relative_to(REPO_ROOT)}")
    return ok, len(failed)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="嘉兴", help="单个城市")
    ap.add_argument("--cities", help="多个城市，逗号分隔（与 --city 二选一）。"
                                     "用于把能力卡城市矩阵对齐到名录城市矩阵")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--min-score", type=int, default=MIN_SCORE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--clean", action="store_true",
                    help="生成前清空旧的自动整理卡片。"
                         "**多城市时不要加**：会删掉前面城市刚生成的结果")
    args = ap.parse_args()

    today = date.today().isoformat()

    if args.clean:
        import shutil
        for d in (REPO_ROOT / "skills" / "vendors").glob("*"):
            cj = d / "capability.json"
            if cj.exists():
                try:
                    if json.loads(cj.read_text(encoding="utf-8")).get(
                            "provenance", {}).get("mode") == "auto":
                        shutil.rmtree(d)
                except Exception:
                    pass
        print("已清空旧的自动整理卡片")

    cities = ([c.strip() for c in args.cities.split(",") if c.strip()]
              if args.cities else [args.city])
    if len(cities) > 1 and args.clean:
        print("[警告] 多城市模式 + --clean：只会保留最后一个城市的结果，已忽略 --clean")

    total_ok = total_fail = 0
    for c in cities:
        ok, fail = run_city(c, args.limit, args.min_score, args.dry_run, today)
        total_ok += ok
        total_fail += fail
    if len(cities) > 1:
        print(f"\n合计：{len(cities)} 个城市，生成成功 {total_ok} 家，失败 {total_fail} 家")
    return 0 if not total_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
