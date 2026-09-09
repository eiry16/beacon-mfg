# -*- coding: utf-8 -*-
"""
导出采集面板演示数据
====================

把「真实引擎跑一遍 fixture」的结果导出成 JS，供 docs/prototype/collect-panel.html 渲染。
这样原型展示的每一条归一化结果都是引擎的真实输出，而不是手写死数据。

用法：
    python scripts/collect/export_panel_data.py CN-MFG-0000005
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collect.render import render_capability, render_fingerprint, render_skill_md  # noqa: E402
from collect.session import CollectSession, normalize_field  # noqa: E402
from collect.cli import load_base  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
OUT = REPO_ROOT / "docs" / "prototype" / "demo-session.js"


def build(sid: str) -> dict:
    base = load_base(sid)
    fx = json.loads((FIXTURES / f"{sid}.json").read_text(encoding="utf-8"))
    ans = fx["answers"]

    ses = CollectSession(sid, base.get("company", ""), base.get("category", ""),
                         fx.get("profile"))
    steps: list[dict] = []
    cur_title = None
    while True:
        q = ses.next_question()
        if q is None:
            break
        if q["step"] != cur_title:
            cur_title = q["step"]
            steps.append({"title": cur_title, "turns": []})
        text = ans.get(q["path"])
        if text is None:
            ses.skip()
            continue
        field = ses.current()[1]
        value, note = normalize_field(field, text)
        steps[-1]["turns"].append({
            "q": q["question"],
            "hint": q.get("hint", ""),
            "a": text,
            "label": q["label"],
            "path": q["path"],
            "value": value,
            "note": note,
            "parsed": value is not None,
        })
        ses.answer(text)

    cap = render_capability(ses, base)
    comp = ses.completeness()
    return {
        "supplier": {"id": sid, "company": base.get("company", ""),
                     "category": base.get("category", ""), "profile": ses.profile},
        "steps": steps,
        "completeness": comp,
        "conflicts": ses.conflicts(),
        "skill_md": render_skill_md(cap),
        "fingerprint": render_fingerprint(cap, comp["score"]),
    }


def main() -> int:
    sid = sys.argv[1] if len(sys.argv) > 1 else "CN-MFG-0000005"
    data = build(sid)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        "// 由 scripts/collect/export_panel_data.py 自动生成，请勿手改\n"
        "const DEMO = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )
    print(f"已导出 {OUT}")
    print(f"  步骤 {len(data['steps'])} 个，问答 {sum(len(s['turns']) for s in data['steps'])} 轮")
    print(f"  完整度 {data['completeness']['score']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
