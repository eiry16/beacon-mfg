#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 L1 能力卡 + L2 厂商 skill 部署到 Cloudflare Pages

⚠ 为什么用 wrangler 而不是手写 API
-----------------------------------
第一版是纯标准库手写 `POST /pages/projects/{name}/deployments`（multipart）。
实测三种写法**全部返回 success、部署状态 deploy success，但站点全线 404**：

  1. 文件字段名 = 相对路径
  2. 文件字段名 = 内容哈希（manifest 做 路径→哈希 映射）
  3. manifest 放在文件字段之前

CF 不报错，文件却一个都没存进去——典型静默成功。改用官方 wrangler 后
同样的内容一次就 200。**结论：别手写这个 API，用 wrangler。**

传什么
------
    dist/capability/manifest.json              客户端入口（分片清单 + SHA1）
    dist/capability/full/gb/**/*.json          完整能力卡，按需拉取
    dist/capability/slim/gb/**/*.json          精简版，App 内置那份的源头
    skills/vendors/{id}/SKILL.md               厂商自述（L2）

**vendors 的路径必须原样保留**：App 里 `skillPath` 写死是
`skills/vendors/{id}/SKILL.md`，部署后拼上域名就能直接打开，
改一层目录 App 那边就 404。

当前规模：4219 个文件 / 约 14 MB（Pages 上限 20000 文件、单文件 25 MB）。

准备
----
1. 仓库根目录 .env 里写（.env 已被 .gitignore 排除，
   **不要写进任何会被提交的文件**）：

    CLOUDFLARE_ACCOUNT_ID=...
    CLOUDFLARE_API_TOKEN=...

   Token 最小权限：`Account > Cloudflare Pages: Edit`。不需要任何 Zone 权限。

2. 装 wrangler（只需一次）：

    cd <node workspace> && npm install wrangler

用法:
    python scripts/deploy_pages.py              # 预览（只构建 staging，不上传）
    python scripts/deploy_pages.py --apply      # 构建并上传
    python scripts/deploy_pages.py --apply --no-vendors   # 不传 L2 厂商 skill
    python scripts/deploy_pages.py --apply --project other-name
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
CAP_DIR = ROOT / "dist" / "capability"
VENDOR_DIR = ROOT / "skills" / "vendors"
STAGING = ROOT / "dist" / "site"

DEFAULT_PROJECT = "beacon-mfg"

# wrangler 的兜底路径（本项目 node workspace）。有环境变量或 PATH 时优先用那些。
NODE_BIN = r"C:/Users/陆斌/.workbuddy/binaries/node/versions/22.22.2-2/node.exe"
WRANGLER_JS = r"C:/Users/陆斌/.workbuddy/binaries/node/workspace/node_modules/wrangler/bin/wrangler.js"


def load_env() -> tuple[str, str]:
    if not ENV_FILE.exists():
        sys.exit(f"缺少 {ENV_FILE}；把 CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN 写进去")
    vals: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip()
    acct = vals.get("CLOUDFLARE_ACCOUNT_ID", "")
    token = vals.get("CLOUDFLARE_API_TOKEN", "")
    if not acct or not token:
        sys.exit(".env 里缺少 CLOUDFLARE_ACCOUNT_ID 或 CLOUDFLARE_API_TOKEN")
    return acct, token


def build_staging(with_vendors: bool) -> int:
    """把两拨内容合成一个站点目录。返回文件数。"""
    if not CAP_DIR.exists():
        sys.exit(f"缺少 {CAP_DIR}；先跑 python scripts/gen_capability_shards.py --apply")
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)

    n = 0
    for p in sorted(CAP_DIR.rglob("*")):
        if p.is_file():
            dst = STAGING / p.relative_to(CAP_DIR)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            n += 1

    m = 0
    if with_vendors and VENDOR_DIR.exists():
        for p in sorted(glob.glob(str(VENDOR_DIR / "*" / "SKILL.md"))):
            src = Path(p).resolve()
            dst = STAGING / src.relative_to(ROOT)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            m += 1

    files = [f for f in STAGING.rglob("*") if f.is_file()]
    total = sum(f.stat().st_size for f in files)
    print("staging：%d 个文件 / %.2f MB（能力卡 %d + 厂商 skill %d）"
          % (len(files), total / 1048576, n, m))
    return len(files)


def find_wrangler() -> list[str] | None:
    """找 wrangler。优先环境变量，其次 PATH，最后兜底到本项目 node workspace。"""
    custom = os.environ.get("WRANGLER")
    if custom:
        return [custom]
    which = shutil.which("wrangler")
    if which:
        return [which]
    if os.path.exists(WRANGLER_JS) and os.path.exists(NODE_BIN):
        return [NODE_BIN, WRANGLER_JS]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="部署 L1/L2 到 Cloudflare Pages（走 wrangler）")
    ap.add_argument("--apply", action="store_true", help="真正上传（默认只构建 staging）")
    ap.add_argument("--no-vendors", action="store_true", help="不传 L2 厂商 skill")
    ap.add_argument("--project", default=DEFAULT_PROJECT, help=f"项目名（默认 {DEFAULT_PROJECT}）")
    a = ap.parse_args()

    count = build_staging(not a.no_vendors)
    if not a.apply:
        print("\n（预览模式，未上传。加 --apply 执行）")
        return 0

    acct, token = load_env()
    w = find_wrangler()
    if not w:
        sys.exit(
            "找不到 wrangler。装一次：\n"
            "  cd <node workspace> && npm install wrangler\n"
            "或设置环境变量 WRANGLER=/path/to/wrangler"
        )

    env = dict(os.environ)
    env["CLOUDFLARE_ACCOUNT_ID"] = acct
    env["CLOUDFLARE_API_TOKEN"] = token

    cmd = w + [
        "pages", "deploy", str(STAGING),
        "--project-name", a.project,
        "--branch", "main",
        "--commit-dirty=true",
    ]
    print("\n上传 %d 个文件…" % count)
    r = subprocess.run(cmd, cwd=str(STAGING), env=env,
                       capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        sys.exit("部署失败：\n" + out[-2000:])

    print(out.strip()[-800:])
    url = f"https://{a.project}.pages.dev"
    print("\n✅ 部署完成：%s" % url)
    print("""
验证（部署后需 30~60s 生效）：
  curl -s -o /dev/null -w 'manifest.json  %%{http_code}\\n' {u}/manifest.json
  curl -s -o /dev/null -w '3525 分片      %%{http_code}\\n' {u}/full/gb/C/35/3525.json
  curl -s -o /dev/null -w '厂商 skill     %%{http_code}\\n' {u}/skills/vendors/CN-MFG-0000005/SKILL.md

注意：CF 会把 /index.html 重定向到 /（308），这是正常的，不是失败。""".format(u=url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
