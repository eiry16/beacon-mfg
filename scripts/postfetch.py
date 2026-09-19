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

执行顺序（有依赖，不能乱；权威定义见下面的 ALL_STEPS）
------------------------------------------------------
    classify → gbindex → recat → enrefile → index → fingerprint
      ↓
    english（可选）      en_backfill + en_sync_industry      ┐ 两个慢活
    autoprofile（可选）  batch_auto_profile 给本轮城市补卡    ┘ 默认都跳过
      ↓  排在 shards 之前，是因为它们产出的卡 / 英文记录必须被下面的步骤收进去
    shards → manifest   能力卡分片 + 清单（App 增量就靠这份清单对 sha1）
      ↓
    readme → validate   README 数字先对齐，再做 --strict 体检
      ↓
    assets              同步 APK 内置资产（现在只在校验通过之后抄）
      ↓
    r2 → pages          上传 R2（云端真源）+ 发布 Pages（App 主源）
      ↓
    git                 最后才推 GitHub（jsDelivr / raw 备源）

这么排的三条理由（2026-09-16 重排）
----------------------------------
1. **git 原先排在 validate 之前** —— 那是「先推 GitHub、后体检」，未校验的 L0
   会先流到备源。现在它是最后一步，run() 里另有一道「validate 未过就跳过 git」的闸。
2. **assets 原先也在 validate 之前** —— 同理，改成先体检再往 APK 里抄。
   （readme 必须留在 validate 之前：--strict 拿 README 与 DATA_STATS.md 对账。）
3. **english / autoprofile 原先挂在发布之后单独跑** —— 于是英文永远比中文晚一天
   上云，能力卡要等下一轮 shards 才进得去。现在两者都挪到 shards 之前，当轮生效。

慢活要隔离（cron / GUI 请照做）
------------------------------
english（每轮几百到上千条，700~1300 条/小时）与 autoprofile（每城全量扫名录 +
LLM 推断）动辄几十分钟。它们是排在发布**之前**的，一旦被外部超时杀掉，当天的
发布就一起没了 —— 这正是 2026-09-15 晚上「本地都重建好了、手机还是旧数据」的成因。

所以调用方应当分两段跑：
    第一段（核心，必须成）：postfetch.py                  # 不含慢活，落地发布 + git
    第二段（增强，允许败）：postfetch.py --only english,autoprofile,shards,manifest,readme,validate,r2,pages,git
慢活超时失败再多次，损失也只是「这一轮的能力卡没补上」，不会把发布一起赔进去。

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
import atexit
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
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
#
# 2026-09-16 重排，三处纠正（旧的排法都不报错，只会静静地漏）：
#   1) git 从 validate **之前**挪到最后 —— 旧排法是「先推 GitHub、后体检」，未校验的
#      L0 会先流到 jsDelivr 备源。现在它排在 r2/pages 之后，run() 里还另加了一道
#      「validate 必须跑过并通过」的闸。
#   2) assets 挪到 validate 之后 —— 只把通过校验的数据抄进 APK 内置资产。
#      （readme 必须留在 validate 之前：--strict 会拿 README 与 DATA_STATS.md 对账。）
#   3) english 从流水线外挪进来，位置必须在 shards / manifest 之前 —— manifest 要把
#      en 分片的 sha1 算进清单；以前 GUI / cron 都把它挂在**发布之后**单独跑，
#      于是英文永远比中文晚一天上云。
#   capability 必须排在 autoprofile **之后、shards 之前**：batch_auto_profile 把卡写在
#   skills/vendors/{id}/，而 gen_capability_shards 只读 skills/registry/capability/，
#   中间这一步把两边接起来，并回写名录的 agent 字段（App 靠它拿 skill_url）。
#   2026-09-17 之前流水线缺这一步 —— 于是新城市就算补了卡也只是躺在 vendors/ 里，
#   分片、云端、App 全都看不到。
ALL_STEPS = ("classify", "gbindex", "recat", "enrefile", "index", "fingerprint",
             "searchindex",
             "english", "autoprofile", "capability", "shards", "manifest", "readme",
             "validate", "assets", "r2", "pages", "git")

# ⚠ 顺序陷阱（2026-09-17 实测踩中，代价：手机端新城市的电话全丢）：
#   gen_manifest 会把 data/phone-index.jsonl 的 sha1 写进清单，而这张索引是
#   sync_assets 从 data/gb 现算出来的**派生层**。原先 assets 排在 manifest **之后**，
#   于是清单记的是上一代索引的哈希、Pages 随后发出去的是新索引
#   → App 端 writePhoneIndex 逐字节校验 sha1 → 不匹配 → **静默丢弃**
#   （不报错、不退版本号，下次启动重试照样失败），手机永远停在旧索引：
#   新抓城市（贵阳等）的企业卡片一律显示「☎ 有电话，但源数据为『待核实』」。
#   线上实证：manifest 的 phone 条目 k=56988/u=9-16，而 data/phone-index.jsonl 已是 83840 条。
#   修法不是把 assets 提前（assets 要留在 validate 之后，只把**已校验**的数据抄进 APK），
#   而是让 manifest 步**自己先把索引重算一遍**（sync_assets --only phone --apply）。
#   另有一道发布前闸门（deploy_pages._check_manifest_consistency）兜底，改坏顺序发不出去。

