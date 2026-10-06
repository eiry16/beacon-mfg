#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Beacon-MFG MCP 自更新检查（纯标准库，Python 实现 = 唯一事实来源）

为什么要有这个文件（修掉原来的三个问题）：
  1. 原来只有 npm launcher（bin/beacon-mfg-mcp.js）会起自更新；
     凡是以 `python server.py` 直接接入的用户（含本地仓库用户）**永远不会检查更新**。
     → 现在由 server.py 在启动时拉起本脚本，**任何接入方式都会检查**。
  2. 原来 `BEACON_SOURCE` 也命中跳过条件（自定义 HTTP 源的联网用户被误判为离线）。
     → 现在只对「本地仓库用户」（BEACON_REPO）走离线分支，且**不是跳过，而是做只读比对**。
  3. 原来只 `npm i -g`（对 npx 缓存/本地 clone 用户无效），且失败完全静默。
     → 现在按接入方式分流动作，并且**一定给出可见提示 + 落状态戳**（失败也如实记录）。

设计约束（与旧实现一致，必须保持）：
  · 绝不阻塞 MCP 启动；本脚本由 server.py 以 detached 子进程拉起。
  · 失败一律降级为“没有更新”，绝不抛异常影响服务。
  · 节流（默认 24h）+ 可关（BEACON_MCP_NO_UPDATE=1）。

用法：
  python selfupdate.py                # 检查并按接入方式处理（默认）
  python selfupdate.py --check        # 只检查并打印 JSON，不安装
  python selfupdate.py --print-mode   # 只打印识别到的接入方式
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request

PKG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "package.json")
STATE_DIR = os.path.join(os.path.expanduser("~"), ".beacon-mfg")
STAMP = os.path.join(STATE_DIR, "update-check.json")
LOCK = os.path.join(STATE_DIR, "update.lock")
REGISTRY = "https://registry.npmjs.org/beacon-mfg-mcp/latest"
DEFAULT_INTERVAL_H = 24.0
LOCK_STALE_S = 600.0
HTTP_TIMEOUT_S = 5.0
DIST_TAG = "latest"

MODE_LOCAL_REPO = "local_repo"
MODE_NPM_GLOBAL = "npm_global"
MODE_NPX_CACHE = "npx_cache"
MODE_OTHER = "other"


def current_version() -> str:
    try:
        with open(PKG_FILE, encoding="utf-8") as f:
            return str(json.load(f).get("version") or "0.0.0")
    except Exception:
        return "0.0.0"


def semver_gt(a: str, b: str) -> bool:
    def parts(v):
        return [int(x) if str(x).isdigit() else 0
                for x in re.split(r"[.\-+]", str(v))[:3]] + [0, 0, 0]

    pa, pb = parts(a), parts(b)
    for i in range(3):
        if pa[i] > pb[i]:
            return True
        if pa[i] < pb[i]:
            return False
    return False


def interval_h() -> float:
    try:
        return float(os.environ.get("BEACON_MCP_UPDATE_INTERVAL_H") or DEFAULT_INTERVAL_H)
    except Exception:
        return DEFAULT_INTERVAL_H


def quiet() -> bool:
    return os.environ.get("BEACON_MCP_UPDATE_QUIET") == "1"


