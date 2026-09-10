#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓后流水线（post-fetch pipeline）—— 抓取脚本收尾必须跑的派生层重建。

为什么必须有这个模块
--------------------
抓取脚本（fetch_batch / fetch_gaode_poi）只落 data/gb/** 的**原始名录**。
但对外发布、客户 Agent 真正读的是**派生层**——全部由名录算出来：

    L0 指纹分片   skills/registry/fingerprint/**   客户 Agent 全量扫一遍找候选
    L1 能力卡分片 dist/capability/**               Pages 站点 + App 内置资产
    分片清单       skills/registry/manifest.json    App 靠它对拍做增量更新
    国标主索引     data/gb-index.json               id → 文件定位 / 各级计数
    App 内置资产   APK/app/src/main/assets/**       手机上的离线副本
    行业/地域索引  data/industry-index.json 等       按行业 / 按城市检索

漏跑任何一步都**不报错**：新抓的企业在检索里静静地搜不到，日志一片绿。
这不是理论风险——region-index 就曾这样静默失效过。
所以在抓取脚本里硬挂钩，人手不必记得补跑。

执行顺序（有依赖，不能乱）
--------------------------
    classify   补写缺失国标行业标签（可选）
      ↓  指纹要拿 industry.code 推工艺，必须先补
    gbindex    重建 gb-index.json（id→文件主索引 + 各级计数）
      ↓  别的脚本（validate / 客户检索）拿它做总数与文件定位。漏跑的后果不是报错，
         是「名录已经 23796 家、索引还停在 23698 家」这种静默落后。
    index      行业索引 + 地域索引 + data/index.json 品类计数
      ↓
    fingerprint  重建 L0 指纹国标分片
      ↓
    shards     重建 L1 能力卡国标分片（full + slim）
      ↓
    manifest   重算分片清单（含 sha1 / 行数，App 增量靠它）
      ↓
    assets     同步 App 内置资产（指纹 / 能力卡 / 号码索引 → APK assets）
      ↓  手机上的离线副本。不同步的话 App 永远是上次手动跑的那版。
    readme     把 README.md 里的统计数字刷到最新
      ↓  必须在 validate 之前：抓完新数据，README 与 DATA_STATS.md 必然打架。
    validate   --strict 全量校验 + 重生成 DATA_STATS
      ↓
    pages      发布到 Cloudflare Pages（L1 分片 + L2 厂商自述）

最后一步为什么是发布：新供应商建档后如果没人手动部署，App 里点开就是 404
（2026-09-10 耐特斯就是这个真因）。**validate 没过不会发布**（见 run() 里的闸）。

失败策略
--------
默认**失败不中断**（后续步骤继续跑），末尾汇总并统计失败数。
理由：一步挂了不该让已经跑完的步骤成果白白丢掉，也不该让抓取进程
半途退出导致已抓数据没人知道。调用方拿到返回值自己决定 exit code。

用法
----
    python scripts/postfetch.py                      # 全跑（推荐）
    python scripts/postfetch.py --skip classify,validate
    python scripts/postfetch.py --dry-run            # 只预览子脚本默认行为，不写盘

从 Python 调用（抓取脚本走这条）：
    from postfetch import run
    failed = run(skip={"validate"})
    raise SystemExit(1 if failed else 0)
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 顺序即依赖，见文件头。别乱排。
ALL_STEPS = ("classify", "gbindex", "index", "fingerprint", "shards",
             "manifest", "assets", "readme", "validate", "pages")

# gen_manifest 的列式层（pq）依赖 pyarrow，缺省就静默少一层 —— 清单看着变绿，
# 实则少了 pq。这里的兜底顺序：当前解释器 → 环境变量 BMFG_PY_PQ → 本机已知 venv。
_DEFAULT_PQ_PY = r"C:/Users/陆斌/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

# 最近一次 run() 里失败的步骤名。run() 返回的是「失败步数」，
# 调用方想知道**具体哪一步**挂了（比如要不要继续发布）时读这个。
_LAST_FAILED: list[str] = []


def last_failed() -> list[str]:
    """返回最近一次 run() 失败的步骤名列表（空列表 = 全通过）。"""
    return list(_LAST_FAILED)

_STEP_DESC = {
    "classify": "补写国标行业标签",
    "gbindex": "重建 gb-index.json（id→文件主索引）",
    "index": "重建行业/地域索引 + 品类计数",
    "fingerprint": "重建 L0 指纹国标分片",
    "shards": "重建 L1 能力卡国标分片",
    "manifest": "重算分片清单（sha1/行数）",
    "assets": "同步 App 内置资产（APK assets）",
    "readme": "同步 README.md 里的统计数字",
    "validate": "全量严格校验 + 重生成 DATA_STATS",
    "pages": "发布到 Cloudflare Pages（能力卡 + 厂商自述）",
}


# ─────────────────────────────────────────────────────────── 子脚本调用装置

@contextlib.contextmanager
def _argv(*args: str):
    """临时替换 sys.argv。

    为什么必须这么做：每个子脚本的 main() 内部直接 ap.parse_args() 读的是
    全局 sys.argv，被人 import 调用时会把**调用方**的参数吞进来认 ||
    典型症状是 fetch_batch.py --limit 20 传进 gen_fingerprint 被当成未知参数
    然后 SystemExit(2)，看起来像「重建脚本坏了」。
    """
    old = sys.argv
    sys.argv = [str(args[0])] + [str(a) for a in args[1:]]
    try:
        yield
    finally:
        sys.argv = old


def _call(name: str, *args: str) -> int:
    """调用同级脚本的 main()，返回退出码（0 = 成功）。"""
    mod = __import__(name)
    try:
        with _argv(f"{name}.py", *args):
            ret = mod.main()
    except SystemExit as exc:
        # 坑：validate.py 用 sys.exit(0) 表达「校验通过」，gen_* 用 return 0/1。
        # 把 SystemExit(0) 当失败会让整条流水线天天假红灯，红灯多了就没人看了。
        code = exc.code
        if code is None or code == 0:
            return 0
        traceback.print_exc(limit=6, file=sys.stdout)
        return int(code) if isinstance(code, int) else 1
    # gen_* 用 return 1 表达失败；返回 None 视为成功
    if ret is None:
        return 0
    return int(ret)


# ───────────────────────────────────────────────────────────────── 各步实现

def step_classify(dry_run: bool = False) -> None:
    if dry_run:
        print("   （--dry-run：跳过补写）")
        return
    from classify_industry import backfill
    backfill()


def step_gbindex(dry_run: bool = False) -> None:
    """重建 data/gb-index.json（id → 文件的主索引 + 各级计数）。

    为什么单独一步：它只有 `gb_store.py --reindex` 会重建，而每日清单里这一步
    靠人手记。实测 09-10 抓完之后名录 23796 家、gb-index 还停在 23698 家 ——
    中间那 98 家新企业在按 id 定位文件的路径里是隐形的，且不报任何错。
    """
    if dry_run:
        print("   （--dry-run：不重建 gb-index.json）")
        return
    import gb_store
    before = _index_total()
    meta = gb_store.rebuild_index()
    after = (meta or {}).get("total_suppliers")
    if before and after:
        print("   [gb-index] 主索引 %d → %d 家" % (before, after))


def _index_total() -> int | None:
    """读取 gb-index.json 里现有的总数，用来报告『落后多少』。"""
    import json
    p = ROOT / "data" / "gb-index.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))["metadata"].get("total_suppliers")
    except Exception:
        return None


def step_index(dry_run: bool = False) -> None:
    if dry_run:
        print("   （--dry-run：不重建索引）")
        return
    from gen_industry_index import main as gen_industry
    from gen_region_index import main as gen_region

    with _argv("gen_industry_index.py"):
        gen_industry()
    with _argv("gen_region_index.py"):
        gen_region()
    sync_index_counts()


def step_fingerprint(dry_run: bool = False) -> None:
    rc = _call("gen_fingerprint", *([] if dry_run else ["--apply"]))
    if rc:
        raise RuntimeError(f"gen_fingerprint 返回 {rc}")


def step_shards(dry_run: bool = False) -> None:
    rc = _call("gen_capability_shards", *([] if dry_run else ["--apply"]))
    if rc:
        raise RuntimeError(f"gen_capability_shards 返回 {rc}")


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except Exception:
        return False


def _pq_interpreters() -> list[str]:
    env = (os.environ.get("BMFG_PY_PQ") or "").strip()
    return [p for p in (env, _DEFAULT_PQ_PY) if p]


def step_manifest(dry_run: bool = False) -> None:
    """重算分片清单。

    坑：pq（列式）层依赖 pyarrow。没装的话 gen_manifest 只在 stderr 打印一句警告
    然后正常退出 0 —— 清单看着是新的，实际少了一层。这里主动检测，装了 pyarrow
    的解释器存在就换它跑，都不可用才算失败（宁可红，不要假绿）。
    """
    if dry_run:
        print("   （--dry-run：不重算清单）")
        return
    if _has_pyarrow():
        rc = _call("gen_manifest")
        if rc:
            raise RuntimeError(f"gen_manifest 返回 {rc}")
        return

    for py in _pq_interpreters():
        if not Path(py).exists():
            continue
        probe = subprocess.run([py, "-c", "import pyarrow"],
                               capture_output=True)
        if probe.returncode != 0:
            continue
        print("   [提示] 当前解释器没有 pyarrow → 换 %s 跑 gen_manifest（否则 pq 层被丢弃）"
              % Path(py).name)
        done = subprocess.run([py, str(SCRIPTS / "gen_manifest.py")], cwd=str(ROOT))
        if done.returncode:
            raise RuntimeError(f"gen_manifest（{py}）返回 {done.returncode}")
        return

    raise RuntimeError(
        "当前解释器没有 pyarrow，pq 层会被静默丢弃。"
        "用装了 pyarrow 的解释器跑本流水线，或设置 BMFG_PY_PQ=<python.exe>"
    )


def step_assets(dry_run: bool = False) -> None:
    """同步 App 内置资产（APK/app/src/main/assets）。

    手机上的 App 读的是这份副本，它不在 git 里，只由 APK/tools/sync_assets.py
    写。跑虚拟 Port 之前藏在这一步之后是很常见的： fingerprints/manifest/能力卡
    都更新了，App 里还是上周那份。
    """
    tool = ROOT / "APK" / "tools" / "sync_assets.py"
    if not tool.exists():
        print("   （跳过：仓库里没有 APK/tools/sync_assets.py）")
        return
    if dry_run:
        print("   （--dry-run：不同步 App 内置资产）")
        return
    done = subprocess.run([sys.executable, str(tool), "--apply"], cwd=str(ROOT))
    if done.returncode:
        raise RuntimeError(f"sync_assets 返回 {done.returncode}")


def step_readme(dry_run: bool = False) -> None:
    """把 README.md 里的统计数字刷新到最新。

    必须在 validate **之前**：validate --strict 会逐项比对 README 与 DATA_STATS.md，
    抓完一批新数据后这两份必然打架（今天就是这样红灯 31 条的）。
    以前靠手改 —— 30 多个数字改漏一个就红灯，天天红灯的下场是没人再看。
    """
    if dry_run:
        print("   （--dry-run：不同步 README 数字）")
        return
    import sync_readme_numbers
    sync_readme_numbers.sync(apply=True)


def step_validate(dry_run: bool = False) -> None:
    """全量严格校验。

    坑（第 6 个「静默成功」）：以前这里调完 `_call` 不看返回值，
    validate 用 sys.exit(1) 报「校验未通过」时流水线照样打 ✓。
    校验就是最后一道闸，**它报错就必须红**。
    """
    if dry_run:
        print("   （--dry-run：跳过校验）")
        return
    rc = _call("validate", "--strict")
    if rc:
        raise RuntimeError(
            f"validate.py --strict 返回 {rc}（README 数字或索引不一致，看上面 [ERROR] 行）"
        )


def step_pages(dry_run: bool = False) -> None:
    """把 L1 能力卡分片 + L2 厂商自述发布到 Cloudflare Pages。

    **这一步就是「新供应商点开 404」的根治。**
    2026-09-10 耐特斯 CN-MFG-0020317 在 App 里点「打开」跳找不到网页，
    真因不是"从来没传过"，而是**发布快照是一次性手工动作**：它当天才建档，
    而上一次部署早于它 → URL 在大盘上根本不存在。App 侧的 HEAD 探活只是止血。
    只要发布不进流水线，每来一家新供应商就得等有人想起来手动部署一次。

    排在 validate 之后：**校验没过就不许发布**。
    约 2.5 分钟（4200+ 文件，未变的走 hash 秒传）。不想发就 `--skip pages`。
    """
    tool = ROOT / "scripts" / "deploy_pages.py"
    if not tool.exists():
        print("   （跳过：仓库里没有 scripts/deploy_pages.py）")
        return
    if dry_run:
        print("   （--dry-run：不发布到 Pages）")
        return
    done = subprocess.run([sys.executable, str(tool)], cwd=str(ROOT))
    if done.returncode:
        raise RuntimeError(f"deploy_pages.py 返回 {done.returncode}")


_STEP_FN = {
    "classify": step_classify,
    "gbindex": step_gbindex,
    "index": step_index,
    "fingerprint": step_fingerprint,
    "shards": step_shards,
    "manifest": step_manifest,
    "assets": step_assets,
    "readme": step_readme,
    "validate": step_validate,
    "pages": step_pages,
}


# ─────────────────────────────────────────────────────────── 计数同步工具

def sync_index_counts() -> None:
    """把 data/index.json 的各品类 count / total_suppliers 刷成实际条数。

    validate.py --strict 会逐品类比对这两个数字，不对就 ERROR。
    以前靠人手改，抓完忘了改就是红灯。
    """
    import json

    import gb_store

    idx_path = ROOT / "data" / "index.json"
    if not idx_path.exists():
        return
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    by_cat: dict[str, int] = {}
    for r in gb_store.load_all():
        by_cat[r.get("category")] = by_cat.get(r.get("category"), 0) + 1
    total = 0
    for c in idx.get("categories", []):
        n = by_cat.get(c["name"], 0)
        c["count"] = n
        total += n
    idx["total_suppliers"] = total
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    print("   [index.json] 品类计数已同步 → %d 条" % total)


# ─────────────────────────────────────────────────────────────── 主入口

def run(skip: set[str] | frozenset[str] | list[str] | None = None,
        only: set[str] | frozenset[str] | list[str] | None = None,
        dry_run: bool = False,
        quiet: bool = False) -> int:
    """跑完（或部分跑完）抓后流水线。返回失败步数（0 = 全通过）。"""
    skip = set(skip or ())
    if only:
        steps = [s for s in ALL_STEPS if s in set(only)]
    else:
        steps = [s for s in ALL_STEPS if s not in skip]

    if not steps:
        if not quiet:
            print("抓后流水线：没有要执行的步骤")
        return 0

    if not quiet:
        print("\n" + "═" * 62)
        print("抓后流水线：%d 步（%s）" % (len(steps), " → ".join(steps)))
        print("═" * 62)

    _LAST_FAILED.clear()

    failed: list[str] = []
    skipped: list[str] = []
    for i, name in enumerate(steps, 1):
        # 发布这道闸：validate 没过（或压根没跑）就不许上线。
        # run() 默认是「失败不中断」，不拦的话校验红灯的产物也会被推到生产。
        # 要单独发布就直跑 scripts/deploy_pages.py。
        if name == "pages" and ("validate" in failed or "validate" not in steps):
            skipped.append(name)
            print("   ⊘ 跳过发布：本轮 validate 未通过或未执行，"
                  "不把未校验的产物推上线（要单独发布请跑 scripts/deploy_pages.py）")
            continue
        if not quiet:
            print("\n[%d/%d] %s —— %s" % (i, len(steps), name, _STEP_DESC[name]))
        t0 = time.time()
        try:
            _STEP_FN[name](dry_run=dry_run)
        except SystemExit as exc:              # 子脚本用 argparse/ sys.exit 报错
            code = exc.code if isinstance(exc.code, int) else exc.code
            failed.append(name)
            print("   ✗ %s 以 SystemExit(%s) 结束" % (name, code))
            if exc.code not in (0, None) and not isinstance(exc.code, int):
                print("     %s" % exc.code)
        except BaseException as exc:           # noqa: BLE001 —— 流水线要扛住一切
            failed.append(name)
            print("   ✗ %s 失败：%s" % (name, exc))
            traceback.print_exc(limit=3, file=sys.stdout)
        else:
            if not quiet:
                print("   ✓ 完成（%.1fs）" % (time.time() - t0))

    _LAST_FAILED.extend(failed)
    if not quiet:
        print("\n" + "═" * 62)
        if failed:
            print("抓后流水线：%d/%d 步失败 → %s" % (len(failed), len(steps), "、".join(failed)))
            print("未通过的步骤对应的产物是**旧数据**，别直接发布。")
        else:
            print("抓后流水线：%d 步全部通过" % (len(steps) - len(skipped)))
            if skipped:
                print("（跳过 %d 步：%s）" % (len(skipped), "、".join(skipped)))
            _coverage_hint()
        print("═" * 62)
    return len(failed)


def _coverage_hint() -> None:
    """报一句「名录多少家 / 有 L1 卡多少家」。

    抓取脚本本身不产能力卡（卡的生成要看配置文件 batch_auto_profile），
    所以每次抓完必然拉低覆盖率。不给这个提示，人就会以为流水线漏了什么。
    """
    try:
        total = _index_total() or 0
        cap_dir = ROOT / "skills" / "registry" / "capability"
        cards = sum(1 for p in cap_dir.rglob("*.json") if p.is_file()) if cap_dir.exists() else 0
        if not total or not cards:
            return
        print("L1 覆盖率：%d/%d 家有能力卡（%.1f%%）——新抓的企业没有卡，"
              "补卡跑 scripts/collect/batch_auto_profile.py --cities <城市>"
              % (cards, total, cards * 100.0 / total))
    except Exception:
        pass


def _main() -> int:
    ap = argparse.ArgumentParser(description="BeaconMFG 抓后流水线（派生层重建）")
    ap.add_argument("--skip", default="",
                    help="跳过某些步骤，逗号分隔。可选：%s" % ",".join(ALL_STEPS))
    ap.add_argument("--only", default="",
                    help="只跑某些步骤，逗号分隔")
    ap.add_argument("--dry-run", action="store_true",
                    help="不写盘（依赖子脚本自身的预览模式）")
    args = ap.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    bad = (skip | only) - set(ALL_STEPS)
    if bad:
        print("未知步骤：%s（可选 %s）" % ("、".join(sorted(bad)), ",".join(ALL_STEPS)))
        return 2

    return 1 if run(skip=skip, only=only, dry_run=args.dry_run) else 0


if __name__ == "__main__":
    raise SystemExit(_main())
