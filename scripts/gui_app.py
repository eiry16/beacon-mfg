#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 一键工具（图形界面）—— 高德爬取 + 脱敏 + 校验 + 上传 Git 仓库

依赖：仅 Python 标准库（tkinter，Windows 自带）
流程：选品类 → 开始 → 后台线程执行 抓取/脱敏/校验/提交推送 → 日志实时显示

配额说明：高德官方 FAQ，个人认证开发者「搜索类」接口日配额约 100 次
（企业 1000 次；非官方渠道说法 2000-5000，以控制台实际为准）。
超限后当日停止，次日 00:00 重置。脚本默认按 100 限制，可在界面调整。

用法: python scripts/gui_app.py
"""
import json
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

DAILY_QUOTA_DEFAULT = 100  # 高德个人认证开发者搜索类日配额（官方 FAQ 保守值）


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def git_run(args, log):
    """在仓库目录执行 git 命令"""
    log(f"$ git {' '.join(args)}")
    r = subprocess.run(["git"] + args, cwd=str(ROOT), capture_output=True, text=True)
    if r.stdout.strip():
        log(r.stdout.strip())
    if r.returncode != 0 and r.stderr.strip():
        log(f"[git 错误] {r.stderr.strip()}")
    return r.returncode == 0


class App:
    def __init__(self, root):
        self.root = root
        root.title("BeaconMFG 供应商灯塔 · 一键采集工具")
        root.geometry("720x640")
        root.configure(bg="#10151d")
        self.env = load_env()

        # 标题
        title = tk.Label(root, text="BeaconMFG · 一键采集与发布", font=("Segoe UI", 16, "bold"),
                         bg="#10151d", fg="#e8edf4")
        title.pack(pady=(16, 4))
        amap_ok = "✓" if self.env.get("AMAP_KEY") else "✗"
        zhipu_ok = "✓" if self.env.get("ZHIPU_API_KEY") else "✗"
        tk.Label(root, text=f"高德 Key {amap_ok}   智谱 Key（翻译）{zhipu_ok}   "
                            f"（.env 文件读取，自动忽略不提交）",
                 font=("Segoe UI", 9), bg="#10151d", fg="#6b7686").pack()

        # 品类选择
        frame = tk.Frame(root, bg="#10151d")
        frame.pack(fill="x", padx=20, pady=10)
        tk.Label(frame, text="采集品类：", font=("Segoe UI", 11), bg="#10151d", fg="#9aa7b8").pack(anchor="w")
        cats = tk.Frame(frame, bg="#10151d")
        cats.pack(anchor="w", pady=4)
        self.cat_vars = {}
        import fetch_batch as batch
        for cat in batch.JOBS.keys():
            v = tk.BooleanVar(value=True)
            self.cat_vars[cat] = v
            tk.Checkbutton(cats, text=cat, variable=v, bg="#10151d", fg="#c9d6e8",
                           selectcolor="#1d242e", activebackground="#10151d",
                           activeforeground="#fff", font=("Segoe UI", 10)).pack(side="left", padx=6)

        # 参数
        param = tk.Frame(root, bg="#10151d")
        param.pack(fill="x", padx=20, pady=4)
        tk.Label(param, text="每日 API 请求上限：", bg="#10151d", fg="#9aa7b8").pack(side="left")
        self.quota_var = tk.StringVar(value=str(DAILY_QUOTA_DEFAULT))
        tk.Entry(param, textvariable=self.quota_var, width=8, bg="#161b23", fg="#fff",
                 insertbackground="#fff", relief="flat", highlightthickness=1,
                 highlightbackground="#2a3340").pack(side="left", padx=6)
        tk.Label(param, text="每任务条数：", bg="#10151d", fg="#9aa7b8").pack(side="left", padx=(16, 0))
        self.limit_var = tk.StringVar(value="40")
        tk.Entry(param, textvariable=self.limit_var, width=6, bg="#161b23", fg="#fff",
                 insertbackground="#fff", relief="flat", highlightthickness=1,
                 highlightbackground="#2a3340").pack(side="left", padx=6)

        # 选项
        opt = tk.Frame(root, bg="#10151d")
        opt.pack(fill="x", padx=20, pady=4)
        self.do_validate = tk.BooleanVar(value=True)
        self.do_english = tk.BooleanVar(value=False)
        self.do_git = tk.BooleanVar(value=True)
        tk.Checkbutton(opt, text="校验数据", variable=self.do_validate, bg="#10151d", fg="#c9d6e8",
                       selectcolor="#1d242e", activebackground="#10151d").pack(side="left", padx=6)
        tk.Checkbutton(opt, text="生成英文版（GLM 翻译）", variable=self.do_english, bg="#10151d", fg="#c9d6e8",
                       selectcolor="#1d242e", activebackground="#10151d").pack(side="left", padx=6)
        tk.Checkbutton(opt, text="提交并推送 GitHub", variable=self.do_git, bg="#10151d", fg="#c9d6e8",
                       selectcolor="#1d242e", activebackground="#10151d").pack(side="left", padx=6)

        # 开始按钮
        self.start_btn = tk.Button(root, text="▶ 开始一键采集发布", command=self.start,
                                   bg="#0f6e56", fg="#fff", font=("Segoe UI", 12, "bold"),
                                   relief="flat", padx=24, pady=8, activebackground="#0b5a47",
                                   activeforeground="#fff")
        self.start_btn.pack(pady=10)

        # 日志
        self.log_box = scrolledtext.ScrolledText(root, height=16, bg="#0b0e13", fg="#9fe1cb",
                                                 insertbackground="#9fe1cb", font=("Consolas", 9),
                                                 relief="flat", wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        self.log_box.configure(state="disabled")

    def log(self, msg):
        def _do():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.after(0, _do)

    def start(self):
        self.start_btn.configure(state="disabled", text="⏳ 执行中…")
        threading.Thread(target=self.worker, daemon=True).start()

    def worker(self):
        try:
            self._worker()
        except Exception as e:
            self.log(f"[错误] {e}")
        finally:
            self.root.after(0, lambda: (self.start_btn.configure(state="normal", text="▶ 开始一键采集发布"),
                                        self.log("===== 本次执行结束 =====")))

    def _worker(self):
        env = load_env()
        amap_key = env.get("AMAP_KEY", "")
        if not amap_key:
            self.log("错误：.env 中缺少 AMAP_KEY，无法采集")
            return
        if not self.env.get("AMAP_KEY"):
            self.env = env

        selected = [c for c, v in self.cat_vars.items() if v.get()]
        if not selected:
            self.log("请至少选择一个品类")
            return
        quota = int(self.quota_var.get() or DAILY_QUOTA_DEFAULT)
        limit = int(self.limit_var.get() or 40)

        # 1. 采集
        sys.path.insert(0, str(ROOT / "scripts"))
        import fetch_gaode_poi as fetcher
        import fetch_batch as batch
        fetcher.AMAP_KEY = amap_key
        fetcher.REQUEST_COUNT = 0

        tasks = [t for t in batch.plan() if t[0] in selected]
        self.log(f"计划 {len(tasks)} 个任务（品类：{'、'.join(selected)}）")
        self.log(f"每日配额上限 {quota} 次请求，每任务目标 {limit} 条")
        for i, (cat, kw, city) in enumerate(tasks, 1):
            if fetcher.REQUEST_COUNT >= quota:
                self.log(f">>> 已用满 {quota} 次配额，停止采集（次日 00:00 重置）")
                break
            self.log(f"[{i}/{len(tasks)}] {cat} × {kw} × {city}")
            try:
                pois = fetcher.fetch(kw, city, limit)
                added = fetcher.save_suppliers(pois, cat, kw)
                self.log(f"  新增 {added} 条；本次已用请求 {fetcher.REQUEST_COUNT}/{quota}")
            except Exception as e:
                self.log(f"  失败: {e}")
            time.sleep(1)

        # 2. 校验
        if self.do_validate.get():
            self.log("\n== 数据校验 ==")
            subprocess.run([sys.executable, str(ROOT / "scripts" / "validate.py")], cwd=str(ROOT))

        # 3. 英文翻译
        if self.do_english.get():
            zhipu = env.get("ZHIPU_API_KEY", "")
            if not zhipu:
                self.log("跳过英文翻译：.env 缺少 ZHIPU_API_KEY")
            else:
                self.log("\n== 英文翻译（GLM-4-Flash）==")
                subprocess.run([sys.executable, str(ROOT / "scripts" / "translate_en.py"), "--key", zhipu],
                               cwd=str(ROOT))

        # 4. 上传 git
        if self.do_git.get():
            self.log("\n== 提交并推送 GitHub ==")
            if not git_run(["add", "-A"], self.log):
                self.log("git add 失败")
                return
            ts = time.strftime("%Y-%m-%d %H:%M")
            msg = f"Auto update via GUI tool @ {ts}"
            r = subprocess.run(["git", "commit", "-m", msg], cwd=str(ROOT), capture_output=True, text=True)
            if "nothing to commit" in (r.stdout + r.stderr):
                self.log("无变更，跳过提交")
            elif r.returncode == 0:
                self.log("提交成功")
            else:
                self.log(f"提交输出: {(r.stdout + r.stderr).strip()[-300:]}")
            if not git_run(["push"], self.log):
                self.log("推送失败，请检查 SSH/网络")
            else:
                self.log("已推送 GitHub ✓")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
