#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 confirm 后新落盘的 L0 指纹 + 清单发布到「检索云」（Git + CDN）。

为什么需要它
------------
confirm 成功后，`append_fingerprint` 把新供应商指纹写进了
`skills/registry/fingerprint/gb/`（通常是 `_unclassified.jsonl`），
但 `data/manifest.json` 里这条分片的 SHA1（字段 h）还是旧哈希。
手机端用 h 比对本地缓存：本地 == manifest.h → 判定无更新 → 跳过下载，
于是新注册的供应商永远到不了手机（赤兔就是这么丢的，2026-09-11）。

本脚本做三件事（全部幂等、可重复跑）：
  1. 刷新 data/manifest.json 里所有分片的 h/k/z/u —— 用 gen_manifest.py，
     **只重算磁盘现有文件的哈希，绝不再生指纹内容**（gen_fingerprint --apply 会从
     data/gb 重建，会把不在 data/gb 的注册商丢掉，禁用）。
  2. git add 仅发布产物（指纹分片 + manifest），git commit。
     **不碰 skills/registry/capability/ 与 skills/vendors/**：L1 卡按设计走 Pages，
     不进 Git。
  3. git push 到当前上游。

L1 能力卡 / SKILL.md 不在 Git 里（被 gitignore），走 Cloudflare Pages：
   python scripts/deploy_pages.py
本脚本默认不自动部署 L1；加 --deploy-l1 才会调用 deploy_pages.py（需 wrangler 凭据）。
这一步与「注册→手机搜得到」无关（手机检索只读 L0 指纹 + manifest），
但若要让人/外部 Agent 在云端取得到 L1/L2，需单独跑 Pages 部署。

用法
----
   python scripts/publish_registry.py                  # 预览：打印将要提交/推送什么
   python scripts/publish_registry.py --apply          # 刷新 + 提交 + 推送
   python scripts/publish_registry.py --apply --no-push     # 只提交不推送
   python scripts/publish_registry.py --apply --deploy-l1  # 顺带部署 L1 到 Pages

也可被后端直接调用：
   from publish_registry import publish
   publish(apply=True, push=True)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 仅这些路径会被纳入发布（避免把 L1 卡/本地数据/无关改动一起 commit）
STAGE_PATHS = [
    "skills/registry/fingerprint/gb",
    "data/manifest.json",
]
MANIFEST = ROOT / "data" / "manifest.json"
L1_DEPLOY_HINT = (
    "L1 卡未发布（按设计走 Pages，不进 Git）。要让云端取得到 L1/L2，请另行运行：\n"
    "  python scripts/deploy_pages.py"
)


def _run(cmd: list[str], timeout: int | None = None) -> tuple[int, str, str]:
    """跑一条命令，返回 (rc, stdout, stderr)。cwd 固定为仓库根。"""
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), encoding="utf-8",
                           errors="replace", capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时（%ds）" % (timeout or 0)
    except Exception as exc:  # pragma: no cover
        return 1, "", "启动失败：%s" % exc
    return r.returncode, (r.stdout or ""), (r.stderr or "")


def _git(args: list[str], timeout: int | None = None) -> tuple[int, str, str]:
    return _run(["git", "-C", str(ROOT)] + args, timeout=timeout)


def refresh_manifest(apply: bool) -> tuple[bool, str]:
    """刷新 data/manifest.json 的分片哈希。

    用 gen_manifest.py：它只 walk 磁盘现有文件算 SHA1 写进 manifest，
    **不重建指纹内容**（那是 gen_fingerprint.py 的职责，会让不在 data/gb 的
    注册商丢失，禁用）。
    """
    py = sys.executable
    if not apply:
        return True, "（预览）将运行 `gen_manifest.py` 刷新 data/manifest.json 分片哈希"
    rc, out, err = _run([py, "scripts/gen_manifest.py"], timeout=120)
    if rc != 0:
        return False, "gen_manifest.py 失败（rc=%d）：%s" % (rc, (err or out).strip()[-600:])
    return True, "已刷新 data/manifest.json 分片哈希"


def publish(apply: bool = False, push: bool = True, deploy_l1: bool = False,
            timeout: int = 120) -> dict:
    """核心发布逻辑。返回结构化结果，便于后端把结论写进响应。

    apply=False 时只预览，不写任何文件、不 commit、不 push。
    """
    res: dict = {
        "ok": False,
        "applied": apply,
        "manifest_refreshed": False,
        "committed": False,
        "commit_hash": None,
        "pushed": False,
        "staged": [],
        "l1_note": L1_DEPLOY_HINT,
        "message": "",
    }

    # ① 刷新 manifest 哈希（先于 git 操作，否则提交的是过期 manifest）
    ok, msg = refresh_manifest(apply)
    if not ok:
        res["message"] = "发布中止（manifest 刷新失败）：" + msg
        return res
    if apply:
        res["manifest_refreshed"] = True

    # ② 暂存仅发布产物
    if apply:
        rc, _, err = _git(["add", "--"] + STAGE_PATHS)
        if rc != 0:
            res["message"] = "git add 失败：" + (err.strip()[-400:])
            return res
        # 检测到底有没有东西要提交
        rc, out, _ = _git(["diff", "--cached", "--quiet", "--"] + STAGE_PATHS)
        if rc == 0:
            res["ok"] = True
            res["message"] = "没有需要发布的变更（manifest 与指纹已是最新），无需提交。"
            return res

        rc, out, _ = _git(["diff", "--cached", "--name-only", "--"] + STAGE_PATHS)
        res["staged"] = [l for l in out.splitlines() if l.strip()]

        # ③ 提交
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        msg_line = "chore(registry): publish fingerprint update via collect confirm (%s)" % stamp
        rc, _, err = _git(["commit", "-m", msg_line,
                           "--"] + STAGE_PATHS, timeout=timeout)
        if rc != 0:
            res["message"] = "git commit 失败：" + (err.strip()[-400:])
            return res
        res["committed"] = True
        rc, out, _ = _git(["rev-parse", "HEAD"])
        res["commit_hash"] = out.strip()[:12] or None

        # ④ 推送
        if push:
            rc, _, err = _git(["push"], timeout=timeout)
            if rc != 0:
                res["message"] = ("已提交 %s，但 git push 失败：%s\n"
                                  "本地提交已保留，请手动 `git push` 后再触发手机更新。"
                                  % (res["commit_hash"], (err.strip() or "未知错误")[-400:]))
                res["ok"] = True  # 提交成功，push 可稍后补
                return res
            res["pushed"] = True

        # ⑤（可选）L1 卡 Pages 部署
        if deploy_l1:
            py = sys.executable
            rc, out, err = _run([py, "scripts/deploy_pages.py"], timeout=300)
            if rc != 0:
                res["l1_note"] = "L1 Pages 部署失败（rc=%d），请手动运行 deploy_pages.py：%s" % (
                    rc, (err or out).strip()[-400:])
            else:
                res["l1_note"] = "L1 卡已触发 Pages 部署。"
    else:
        # 预览：列出当前脏的发布产物
        rc, out, _ = _git(["status", "--porcelain", "--"] + STAGE_PATHS)
        dirty = [l for l in out.splitlines() if l.strip()]
        if not dirty:
            res["ok"] = True
            res["message"] = "（预览）当前没有待发布的变更。"
        else:
            res["message"] = "（预览）将提交并推送以下变更：\n  " + "\n  ".join(dirty)
            res["staged"] = dirty

    res["ok"] = res.get("ok", False) or res["committed"] or res["pushed"] \
        or (apply and res["manifest_refreshed"])
    if apply and res["pushed"]:
        res["message"] = ("发布完成：已刷新 manifest 哈希、提交 %s 并推送到云端。"
                           "手机端联网自动更新指纹后即可搜到新供应商。"
                           % res["commit_hash"])
    elif apply and res["committed"]:
        res["message"] = ("已刷新 manifest 哈希并提交 %s（未推送）。"
                           "请 `git push` 后手机端才会拉到更新。" % res["commit_hash"])
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="发布 L0 指纹 + 清单到检索云（Git + CDN）")
    ap.add_argument("--apply", action="store_true", help="真正刷新 + 提交 + 推送（默认仅预览）")
    ap.add_argument("--no-push", action="store_true", help="只提交不推送")
    ap.add_argument("--deploy-l1", action="store_true", help="顺带把 L1 卡部署到 Pages（需 wrangler 凭据）")
    ap.add_argument("--timeout", type=int, default=120, help="git push 超时秒数")
    a = ap.parse_args()

    res = publish(apply=a.apply, push=not a.no_push, deploy_l1=a.deploy_l1,
                  timeout=a.timeout)

    print("═" * 64)
    print("发布 L0 指纹 + 清单  %s" % ("【落盘+提交+推送】" if a.apply else "【预览，不写任何文件】"))
    print("═" * 64)
    print(res["message"])
    if res.get("staged"):
        print("\n涉及文件：")
        for p in res["staged"]:
            print("  - %s" % p)
    if res.get("commit_hash"):
        print("\ncommit: %s" % res["commit_hash"])
    print("\nL1 卡：%s" % res["l1_note"])
    print("manifest 刷新：%s | 已提交：%s | 已推送：%s"
          % (res["manifest_refreshed"], res["committed"], res["pushed"]))

    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
