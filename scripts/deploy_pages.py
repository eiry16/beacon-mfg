#!/usr/bin/env python3
"""组装 dist/site 并部署到 Cloudflare Pages（用 wrangler，不再手写 API）。

为什么改用 wrangler
-------------------
2026-09-10 踩过一次大的：手写 multipart 打
`POST /accounts/{a}/pages/projects/{p}/deployments`（manifest + 每文件以 hash
为字段名）**接口返回 success、deployment 也建出来了，但资产从未真正落库**，
取的时候边缘层 500（空 body，CF-RAY 打到法兰克福）。更糟的是它把上一个能用的
部署（08c9bc7c，4136 家 SKILL.md 真实可下载）覆盖成了只有 1 个 index.html 的
空壳 —— 全站 500，4137 家全部取不到。

官方现行协议（cloudflare/workers-sdk，packages/wrangler/src/pages/upload.ts）
是 `upload-token → check-missing → assets/upload(JSON 数组 + base64) →
upsert-hashes → deployments` 五步，没文档、会变、且失败时假装成功。
**结论：不要手写官方协议。** wrangler 直接读 CLOUDFLARE_API_TOKEN /
CLOUDFLARE_ACCOUNT_ID，不需要交互登录，也不多一个要维护的凭据。
（上一版脚本里"不用 wrangler 少一个凭据"的理由是错的，已在事故里证伪。）

站点里装什么
------------
    dist/site/
      index.html                      人类入口
      data/manifest.json              L0 指纹/档案分片清单（App 主源，2026-09-14 起从 jsDelivr 迁到 Pages）
      data/**                        L0 完整档案（gb/ 中文、en/ 英文）、号码索引、endpoint 指针
      skills/registry/fingerprint/** L0 指纹增量分片（updateFingerprints 按需拉）
      skills/vendors/{id}/SKILL.md    L2 自述 ← App + 客户 agent 都能拉的一层
      full/gb/**                      L1 完整能力卡分片（对外 Agent 按需拉）
      slim/gb/**                      L1 精简分片
      manifest.json                   分片清单（L1）

App 拉的是 `capabilityBase + "/" + skillPath`，skillPath =
`skills/vendors/{id}/SKILL.md`（ChatScreen.kt:608 / Model.kt:188）。
旧部署 08c9bc7c 实测也只有 SKILL.md（capability.json、data/manifest.json
全是 404）。full/ slim/ 是给外部 Agent 用的完整卡，SettingsRepo 注释里也写了
Pages 承担这两层，所以一并传 —— 总共约 4200 文件 / 24 MB，远低于 Pages 上限。

能力卡分片交给 `scripts/gen_capability_shards.py` 产，**不要自己拼**。

⚠ 根因（比部署协议更重要）
--------------------------
耐特斯 CN-MFG-0020317 404 的真因不是"从来没传过"，是**发布快照是一次性手工
动作**：它当天才建档，而 09-09 的部署快照早于它 → URL 在大盘上不存在。
App 侧的 HEAD 探活只是止血。要根治，部署必须能被低成本重复触发 —— 这就是
本脚本存在的意义：新供应商建档后重跑一次即可，不用等人记得。

用法
----
    python scripts/deploy_pages.py --build     # 只组装 dist/site，不上传
    python scripts/deploy_pages.py             # 组装 + 部署 + 验证（默认）
    python scripts/deploy_pages.py --verify    # 只做部署后抽样验证
    python scripts/deploy_pages.py --deploy-only  # 跳过组装直接部署
    python scripts/deploy_pages.py --dry-run   # 只打印 wrangler 命令

环境变量：CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID（写在 .env 里，
**等号两侧不要留空格** —— bash 会 command not found，Python 取到带尾空格的
键名，不报错只静默失效）。wrangler / node 路径可用 WRANGLER_JS / NODE_BIN 覆盖。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGES_SRC = REPO_ROOT / "pages"
VENDORS = REPO_ROOT / "skills" / "vendors"
DIST = REPO_ROOT / "dist" / "site"

PROJECT_NAME = "beacon-mfg"
BRANCH = "main"
BASE_URL = "https://beacon-mfg.pages.dev"

def _resolve_node_bin() -> str:
    """解析 node 可执行文件。

    别硬编码版本目录：托管 runtime 升级会把 22.22.2-2 换成 22.22.2-3，
    旧路径消失后 wrangler 子进程会 FileNotFoundError [WinError 2]（09-12 踩过）。
    顺序：env NODE_BIN → 扫 ~/.workbuddy/binaries/node/versions/*/node.exe 取最高版本 → PATH。
    """
    env_bin = os.environ.get("NODE_BIN")
    if env_bin and Path(env_bin).exists():
        return env_bin

    versions_dir = Path(os.path.expanduser("~/.workbuddy/binaries/node/versions"))
    if versions_dir.is_dir():
        def _key(p: Path) -> tuple:
            nums = []
            for part in p.name.replace("-", ".").split("."):
                nums.append(int(part) if part.isdigit() else -1)
            return tuple(nums)
        cands = [p for p in versions_dir.glob("*/node.exe") if p.exists()]
        if cands:
            return str(max(cands, key=lambda p: _key(p.parent)))

    return shutil.which("node") or "node"


