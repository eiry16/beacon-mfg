#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把已认证企业从「私有认证档案」推到「买家真的搜得到」——同步环节的唯一入口。

为什么必须有这个脚本
--------------------
认证链路（主体核验 / 能力登记 / 人工复核 / 灯牌）做完之后，企业其实还**看不见**：

    认证档案  server/.data/certification/CERT-*.json   ← 私有层，不进 Git
        ↓  (certify_writeback.py)
    公开名录  data/gb/**.json + data/certified-index.json
        ↓  (gen_fingerprint.py / gen_capability_shards.py / gen_manifest.py)
    检索层    skills/registry/fingerprint/**.jsonl + dist/capability/**
        ↓  (APK/tools/sync_assets.py)
    买家可见  APK/app/src/main/assets/**  ← 买家 App 的只读内置快照

这条链的六步**每一步都已存在**，但从来没有被串起来过 —— 实测耐特斯
（CN-MFG-0020317）是靠人手逐个脚本敲出来的：敲漏一步，数据就停在半路，
而**所有环节都是静默成功**的：writeback 不报错、分片不报错、assets 不报错，
只有买家搜不到。所以本脚本做三件事：

1. **按固定顺序跑完六步**（顺序不能换，理由见下）
2. 每一步都如实打印产出，不吞输出
3. **跑完逐 id 校验**：名录 / 指纹 / assets 三层都得真能查到这家企业；
   少一层就报 FAIL 并以非零码退出（CI 能拦住）

顺序为什么是死的
----------------
- writeback 必须在最前：它才把认证档案落成名录记录，后面所有层都读它；
- sync_vendor_skills 在 writeback 之后：它往**名录记录**上补 `agent` 字段，
  记录得先存在（否则补到空气里）；
- gen_capability_shards 读 skills/registry/capability/（由 sync_vendor_skills 铺）；
- gen_fingerprint 从 data/gb 全量重建，但**会保留旧行里的富字段**
  （工艺/材料/硬指标），所以它必须排在 sync_vendor_skills 之后，
  否则新写的指纹行被当成"旧的"合并了一遍还不够、还可能丢字段；
- gen_manifest 统计指纹与名录分片，必须在它们定稿之后；
- sync_assets 是末端，把上面所有产物拷进 APK。

门槛（哪些企业可以被发布）
--------------------------
只发布**人工复核通过**（`stage == "approved"` 且有 `badge`）的档案 ——
这条门槛在 `certify_writeback.py` 里，本脚本不绕过、不重写。

为什么不发 L1：名录是买家做初筛的地方，混进一堆"自己来登记、平台没核过"的记录，
买家搜出来的"供应商"里有真厂也有空壳，名录就废了。L1 企业照常能在企业端看到
自己的灯牌与还差什么，只是**不进公开名录**。

用法
----
    python scripts/publish_vendor.py                      # 预览（不写任何文件）
    python scripts/publish_vendor.py --apply               # 真正发布
    python scripts/publish_vendor.py --apply --ids CN-MFG-0020317
    python scripts/publish_vendor.py --apply --skip-assets # 不更新 App 内置快照
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CERT_DIR = ROOT / "server" / ".data" / "certification"
FP_DIR = ROOT / "skills" / "registry" / "fingerprint" / "gb"
ASSETS_FP = ROOT / "APK" / "app" / "src" / "main" / "assets" / "fingerprint" / "gb"

import gb_store  # noqa: E402


def approved_ids() -> list[str]:
    """可发布的企业：人工复核通过、且算出了灯牌。

    读法与 `certify_writeback.py` 保持一致（同一个门槛），不另立标准。
    """
    out: list[str] = []
    for f in sorted(CERT_DIR.glob("CERT-*.json")):
        try:
            app = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if app.get("stage") == "approved" and app.get("badge"):
            sid = app.get("supplier_id")
            if sid:
                out.append(sid)
    return out


def pending_summary() -> list[tuple[str, str, str]]:
    """未达门槛的档案也要列出来。**沉默的跳过等于数据黑洞**：
    企业等了半天没上线，而这边一个字都没说，谁也查不出卡在哪。
    """
    rows: list[tuple[str, str, str]] = []
    for f in sorted(CERT_DIR.glob("CERT-*.json")):
        try:
            app = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if app.get("stage") == "approved" and app.get("badge"):
            continue
        rows.append((app.get("supplier_id") or "?", app.get("stage") or "?",
                     (app.get("company") or "")[:28]))
    return rows


def run(step: str, cmd: list[str], apply: bool, fatal: bool = True) -> int:
    """跑一步子流程。**命令与输出都如实透传**，不吞、不美化。"""
    mark = "APPLY" if apply else "预览"
    print("\n" + "─" * 68)
    print("▶ [%s] %s" % (mark, step))
    print("  $ " + " ".join(cmd))
    print("─" * 68)
    r = subprocess.run(cmd, cwd=str(ROOT), encoding="utf-8",
                       errors="replace", capture_output=True)
    out = (r.stdout or "").rstrip()
    if out:
        print(out)
    if r.stderr and r.stderr.strip():
        print("  [stderr] " + r.stderr.strip()[:1500])
    if r.returncode != 0:
        if not fatal:
            print("  · 退出码 %d（预览下属预期：改动还没落盘，现有清单自然对不上）"
                  % r.returncode)
            return 0
        print("  ✗ 这一步失败（退出码 %d）——**已中止，不继续往下跑**。" % r.returncode)
        print("    继续往下只会产出半截数据，而每一层都是静默成功的。")
    return r.returncode


def verify(ids: list[str]) -> list[str]:
    """逐 id 校验三层是否真能查到。返回失败清单。

    为什么要"校验"而不是"信任退出码"：这六步每一层都可能什么都没写却返回 0
    （路径写错、空输入、分片没变）。只有**回读**才能证明企业真的可见了。
    """
    fails: list[str] = []
    for sid in ids:
        bucket = None
        try:
            bucket = gb_store.locate(sid)
        except Exception as e:                                   # pragma: no cover
            fails.append("%s 名录定位异常：%s" % (sid, e))
            continue
        if not bucket:
            fails.append("%s 不在 data/gb 名录里" % sid)
            continue
        fp = FP_DIR / ("%s.jsonl" % bucket)
        if not fp.exists():
            fails.append("%s 的指纹分片不存在：%s" % (sid, fp.relative_to(ROOT)))
            continue
        if ('"%s"' % sid) not in fp.read_text(encoding="utf-8"):
            fails.append("%s 不在指纹分片 %s 里" % (sid, fp.relative_to(ROOT)))
        if ASSETS_FP.exists():
            art = ASSETS_FP / ("%s.jsonl" % bucket)
            if not art.exists() or ('"%s"' % sid) not in art.read_text(encoding="utf-8"):
                fails.append("%s 不在 App 内置快照里（买家侧仍然搜不到）" % sid)
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description="发布已认证企业（认证档案 → 买家可见）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    ap.add_argument("--ids", help="限定供应商 ID（逗号分隔），默认全部已复核通过的企业")
    ap.add_argument("--skip-assets", action="store_true",
                    help="不更新 App 内置快照（只发云端检索层时用）")
    a = ap.parse_args()

    targets = approved_ids()
    if a.ids:
        want = {s.strip() for s in a.ids.split(",") if s.strip()}
        unknown = want - set(targets)
        if unknown:
            print("✗ 这些企业还没通过人工复核，不能被发布：%s"
                  % "、".join(sorted(unknown)))
            print("  （发布门槛是「人工复核通过 + 灯牌已算出」，不是注册或提交材料）")
            return 2
        targets = [t for t in targets if t in want]

    print("═" * 68)
    print("发布已认证企业  %s" % ("【落盘】" if a.apply else "【预览，不写任何文件】"))
    print("═" * 68)
    if targets:
        print("待发布 %d 家：%s" % (len(targets), "、".join(targets)))
    else:
        print("待发布 0 家。")

    pend = pending_summary()
    if pend:
        print("\n以下档案还没到发布门槛（列出来，免得企业「等了却没上线」查不出原因）：")
        for sid, stage, co in pend:
            print("  - %-16s stage=%-20s %s" % (sid, stage, co))

    if not targets:
        print("\n没有可发布的企业。")
        print("要让企业变可见：企业端走完主体核验 + 能力登记，"
              "再由平台侧调用 POST /v1/certify/{app_id}/review 复核通过。")
        return 0

    ids_arg = ",".join(targets)
    py = sys.executable
    # (标签, 命令, 失败是否致命)。⚠ gen_manifest.py **没有预览模式**，一跑就写盘，
    # 所以预览时必须退回到只读的 --check —— 否则这个"预览"会偷偷改掉 data/manifest.json，
    # 而它对用户声明的行为是"不写任何文件"。
    steps: list[tuple[str, list[str], bool]] = [
        ("① 认证档案 → 公开名录（data/gb + certified-index + gb-index）",
         [py, "scripts/certify_writeback.py"] + (["--apply"] if a.apply else []), True),
        ("② 供应商 Skill → 检索链路（registry/capability + 指纹行 + 名录 agent 字段）",
         [py, "scripts/sync_vendor_skills.py", "--ids", ids_arg]
         + ([] if a.apply else ["--dry-run"]), True),
        ("③ 能力卡 → 国标分片（dist/capability/{full,slim}）",
         [py, "scripts/gen_capability_shards.py"] + (["--apply"] if a.apply else []), True),
        ("④ 名录 → L0 指纹层（skills/registry/fingerprint/gb）",
         [py, "scripts/gen_fingerprint.py"] + (["--apply"] if a.apply else []), True),
        ("⑤ 生成客户入口清单（data/manifest.json）"
         + ("" if a.apply else "（无预览模式，这里只校验现有清单）"),
         [py, "scripts/gen_manifest.py"] + ([] if a.apply else ["--check"]), a.apply),
    ]
    if not a.skip_assets:
        steps.append(
            ("⑥ 同步进 App 内置快照（买家侧只读数据源）",
             [py, "APK/tools/sync_assets.py"] + (["--apply"] if a.apply else []), True))

    for label, cmd, fatal in steps:
        rc = run(label, cmd, a.apply, fatal=fatal)
        if rc != 0:
            return rc

    print("\n" + "═" * 68)
    if not a.apply:
        print("预览结束 —— 以上是六步将要做的事。加 --apply 落盘。")
        return 0

    print("校验：这些企业是不是真的在三个层里都查得到")
    print("═" * 68)
    fails = verify(targets)
    for sid in targets:
        if sid not in " ".join(fails):
            print("  ✓ %s：名录 + 指纹" % sid
                  + ("" if a.skip_assets else " + App 内置快照") + " 均命中")
    if fails:
        print()
        for f in fails:
            print("  ✗ " + f)
        print("\n✗ 校验未通过 —— 买家侧可能仍然搜不到。别当成功。")
        return 1
    print("\n✓ 发布完成：%d 家企业已对买家可见（云端检索层%s）"
          % (len(targets), " 与 App 内置快照" if not a.skip_assets else ""))
    print("  提醒：App 内置快照要重新出包才生效；云端检索层立即生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