def note(msg: str) -> None:
    """只写 stderr：stdout 是 MCP 协议通道；stderr 会被 MCP 客户端记入日志。"""
    if quiet():
        return
    try:
        sys.stderr.write("[beacon-mfg-mcp] " + msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def read_stamp() -> dict:
    try:
        with open(STAMP, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def write_stamp(extra: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        rec = {"ts": int(time.time() * 1000), "version": current_version()}
        rec.update(extra or {})
        with open(STAMP, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
    except Exception:
        pass


def lock_held() -> bool:
    """原子创建锁文件；残留锁超时自愈（detached 进程可能被中途杀掉）。"""
    def grab() -> bool:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except Exception:
            return False
        try:
            os.write(fd, ("%d %d" % (os.getpid(), int(time.time() * 1000))).encode())
        finally:
            os.close(fd)
        return True

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except Exception:
        return False
    if grab():
        return True
    try:
        if time.time() - os.path.getmtime(LOCK) > LOCK_STALE_S:
            os.unlink(LOCK)
            return grab()
    except Exception:
        pass
    return False


def unlock() -> None:
    try:
        os.unlink(LOCK)
    except Exception:
        pass


def disabled() -> bool:
    """唯一的“完全跳过”条件：显式关闭。"""
    return os.environ.get("BEACON_MCP_NO_UPDATE") == "1"


def detect_mode(here: str | None = None) -> str:
    repo = os.environ.get("BEACON_REPO")
    if repo and os.path.isdir(repo):
        return MODE_LOCAL_REPO
    here = (here or os.path.dirname(os.path.abspath(__file__))).replace("\\", "/").lower()
    if "/_npx/" in here:
        return MODE_NPX_CACHE
    # 全局安装的典型路径： <prefix>/node_modules/beacon-mfg-mcp
    if "node_modules/beacon-mfg-mcp" in here:
        return MODE_NPM_GLOBAL
    return MODE_OTHER


def fetch_latest(timeout_s: float = HTTP_TIMEOUT_S):
    try:
        req = urllib.request.Request(
            REGISTRY, headers={"Accept": "application/json",
                               "User-Agent": "beacon-mfg-mcp-selfupdate"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        v = data.get("version")
        return str(v) if v else None
    except Exception:
        return None


def git_repo_state(repo: str):
    """本地仓库模式：只读比对本地 HEAD 与远端 HEAD（绝不写仓库）。"""
    try:
        local = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=15)
        if local.returncode != 0 or not local.stdout.strip():
            return None
        local_sha = local.stdout.strip()
        remote = subprocess.run(["git", "-C", repo, "ls-remote", "origin", "HEAD"],
                                capture_output=True, text=True, timeout=25)
        if remote.returncode != 0 or not remote.stdout.strip():
            return {"local": local_sha, "remote": None}
        remote_sha = remote.stdout.split()[0].strip()
        return {"local": local_sha, "remote": remote_sha, "behind": remote_sha != local_sha}
    except Exception:
        return None


def npm_install(latest: str):
    """只有全局安装才尝试自动升级；npx 缓存/其它方式只提示。"""
    is_win = os.name == "nt"
    npm = "npm.cmd" if is_win else "npm"
    try:
        r = subprocess.run([npm, "i", "-g", "beacon-mfg-mcp@" + latest],
                           capture_output=True, text=True, timeout=180, shell=is_win)
        return r.returncode == 0, (r.stderr or "").strip()[-300:]
    except Exception as e:  # 权限/离线/没装 npm
        return False, str(e)


def run(apply: bool = True) -> dict:
    result = {"mode": detect_mode(), "current": current_version()}
    if disabled():
        result["result"] = "disabled"
        return result

    mode = result["mode"]

    # 本地仓库：不联网升级，但**做只读比对并给提示**（原来这里是直接跳过）
    if mode == MODE_LOCAL_REPO:
        state = git_repo_state(os.environ["BEACON_REPO"])
        if not state:
            result["result"] = "local_repo_unknown"
            write_stamp(result)
            return result
        if state.get("remote") is None:
            result["result"] = "local_repo_remote_unreachable"
            write_stamp(result)
            return result
        result.update({"result": "local_repo_behind" if state.get("behind") else "local_repo_uptodate",
                       "local": state["local"][:12], "remote": state["remote"][:12]})
        write_stamp(result)
        if state.get("behind"):
            note("本地仓库有远端更新（%s → %s）：请 git pull 后重启本 MCP。"
                 % (state["local"][:8], state["remote"][:8]))
        return result

    # NPM_GLOBAL / NPX_CACHE / OTHER：查 registry
    latest = fetch_latest()
    if not latest:
        result["result"] = "registry_unreachable"
        write_stamp(result)
        return result
    result["latest"] = latest
    if not semver_gt(latest, result["current"]):
        result["result"] = "uptodate"
        write_stamp(result)
        return result

    if not apply:
        result["result"] = "update_available"
        return result

    if mode == MODE_NPM_GLOBAL:
        note("发现新版 %s（当前 %s），正在自动更新…" % (latest, result["current"]))
        ok, err = npm_install(latest)
        result["result"] = "updated" if ok else "auto_update_failed"
        if not ok:
            result["error"] = err
        write_stamp(result)
        if ok:
            note("已更新到 %s，下次启动生效。" % latest)
        else:
            note("自动更新未成功（可能是权限/网络受限）。请手动执行：npm i -g beacon-mfg-mcp@latest")
        return result

    # npx 缓存 / 其它接入方式：升级对它无效，只给可见提示（原来完全静默）
    result["result"] = "update_available_manual"
    write_stamp(result)
    note("发现新版 %s（当前 %s）。当前接入方式无法自动升级，请执行：npm i -g beacon-mfg-mcp@latest"
         % (latest, result["current"]))
    return result


def main(argv) -> int:
    args = [a for a in argv[1:]]
    if "--print-mode" in args:
        print(detect_mode())
        return 0
    if "--check" in args:
        print(json.dumps(run(apply=False), ensure_ascii=False))
        return 0

    # 显式关闭时直接返回：不动锁、不写状态戳、不联网
    if disabled():
        return 0

    # 节流（--force 可跳过，便于排障）
    if "--force" not in args:
        stamp = read_stamp()
        try:
            if time.time() * 1000 - int(stamp.get("ts") or 0) < interval_h() * 3600 * 1000:
                return 0
        except Exception:
            pass
    if not lock_held():
        return 0
    try:
        run(apply=True)
    finally:
        unlock()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)
