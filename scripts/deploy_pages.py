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
      skills/vendors/{id}/SKILL.md    L2 自述 ← App 唯一依赖的一层
      full/gb/**                      L1 完整能力卡分片（对外 Agent 按需拉）
      slim/gb/**                      L1 精简分片
      manifest.json                   分片清单

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

NODE_BIN = os.environ.get(
    "NODE_BIN", os.path.expanduser(r"~/.workbuddy/binaries/node/versions/22.22.2-2/node.exe"))
WRANGLER_JS = os.environ.get(
    "WRANGLER_JS",
    os.path.expanduser(r"~/.workbuddy/binaries/node/workspace/node_modules/wrangler/bin/wrangler.js"))


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
    n_skill, missing = 0, []
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
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_dir / "SKILL.md")
            written_ids.add(vendor_dir.name)
            n_skill += 1

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
        "missing_skill_md": missing,
        "orphans": orphans,
        "files": len(files),
        "mb": round(size_mb, 2),
        "seconds": round(time.time() - t0, 1),
    }
    if verbose:
        print(f"\n✓ dist/site 组装完成：{n_skill} 份 SKILL.md · 共 {len(files)} 文件 · "
              f"{size_mb:.1f} MB · {stats['seconds']}s")
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


def verify() -> int:
    """抽样验证。**只有全绿才敢写「404 已修」。**

    ⚠ Pages 的 SPA 回退：路径不存在时**不返回 404，而是返回 index.html + 200**
    （2026-09-10 实测：CN-MFG-0023533 本地根本没有 SKILL.md，线上照样 200，
    内容和 index.html 一模一样）。所以**只看状态码是假绿**，必须验内容。
    ——同一个坑也打在 App 侧：ChatScreen 的 HEAD 探活因此永远成功。
    """
    checks = ["/", "/index.html", "/manifest.json", "/full/_unclassified.json"]
    checks += [f"/skills/vendors/{i}/SKILL.md" for i in _sample_ids()]

    bad = 0
    for path in checks:
        url = BASE_URL + path
        try:
            req = urllib.request.Request(
                url, headers={"Cache-Control": "no-cache",
                              "User-Agent": "beaconmfg-verify/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read()
                ok = r.status == 200 and bool(body)
                note = ""
                # 内容是不是被回退成了 HTML 页（index.html / 404.html）
                if body.lstrip()[:16].lower().startswith((b"<!doctype", b"<html")):
                    if not path.endswith((".html", "/")):
                        ok, note = False, "  ✗ 回退成 HTML 页（该路径其实不存在）"
                print(f"  {r.status}  {len(body):>9,} B  {path}{note}"
                      + ("" if ok else "   ✗"))
                if not ok:
                    bad += 1
        except urllib.error.HTTPError as e:
            print(f"  {e.code}  {'':>9}      {path}   ✗")
            bad += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERR {'':>9}      {path}   ✗ {type(e).__name__}: {e}")
            bad += 1

    # 反向探测：一个肯定不存在的路径，期望 404。
    # ⚠ 必须带 User-Agent：实测无 UA 的请求会被 CF 直接 403，
    #   而 403 会被误读成「有 404 行为」——2026-09-10 就差点这么骗过自己。
    probe = "/skills/vendors/CN-MFG-9999999/SKILL.md"
    try:
        req = urllib.request.Request(
            BASE_URL + probe,
            headers={"Cache-Control": "no-cache", "User-Agent": "beaconmfg-verify/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            print(f"\n⚠ 不存在的路径 {probe} 返回 {r.status} —— Pages 在做 SPA 回退，"
                  f"App 的 HEAD 探活会永远判为「已发布」（检查 404.html 传上去没有）")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"\n✓ 不存在的路径正确返回 404（404.html 生效，App 探活可用）")
        else:
            print(f"\n⚠ 不存在的路径返回 {e.code}（预期 404，需人工确认）")

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
