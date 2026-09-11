#!/usr/bin/env python3
"""平台接口地址（App 里的 apiBase）自动发布 + 服务看护。

为什么要有这个脚本
------------------
供应商侧的「注册 / 认领 / 采集」走平台服务端（`server/routers/*`），服务端跑在
本机 uvicorn 上，靠 cloudflared 快速隧道暴露到公网。**快速隧道每次启动都是新的
随机域名**，于是每次重启都要人手把新地址抄进手机「设置 → 平台接口地址」。

这个脚本把两件事自动化：

1. **看护**：uvicorn / cloudflared 挂了就拉起（PC 开机时跑 `--watch` 即可）。
2. **发布**：拿到新的公网地址后，写进 `data/endpoint.json` 并 push 到 GitHub，
   手机端点一下「刷新地址」就能自动取到并保存 —— 不用再手抄。

⚠ 一个必须知道的取舍
--------------------
`data/endpoint.json` 要能被手机读到，就必须**公开**（仓库是 public 的）。
也就是说你的后端地址会出现在公开仓库里。快速隧道地址本来就是"知道就能访问"的，
真正的访问控制靠服务端自己的鉴权（`server/auth/api_key`），不靠地址保密。
如果你不接受，用 `--no-push`：只写本地文件、不推送，手机端就只能手动填。

用法
----
    python scripts/endpoint_watch.py --once          # 拉起服务 + 发布地址（最常用）
    python scripts/endpoint_watch.py --print         # 只打印当前地址，不动服务
    python scripts/endpoint_watch.py --watch 60      # 常驻看护，每 60s 检查一次
    python scripts/endpoint_watch.py --once --no-push # 只拉起服务，不推送地址

环境变量（可选覆盖）
--------------------
    PYTHON       uvicorn 用的解释器
    CLOUDFLARED  cloudflared.exe 路径
    PORT         本机端口（默认 8000）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "server"
ENDPOINT_PATH = ROOT / "data" / "endpoint.json"
CFD_LOG = SERVER_DIR / "cloudflared.log"
UVICORN_LOG = SERVER_DIR / "uvicorn-dev.log"

DEFAULT_PY = r"C:/Users/陆斌/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
DEFAULT_CFD = r"C:/Users/陆斌/bin/cloudflared.exe"

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
REPO = "eiry16/beacon-mfg"
BRANCH = "main"
CST = timezone(timedelta(hours=8))


# ── 基础工具 ────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def clean_env() -> dict:
    """去掉代理环境变量 —— 否则本机 127.0.0.1 的请求会被送进代理然后超时。"""
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    return env


def http_ok(url: str, timeout: float = 5.0) -> bool:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def git(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    p = subprocess.run(["git"] + args, cwd=str(ROOT), timeout=timeout,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return p.returncode, p.stdout.strip(), p.stderr.strip()


# ── 服务拉起 ────────────────────────────────────────────────────────────────

def last_url_in_log() -> str | None:
    """从 cloudflared 日志里取最近一次分配到的地址。"""
    if not CFD_LOG.exists():
        return None
    try:
        text = CFD_LOG.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    hits = URL_RE.findall(text)
    return hits[-1] if hits else None


def ensure_uvicorn(py: str, port: int) -> bool:
    if http_ok(f"http://127.0.0.1:{port}/health", timeout=2):
        print(f"[watch] uvicorn 已在跑 (:{port})")
        return True
    print(f"[watch] 拉起 uvicorn (:{port}) …")
    try:
        UVICORN_LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(UVICORN_LOG, "ab")
        subprocess.Popen(
            [py, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(SERVER_DIR), stdout=log, stderr=log,
            env=clean_env(), stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[watch] uvicorn 启动失败：{e}")
        return False
    for _ in range(25):
        if http_ok(f"http://127.0.0.1:{port}/health", timeout=2):
            print("[watch] uvicorn 已就绪")
            return True
        time.sleep(1)
    print("[watch] uvicorn 超时未就绪，看日志：" + str(UVICORN_LOG))
    return False


def ensure_cloudflared(cfd: str, port: int, current: str | None) -> str | None:
    """返回一个公网地址。

    ⚠ 探活失败**不要**重启隧道：国内 `*.trycloudflare.com` 常被 DNS 污染
    （本机 DNS 回 Non-existent domain，换 1.1.1.1 却能解析出 Cloudflare IP）。
    这种失败重启多少次都一样，只会让地址反复变、手机永远追不上。
    所以：地址在但探活不通 —— 照常发布并警告；地址不在 —— 才拉起新隧道。
    """
    if current:
        if http_ok(current.rstrip("/") + "/health", timeout=8):
            print(f"[watch] 隧道仍可用：{current}")
        else:
            print(f"[watch] ⚠ {current} 探活未通过（多是国内 DNS 污染，不是隧道挂了）；照常发布")
        return current

    if not Path(cfd).exists():
        print(f"[watch] 找不到 cloudflared：{cfd}")
        return None

    marker = CFD_LOG.stat().st_size if CFD_LOG.exists() else 0
    print("[watch] 拉起 cloudflared 隧道 …")
    try:
        CFD_LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(CFD_LOG, "ab")
        subprocess.Popen(
            [cfd, "tunnel", "--url", f"http://localhost:{port}",
             "--logfile", str(CFD_LOG), "--loglevel", "info"],
            cwd=str(SERVER_DIR), stdout=log, stderr=log,
            env=clean_env(), stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[watch] cloudflared 启动失败：{e}")
        return None

    # 等新地址出现在日志里（只看本次追加的部分，避免抓到上一次的旧地址）
    for _ in range(40):
        time.sleep(1)
        if not CFD_LOG.exists():
            continue
        try:
            with open(CFD_LOG, "rb") as f:
                f.seek(marker)
                fresh = f.read().decode("utf-8", errors="ignore")
        except Exception:
            continue
        hits = URL_RE.findall(fresh)
        if hits:
            url = hits[-1]
            for _ in range(10):  # 隧道刚建立时前几秒会 502，多试几下
                if http_ok(url.rstrip("/") + "/health", timeout=8):
                    print(f"[watch] 新隧道地址：{url}")
                    return url
                time.sleep(2)
            print(f"[watch] 隧道地址 {url} 探活未通过（隧道可能刚建立，稍等再试）")
            return url  # 仍返回，让上层决定
    print("[watch] 日志里没等到新地址，看日志：" + str(CFD_LOG))
    return None


# ── 发布指针 ────────────────────────────────────────────────────────────────

def write_endpoint(url: str, port: int) -> dict:
    data: dict = {}
    if ENDPOINT_PATH.exists():
        try:
            data = json.loads(ENDPOINT_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    history = data.get("history") or []
    if data.get("base_url") and data["base_url"] != url:
        history.insert(0, {"url": data["base_url"], "at": data.get("updated_at", "")})
    history = [h for h in history if h.get("url") != url][:4]

    doc = {
        "base_url": url,
        "updated_at": now_iso(),
        "port": port,
        "history": history,
        "_note": "App 设置 → 平台接口地址 点『刷新地址』自动取这里。由 scripts/endpoint_watch.py 维护。",
    }
    ENDPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENDPOINT_PATH.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return doc


def publish(url: str, port: int, push: bool) -> bool:
    write_endpoint(url, port)
    if not push:
        print(f"[watch] 已写 {ENDPOINT_PATH.relative_to(ROOT)}（--no-push，不推送）")
        return True

    rc, _, err = git(["add", "--", "data/endpoint.json"])
    if rc != 0:
        print(f"[watch] git add 失败：{err}")
        return False
    rc, out, _ = git(["status", "--porcelain", "--", "data/endpoint.json"])
    if rc == 0 and not out.strip():
        print("[watch] 地址无变化，跳过提交")
        return True

    rc, _, err = git(["commit", "-m", f"chore(endpoint): 更新平台接口地址 {url}"])
    if rc != 0:
        print(f"[watch] git commit 失败：{err}")
        return False
    rc, _, err = git(["push"], timeout=120)
    if rc != 0:
        print(f"[watch] git push 失败：{err}")
        return False
    print(f"[watch] 已推送：{url}")

    # 清 jsDelivr 边缘缓存（best-effort：分支 URL 有 s-maxage=43200，
    # 不清的话手机最长 12h 拿到的还是旧指针）
    try:
        req = urllib.request.Request(
            f"https://purge.jsdelivr.net/gh/{REPO}@{BRANCH}/data/endpoint.json")
        with urllib.request.urlopen(req, timeout=20):
            print("[watch] 已清 jsDelivr 缓存")
    except Exception as e:
        print(f"[watch] 清缓存失败（不影响本次更新，最坏延迟几分钟）：{e}")
    return True


# ── 主流程 ──────────────────────────────────────────────────────────────────

def run_once(py: str, cfd: str, port: int, push: bool) -> str | None:
    if not ensure_uvicorn(py, port):
        return None
    url = ensure_cloudflared(cfd, port, last_url_in_log())
    if not url:
        return None
    publish(url, port, push)
    return url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="跑一次：拉起服务 + 发布地址（默认）")
    ap.add_argument("--print", dest="do_print", action="store_true", help="只打印当前地址")
    ap.add_argument("--watch", type=int, metavar="SEC", help="常驻看护，每 SEC 秒检查一次")
    ap.add_argument("--no-push", action="store_true", help="只写本地文件，不 commit/push")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--py", default=os.environ.get("PYTHON", DEFAULT_PY))
    ap.add_argument("--cfd", default=os.environ.get("CLOUDFLARED", DEFAULT_CFD))
    args = ap.parse_args()

    if args.do_print:
        if ENDPOINT_PATH.exists():
            doc = json.loads(ENDPOINT_PATH.read_text(encoding="utf-8"))
            print(doc.get("base_url", ""), "（更新于", doc.get("updated_at"), "）")
        else:
            print("（还没有 data/endpoint.json，先跑 --once）")
        return 0

    if args.watch:
        print(f"[watch] 常驻看护启动，每 {args.watch}s 检查一次（Ctrl+C 退出）")
        last: str | None = None
        try:
            while True:
                url = run_once(args.py, args.cfd, args.port, push=not args.no_push)
                if url and url != last:
                    print(f"[watch] ✔ 当前平台接口地址：{url}")
                    last = url
                time.sleep(max(5, args.watch))
        except KeyboardInterrupt:
            print("\n[watch] 已退出")
        return 0

    url = run_once(args.py, args.cfd, args.port, push=not args.no_push)
    if url:
        print("=" * 60)
        print(f"  平台接口地址：{url}")
        print("  手机端：设置 → 平台接口地址 → 点『刷新地址』即可自动填入")
        print("=" * 60)
        return 0
    print("[watch] ✖ 没能拿到可用地址，检查 uvicorn-dev.log / cloudflared.log")
    return 1


if __name__ == "__main__":
    sys.exit(main())
