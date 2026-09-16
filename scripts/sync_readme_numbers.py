#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 README.md / README_EN.md 里的统计数据同步到真实值（stats.compute_stats()）。

为什么要有这个脚本
------------------
每次抓取之后，README 里 30 多个数字会集体过期：徽章、总数、已核实/待核实、
品类表、行业 TOP、大类分布。英文 README 同理（记录总数、已核实/待核实、
国标大类数、已归类、TOP 类、英文镜像数）。以前靠手改 —— 改漏一个
validate.py --strict 就红灯，天天红灯的下场是没人再看。

做法上保守一点：
  - **只替换数字**，不动周围的文字；表述变了（比如句子改写）就 WARN 出来让你改，
    而不是偷偷重写整段（那会把人工写的说明句吃掉）。
  - 默认只预览，加 --apply 才写盘；没有变化就不重写文件（避免搅乱行尾/mtime）。
  - 默认同步中英文两份 README；--readme <path> 可只同步单个文件。

用法
----
    python scripts/sync_readme_numbers.py            # 预览中英文 README 将改哪些数字
    python scripts/sync_readme_numbers.py --apply    # 写入 README.md 与 README_EN.md
    python scripts/sync_readme_numbers.py --quiet    # 不打印逐条改动（给流水线用）
    python scripts/sync_readme_numbers.py --readme README_EN.md --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

README = ROOT / "README.md"
README_EN = ROOT / "README_EN.md"
INDUSTRY_INDEX = ROOT / "data" / "industry-index.json"
TOP_N = 12          # 「行业家数 TOP」表行数
DIST_N = 6          # 「大类分布」句子里提到的品类数
EN_TOP_N = 8        # 英文 README「Top classes」列出的类数


def _meta() -> dict:
    if not INDUSTRY_INDEX.exists():
        return {}
    return json.loads(INDUSTRY_INDEX.read_text(encoding="utf-8")).get("metadata") or {}


def _sub_num(text: str, pattern: str, repl: str, note: str, changes: list[str]) -> str:
    """只替换第 1 个捕获组（即数字本身），周围的文字原样保留。

    ⚠ 踩过的坑：写成 `rx.sub(repl, text)` 会把**整个匹配**（含「条记录」这类字）
    一起替换掉，README 里的句子瞬间残缺，而 validate 只报「未找到 xx 的表述」——
    典型的改坏又不报错。这里按 group(1) 的起止位置拼接。
    """
    rx = re.compile(pattern)
    m = rx.search(text)
    if not m:
        changes.append("   WARN  没找到 %s 的表述（句式改过？脚本不猜，请手改）" % note)
        return text
    old = m.group(1)
    if old == repl:
        return text
    changes.append("   %s：%s → %s" % (note, old, repl))
    return text[:m.start(1)] + repl + text[m.end(1):]


def _sub_full(text: str, pattern: str, repl: str, note: str, changes: list[str]) -> str:
    """整段替换（用于「A / B 条已归类」这种多个数字要一起改的表述）。"""
    rx = re.compile(pattern)
    m = rx.search(text)
    if not m:
        changes.append("   WARN  没找到 %s 的表述（句式改过？脚本不猜，请手改）" % note)
        return text
    if m.group(0) == repl:
        return text
    changes.append("   %s：%s → %s" % (note, m.group(0), repl))
    return text[:m.start()] + repl + text[m.end():]


def top_industries(n: int) -> list[tuple[str, str, int]]:
    """行业家数 TOP n：从 data/industry-index.json 取（它就是按家数排的）。"""
    if not INDUSTRY_INDEX.exists():
        return []
    idx = json.loads(INDUSTRY_INDEX.read_text(encoding="utf-8")).get("index") or {}
    rows = [(code, v.get("name", ""), int(v.get("count", 0)))
            for code, v in idx.items() if v.get("count")]
    rows.sort(key=lambda r: (-r[2], r[0]))
    return rows[:n]


def top_industries_en(n: int) -> list[tuple[str, str, int]]:
    """英文版：优先用 name_en，缺失时回退中文 name。"""
    rows = []
    for code, name, count in top_industries(n):
        en = _industry_name_en(code) or name
        rows.append((code, en, count))
    return rows


def _industry_name_en(code: str):
    if not INDUSTRY_INDEX.exists():
        return None
    idx = json.loads(INDUSTRY_INDEX.read_text(encoding="utf-8")).get("index") or {}
    return (idx.get(code) or {}).get("name_en")


