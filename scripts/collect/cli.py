# -*- coding: utf-8 -*-
"""
BeaconMFG 采集引擎命令行入口
============================

用法：
    # 演示：用 fixtures 里的口述答案跑完整链路（采集→归一化→生成→校验）
    python scripts/collect/cli.py --demo CN-MFG-0000005

    # 交互式：人工逐题录入
    python scripts/collect/cli.py --id CN-MFG-0000005

    # 只列出会问哪些问题
    python scripts/collect/cli.py --questions precision-machining
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collect.render import (  # noqa: E402
    append_fingerprint, render_capability, render_skill_md, write_vendor_skill,
)
from collect.session import CollectSession  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gb_store  # noqa: E402
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_base(supplier_id: str) -> dict | None:
    return next((r for r in gb_store.load_all() if r.get("id") == supplier_id), None)


def run_session(ans: dict[str, str], supplier_id: str, company: str,
                category: str, verbose: bool = True) -> CollectSession:
    ses = CollectSession(supplier_id, company, category)
    while True:
        q = ses.next_question()
        if q is None:
            break
        text = ans.get(q["path"])
        if text is None:
            ses.skip()
            if verbose:
                print(f"  [跳过] {q['label']}（{q['path']}）")
            continue
        r = ses.answer(text)
        if verbose:
            mark = "OK " if r["parsed"] else "?? "
            print(f"  [{mark}] {r['label']}: {text!r}")
            print(f"        -> {r['value']}   ({r['note']})")
    return ses


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="供应商 ID（交互模式）")
    ap.add_argument("--demo", help="用 fixtures 中的演示答案跑通链路")
    ap.add_argument("--questions", help="只列出某 profile 会问的问题")
    ap.add_argument("--no-write", action="store_true", help="不落盘，仅预览")
    args = ap.parse_args()

    if args.questions:
        ses = CollectSession("CN-MFG-0000000", "示例", "精密机械加工", args.questions)
        for i, (step, f) in enumerate(ses.flat, 1):
            print(f"{i:3d}. [{step}] {f['label']}  ({f['path']}, {f['kind']})")
            print(f"     Q: {f['question']}")
        return 0

    sid = args.demo or args.id
    if not sid:
        ap.error("需要 --id 或 --demo")

    base = load_base(sid)
    if not base:
        print(f"[ERROR] data/ 中找不到 {sid}")
        return 1
    company = base.get("company", "")
    category = base.get("category", "")
    print(f"\n=== 采集：{company}（{sid}· {category}）===\n")

    if args.demo:
        fx = FIXTURES / f"{sid}.json"
        if not fx.exists():
            print(f"[ERROR] 缺少演示数据 {fx}")
            return 1
        ans = json.loads(fx.read_text(encoding="utf-8"))["answers"]
    else:
        ans = {}
        ses0 = CollectSession(sid, company, category)
        print("请逐题回答（回车跳过）：\n")
        while True:
            q = ses0.next_question()
            if q is None:
                break
            print(f"[{q['index']+1}/{q['total']}] {q['question']}")
            if q["hint"]:
                print(f"      提示：{q['hint']}")
            v = input("      > ").strip()
            if v:
                ans[q["path"]] = v
            ses0.skip()

    ses = run_session(ans, sid, company, category)

    comp = ses.completeness()
    print(f"\n--- 采集完成 ---")
    print(f"字段 {comp['filled']}/{comp['total']}，必填 {comp['required_filled']}"
          f"/{comp['required_total']}，完整度 {comp['score']}%")

    conflicts = ses.conflicts()
    if conflicts:
        print("\n冲突检测：")
        for c in conflicts:
            print(f"  [{c['level'].upper()}] {c['message']}")
    else:
        print("冲突检测：无")

    cap = render_capability(ses, base)
    md = render_skill_md(cap)
    fp = None

    print("\n--- 生成的 SKILL.md（前 40 行）---")
    print("\n".join(md.splitlines()[:40]))

    if not args.no_write:
        paths = write_vendor_skill(cap, md)
        from collect.render import render_fingerprint
        fp = render_fingerprint(cap, comp["score"])
        fpath = append_fingerprint(cap, comp["score"])
        print(f"\n已写入：")
        print(f"  {paths['capability']}")
        print(f"  {paths['skill']}")
        print(f"  {fpath}")
        print(f"\n指纹行：{json.dumps(fp, ensure_ascii=False, separators=(',', ':'))}")

    print("\n--- 校验 ---")
    import subprocess
    py = sys.executable
    r = subprocess.run([py, str(REPO_ROOT / "scripts" / "validate_vendor_skills.py"), sid],
                       cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8")
    print(r.stdout or r.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
