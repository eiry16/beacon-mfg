#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 一键工具（图形界面）—— 高德爬取 + 校验 + 上传 Git 仓库

依赖：仅 Python 标准库（tkinter，Windows 自带）
流程：选品类 → 开始 → 后台线程执行 抓取/校验/提交推送 → 日志实时显示

配额说明：高德官方 FAQ，个人认证开发者「搜索类」接口日配额约 100 次
（企业 1000 次；非官方渠道说法 2000-5000，以控制台实际为准）。
超限后当日停止，次日 00:00 重置。脚本默认按 100 限制，可在界面调整。

用法: python scripts/gui_app.py
"""
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

DAILY_QUOTA_DEFAULT = 1000  # 高德个人认证开发者搜索类日配额（官方 FAQ 保守值）

# 子进程（postfetch / validate / translate_en）用哪个解释器跑。
#
# 坑：venv 里的 python 装齐了采集依赖但**通常没有 tkinter**，而系统 python 有
# tkinter 却没有项目依赖 —— 两边都不完整。若子进程沿用 sys.executable，
# 拿哪个启动 GUI 就决定了子进程会不会 ModuleNotFoundError。
# 所以显式可配：用 BEACON_PY 指向「装齐依赖」的那个 python。
PY = os.environ.get("BEACON_PY") or sys.executable


def run_cmd(args, log, tail=40, timeout=None):
    """统一跑子进程：强制 UTF-8 编解码，并**流式**回传输出到 GUI 日志框。

    坑1（编码）：Windows 中文环境下 locale 编码是 gbk，子进程若不强制 UTF-8，
    输出中文会在读取线程里抛 UnicodeDecodeError。故给子进程设
    PYTHONUTF8/PYTHONIOENCODING，父进程用 utf-8 + errors=replace 解码。

    坑2（看不到进度）：旧实现用 subprocess.run(capture_output=True)，会等子进程
    彻底跑完才一次性把输出倒进日志框 —— 英文翻译这种跑几十分钟的步骤，期间 GUI
    日志框全程停在标题行、毫无滚动，看着像卡死。现改为 Popen + 双读线程：stdout /
    stderr 各开一个后台线程实时逐行回传 log()，GUI 日志框跟着滚动，长跑也能看到进度。
    （tail 参数保留以兼容旧调用方，但流式模式不再截断，全部实时显示。）
    """
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    log(f"$ {' '.join(str(a) for a in args)}")
    try:
        p = subprocess.Popen(
            [str(a) for a in args], cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except OSError as e:
        log(f"[启动失败] {e}")
        return False

    # 两个管道各开一个读线程，避免某一侧写满导致死锁；逐行实时回传 log()。
    # log() 内部用 root.after(0, ...) 把更新调度回主线程，跨线程更新 tkinter 安全。
    def _pump(pipe, prefix):
        try:
            for line in pipe:
                log(prefix + line.rstrip("\r\n"))
        except Exception:
            pass

    t_out = threading.Thread(target=_pump, args=(p.stdout, "  "), daemon=True)
    t_err = threading.Thread(target=_pump, args=(p.stderr, "  [stderr] "), daemon=True)
    t_out.start()
    t_err.start()
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"[超时] {' '.join(str(a) for a in args)}（{timeout}s）")
        try:
            p.kill()
        except Exception:
            pass
        t_out.join()
        t_err.join()
        return False
    t_out.join()
    t_err.join()
    if rc != 0:
        log(f"  [返回码 {rc}]")
    return rc == 0


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


# 把 .env 里的变量补进运行环境（只补缺失的，不覆盖已显式设置的系统值），
# 让子进程（postfetch → deploy_pages / publish_r2 → 读 CLOUDFLARE_* / AMAP_KEY）能拿到凭证。
# 否则 run_cmd 用的是 os.environ.copy()（系统环境），.env 里的 Cloudflare 凭证对子进程不可见，
# 即便去掉 --skip r2,pages，发布也会因缺凭证失败。
for _k, _v in load_env().items():
    os.environ.setdefault(_k, _v)


def git_run(args, log):
    """在仓库目录执行 git 命令（UTF-8 解码，中文路径也不会崩）"""
    return run_cmd(["git"] + args, log)


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
        self.status_label = tk.Label(root, text=f"高德 Key {amap_ok}   智谱 Key（翻译）{zhipu_ok}   "
                            f"（点击「设置」可修改）",
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

        # 采集层级：直接对应 fetch_batch.plan(tier=...) 的过滤口径
        #   - 全部：plan(tier="all", industries=手工勾选的品类)
        #   - core / extended / service / retail / tech / leisure：整层跑，忽略手工勾选
        #     （否则会出现"界面选了 service、却因没勾码而跑空"的陷阱）
        #
        # 2026-09-14：补上 retail/tech/leisure 三层。它们与 service 一起构成
        # 非制造门类（F/H/I/M/O/R），缺了这三项下拉，界面上根本选不到新门类，
        # 只能靠命令行 —— 那等于把「7 大门类全覆盖」的设计面藏起来。
        tier_row = tk.Frame(root, bg="#10151d")
        tier_row.pack(fill="x", padx=20, pady=(0, 2))
        tk.Label(tier_row, text="采集层级：", bg="#10151d", fg="#9aa7b8",
                 font=("Segoe UI", 10)).pack(side="left")
        self.tier_map = {
            "all": "全部（按上方勾选的品类）",
            "core": "core · 现有制造覆盖",
            "extended": "extended · 补制造新行业",
            "service": "service · 住宿餐饮 + 居民服务修理",
            "retail": "retail · F 批发零售",
            "tech": "tech · I 信息技术 + M 科研技术",
            "leisure": "leisure · R 文体娱乐",
        }
        self.tier_var = tk.StringVar(value=self.tier_map["all"])
        om = tk.OptionMenu(tier_row, self.tier_var, *self.tier_map.values())
        om.configure(bg="#1d242e", fg="#c9d6e8", activebackground="#2a3340",
                     activeforeground="#fff", highlightthickness=0, relief="flat")
        om["menu"].configure(bg="#1d242e", fg="#c9d6e8", activebackground="#2a3340")
        om.pack(side="left", padx=6)

        # 参数
        param = tk.Frame(root, bg="#10151d")
        param.pack(fill="x", padx=20, pady=4)
        tk.Label(param, text="每日 API 请求上限：", bg="#10151d", fg="#9aa7b8").pack(side="left")
        self.quota_var = tk.StringVar(value=str(DAILY_QUOTA_DEFAULT))
        tk.Entry(param, textvariable=self.quota_var, width=8, bg="#161b23", fg="#fff",
                 insertbackground="#fff", relief="flat", highlightthickness=1,
                 highlightbackground="#2a3340").pack(side="left", padx=6)
        tk.Label(param, text="每任务条数：", bg="#10151d", fg="#9aa7b8").pack(side="left", padx=(16, 0))
        # 默认 200 = 高德同参数翻页硬上限（翻到底）。以前默认 40 只翻 2 页，
        # 抓到的永远是排序最前那一批，去重后新增常年为 0。
        self.limit_var = tk.StringVar(value="200")
        tk.Entry(param, textvariable=self.limit_var, width=6, bg="#161b23", fg="#fff",
                 insertbackground="#fff", relief="flat", highlightthickness=1,
                 highlightbackground="#2a3340").pack(side="left", padx=6)

        # 选项
        opt = tk.Frame(root, bg="#10151d")
        opt.pack(fill="x", padx=20, pady=4)
        self.do_validate = tk.BooleanVar(value=True)
        self.do_english = tk.BooleanVar(value=False)
        self.do_git = tk.BooleanVar(value=True)
        self.do_schedule = tk.BooleanVar(value=True)
        self.do_district = tk.BooleanVar(value=True)
        self.do_publish = tk.BooleanVar(value=True)   # 发布到 Cloudflare（R2 + Pages）
        self.do_profile = tk.BooleanVar(value=False)  # 自动补能力卡（仅补本轮新抓城市，烧 LLM，故默认关）
        tk.Checkbutton(opt, text="校验数据", variable=self.do_validate, bg="#10151d", fg="#c9d6e8",
                       selectcolor="#1d242e", activebackground="#10151d").pack(side="left", padx=6)
        tk.Checkbutton(opt, text="账本调度", variable=self.do_schedule, bg="#10151d", fg="#c9d6e8",
                       selectcolor="#1d242e", activebackground="#10151d").pack(side="left", padx=6)
        tk.Checkbutton(opt, text="区县分片", variable=self.do_district, bg="#10151d", fg="#c9d6e8",
                       selectcolor="#1d242e", activebackground="#10151d").pack(side="left", padx=6)
        tk.Checkbutton(opt, text="生成英文版（GLM 翻译）", variable=self.do_english, bg="#10151d", fg="#c9d6e8",
                       selectcolor="#1d242e", activebackground="#10151d").pack(side="left", padx=6)
        tk.Checkbutton(opt, text="提交并推送 GitHub", variable=self.do_git, bg="#10151d", fg="#c9d6e8",
                       selectcolor="#1d242e", activebackground="#10151d").pack(side="left", padx=6)
        tk.Checkbutton(opt, text="发布到 Cloudflare", variable=self.do_publish, bg="#10151d", fg="#c9d6e8",
                       selectcolor="#1d242e", activebackground="#10151d").pack(side="left", padx=6)
        tk.Checkbutton(opt, text="自动补能力卡", variable=self.do_profile, bg="#10151d", fg="#c9d6e8",
                       selectcolor="#1d242e", activebackground="#10151d").pack(side="left", padx=6)

        # 设置 + 开始按钮
        btn_row = tk.Frame(root, bg="#10151d")
        btn_row.pack(pady=10)
        tk.Button(btn_row, text="⚙ 设置", command=self.open_settings,
                  bg="#1d242e", fg="#9aa7b8", font=("Segoe UI", 10),
                  relief="flat", padx=16, pady=8, activebackground="#2a3340",
                  activeforeground="#fff").pack(side="left", padx=8)
        self.start_btn = tk.Button(btn_row, text="▶ 开始一键采集发布", command=self.start,
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

    def open_settings(self):
        """设置弹窗：用户输入高德 Key + 智谱 Key，保存到 .env"""
        win = tk.Toplevel(self.root)
        win.title("⚙ 设置 · API Keys")
        win.geometry("480x300")
        win.configure(bg="#10151d")
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="高德 Web 服务 API Key", font=("Segoe UI", 11, "bold"),
                 bg="#10151d", fg="#e8edf4").pack(anchor="w", padx=20, pady=(16, 4))
        amap_entry = tk.Entry(win, width=52, bg="#161b23", fg="#fff",
                              insertbackground="#fff", relief="flat", show="",
                              highlightthickness=1, highlightbackground="#2a3340")
        amap_entry.insert(0, self.env.get("AMAP_KEY", ""))
        amap_entry.pack(padx=20, fill="x")
        tk.Label(win, text="高德开放平台 → 控制台 → 应用管理 → 创建应用（Web服务）",
                 font=("Segoe UI", 8), bg="#10151d", fg="#6b7686").pack(anchor="w", padx=20)

        tk.Label(win, text="智谱 API Key（英文翻译用，可选）", font=("Segoe UI", 11, "bold"),
                 bg="#10151d", fg="#e8edf4").pack(anchor="w", padx=20, pady=(12, 4))
        zhipu_entry = tk.Entry(win, width=52, bg="#161b23", fg="#fff",
                               insertbackground="#fff", relief="flat", show="",
                               highlightthickness=1, highlightbackground="#2a3340")
        zhipu_entry.insert(0, self.env.get("ZHIPU_API_KEY", ""))
        zhipu_entry.pack(padx=20, fill="x")

        status = tk.Label(win, text="", font=("Segoe UI", 9), bg="#10151d", fg="#9fe1cb")
        status.pack(pady=8)

        def save():
            amap_val = amap_entry.get().strip()
            zhipu_val = zhipu_entry.get().strip()
            if not amap_val:
                status.configure(text="高德 Key 不能为空", fg="#e85d5d")
                return
            # 写入 .env（保留其他行）
            existing = {}
            if ENV_FILE.exists():
                for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        existing[k.strip()] = v.strip()
            existing["AMAP_KEY"] = amap_val
            existing["ZHIPU_API_KEY"] = zhipu_val
            lines = [f"{k}={v}" for k, v in existing.items()]
            ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
            # 更新当前内存
            self.env["AMAP_KEY"] = amap_val
            self.env["ZHIPU_API_KEY"] = zhipu_val
            amap_ok = "✓" if amap_val else "✗"
            zhipu_ok2 = "✓" if zhipu_val else "✗"
            self.status_label.configure(text=f"高德 Key {amap_ok}   智谱 Key（翻译）{zhipu_ok2}   "
                                        f"（点击「设置」可修改）")
            status.configure(text="✓ 已保存到 .env", fg="#9fe1cb")
            win.after(800, win.destroy)

        tk.Button(win, text="保存", command=save,
                  bg="#0f6e56", fg="#fff", font=("Segoe UI", 11, "bold"),
                  relief="flat", padx=32, pady=6, activebackground="#0b5a47",
                  activeforeground="#fff").pack(pady=8)

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
        # 下拉「采集层级」→ plan 的 tier 参数；具体层整层跑、忽略手工勾选
        # 反向查 tier：下拉里存的是中文标签，plan 要的是英文键。
        # 用带默认值的 next —— 万一标签对不上也别把整个采集崩掉。
        tier = next((k for k, v in self.tier_map.items() if v == self.tier_var.get()), "all")
        if tier == "all" and not selected:
            self.log("请至少选择一个品类，或把「采集层级」设为具体层级（制造/服务）")
            return
        quota = int(self.quota_var.get() or DAILY_QUOTA_DEFAULT)
        limit = int(self.limit_var.get() or 40)

        # 1. 采集
        sys.path.insert(0, str(ROOT / "scripts"))
        import fetch_gaode_poi as fetcher
        import fetch_batch as batch
        import fetch_cursor as cursor
        fetcher.AMAP_KEY = amap_key
        fetcher.REQUEST_COUNT = 0
        fetcher.QUOTA_EXHAUSTED = False
        # 翻页内也要有护栏：只在任务之间检查配额，一个多页任务照样能打穿上限
        fetcher.MAX_REQUESTS = quota

        if not fetcher.acquire_fetch_lock():
            self.log("⛔ 另一抓取进程已在进行，本次采集中止以避免 id 区间重叠碰撞。请稍后重试。")
            return

        # tier 下拉语义：
        #  - "all"：按手工勾选的品类运行（plan 不过滤 tier，仅 industries 交集）
        #  - 具体层(core/extended/service)：整层跑，industries=None 忽略手工勾选，
        #    避免「界面选了 service 却因没勾码而跑空」的陷阱
        if tier == "all":
            industries = set(selected)
            preview = sorted(selected)[:12]
            scope = f"层级=全部，勾选品类 {len(selected)} 个"
        else:
            industries = None
            preview = sorted(c for c, (t, *_d) in batch.JOBS.items() if t == tier)[:12]
            scope = f"层级={tier}（整层运行，忽略手工勾选）"
        tasks = batch.plan(tier=tier, industries=industries)
        self.log(f"计划 {len(tasks)} 个任务（{scope}）："
                 f"{'、'.join(preview)}{' …' if len(preview) == 12 else ''}")

        # 账本调度：不排序的话每次都从 JOBS 字典序第一个任务开始，配额全喂给头部，
        # 后面 60%（extended / service 全部新行业）一次都轮不到。
        ledger = None
        if self.do_schedule.get():
            ledger = cursor.load()
            # 翻到底还不够的组合，按区县 adcode 再切片：官方 200 条上限是按请求参数算的，
            # 换 adcode 就是全新的 200 条（实测全市 vs 虎丘区第 1 页只重叠 1 条）
            if self.do_district.get():
                tasks, n_exp = cursor.expand(tasks, ledger, key=fetcher.AMAP_KEY)
                if n_exp:
                    self.log(f"[账本] {n_exp} 个组合已翻到底且货很多 → 拆成区县分片"
                             f"（各再得 200 条）")
                    cursor.save(ledger)
            st = cursor.summary(tasks, ledger)
            self.log(f"[账本] 分片 {st['total']} 个（区县层 {st['districts']}）："
                     f"从未跑过 {st['never']} / 已到期 {st['due']} / 冷却中跳过 {st['cooling']}")
            per_task = max(1, min(cursor.DEEP_PAGES,
                                  -(-min(limit, 200) // cursor.PAGE_SIZE)))
            tasks = cursor.order(tasks, ledger, budget=quota, per_task=per_task)
            self.log(f"[账本] 预算 {quota} 请求，选中 {len(tasks)} 个任务"
                     f"（没跑过的先跑；只挑能完整跑完的，不让翻页半途被掐断）")
            if not tasks:
                self.log("[账本] 所有分片都在冷却期内，本轮无可跑任务")
        else:
            self.log("[账本] 已关闭调度，按 JOBS 固定顺序跑（每次都从同一个任务开始）")

        self.log(f"每日配额上限 {quota} 次请求，每任务目标 {limit} 条")
        self._profile_cities = set()  # 本轮抓到的城市（城市层，供「自动补能力卡」用）
        for i, (code, cat, kw, city) in enumerate(tasks, 1):
            if fetcher.REQUEST_COUNT >= quota:
                self.log(f">>> 已用满 {quota} 次配额，停止采集（次日 00:00 重置）")
                break
            if getattr(fetcher, "QUOTA_EXHAUSTED", False):
                self.log(">>> 高德返回配额已用尽，停止采集（次日 00:00 重置）")
                break
            self.log(f"[{i}/{len(tasks)}] {cat} × {kw} × {city}")
            if not (city.isdigit() and len(city) == 6):
                self._profile_cities.add(city)  # 城市层任务（非区县 adcode）才纳入自动补卡
            pois = []
            added = 0
            try:
                pois = fetcher.fetch(kw, city, limit)
                # industry_code=code 把国标小类码传给入库分类器做兜底证据，
                # 与 CLI 的 fetcher.save_suppliers(pois, cat, kw, industry_code=code) 一致
                added = fetcher.save_suppliers(pois, cat, kw, industry_code=code)
                self.log(f"  新增 {added} 条；本次已用请求 {fetcher.REQUEST_COUNT}/{quota}")
            except Exception as e:
                self.log(f"  失败: {e}")
            # 抓多抓少都要记账：不记的话下轮它还排「从未跑过」的队首，重复烧配额
            if ledger is not None:
                # 用 fetch() 自己报的元信息，别靠 len(pois) < limit 猜：
                # 撞上 200 条上限时 pois 正好等于 limit，猜会误判成「还没到底」
                meta = getattr(fetcher, "LAST_FETCH", None) or {}
                cursor.record(cursor.key_of(code, kw, city), len(pois), added,
                              meta.get("exhausted", len(pois) < limit),
                              int(meta.get("requests", 0) or 0), ledger)
                if i % cursor.FLUSH_EVERY == 0:
                    cursor.save(ledger)
            time.sleep(1)

        if ledger is not None:
            cursor.save(ledger)
            self.log(f"[账本] 已保存 {cursor.LEDGER.name}"
                     f"（累计 {len(ledger.get('shards', {}))} 个分片）")

        # 1.5 重建派生层（索引/指纹/能力卡分片/清单/校验/上云发布）
        # r2/pages（上传 R2 + 发布 Cloudflare Pages）现已接入：.env 的 CLOUDFLARE_* 凭证
        # 已由模块顶部注入 os.environ，子进程（deploy_pages/publish_r2）读得到。
        # 默认勾选「发布到 Cloudflare」即会跑；不想上云就取消勾选。
        # 「自动补能力卡」仅对本轮抓到的城市增量补（不 --clean），烧 LLM，默认关。
        skip = ["r2", "pages"]
        if self.do_publish.get():
            skip = []
        apo = []
        if self.do_profile.get():
            cities = ",".join(sorted(getattr(self, "_profile_cities", set())))
            if cities:
                apo = ["--autoprofile-cities", cities]
                self.log(f"  自动补能力卡范围：{cities}（仅增量，不清空已有卡）")
            else:
                self.log("  （本轮无城市层任务，跳过自动补卡）")
        self.log("\n== 重建派生层" + (" + 上云发布 ==" if self.do_publish.get()
                 else "（本次跳过上云发布）=="))
        ok = run_cmd([PY, str(ROOT / "scripts" / "postfetch.py"), "--skip", ",".join(skip)] + apo,
                     self.log, tail=40)
        if ok:
            self.log("  派生层重建完成 ✓")
        else:
            self.log("  [警告] 派生层重建未成功返回（详见上方输出）；本地索引可能未完全更新")

        # 2. 校验
        if self.do_validate.get():
            self.log("\n== 数据校验 ==")
            run_cmd([PY, str(ROOT / "scripts" / "validate.py")], self.log, tail=40)

        # 3. 英文翻译
        if self.do_english.get():
            zhipu = env.get("ZHIPU_API_KEY", "")
            if not zhipu:
                self.log("跳过英文翻译：.env 缺少 ZHIPU_API_KEY")
            else:
                # 2026-09-12：这里原来调 translate_en.py，但它读的 data/suppliers
                # 是 2026-09-08 已退役的 8 品类布局，一进来就报「目录不存在」直接退出 ——
                # 勾了「生成英文版」等于啥也没干。现役脚本是 en_backfill.py
                # （国标四级、增量、断点续跑），跑完再补 industry_en 标签。
                # 2026-09-15：Key 不再走命令行参数（--key），改为注入环境变量，
                # 否则 Get-CimInstance/ps 能直接看到明文 Key。en_backfill.py 优先读
                # ZHIPU_API_KEY 环境变量，没有才回退读 .env。
                os.environ["ZHIPU_API_KEY"] = zhipu
                self.log("\n== 英文翻译（GLM-4-Flash · 增量补齐）==")
                if run_cmd([PY, str(ROOT / "scripts" / "en_backfill.py")],
                           self.log, tail=40):
                    run_cmd([PY, str(ROOT / "scripts" / "en_sync_industry.py")],
                            self.log, tail=20)

        # 4. 上传 git
        if self.do_git.get():
            self.log("\n== 提交并推送 GitHub ==")
            # 先亮清单：git add -A 会把仓库里任何改动（含与本次采集无关的）
            # 一起卷进 commit。列出来，避免「不知情地提交了别人的改动」。
            st = subprocess.run(["git", "status", "--short"], cwd=str(ROOT),
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace")
            st_lines = [l for l in (st.stdout or "").splitlines() if l.strip()]
            if st_lines:
                self.log(f"  待提交 {len(st_lines)} 项（前 20 行）：")
                for line in st_lines[:20]:
                    self.log("    " + line)
                if len(st_lines) > 20:
                    self.log(f"    …另有 {len(st_lines) - 20} 项")
            else:
                self.log("  工作区无改动")
                return
            if not git_run(["add", "-A"], self.log):
                self.log("git add 失败")
                return
            ts = time.strftime("%Y-%m-%d %H:%M")
            msg = f"Auto update via GUI tool @ {ts}"
            r = subprocess.run(["git", "commit", "-m", msg], cwd=str(ROOT),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env={**os.environ, "PYTHONUTF8": "1"})
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