def build(st: dict, quiet: bool) -> tuple[str, int]:
    """中文 README.md 数字同步。"""
    text = README.read_text(encoding="utf-8")
    changes: list[str] = []

    # ── 1. 顶部徽章 ───────────────────────────────────────────────────
    text = _sub_num(text, r"records-(\d[\d,]*)", str(st["cn_total"]),
                    "records 徽章", changes)
    text = _sub_num(text, r"categories-(\d[\d,]*)", str(st["n_categories"]),
                    "categories 徽章", changes)

    # ── 2. 「数据现状」总述句 ─────────────────────────────────────────
    m = re.search(r"^##\s*数据现状\s*$.*?(?=^##\s)", text, re.M | re.S)
    if not m:
        changes.append("   WARN  没找到「## 数据现状」章节，只能改徽章")
        sec = text
    else:
        sec = m.group(0)
    new_sec = sec
    new_sec = _sub_num(new_sec, r"(\d[\d,]*)\s*条记录", str(st["cn_total"]),
                       "中文记录总数", changes)
    new_sec = _sub_num(new_sec, r"(\d[\d,]*)\s*条电话已核实", str(st["verified_total"]),
                       "已核实数", changes)
    new_sec = _sub_num(new_sec, r"(\d[\d,]*)\s*条待核实", str(st["unverified_total"]),
                       "待核实数", changes)
    new_sec = _sub_num(new_sec, r"(\d[\d,]*)\s*个国标大类", str(st["n_categories"]),
                       "国标大类数", changes)
    n_cities = st["cities"] if isinstance(st["cities"], int) else len(st["cities"])
    new_sec = _sub_num(new_sec, r"(\d[\d,]*)\s*个城市", str(n_cities),
                       "覆盖城市数", changes)
    if new_sec != sec:
        text = text.replace(sec, new_sec)

    # ── 2b. 英文镜像数（在「## 英文数据集」节，必须对整个文件做，不能只限数据现状节）──
    #    旧实现只在数据现状节内替换，导致「条英文镜像」永远不更新 → CI 偶发红。
    text = _sub_num(text, r"(\d[\d,]*)\s*条英文镜像", str(st["en_total"]),
                    "英文镜像数", changes)

    # ── 3. 各品类表格行：| 品类 | 总数 | 已核实 | 待核实 | ──────────────
    for r in st["categories"]:
        name = r["name"]
        pat = re.compile(
            r"\|\s*" + re.escape(name) + r"\s*\|\s*\d[\d,]*\s*\|\s*\d[\d,]*\s*\|\s*\d[\d,]*\s*\|")
        row = "| %s | %d | %d | %d |" % (name, r["total"], r["verified"],
                                         r["unverified_poi"])
        old = pat.search(text)
        if not old:
            changes.append("   WARN  品类表找不到「%s」那一行（表格列名改了？）" % name)
            continue
        if old.group(0) != row:
            changes.append("   品类「%s」→ %s" % (name, row))
        text = pat.sub(lambda _m: row, text, count=1)

    # ── 4. 已归类 / 未归类 / 国标小类数 ────────────────────────────────
    meta = _meta()
    if meta:
        text = _sub_full(text, r"\d[\d,]*\s*/\s*\d[\d,]*\s*条已归类",
                         "%d / %d 条已归类" % (meta.get("classified", 0),
                                               meta.get("total_suppliers", 0)),
                         "已归类条数", changes)
        text = _sub_num(text, r"\*\*(\d[\d,]*)\s*个国标小类码", str(meta.get("total_codes", 0)),
                        "国标小类数", changes)
        text = _sub_num(text, r"(\d[\d,]*)\s*条未归类", str(meta.get("unclassified", 0)),
                        "未归类条数", changes)

    # ── 5. 行业家数 TOP 表（整块重建，行数由 TOP_N 决定）────────────────
    top = top_industries(TOP_N)
    if top:
        rows = "\n".join("| %s | %s | %d |" % (c, n, k) for c, n, k in top)
        pat = re.compile(
            r"(?P<head>行业家数 TOP[^\n]*\n(?:[^\n]*\n)?\|[^\n]*\|\n\|[^\n]*\|\n)"
            r"(?P<body>(?:\|[^\n]*\|\n)+)")
        m = pat.search(text)
        if m:
            body = rows + "\n"
            if m.group("body") != body:
                changes.append("   行业家数 TOP：%d 行已刷新" % len(top))
            text = text[:m.start("body")] + body + text[m.end("body"):]
        else:
            changes.append("   WARN  没找到「行业家数 TOP」表格，跳过")

    # ── 6. 「大类分布」句子 ────────────────────────────────────────────
    top_cats = sorted(st["categories"], key=lambda r: -r["total"])[:DIST_N]
    tail = lambda s: s.split(" ", 1)[1] if " " in s else s  # noqa: E731
    line = "大类分布：" + "、".join(
        "%s %d" % (tail(c["name"]), c["total"]) for c in top_cats) + "。"
    text = _sub_full(text, r"大类分布：[^\n]*", line, "大类分布", changes)

    if not quiet:
        print("README 数字同步（来源：stats.compute_stats / industry-index.json）")
        if changes:
            for c in changes:
                print(c)
        else:
            print("   全部数字已是最新，无需改动")
    return text, len(changes)