NODE_BIN = _resolve_node_bin()
WRANGLER_JS = os.environ.get(
    "WRANGLER_JS",
    os.path.expanduser(r"~/.workbuddy/binaries/node/workspace/node_modules/wrangler/bin/wrangler.js"))


def _robust(op: str, fn, *args, tries: int = 8, delay: float = 0.5, **kw):
    """吞掉 Windows 实时杀毒在批量建目录/写文件时的瞬时 WinError 5（拒绝访问）。

    2026-09-14 实测：一次性 `mkdir` 几千个 vendors 子目录时，Defender 实时扫描会
    短暂锁住 `skills/vendors` 目录，导致其中某一个 `mkdir` 抛 PermissionError，
    整次部署因此中断（前面几千个都已建好，却卡在中间一个）。重试几次即可绕过。
    """
    last = None
    for i in range(tries):
        try:
            return fn(*args, **kw)
        except (PermissionError, OSError) as e:
            last = e
            if i < tries - 1:
                time.sleep(delay)
                continue
            raise
    raise last


def load_env() -> tuple[str, str]:
    """读 .env。键和值都要 strip（09-10 踩过等号带空格 → 静默失效的坑）。"""
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not token or not account:
        print("✗ 缺少 CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID（写在 .env 里）",
              file=sys.stderr)
        sys.exit(1)
    return token, account


# ── 行尾归一化（LF）────────────────────────────────────────────────────
# ⚠ 这是**全流水线唯一**一处行尾归一化，任何要发布给 App / 客户 Agent 的文本
# 都必须过这一关，不许直接 `shutil.copy2`。
#
# 为什么：仓库 core.autocrlf=true，Windows 工作树是 CRLF，而 manifest 里每个分片的
# 哈希 h 是按 **LF 归一化后** 算的（gen_manifest.sha1_of 内部 replace \r\n → \n）。
# 客户端（App 的 DataStore.writeShard、客户 Agent）拿到内容后直接算 SHA1 比对，
# **不会**再做归一化 —— 所以只要发布出去的是 CRLF，哈希就永远对不上。
# 后果不是报错，是「静默丢弃」：App 按「宁可留旧数据」把分片丢掉，
# 2026-09-14 实测 108 片只成功 9 片，且重试无效（字节本身不对，不是网络问题）。
#
# 2026-09-15 排查「主源不通」时复核了一遍流水线：归一化原本只覆盖 step 3.5 的
# data/** 与 fingerprint/**，L2 的 SKILL.md（step 3）走的是 copy2 —— 只是因为
# 目前没人校验 md 的哈希才没出事。现在统一收敛到这里。
TEXT_SUFFIXES = {".jsonl", ".json", ".md", ".txt", ".csv", ".xml", ".yaml", ".yml"}


def _write_lf(src: Path, dst: Path) -> bool:
    """把 src 拷到 dst；文本文件强制按 LF 落盘。返回是否做了归一化。"""
    if src.suffix.lower() in TEXT_SUFFIXES:
        data = src.read_bytes()
        if b"\r\n" in data:
            _robust("write", dst.write_bytes, data.replace(b"\r\n", b"\n"))
            return True
        _robust("write", dst.write_bytes, data)
        return False
    _robust("copy2", shutil.copy2, src, dst)
    return False