# 工作树掩码体检阈值：data/gb + phone-index 里允许存在的掩码号上限。
# 正常水位个位数（源里查不到全号的残差）；一次 checkout 事故会把它顶到 4 万+。
_WORKTREE_MASK_TOLERANCE = 300

# gen_manifest 的列式层（pq）依赖 pyarrow，缺省就静默少一层 —— 清单看着变绿，
# 实则少了 pq。这里的兜底顺序：当前解释器 → 环境变量 BMFG_PY_PQ → 本机已知 venv。
_DEFAULT_PQ_PY = os.environ.get("BMFG_PY_PQ") or os.path.expanduser(
    r"~/.workbuddy/binaries/python/envs/default/Scripts/python.exe")

# 最近一次 run() 里失败的步骤名。run() 返回的是「失败步数」，
# 调用方想知道**具体哪一步**挂了（比如要不要继续发布）时读这个。
_LAST_FAILED: list[str] = []


def last_failed() -> list[str]:
    """返回最近一次 run() 失败的步骤名列表（空列表 = 全通过）。"""
    return list(_LAST_FAILED)

_STEP_DESC = {
    "classify": "补写国标行业标签",
    "gbindex": "重建 gb-index.json（id→文件主索引）",
    "enrefile": "英文镜像按国标码重新落位（重分类后必须搬）",
    "recat": "category 重算为 industry.code 的派生值（清错标残留）",
    "index": "重建行业/地域索引 + 品类计数",
    "fingerprint": "重建 L0 指纹国标分片",
    "searchindex": "重建 MCP 检索倒排索引（让 Agent 按需拉分片，避免全量冷启动）",
    "english": "英文镜像增量补齐（en_backfill + en_sync_industry，慢/需 ZHIPU key）",
    "autoprofile": "给本轮新抓城市补未认证能力卡（调 batch_auto_profile，纯本地推断）",
    "capability": "把 vendors/ 下的能力卡同步进 registry/capability + 回写名录 agent 字段",
    "shards": "重建 L1 能力卡国标分片",
    "manifest": "重算分片清单（sha1/行数）",
    "git": "提交并推送 L0 数据到 GitHub（让 jsDelivr/Pages 拿到新鲜分片）",
    "assets": "同步 App 内置资产（APK assets）",
    "readme": "同步 README.md 里的统计数字",
    "validate": "全量严格校验 + 重生成 DATA_STATS",
    "r2": "增量上传能力卡到 Cloudflare R2（云端真源）",
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


def step_enrefile(dry_run: bool = False) -> None:
    """英文镜像按国标码重新落位。

    为什么必须单列一步（2026-09-14）：中文库重分类后（比如把「粮食/食品/纺织」
    这些原本被 OUT_OF_SCOPE 丢成 industry=null 的记录改判到真实国标码），
    英文镜像不会自动跟着搬 —— 它们还躺在 data/en/gb/_unclassified.json，
    validate 于是报 254 条「落位错误」，r2/pages 全被跳过。
    """
    rc = _call("en_refile", *([] if dry_run else ["--apply"]))
    if rc:
        raise RuntimeError(f"en_refile.py 返回 {rc}")


def step_recat(dry_run: bool = False) -> None:
    """category 重算为 industry.code 的派生值。

    classify 会修正 industry.code，却不会回头更新 category —— 后者停留在
    抓取时按关键词写入的旧值，于是和门类对不上（2026-09-14 实测 1426 条，
    典型如药店 5251 被标成制造业的「原材料」）。客户 Agent 按品类检索
    会因此错配。必须在 index（重算品类计数）之前跑。
    """
    rc = _call("recat", *([] if dry_run else ["--apply"]))
    if rc:
        raise RuntimeError(f"recat.py 返回 {rc}")


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


def step_searchindex(dry_run: bool = False) -> None:
    """重建 MCP 检索倒排索引（skills/registry/index/）。

    必须排在 fingerprint 之后（读它的产物）。缺了这一步不会报错，只会静默导致
    MCP 检索回退成「全量拉分片 + 客户端建索引」——数据量一大就慢到不可用。
    """
    rc = _call("gen_search_index", *(["--dry-run"] if dry_run else []))
    if rc:
        raise RuntimeError(f"gen_search_index 返回 {rc}")


_DO_ENGLISH = False  # run(english=True) / 命令行 --english 打开


def step_english(dry_run: bool = False) -> None:
    """英文镜像增量补齐（en_backfill → en_sync_industry）。

    慢（GLM-4-Flash 限速，实测 700~1300 条/小时）且吃 ZHIPU_API_KEY，故默认关。

    **必须排在 shards / manifest 之前**：manifest 会统计 en 分片的数量与 sha1，
    先把清单算出来再补英文 = 清单里那批英文分片是空的。
    2026-09-16 之前 GUI / cron 都把它当独立尾巴挂在**发布之后**跑，于是英文
    永远比中文晚一天上云 —— 现在挪进来，当轮就能随 r2/pages 一起发布。
    """
    if not _DO_ENGLISH:
        print("   （跳过：未开启英文镜像）")
        return
    if dry_run:
        print("   （--dry-run：不翻译）")
        return
    if not os.environ.get("ZHIPU_API_KEY"):
        # en_backfill 自身会回退读 .env，这里只是提前给个明白话，避免跑半天才发现没 key。
        print("   （提示：环境里没有 ZHIPU_API_KEY，将依赖 en_backfill 回退读 .env）")

    rc = _call("en_backfill")
    if rc:
        raise RuntimeError(f"en_backfill 返回 {rc}")
    rc = _call("en_sync_industry")
    if rc:
        raise RuntimeError(f"en_sync_industry 返回 {rc}")


_AUTOPROFILE_CITIES = None  # run() 经 autoprofile_cities= 注入；step_autoprofile 读取
_AUTOPROFILE_LIMIT = None   # run() 经 autoprofile_limit= 注入；None = 用 batch_auto_profile 默认(300)


def step_autoprofile(dry_run: bool = False) -> None:
    """给本轮新抓到的城市补「未认证」能力卡（skills/vendors/{id}/SKILL.md + capability.json）。

    这不是每次都跑的轻活：调 collect/batch_auto_profile.py，会按城市筛选名录、
    调 LLM 推断工艺、渲染卡片 —— 慢且烧钱。故默认 skip（fetch_batch / GUI 不传
    --autoprofile / 不勾选「自动补能力卡」就 skip）。勾选时只对传入 cities 增量补，
    不加 --clean，已生成的卡不会被清空。

    放 shards 之前：新生成的 L2 自述 + capability.json 要被 gen_capability_shards 切进
    L1 分片，再被 r2/pages 上传，链路才完整。
    """
    cities = _AUTOPROFILE_CITIES
    if not cities:
        print("   （跳过：未指定要补卡的城市）")
        return
    tool = ROOT / "scripts" / "collect" / "batch_auto_profile.py"
    if not tool.exists():
        print("   （跳过：仓库里没有 scripts/collect/batch_auto_profile.py）")
        return
    if dry_run:
        print("   （--dry-run：不补卡）")
        return
    cmd = [sys.executable, str(tool), "--cities", cities]
    if _AUTOPROFILE_LIMIT:
        cmd += ["--limit", str(_AUTOPROFILE_LIMIT)]
    done = subprocess.run(cmd, cwd=str(ROOT))
    if done.returncode:
        raise RuntimeError(f"batch_auto_profile 返回 {done.returncode}")


def step_capability(dry_run: bool = False) -> None:
    """能力卡回流：skills/vendors/{id}/ → skills/registry/capability/{id}.json。

    这一步是「补了卡却搜不到」的解药。三件事，全部幂等：
      1. vendors/{id}/capability.json → registry/capability/{id}.json
         （gen_capability_shards **只读后者**，少这一步分片永远是旧的）
      2. 回写名录 data/gb/ 的 agent 字段（skill_url / capabilities / verified）
         （App 详情页靠它决定要不要去拉能力卡）
      3. 重算 L0 指纹行（按 id 覆盖，不产生幽灵数据）

    幂等且纯本地。**按 diff 增量做**：只同步「registry 里没有」或「源卡比目标新」的 id，
    所以日常一轮只处理本轮新补的几百张（秒级）。若哪天需要全量重建，删掉
    skills/registry/capability/ 即可，下一步会自动把 14000+ 家全搬一遍（约 100 分钟）。

    ⚠ **不要中途 kill 这一步**（2026-09-17 血的教训）：写回名录走
    supplier_loader.persist_supplier，跨桶迁移是「旧桶剔除 → 新桶追加」**两次独立写盘**，
    在两步之间被杀就会留下两份同 id 记录（一个留在原小类、一个进了 _unclassified），
    validate --strict 随即报「重复 id」，而当晚的数据就发不出去了。

    ⚠⚠ 中断后的恢复：**只回滚被污染的那几个文件**，然后
    `git checkout -- data/gb/<那几个文件>` 之后**必须**再跑
    `python scripts/restore_full_phones.py --apply`。
    原因是 `git checkout`/`git restore` 会把 index 里的**脱敏**内容写回工作树，
    而工作树本该是全号（.gitattributes 的约定）——更阴的是 git status 依然显示干净
    （clean(全号)==clean(脱敏)），事故无声。2026-09-17 实测：一次
    `git checkout -- data/gb/` 让全树 42578 个手机号变成掩码，随后
    sync_assets 依此重建号码索引，连 App 内置资产也一起脱敏了。
    """
    tool = ROOT / "scripts" / "sync_vendor_skills.py"
    if not tool.exists():
        print("   （跳过：仓库里没有 scripts/sync_vendor_skills.py）")
        return

    vendor = ROOT / "skills" / "vendors"
    reg = ROOT / "skills" / "registry" / "capability"
    if not vendor.exists():
        print("   （跳过：没有 skills/vendors/）")
        return

    pending = []
    for d in vendor.iterdir():
        src = d / "capability.json"
        if not src.exists():
            continue
        dst = reg / f"{d.name}.json"
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            pending.append(d.name)

    if not pending:
        print("   （能力卡已是最新，无需同步）")
        return

    pending.sort()
    print(f"   待同步 {len(pending)} 家")
    if dry_run:
        print(f"   （--dry-run：不同步。示例 id：{'、'.join(pending[:5])}）")
        return

    # 分批传给 --ids：命令行长度有上限，单批失败也不会拖垮整轮
    failed = 0
    for i in range(0, len(pending), 500):
        batch = pending[i:i + 500]
        done = subprocess.run(
            [sys.executable, str(tool), "--ids", ",".join(batch)], cwd=str(ROOT))
        if done.returncode:
            failed += len(batch)
            print(f"   [警告] 第 {i // 500 + 1} 批同步失败（退出码 {done.returncode}）")
    if failed:
        raise RuntimeError(f"capability 同步失败 {failed} 家")


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

    ① 先把号码索引重算一遍（sync_assets --only phone --apply）。清单要哈希的是
       **最新**的 data/phone-index.jsonl，见 ALL_STEPS 上方的顺序陷阱说明：
       索引是 data/gb 的派生层，若留给后面的 assets 步重建，清单就慢一代，
       App 端 sha1 校验不过会静默丢弃整份索引 —— 手机端新城市的电话永远出不来。

    ② 坑：pq（列式）层依赖 pyarrow。没装的话 gen_manifest 只在 stderr 打印一句警告
    然后正常退出 0 —— 清单看着是新的，实际少了一层。这里主动检测，装了 pyarrow
    的解释器存在就换它跑，都不可用才算失败（宁可红，不要假绿）。
    """
    if dry_run:
        print("   （--dry-run：不重算清单）")
        return
    tool = ROOT / "APK" / "tools" / "sync_assets.py"
    if tool.exists():
        rc = subprocess.run([sys.executable, str(tool), "--only", "phone", "--apply"],
                            cwd=str(ROOT)).returncode
        if rc:
            raise RuntimeError(f"sync_assets --only phone 返回 {rc}")
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


def step_r2(dry_run: bool = False) -> None:
    """把 L1/L2 产物增量上传到 Cloudflare R2 —— **云端真源**。

    Git 里的 skills/ 只是发布快照，**R2 才是真源**（P0 的核心决策）。
    这一步不做，新供应商的能力卡就只活在某人电脑的磁盘上。

    ⚠ 速率限制（2026-09-10 16:35 亲测）：免费账户约 1200 请求 / 5 分钟，
    12 并发跑 12411 个文件 → 成功 4543、失败 7868（全是 429），15 分钟只完成 37%。
    修法已写进 publish_r2.py：并发降到 4、429/5xx 指数退避、默认跳过已存在的对象。
    **日常增量（每天约 100 家 = 300 文件）几十秒就完事**，只有首次全量才慢。

    排在 validate 之后：校验没过的数据不上云。不想传就 `--skip r2`。
    """
    tool = ROOT / "scripts" / "publish_r2.py"
    if not tool.exists():
        print("   （跳过：仓库里没有 scripts/publish_r2.py）")
        return
    if dry_run:
        print("   （--dry-run：不上传 R2）")
        return
    done = subprocess.run([sys.executable, str(tool), "--all"], cwd=str(ROOT))
    if done.returncode:
        raise RuntimeError(f"publish_r2.py 返回 {done.returncode}")


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


# L0 白名单：只有这些路径会被自动提交推送。
#
# **绝不要用 git add -A** —— 主人常并行开发 APK/**/*.kt、scripts/*.py、
# skills/schema/vendor-skill.schema.json，-A 会把没写完的东西一起推上去。
# GUI（git 步）与每日 cron 都从这里取同一份定义，避免两套口径各自漂移。
L0_PATHS = (
    "data/gb", "data/en", "data/manifest.json", "data/index.json",
    "data/gb-index.json", "data/phone-index.jsonl", "data/region-index.json",
    "data/fetch_cursor.json", "skills/registry/fingerprint",
    "skills/registry/index.json", "skills/registry/index",
    "skills/registry/gb-proc-map.json",
)

# 派生/文档文件：validate.py --strict 会拿 README.md / DATA_STATS.md / industry-index.json
# 与数据集对账（审计议题 #1 第 4 条「把 count/溯源校验加进 CI」落地后的必然检查）。
# 它们由 postfetch 的 readme / stats / industry-index 步骤刷新，却不是 App 运行时 L0 源，
# 所以旧版只提交 L0_PATHS → 每次数据更新后 README 永远滞后 → CI 恒红。
# 必须与 L0 数据一起提交，CI 才不会对账失败。（2026-09-16 修复）
L0_DERIVED = ("README.md", "README_EN.md", "data/DATA_STATS.md", "data/industry-index.json")


# ─── 手机号脱敏：git clean filter 保障（方案 A）───
# 见仓库根 .gitattributes 与 scripts/mask_phones.py。
# 目标：GitHub 仓库里的 data/gb / data/en / phone-index 入库即脱敏（手机→138****0000，
# 座机原样，已认领 claim.status=claimed/verified 保留全号），但**工作树（Pages 部署源）
# 始终是全号** → App 端照常看全号+拨号；已装旧版 App 读的是 Pages（全号），完全无感。
import re as _re


def _git_run(args: list[str]) -> tuple[int, str]:
    p = subprocess.run(["git", "-C", str(ROOT)] + args, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def _ensure_mask_filter() -> None:
    """确保 git clean filter `maskphone` 已在本地配置（幂等）。

    .gitattributes 已声明 filter=maskphone，但 filter 命令只在本地 config（不随仓库走）。
    首次运行自动写入，避免「filter 未定义」导致 git add 报错，或（更糟）把全号入库。

    ⚠️ 路径必须用正斜杠（POSIX）：git 通过 MSYS shell 执行 clean/smudge 命令，
    反斜杠会被当成转义符（"C:\\Users\\..." → "C:Users..." → command not found），
    导致 filter 静默失败、全号直接入库。故此处用 Path(...).as_posix() 归一化。

    ⚠️ 路径必须加引号（2026-09-17 实测）：sys.executable 为
    "C:/Program Files/Python/python.exe" 这类**带空格**的路径时，sh 会按空格切成
    `C:/Program` + `Files/Python/...` → `line 1: C:/Program: No such file or directory`
    → filter 退出 127。而 `filter.maskphone.required` 缺省为 false，git **忽略失败、
    把未脱敏原文直接存进索引**，于是未认领全号进了暂存区（靠安全闸门才没推上去）。
    故：① 两段路径都用双引号包裹；② 显式置 required=true，让失败变成**响亮报错**
    而不是静默降级。
    """

    def _q(p: str) -> str:
        return '"' + str(p).replace('"', '\\"') + '"'

    py = str(Path(sys.executable).as_posix())
    script = str((SCRIPTS / "mask_phones.py").as_posix())
    cmd = f"{_q(py)} {_q(script)} --filter"
    rc, out = _git_run(["config", "--local", "--get", "filter.maskphone.clean"])
    if rc == 0 and out.strip() == cmd:
        # 已正确配置时也要保证 required 是 true（老配置/手工改过的情况）
        rc2, out2 = _git_run(["config", "--local", "--get", "filter.maskphone.required"])
        if rc2 == 0 and out2.strip().lower() == "true":
            return
    _git_run(["config", "--local", "filter.maskphone.clean", cmd])
    _git_run(["config", "--local", "filter.maskphone.smudge", "cat"])
    _git_run(["config", "--local", "filter.maskphone.required", "true"])


# 自检样本：一条带 11 位手机的极简记录（--path 让它命中 .gitattributes 的 maskphone）
_MASK_SELFTEST_SAMPLE = '{"id": "__mask_selftest__", "contact_phone": "13800138000"}'


def _selftest_mask_filter() -> None:
    """活体自检：确认 clean filter **真的会改写内容**，而不是「配了但没生效」。

    2026-09-17 事故复盘：filter 命令因 `C:/Program Files/` 带空格被 sh 切碎（退出 127），
    而当时 `filter.maskphone.required` 未设置 → git **忽略 filter 失败、把未脱敏原文
    直接写进索引**，444 个分片的未认领全号进了暂存区。最后是靠 step_git 末尾的安全闸门
    才没推上 GitHub——但那时整轮抓取（1h16m）已经跑完了。

    教训：**配置存在 ≠ filter 生效**。所以这里在 git add 之前用一条真实样本走完整链路，
    一旦 filter 没改写内容就立刻中止，把失败点提前到流水线第 1 分钟。

    实现：对同一样本分别算「不过 filter」和「过 filter」的 blob hash —— 相等即说明
    filter 是 no-op（失败/未配置），直接抛错。不需要写对象库，无副作用。
    """
    def _hash_obj(extra: list[str]) -> tuple[int, str]:
        p = subprocess.run(["git", "-C", str(ROOT), "hash-object", "--stdin", *extra],
                           input=_MASK_SELFTEST_SAMPLE, capture_output=True, text=True)
        return p.returncode, (p.stdout + p.stderr).strip()

    rc_raw, raw = _hash_obj([])
    rc_flt, filtered = _hash_obj(["--path", "data/gb/__mask_selftest__.json"])
    if rc_raw or rc_flt:
        raise RuntimeError(
            "maskphone filter 自检失败（hash-object 报错）：raw=%s filtered=%s" % (raw, filtered))
    if raw == filtered:
        raise RuntimeError(
            "maskphone clean filter 未生效：自检样本经 filter 后内容未变（仍含全号）。\n"
            "  filter 命令 = %s\n"
            "  常见原因：路径含空格未加引号、路径用了反斜杠、python 解释器不存在。\n"
            "  修复后重跑；在此之前**不要**绕过安全闸门提交。"
            % (_git_run(["config", "--local", "--get", "filter.maskphone.clean"])[1],))


# 独立的 11 位手机（前后非数字）；座机/400 含 - 不命中；已脱敏幂等。
_STANDALONE_MOBILE_RE = _re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_PHONE_FIELDS = ("contact_phone", "address", "address_en")


def _parse_records(content: str) -> list:
    """把暂存区文件内容（JSON 数组 / 单对象 / JSONL）解析成记录列表。

    用于安全闸门逐条核对手机号脱敏状态；解析失败的部分直接跳过（不误报）。
    """
    content = (content or "").strip()
    if not content:
        return []
    try:
        data = json.loads(content)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except Exception:
        pass
    # 退化为逐行 JSONL（如 phone-index.jsonl）
    recs: list = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            recs.append(obj)
    return recs


def _is_claimed(rec: dict) -> bool:
    """与 scripts/mask_phones.py 的 is_claimed 保持一致：claimed/verified 视为已认领。"""
    claim = rec.get("claim") if isinstance(rec, dict) else None
    if isinstance(claim, dict):
        return claim.get("status") in ("claimed", "verified")
    return False


def _staged_phone_masked_ok() -> bool:
    """安全闸门：抽查已暂存的 data/gb|en|phone-index，确认「未认领」手机号已脱敏。

    git clean filter 万一没生效（命令路径错/未配置），未认领记录会带着全号入库 → 必须拦下，
    绝不把全号推上 GitHub。返回 False 表示发现「未认领却仍是全号」的手机号。

    关键：claim.status=claimed/verified 的记录**允许保留全号**（用户决策 #1：认领后展示全号），
    这类全号不算违规；只有「未认领 / 无 claim」仍含 11 位全号才算脱敏失败。
    因此本闸门逐条解析记录，而不是简单正则全文匹配（后者会误杀已认领全号）。
    """
    rc, out = _git_run(["diff", "--cached", "--name-only", "--",
                        "data/gb", "data/en", "data/phone-index.jsonl"])
    if rc or not out.strip():
        return True  # 这些路径本轮无改动，无需校验
    for path in out.splitlines():
        rc2, content = _git_run(["show", f":{path}"])
        if rc2 or not content.strip():
            continue
        for rec in _parse_records(content):
            if not isinstance(rec, dict):
                continue
            if _is_claimed(rec):
                continue  # 已认领：按设计保留全号，不拦
            # 未认领记录：逐一核对手机号字段，凡仍含独立 11 位全号即判脱敏失败
            # （覆盖单号、多号、'座机; 手机'组合，以及 address/address_en 内嵌号码）
            for f in _PHONE_FIELDS:
                v = rec.get(f)
                if isinstance(v, str) and _STANDALONE_MOBILE_RE.search(v):
                    return False  # 未认领却仍是全号 → 脱敏失败
    return True


def step_git(dry_run: bool = False) -> None:
    """把本轮重建出来的 L0 数据提交并推送到 GitHub.

    为什么必须有这一步（2026-09-14 复盘）：抓后流水线一直只做「派生层重建 → 上传 R2
    → 发布 Pages」，从没有一步把 L0 数据回写 git。于是 jsDelivr（乃至 Pages 的 L0 镜像）
    永远停在旧快照——本地 584 个分片、GitHub HEAD 只有 346 个，新门类（H/I/M/O/R）
    整批进不了 App。用户端表现就是「主源不通 / 数据永远是旧的那版」。

    **只提交 L0 数据目录**，不碰源码、文档、APK、.backup_legacy 等：抓取流水线
    的职责是数据，源码改动由开发者单独 review 提交。

    推送走仓库已配好的 core.sshCommand（中文路径 id_ed25519 + 跳过 known_hosts），
    不需要额外的凭据。没有改动就静默跳过（不是失败）。
    """
    # L0_PATHS 见模块顶部（GUI / cron 共用同一份）

    # 手机号脱敏保障：确保 maskphone clean filter 已在本地配置（首次运行自动写入），
    # 否则 git add 会把全号直接入库。
    _ensure_mask_filter()
    _selftest_mask_filter()  # filter 活体自检：在 add 之前就拦下「配了但没生效」

    def _g(args: list[str]) -> tuple[int, str]:
        p = subprocess.run(["git", "-C", str(ROOT)] + args,
                           capture_output=True, text=True)
        return p.returncode, (p.stdout + p.stderr).strip()

    # 先看这些路径里有没有改动，没有就别硬提交
    # 注意：L0_PATHS（运行时数据源）和 L0_DERIVED（被 validate 对账的派生/文档）都要纳入，
    # 否则 README/DATA_STATS/industry-index 滞后会让 CI 的 --strict 对账失败。
    rc, out = _g(["status", "--porcelain", "--", *L0_PATHS, *L0_DERIVED])
    if rc:
        raise RuntimeError(f"git status 返回 {rc}: {out}")
    changed = [ln for ln in out.splitlines() if ln.strip()]
    if not changed:
        print("   （跳过：L0 数据无改动）")
        return
    if dry_run:
        print("   （--dry-run：以下 L0 改动不提交，仅预览）")
        for ln in changed[:20]:
            print("     " + ln)
        if len(changed) > 20:
            print(f"     … 还有 {len(changed) - 20} 条")
        return

    # 仅 add 指定的 L0 路径 + 派生文档（绝不用 git add -A，避免把源码/APK 一起卷进去）
    # 含手机号的路径（data/gb / data/en / phone-index）用 --renormalize 强制重跑 clean
    # filter，保证「已入库的全号」在首次提交时被转成脱敏版（普通 add 对未改内容的文件是 no-op，
    # 不会重新脱敏）。其余路径（不含手机号，如 manifest/index）普通 add 即可。
    _MASK_PATHS = ("data/gb", "data/en", "data/phone-index.jsonl")
    _other_paths = [p for p in (*L0_PATHS, *L0_DERIVED) if p not in _MASK_PATHS]
    for p in _MASK_PATHS:
        # --renormalize 强制重跑 clean filter（把已入库的全号转成脱敏版）。
        # ⚠️ 但它【不会】暂存「本轮新增的未跟踪分片」——若只靠它，新抓取的供应商
        # 根本进不了暂存区（既不脱敏也不入库）。所以再补一次普通 git add，
        # 把新文件也纳入（普通 add 同样会跑 clean filter → 脱敏后入库）。
        subprocess.run(["git", "-C", str(ROOT), "add", "--renormalize", "--", p],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(ROOT), "add", "--", p],
                       capture_output=True, text=True)
    for p in _other_paths:
        subprocess.run(["git", "-C", str(ROOT), "add", "--", p],
                       capture_output=True, text=True)

    # ── 安全闸门：确认入库的手机号已脱敏，否则绝不提交/推送全号 ──
    if not _staged_phone_masked_ok():
        raise RuntimeError(
            "安全闸门：暂存区 data/gb|en|phone-index 仍含未脱敏的 11 位手机号，"
            "疑似 maskphone clean filter 未生效。已中止提交，未推送任何全号。"
            "请检查 `git config --local filter.maskphone.clean` 是否指向 scripts/mask_phones.py。")

    rc, out = _g(["commit", "-m",
                  "chore(data): 自动同步 L0 分片（%d 个文件）" % len(changed)])
    if rc:
        # commit 在没东西可提交时会返回 1，但这里已经确认有改动；真失败就抛出
        raise RuntimeError(f"git commit 返回 {rc}: {out}")
    print("   [git] 已提交 L0 数据 %d 个文件" % len(changed))
    rc, out = _g(["push", "origin", "main"])
    if rc:
        # 推送失败不要把已提交的数据丢掉，但要让流水线知道没同步上去
        raise RuntimeError(f"git push 返回 {rc}: {out}")
    print("   [git] 已推送到 origin/main")


_STEP_FN = {
    "classify": step_classify,
    "gbindex": step_gbindex,
    "enrefile": step_enrefile,
    "recat": step_recat,
    "index": step_index,
    "fingerprint": step_fingerprint,
    "searchindex": step_searchindex,
    "english": step_english,
    "autoprofile": step_autoprofile,
    "capability": step_capability,
    "shards": step_shards,
    "manifest": step_manifest,
    "git": step_git,
    "assets": step_assets,
    "readme": step_readme,
    "validate": step_validate,
    "r2": step_r2,
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

# ─────────────────────────────────────────────────────────────── 发布互斥锁
# 2026-09-16 补：cron（每日）/ GUI（手动）/「回填后自动发布」定时任务都会调本模块，三者都跑
# postfetch.py 写同一批派生层文件（shards / manifest / Pages / git）。两个 postfetch 并发时，
# 中间态可能互相覆盖 → 发表单或清单损坏。这里用一把咨询锁把「整次发布」互斥掉，
# 与 en_backfill 的 beacon_mfg_en.lock 各管一段（翻译 vs 发布），互不干扰。
# 语义对齐 en_backfill：拿不到锁就**跳过本轮**（非阻塞、不死锁）；进程被强杀 OS 自动放锁。
_PUBLISH_LOCK_FD = None
_PUBLISH_LOCK_PATH = Path(tempfile.gettempdir()) / "beacon_mfg_publish.lock"


def acquire_publish_lock(timeout: int = 900) -> bool:
    """拿到发布互斥锁返回 True；timeout 秒内仍被别的进程持有则返回 False（本轮跳过）。

    - BMFG_PUBLOCK_PARENT=1：父进程（如定时发布脚本）已持锁并会传给子进程，
      子进程 postfetch 直接放行，避免重复加锁把自己挡在门外。
    - BMFG_PUBLISH_LOCK=0：逃生阀，手动要并发时关闭本锁。
    """
    global _PUBLISH_LOCK_FD
    if os.environ.get("BMFG_PUBLOCK_PARENT") == "1":
        return True
    if _PUBLISH_LOCK_FD is not None:
        return True
    if os.environ.get("BMFG_PUBLISH_LOCK") == "0":
        return True
    try:
        fd = os.open(str(_PUBLISH_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return True  # 连锁文件都建不了就放行，别把正事挡在外面
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    if msvcrt is not None:
        deadline = time.time() + timeout
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                if time.time() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(5)
                continue
            _PUBLISH_LOCK_FD = fd
            atexit.register(release_publish_lock)
            return True
    return True


def release_publish_lock() -> None:
    global _PUBLISH_LOCK_FD
    if _PUBLISH_LOCK_FD is None:
        return
    try:
        import msvcrt
        msvcrt.locking(_PUBLISH_LOCK_FD, msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        os.close(_PUBLISH_LOCK_FD)
    except Exception:
        pass
    _PUBLISH_LOCK_FD = None


def _warn_if_worktree_masked() -> int:
    """工作树全号体检：data/gb + phone-index 里掩码号突然变多就大声报警。

    为什么需要它：`git checkout/restore` 作用在 maskphone 过滤的路径上时，git 会把
    **入库的脱敏版**写回工作树，而 `git status` 依旧显示干净
    （因为 clean(全号) == clean(脱敏)，这正是脱敏设计的副作用）——事故完全无声。
    2026-09-17 实测：一次 `git checkout -- data/gb/` → 42578 个号码变掩码，
    紧接着 sync_assets 依此重建 → App 内置索引与 Pages 产物一起被脱敏。
    正常水位是个位数（源里查不到全号的残差），阈值取 300 留足余量。
    """
    total = 0
    pat = _re.compile(r"\d{3}\*{4}\d{4}")   # 掩码手机：138****0000
    for p in (ROOT / "data" / "gb").rglob("*.json"):
        try:
            total += len(pat.findall(p.read_text(encoding="utf-8")))
        except OSError:
            continue
    idx = ROOT / "data" / "phone-index.jsonl"
    if idx.exists():
        total += len(pat.findall(idx.read_text(encoding="utf-8", errors="replace")))
    if total > _WORKTREE_MASK_TOLERANCE:
        print("⚠" * 30)
        print(f"⚠ 工作树全号体检不通过：data/gb + phone-index 里有 {total} 个掩码手机号")
        print(f"  （正常水位 <{_WORKTREE_MASK_TOLERANCE}）")
        print("  多半是有人对 data/gb / data/phone-index.jsonl 跑过 git checkout / git restore：")
        print("  那会把入库的脱敏版写回工作树，而 git status 照样显示干净。")
        print("  继续跑会把脱敏号码重建进索引并发到 App（卡片显示 138****0000、无法拨号）。")
        print("  恢复：python scripts/restore_full_phones.py --apply")
        print("⚠" * 30)
    return total


def run(skip: set[str] | frozenset[str] | list[str] | None = None,
        only: set[str] | frozenset[str] | list[str] | None = None,
        dry_run: bool = False,
        quiet: bool = False,
        autoprofile_cities: str | None = None,
        autoprofile_limit: int | None = None,
        english: bool = False) -> int:
    """跑完（或部分跑完）抓后流水线。返回失败步数（0 = 全通过）。

    autoprofile_cities: 逗号分隔的城市名；非空时 autoprofile 步会调 batch_auto_profile
    给这些城市补「未认证」能力卡。为空则 autoprofile 步跳过（默认行为，避免每轮写大量新文件）。
    autoprofile_limit: 每城最多补多少家；None = 用 batch_auto_profile 自己的默认（300）。
    english: 打开英文镜像（en_backfill + en_sync_industry）。默认关。
    """
    global _AUTOPROFILE_CITIES, _AUTOPROFILE_LIMIT, _DO_ENGLISH
    _AUTOPROFILE_CITIES = autoprofile_cities
    _AUTOPROFILE_LIMIT = autoprofile_limit
    _DO_ENGLISH = bool(english)
    skip = set(skip or ())
    if only:
        steps = [s for s in ALL_STEPS if s in set(only)]
    else:
        steps = [s for s in ALL_STEPS if s not in skip]

    if not steps:
        if not quiet:
            print("抓后流水线：没有要执行的步骤")
        return 0

    # 发布互斥：与 cron / GUI / 定时任务串行，避免并发写派生层（shards/manifest/Pages/git）
    if not acquire_publish_lock():
        if not quiet:
            print("⊘ 另一个发布流程（cron / GUI / 定时任务）正在运行，本实例跳过，"
                  "稍后由对方或下个周期完成发布。")
        return 0

    if not quiet:
        print("\n" + "═" * 62)
        print("抓后流水线：%d 步（%s）" % (len(steps), " → ".join(steps)))
        print("═" * 62)

    _LAST_FAILED.clear()

    # 工作树全号体检：data/gb|data/en|phone-index 本该是全号（见 .gitattributes），
    # 一旦大面积出现掩码，说明有人对它们跑过 git checkout/restore（git status 仍是干净的，
    # 靠 status 发现不了）。此刻重建索引 = 把脱敏结果发到 App，必须当场喊出来。
    _warn_if_worktree_masked()

    failed: list[str] = []
    skipped: list[str] = []
    for i, name in enumerate(steps, 1):
        # 发布这道闸：validate 没过（或压根没跑）就不许上线。
        # run() 默认是「失败不中断」，不拦的话校验红灯的产物也会被推到生产。
        # 要单独发布就直跑 scripts/deploy_pages.py。
        if name in ("r2", "pages") and ("validate" in failed or "validate" not in steps):
            skipped.append(name)
            print("   ⊘ 跳过：本轮 validate 未通过或未执行，不把未校验的产物推上线"
                  "（要单独跑请直跑 scripts/publish_r2.py / deploy_pages.py）")
            continue
        # git 这道闸（2026-09-16 加）：以前 git 排在 validate **之前**，等于「先上榜、
        # 后体检」，未经校验的 L0 直接进了 GitHub → jsDelivr 备源拿到脏数据。
        # 现在 git 是最后一步，且再次确认 validate 确实跑过并通过。
        if name == "git" and ("validate" in failed or "validate" not in steps):
            skipped.append(name)
            print("   ⊘ 跳过：本轮 validate 未通过或未执行，不把未校验数据推 GitHub")
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
    ap.add_argument("--english", action="store_true",
                    help="跑英文镜像：调用 en_backfill + en_sync_industry（默认关："
                         "慢且依赖 ZHIPU_API_KEY）。开启后它排在 shards/manifest 之前，"
                         "当轮就能随 r2/pages 一起发布")
    ap.add_argument("--autoprofile-cities", default="",
                    help="逗号分隔的城市名；非空时 autoprofile 步给这些城市补未认证能力卡"
                         "（对应 GUI「自动补能力卡」勾选框 / fetch_batch --autoprofile）")
    ap.add_argument("--autoprofile-limit", type=int, default=None,
                    help="每城最多补多少家（默认 300，由 batch_auto_profile 决定）")
    args = ap.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    bad = (skip | only) - set(ALL_STEPS)
    if bad:
        print("未知步骤：%s（可选 %s）" % ("、".join(sorted(bad)), ",".join(ALL_STEPS)))
        return 2

    return 1 if run(skip=skip, only=only, dry_run=args.dry_run,
                    autoprofile_cities=args.autoprofile_cities or None,
                    autoprofile_limit=args.autoprofile_limit,
                    english=args.english) else 0


if __name__ == "__main__":
    raise SystemExit(_main())
