#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 README.md 里的统计数据同步到真实值（stats.compute_stats()）。

为什么要有这个脚本
------------------
每次抓取之后，README 里 30 多个数字会集体过期：徽章、总数、已核实/待核实、
23 行品类表、已归类/未归类、行业家数 TOP、大类分布。
以前靠手改 —— 改漏一个 validate.py --strict 就红灯，天天红灯的下场是没人再看。

做法上保守一点：
  - **只替换数字**，不动周围的文字；表述变了（比如句子改写）就 WARN 出来让你改，
    而不是偷偷重写整段（那会把人工写的说明句吃掉）。
  - 默认只预览，加 --apply 才写盘；没有变化就不重写文件（避免搅乱行尾/mtime）。

用法
----
    python scripts/sync_readme_numbers.py           # 预览将要改哪些数字
    python scripts/sync_readme_numbers.py --apply   # 写入 README.md
    python scripts/sync_readme_numbers.py --quiet   # 不打印逐条改动（给流水线用）
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
INDUSTRY_INDEX = ROOT / "data" / "industry-index.json"
TOP_N = 12          # 「行业家数 TOP」表行数
DIST_N = 6          # 「大类分布」句子里提到的品类数


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


def build(st: dict, quiet: bool) -> tuple[str, int]:
    text = README.read_text(encoding="utf-8")
    changes: list[str] = []

    # ── 1. 顶部徽章 ───────────────────────────────────────────────────
    text = _sub_num(text, r"records-(\d[\d,]*)", str(st["cn_total"]),
                    "records 徽章", changes)
    text = _sub_num(text, r"categories-(\d[\d,]*)", str(st["n_categories"]),
                    "categories 徽章", changes)

    # ── 2. 「数据现状」总述句 ─────────────────────────────────────────
    # 注意只在这一节里模板 *_条记录_，别把徽章里的数字顺手改了。
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
    new_sec = _sub_num(new_sec, r"(\d[\d,]*)\s*条英文镜像", str(st["en_total"]),
                       "英文镜像数", changes)
    new_sec = _sub_num(new_sec, r"(\d[\d,]*)\s*个国标大类", str(st["n_categories"]),
                       "国标大类数", changes)
    # stats 里 cities 是城市个数（int），不是列表——别对它 len()。
    n_cities = st["cities"] if isinstance(st["cities"], int) else len(st["cities"])
    new_sec = _sub_num(new_sec, r"(\d[\d,]*)\s*个城市", str(n_cities),
                       "覆盖城市数", changes)
    if new_sec != sec:
        text = text.replace(sec, new_sec)

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
    meta = {}
    if INDUSTRY_INDEX.exists():
        meta = json.loads(INDUSTRY_INDEX.read_text(encoding="utf-8")).get("metadata") or {}
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
    # 「29 橡胶和塑料制品业」→「橡胶和塑料制品业」，句子里只写行业名
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


def sync(apply: bool = False, quiet: bool = False) -> int:
    """读 README → 算出最新数字 → 需要时写盘。返回改动条数（含 WARN）。"""
    import stats  # 这里才 import：compute_stats 会全量扫库，别拖慢 --help
    st = stats.compute_stats()
    text, n = build(st, quiet)
    if not apply:
        return n
    if text != README.read_text(encoding="utf-8"):
        README.write_text(text, encoding="utf-8")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="同步 README.md 的统计数字")
    ap.add_argument("--apply", action="store_true", help="写入 README.md（默认只预览）")
    ap.add_argument("--quiet", action="store_true", help="不打印逐条改动")
    a = ap.parse_args()

    n = sync(apply=a.apply, quiet=a.quiet)

    if not a.apply:
        if not a.quiet:
            print("\n（预览模式，未写入。加 --apply 执行）")
        return 0
    if n and not a.quiet:
        print("\n已写入 %s（%d 处改动）" % (README.name, n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