# manifest 里合法条目的类型码（见 gen_manifest.TYPE_*）。白名单同时挡住
# 清单里那行「列名说明」（它也有 p/h 字段，但 t 是整句中文说明）。
_MANIFEST_TYPES = {"fp", "zh", "en", "pq", "phone"}


def _check_manifest_consistency(dist: Path) -> int:
    """发布前闸门：manifest 登记的 sha1 必须等于**即将上传**文件的 sha1。

    对不上 = 清单与内容差了一代。App 端 writeShard / writePhoneIndex 会逐字节校验 sha1，
    不匹配就**静默丢弃**（不报错、不退版本号、下次启动再试一遍照样失败），
    表现为「后台数据明明更新了，手机端却一直用旧版」。

    2026-09-17 线上实证：manifest 的 phone 条目 k=56988/u=9-16，而实际提供的
    data/phone-index.jsonl 已是 83840 条 → 手机端号码索引永远更新不上，新抓城市
    （贵阳等）的企业卡片一律显示「☎ 有电话，但源数据为『待核实』」。
    根因是流水线顺序（manifest 排在 assets 之前，记的是上一代索引的哈希），已修；
    这道闸门保证谁再改坏顺序都发不出去。
    """
    man = dist / "data" / "manifest.json"
    if not man.exists():
        return 0
    try:
        spec = json.loads(man.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"✗ manifest 解析失败：{exc}", file=sys.stderr)
        sys.exit(1)

    entries: list[dict] = []

    def walk(o) -> None:
        if isinstance(o, dict):
            if o.get("t") in _MANIFEST_TYPES and "p" in o and "h" in o:
                entries.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(spec)

    bad: list[tuple[str, str]] = []
    for e in entries:
        rel = str(e.get("p") or "").lstrip("/")
        f = dist / rel
        if not f.exists():
            bad.append((rel, "dist 里没有这个文件"))
            continue
        h = hashlib.sha1(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        if h != str(e.get("h")):
            bad.append((rel, f"manifest={str(e.get('h'))[:10]} 实际={h[:10]}"
                             f"（manifest 登记 k={e.get('k')}）"))
    if bad:
        print("✗ 发布前一致性闸门不通过：manifest 与内容对不上，"
              "App 会逐片校验失败并静默丢弃：", file=sys.stderr)
        for rel, why in bad[:10]:
            print(f"    {rel}: {why}", file=sys.stderr)
        print("  → 先跑 python scripts/postfetch.py --only assets,manifest,validate "
              "让清单重新对齐（步骤顺序必须 assets 在 manifest 之前）", file=sys.stderr)
        sys.exit(1)
    if entries:
        print(f"  ✓ manifest 一致性：{len(entries)} 条全部与待上传内容吻合")
    return len(entries)


def build(verbose: bool = True, with_worker: bool = False) -> dict:
    """组装 dist/site。返回统计。"""
    t0 = time.time()

    # 1) 覆盖式写入，不做批量删除
    #    为什么不先清空：09-10「gen_capability_shards 删除风暴」的教训 —— 任何批量
    #    删除保护/杀软都会让流水线红掉，且删完到写完之间有窗口让读方 404。
    #    这里更进一步：环境的 safe-delete 会拦截 rmtree（实测删 dist/site/full
    #    直接抛 OSError）。所以只覆盖、只增补，孤儿（供应商已下线但仍留在 dist 里的
    #    文件）**只报告不删**，由人决定。
    DIST.mkdir(parents=True, exist_ok=True)

    # 2) 人类入口 + 404 页（pages/*.html）
    #    404.html 不只是好看：Pages 对未匹配路径默认**回退 index.html 并返回 200**，
    #    有了 404.html 才能给出真的 404 —— App 的 HEAD 探活靠这个区分
    #    「已发布 / 还没进快照」。
    for html in sorted(PAGES_SRC.glob("*.html")):
        shutil.copy2(html, DIST / html.name)

    # Pages Functions 入口（P0 第 4 步）：/skills/** 转发到 R2，其余走静态资源。
    #
    # ⚠ 默认**不带** worker，必须显式 `--with-worker`：
    #   Pages 的 R2 binding 没有公开写入 API（PATCH 一律 400），只能在 Dashboard 配。
    #   binding 没配上就把 _worker.js 传上去 = /skills/** 全部 503（env.CAPS 判空分支），
    #   而静态首页还是 200 —— 看起来"没坏"，实际 App 里 4137 家全打不开。
    #   2026-09-10 已经因为部署把站点搞挂过一次，这里不能再赌。
    #
    # 配好 binding 后的启用方式：
    #   python scripts/deploy_pages.py --with-worker
    worker = PAGES_SRC / "_worker.js"
    toml = PAGES_SRC / "wrangler.toml"
    if with_worker and worker.exists():
        shutil.copy2(worker, DIST / "_worker.js")
        if toml.exists():
            shutil.copy2(toml, DIST / "wrangler.toml")
        if verbose:
            print("  Functions 入口：_worker.js（/skills/** → R2 binding CAPS）")
    else:
        # 静态模式：确保 dist 里没有残留的 worker（否则会静默接管路由）
        for stray in ("_worker.js", "wrangler.toml"):
            f = DIST / stray
            if f.exists():
                f.unlink()
                if verbose:
                    print(f"  移除 dist 里的 {stray}（静态模式不需要，留着会接管 /skills）")

    # 3) L2 自述：skills/vendors/{id}/SKILL.md —— App 唯一依赖的一层
    n_skill, n_skill_lf, missing = 0, 0, []
    written_ids: set[str] = set()
    if VENDORS.exists():
        for vendor_dir in sorted(VENDORS.iterdir()):
            if not vendor_dir.is_dir():
                continue
            src = vendor_dir / "SKILL.md"
            if not src.exists():
                missing.append(vendor_dir.name)
                continue
            dst_dir = DIST / "skills" / "vendors" / vendor_dir.name
            _robust("mkdir", dst_dir.mkdir, parents=True, exist_ok=True)
            # ⚠ 不能 copy2：SKILL.md 是文本，工作树 CRLF 会原样上云。
            # 目前 App / 客户 Agent 都不校验 md 的哈希所以没出事，但只要哪天加了校验
            # 就是 09-14 那个「分片全被静默丢弃」的复刻。统一走 _write_lf。
            if _write_lf(src, dst_dir / "SKILL.md"):
                n_skill_lf += 1
            written_ids.add(vendor_dir.name)
            n_skill += 1

    # 3.5) L0 指纹分片 + 完整档案 + 索引：data/** 与 skills/registry/fingerprint/**
    #     —— 这一层原本只挂在 jsDelivr(GitHub) 上，App 的 dataBase 默认就指向它。
    #     2026-09-14 复盘：手机端（即使开了 flash 代理）连不上 fastly.jsdelivr.net，
    #     但 beacon-mfg.pages.dev 可达。所以把 L0 也拷进 Pages，让 dataBase 改指 Pages，
    #     主源从「手机连不上的 jsDelivr」换成「手机已实测可达的 Pages」。
    #     ⚠ App 只按需拉单个分片（fp/zh/en/phone），不会一次下完这 ~84MB；
    #       Pages 承载的是「按需取」而非「整包下」，体量不是问题。
    #     覆盖式写入、不删除（与全脚本一致：环境的 safe-delete 会拦截 rmtree）。
    DATA_SRC = REPO_ROOT / "data"
    FP_SRC = REPO_ROOT / "skills" / "registry" / "fingerprint"
    # ⚠ 文本文件必须按 LF 落盘，不能原样拷贝。
    # 仓库 core.autocrlf=true：工作树 CRLF → git 存 LF。manifest 里每个分片的 sha1（h）
    # 也是按 **LF 归一化后** 算的（见 gen_manifest.sha1_of），jsDelivr 从 git 取、下发 LF，
    # 所以历史上一路匹配。但 Windows 工作树是 CRLF，若用 copy2 原样拷上 Pages，
    # App 下载到 CRLF 内容 → 算出的哈希与 manifest 的 h 不符 → writeShard 校验失败
    # → 整轮增量更新作废（2026-09-14 实测 108 片只成功 9 片，且重试无效——
    # 因为字节本身不对，不是网络丢包）。
    n_l0 = 0
    n_l0_lf = 0
    for src_root, dst_root in ((DATA_SRC, DIST / "data"),
                               (FP_SRC, DIST / "skills" / "registry" / "fingerprint")):
        if not src_root.exists():
            continue
        for f in src_root.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(src_root)
            dst = dst_root / rel
            _robust("mkdir", dst.parent.mkdir, parents=True, exist_ok=True)
            if _write_lf(f, dst):  # 文本后缀 → 强制 LF；其余原样拷
                n_l0_lf += 1
            n_l0 += 1
    if verbose:
        print(f"  L0 {n_l0} 文件（其中 {n_l0_lf} 个做了 CRLF→LF 归一化）")

    # 3.6) 发布前一致性闸门：manifest 的每条 sha1 必须等于刚拷进 dist 的内容。
    #      放在这里（data/** 刚拷完、能力卡分片之前）——最早能看到「清单/内容两代」的位置。
    _check_manifest_consistency(DIST)

    # 4) L1 能力卡分片（full/ slim/ manifest.json）—— 交给专业脚本，不自己拼
    shard = REPO_ROOT / "scripts" / "gen_capability_shards.py"
    if shard.exists():
        # ⚠ 必须带 --apply：该脚本默认是**预览模式不写盘**，且退出码仍是 0。
        #    漏了这个参数 = full/ slim/ manifest.json 静默停留在上一版，
        #    而 stdout 照样打出漂亮的统计（09-10 踩过：4181 文件里分片全是旧的）。
        proc = subprocess.run(
            [sys.executable, str(shard), "--out", str(DIST), "--apply"],
            cwd=str(REPO_ROOT), capture_output=True, text=True)
        if proc.returncode != 0:
            print("✗ gen_capability_shards.py 失败：", file=sys.stderr)
            print(proc.stdout[-2000:], "\n", proc.stderr[-2000:], file=sys.stderr)
            sys.exit(1)
        if "预览模式" in proc.stdout or "未写盘" in proc.stdout:
            print("✗ gen_capability_shards.py 仍在预览模式，分片没写盘", file=sys.stderr)
            sys.exit(1)
        if verbose:
            for line in proc.stdout.strip().splitlines()[-4:]:
                print(f"  {line}")
    else:
        print("⚠ 没找到 gen_capability_shards.py，跳过 full/ slim/ 分片")

    # 孤儿：dist 里还留着、但源里已经没有的供应商（覆盖式写入不会自动清）
    dist_vendor_root = DIST / "skills" / "vendors"
    orphans = sorted(
        {p.parent.name for p in dist_vendor_root.glob("*/SKILL.md")} - written_ids
    ) if dist_vendor_root.exists() else []

    files = [f for f in DIST.rglob("*") if f.is_file()]
    size_mb = sum(f.stat().st_size for f in files) / 1e6
    stats = {
        "skill_md": n_skill,
        "l0_files": n_l0,
        "missing_skill_md": missing,
        "orphans": orphans,
        "files": len(files),
        "mb": round(size_mb, 2),
        "seconds": round(time.time() - t0, 1),
    }
    if verbose:
        print(f"\n✓ dist/site 组装完成：{n_skill} 份 SKILL.md（{n_skill_lf} 份做了 LF 归一化）· "
              f"{n_l0} 个 L0 文件（{n_l0_lf} 个做了 LF 归一化）· "
              f"共 {len(files)} 文件 · {size_mb:.1f} MB · {stats['seconds']}s")
        if missing:
            print(f"⚠ {len(missing)} 家缺 SKILL.md：{missing[:5]}"
                  f"{' …' if len(missing) > 5 else ''}")
        if orphans:
            print(f"⚠ {len(orphans)} 个孤儿目录（源里已无，dist 仍会上传）："
                  f"{orphans[:5]}{' …' if len(orphans) > 5 else ''}")
    return stats


def deploy(dry_run: bool = False, branch: str = BRANCH) -> int:
    token, account = load_env()

    if (DIST / "_worker.js").exists():
        print("⚠ 本次部署带 _worker.js —— /skills/** 将改由 R2 binding CAPS 提供。"
              "binding 没配上这些路径会全站 503（首页仍 200，最容易看漏）。")

    if not Path(WRANGLER_JS).exists():
        print(f"✗ 找不到 wrangler：{WRANGLER_JS}", file=sys.stderr)
        sys.exit(1)

    cmd = [
        NODE_BIN, WRANGLER_JS, "pages", "deploy", str(DIST),
        "--project-name", PROJECT_NAME,
        "--branch", branch,
        # 工作区几乎总有未提交改动，不打这个标 wrangler 每次都要刷一行警告
        "--commit-dirty=true",
    ]
    env = dict(os.environ)
    env["CLOUDFLARE_API_TOKEN"] = token
    env["CLOUDFLARE_ACCOUNT_ID"] = account
    env["WRANGLER_SEND_METRICS"] = "false"

    print("› " + " ".join(cmd))
    if dry_run:
        print("（--dry-run，未执行）")
        return 0

    return subprocess.run(cmd, cwd=str(DIST), env=env).returncode


def _sample_ids() -> list[str]:
    """从 dist 里挑真实存在的 id（首/中/尾 + 固定关注的两家）。

    ⚠ 不要写死 id 当样本：4137 家里挑一个号段中不存在的号（比如 CN-MFG-0012000）
    会得到 index.html + 200（见下方 SPA 回退），验证就变成假绿。
    """
    vroot = DIST / "skills" / "vendors"
    ids = sorted(p.parent.name for p in vroot.glob("*/SKILL.md")) if vroot.exists() else []
    if not ids:
        return ["CN-MFG-0020317", "CN-MFG-0000005"]
    pick = [ids[0], ids[len(ids) // 2], ids[-1]]
    for fixed in ("CN-MFG-0020317", "CN-MFG-0000005"):  # 耐特斯 + 旧部署基准
        if fixed in ids:
            pick.append(fixed)
    # 去重保序
    return list(dict.fromkeys(pick))


def _fetch(path: str, tries: int = 3, ua: str = "beaconmfg-verify/1.0") -> tuple[int, bytes, str]:
    """取一个路径，失败退避重试。

    ⚠ 为什么要重试（2026-09-14 事故）：部署刚结束就校验，边缘节点还没同步完，
    刚传上去的 SKILL.md 会短暂回退成 index.html → 被判成"该路径其实不存在"，
    postfetch 整条流水线因此报 pages 失败，**实际上部署是好的**（隔 1 分钟重跑
    --verify 全绿）。真 404 重试 N 次仍然是 404，所以重试只会吃掉 CDN 同步延迟，
    不会把真问题洗成绿的。
    """
    last = (0, b"", "")
    for k in range(tries):
        try:
            headers = {"Cache-Control": "no-cache"}
            if ua:  # ua=None → 完全不发 User-Agent 头（模拟 OkHttp 默认行为）
                headers["User-Agent"] = ua
            req = urllib.request.Request(BASE_URL + path, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.status, r.read(), ""
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:  # noqa: BLE001
                body = b""
            if e.code == 404 and k < tries - 1:
                time.sleep(4 * (k + 1))
                last = (e.code, body, "")
                continue
            return e.code, body, ""
        except Exception as e:  # noqa: BLE001
            last = (0, b"", f"{type(e).__name__}: {e}")
            if k < tries - 1:
                time.sleep(4 * (k + 1))
                continue
            return last
    return last


def verify() -> int:
    """抽样验证。**只有全绿才敢写「404 已修」。**

    ⚠ Pages 的 SPA 回退：路径不存在时**不返回 404，而是返回 index.html + 200**
    （2026-09-10 实测：CN-MFG-0023533 本地根本没有 SKILL.md，线上照样 200，
    内容和 index.html 一模一样）。所以**只看状态码是假绿**，必须验内容。
    ——同一个坑也打在 App 侧：ChatScreen 的 HEAD 探活因此永远成功。
    """
    checks = ["/", "/index.html", "/manifest.json", "/full/_unclassified.json",
              "/data/manifest.json", "/data/endpoint.json"]
    checks += [f"/skills/vendors/{i}/SKILL.md" for i in _sample_ids()]

    bad = 0
    for path in checks:
        status, body, err = _fetch(path)
        if err:
            print(f"  ERR {'':>9}      {path}   ✗ {err}")
            bad += 1
            continue
        ok = status == 200 and bool(body)
        note = ""
        # 内容是不是被回退成了 HTML 页（index.html / 404.html）
        if body.lstrip()[:16].lower().startswith((b"<!doctype", b"<html")):
            if not path.endswith((".html", "/")):
                ok, note = False, "  ✗ 回退成 HTML 页（该路径其实不存在）"
        print(f"  {status}  {len(body):>9,} B  {path}{note}" + ("" if ok else "   ✗"))
        if not ok:
            bad += 1

    # 反向探测：一个肯定不存在的路径，期望 404。
    # ⚠ 必须带 User-Agent：实测无 UA 的请求会被 CF 直接 403，
    #   而 403 会被误读成「有 404 行为」——2026-09-10 就差点这么骗过自己。
    probe = "/skills/vendors/CN-MFG-9999999/SKILL.md"
    probe_code = 0
    for k in range(4):  # 同样等一等：404.html 也是刚传上去的资产
        status, _body, err = _fetch(probe, tries=1)
        if status == 404:
            probe_code = 404
            break
        probe_code = status
        if k < 3:
            time.sleep(5)
    if probe_code == 404:
        print("\n✓ 不存在的路径正确返回 404（404.html 生效，App 探活可用）")
    elif probe_code == 200:
        print(f"\n⚠ 不存在的路径 {probe} 返回 200 —— Pages 在做 SPA 回退，"
              f"App 的 HEAD 探活会永远判为「已发布」（检查 404.html 传上去没有）")
    else:
        print(f"\n⚠ 不存在的路径返回 {probe_code or err}（预期 404，需人工确认）")

    # ── 无 UA 守卫 ──────────────────────────────────────────────────────
    # 上面所有校验都自带 UA（requests/urllib 都有），所以「全绿」**证明不了 App 能取到**：
    # OkHttp 默认**不发 User-Agent**，而 Cloudflare 的浏览器签名校验会把无 UA 请求
    # 打成 403 `error code: 1010` —— 现象就是 App 报「主源不通」，
    # 而浏览器和部署校验一切正常（2026-09-15 事故，排查方向被带偏了很久）。
    status, body, err = _fetch("/data/endpoint.json", tries=1, ua=None)
    if status == 403 and b"1010" in body:
        print("⚠ 无 User-Agent 的请求被 Cloudflare 拒绝（403 / error code 1010）。\n"
              "  → 浏览器与本次校验（自带 UA）不受影响，但 **OkHttp 客户端会被全数拦掉**。\n"
              "  → App 侧：RemoteSource/PlatformApi 必须挂 beaconIdentityInterceptor()。\n"
              "  → 或在 Cloudflare 控制台关闭 Bot Fight Mode / Browser Integrity Check。")
    elif status == 200:
        print("✓ 无 User-Agent 的请求也能取到（Cloudflare 未开启浏览器签名校验）")
    else:
        print(f"· 无 UA 探测返回 {status or err}（非 1010，忽略）")

    print()
    if bad:
        print(f"✗ {bad}/{len(checks)} 项未通过 —— 不要写「404 已修」")
    else:
        print(f"✓ {len(checks)}/{len(checks)} 全绿")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="只组装 dist/site")
    ap.add_argument("--verify", action="store_true", help="只做部署后抽样验证")
    ap.add_argument("--deploy-only", action="store_true", help="跳过组装直接部署")
    ap.add_argument("--dry-run", action="store_true", help="只打印 wrangler 命令")
    ap.add_argument("--branch", default=BRANCH,
                    help=f"部署分支（默认 {BRANCH} = 生产；"
                         f"填别的名字会部署到 preview 子域，不影响生产）")
    ap.add_argument("--with-worker", action="store_true",
                    help="带上 pages/_worker.js（/skills/** 走 R2）。"
                         "**只有在 Pages 项目已绑定 R2 bucket CAPS 之后才能用**，"
                         "否则 /skills/** 全站 503")
    a = ap.parse_args()

    if a.verify:
        return verify()

    if not a.deploy_only:
        build(with_worker=a.with_worker)
    if a.build:
        return 0

    if a.branch != BRANCH:
        print(f"\n⚠ 部署到 preview 分支 '{a.branch}' —— "
              f"生产 {BASE_URL} 不受影响，验证地址见下方输出")

    rc = deploy(dry_run=a.dry_run, branch=a.branch)
    if rc != 0:
        print(f"✗ wrangler 退出码 {rc}", file=sys.stderr)
        return rc
    if a.dry_run:
        return 0

    print("\n=== 验证（生产域名，可能要等 CDN 生效）===")
    return verify()


if __name__ == "__main__":
    sys.exit(main())