def build_en(st: dict, quiet: bool) -> tuple[str, int]:
    """英文 README_EN.md 数字同步。

    英文版是散文式表述（数字内嵌在句子里），不能用中文表格正则，
    改用英文 label 正则匹配。只替换数字，句式变了就 WARN（不猜、不破坏散文）。
    「Top classes」整块用 industry-index 的 name_en 重建。
    """
    text = README_EN.read_text(encoding="utf-8")
    changes: list[str] = []
    meta = _meta()

    # ── 1. 中文数据集总述：24,085 records total ──────────────────────
    text = _sub_num(text, r"(\d[\d,]*)\s*records total", str(st["cn_total"]),
                    "EN records total", changes)
    # ── 2. phone-verified / pending ─────────────────────────────────
    text = _sub_num(text, r"(\d[\d,]*)\s*phone-verified", str(st["verified_total"]),
                    "EN phone-verified", changes)
    text = _sub_num(text, r"(\d[\d,]*)\s*pending", str(st["unverified_total"]),
                    "EN pending", changes)
    # ── 3. national divisions（对应中文 n_categories）─────────────────
    text = _sub_num(text, r"(\d[\d,]*)\s*national divisions", str(st["n_categories"]),
                    "EN national divisions", changes)
    # ── 4. classified / total / classes / unclassified ────────────────
    if meta:
        text = _sub_full(text, r"\d[\d,]*\s*/\s*\d[\d,]*\s*records classified",
                         "%d / %d records classified" % (
                             meta.get("classified", 0), meta.get("total_suppliers", 0)),
                         "EN records classified", changes)
        text = _sub_num(text, r"(\d[\d,]*)\s*national industry classes",
                        str(meta.get("total_codes", 0)),
                        "EN national industry classes", changes)
        text = _sub_num(text, r"(\d[\d,]*)\s*unclassified",
                        str(meta.get("unclassified", 0)),
                        "EN unclassified", changes)
    # ── 5. English-mirror records ─────────────────────────────────────
    text = _sub_num(text, r"(\d[\d,]*)\s*English-mirror records", str(st["en_total"]),
                    "EN English-mirror records", changes)
    # ── 6. Top classes 整块重建（用 industry-index 的 name_en）──────────
    top = top_industries_en(EN_TOP_N)
    if top:
        body = " · ".join("%s %s %s" % (c, n, "{:,}".format(k)) for c, n, k in top) + "."
        pat = re.compile(r"Top classes \(full list in [^\n]*\):\s*.*?(?=\n\n|\n>|\Z)",
                         re.S)
        m = pat.search(text)
        if m:
            repl = "Top classes (full list in `data/industry-index.json`): " + body
            if m.group(0) != repl:
                changes.append("   EN Top classes：%d 类已刷新（name_en）" % len(top))
            text = text[:m.start()] + repl + text[m.end():]
        else:
            changes.append("   WARN  没找到英文「Top classes」段落，跳过")

    if not quiet:
        print("README_EN 数字同步（来源：stats.compute_stats / industry-index.json）")
        if changes:
            for c in changes:
                print(c)
        else:
            print("   全部数字已是最新，无需改动")
    return text, len(changes)


def sync(apply: bool = False, quiet: bool = False, path=None) -> int:
    """读 README → 算出最新数字 → 需要时写盘。返回改动条数（含 WARN）。

    path 为空：同步 README.md + README_EN.md；
    path 指定：只同步该文件（按文件名判断用中文/英文逻辑）。
    """
    import stats  # 这里才 import：compute_stats 会全量扫库，别拖慢 --help
    st = stats.compute_stats()
    targets = []
    if path:
        p = Path(path)
        fn = build_en if p.name.lower().endswith("_en.md") else build
        targets = [(p, fn)]
    else:
        if README.exists():
            targets.append((README, build))
        if README_EN.exists():
            targets.append((README_EN, build_en))
    total = 0
    for p, fn in targets:
        text, n = fn(st, quiet)
        total += n
        if apply and p.exists() and text != p.read_text(encoding="utf-8"):
            p.write_text(text, encoding="utf-8")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="同步 README.md / README_EN.md 的统计数字")
    ap.add_argument("--apply", action="store_true", help="写入 README（默认只预览）")
    ap.add_argument("--quiet", action="store_true", help="不打印逐条改动")
    ap.add_argument("--readme", help="只同步指定 README 文件（默认同步中英文两份）")
    a = ap.parse_args()

    n = sync(apply=a.apply, quiet=a.quiet, path=a.readme)

    if not a.apply:
        if not a.quiet:
            print("\n（预览模式，未写入。加 --apply 执行）")
        return 0
    if n and not a.quiet:
        print("\n已写入 README（%d 处改动）" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
