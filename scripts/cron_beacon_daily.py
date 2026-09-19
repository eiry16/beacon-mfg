#!/usr/bin/env python
"""BeaconMFG 每日定时采集（取代已失效的旧 cron）。

等价 GUI「自动抓取」的默认口径，分两段跑（与 GUI 的 1.5 / 1.6 对齐）：
    1) 核心段 postfetch.py --skip english,autoprofile      （尽快上线当天中文数据）
    2) 增强段 postfetch.py --only english,autoprofile,capability,shards,manifest,readme,
                                    validate[,r2,pages][,git]   （英文 + 补卡 + 二次发布）

**第 2 段就是能力卡** —— 2026-09-17 之前这里被无脑 skip 掉，结果是新抓的城市
（如西南四城）一条能力卡都没有，App 里搜出来全是空白卡片。补卡是**纯本地规则
推断**（scripts/collect/auto_profile.py），不调 LLM、不烧钱，只是会写盘，所以
默认打开、单独成段：就算它失败，当天已经重建好的数据也照常发出去。

额外增加 GUI 没有的能力：**优先抓取城市**。把当日配额切给指定城市，用来集中
补齐某个区域（比如西南四城）。优先城市跑完（「从未跑过 / 已到期」归零）后会自动
回归全局轮转，不需要改回来。

用法：
    python scripts/cron_beacon_daily.py                          # 全局轮转（默认配置）
    python scripts/cron_beacon_daily.py --priority-cities 昆明,遵义
    python scripts/cron_beacon_daily.py --priority-share 0.6     # 60% 给优先城市，余下全局
    python scripts/cron_beacon_daily.py --daily-quota 2000
    python scripts/cron_beacon_daily.py --autoprofile-cities 贵阳,昆明
    python scripts/cron_beacon_daily.py --no-autoprofile         # 临时关掉补卡
    python scripts/cron_beacon_daily.py --dry-run                # 只打印计划，不烧配额

配置优先级：**命令行 > scripts/cron_priority_cities.json > 代码内默认值**。
长期调整建议直接改那个 JSON（已入库），临时覆盖用命令行。

配额说明（见 SKILL §14.2）：每任务最多 8 次请求（25 条/页 × 8 页 = 翻到底 200）。
默认日配额 1000 ≈ 125 个任务/轮。给四座新城市铺第一遍要 476 任务 ≈ 3808 请求，
按 1000/天算约 4 天 —— 这是正常节奏，不是卡住。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
CONFIG = SCRIPTS / "cron_priority_cities.json"
DEFAULT_LOG_DIR = Path("C:/DATA/QClaw/cron_beacon_logs")

# 每页 25 条、最多翻 8 页（fetch_cursor.PAGE_SIZE / DEEP_PAGES），与 fetch_batch 一致
PER_TASK_REQUESTS = 8

DEFAULTS = {
    "priority_cities": [],
    "priority_share": 1.0,
    "daily_quota": 1000,
    "tier": "all",
    "publish": True,
    "git": True,
    # 英文镜像：调 LLM 翻译，慢（2026-09-19 实测约 5,000~6,500 条/小时）且吃 ZHIPU_API_KEY。
    # 默认**开** —— 以前默认关，结果英文永远比中文晚一天上云（2026-09-18 起修正）。
    # 它跑在增强段最前，随当轮 r2/pages 一起发布，不再是滞后一天的尾巴。
    "english": True,
    # 能力卡（未认证）：纯本地规则推断，不调 LLM；默认开，单独成段跑
    "autoprofile": True,
    "autoprofile_limit": 300,      # 每城最多补多少家
    "autoprofile_cities": [],      # 为空则回退 priority_cities
}


# ------------------------------------------------------------------ 环境
def load_env() -> dict:
    """读 .env 并注入 os.environ —— 子进程要靠它拿 AMAP_KEY / Cloudflare / ZHIPU 凭证。

    GUI 用自己的 env 加载逻辑，这里与之等价：Key 只走环境变量，不拼进命令行，
    否则 `Get-CimInstance Win32_Process` 能看到明文。
    """
    env = {}
    dotenv = ROOT / ".env"
    if not dotenv.exists():
        return env
    for raw in dotenv.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        env[k] = v
        os.environ.setdefault(k, v)
    return env


def due_shards(cities: list[str], tier: str) -> int:
    """统计这些城市还有多少「从未跑过 / 已到期」的待跑任务（只读账本，不烧配额）。"""
    sys.path.insert(0, str(SCRIPTS))
    import fetch_batch as batch  # noqa: E402
    import fetch_cursor as cursor  # noqa: E402

    tasks = []
    for c in cities:
        tasks += batch.plan(tier, None, None, None, c)
    summary = cursor.summary(tasks, cursor.load())
    return int(summary.get("never", 0)) + int(summary.get("due", 0))


# ------------------------------------------------------------------ 执行
def run_step(name: str, cmd: list[str], dry_run: bool, log_fp) -> int:
    printable = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    print(f"\n== {name}\n   $ {printable}", flush=True)
    if log_fp:
        log_fp.write(f"\n== {name}\n   $ {printable}\n")
        log_fp.flush()
    if dry_run:
        print("   [dry-run] 已跳过执行", flush=True)
        if log_fp:
            log_fp.write("   [dry-run] 已跳过执行\n")
            log_fp.flush()
        return 0

    proc = subprocess.run(cmd, cwd=str(ROOT))
    print(f"   → 退出码 {proc.returncode}", flush=True)
    if log_fp:
        log_fp.write(f"   → 退出码 {proc.returncode}\n")
        log_fp.flush()
    return proc.returncode


def build_plan(args, cities_cfg: list[str], quota: int) -> list[dict]:
    """把当晚要跑的抓取阶段排出来（含是否跑后处理）。

    最后一个阶段负责触发后处理流水线，所以阶段数变化时不会出现「抓了没发布」。
    """
    py = sys.executable
    fetch_py = str(SCRIPTS / "fetch_batch.py")

    runs: list[dict] = []
    priority = list(args.priority_cities or cities_cfg)

    if priority:
        pending = due_shards(priority, args.tier)
        print(f"[优先城市] {'、'.join(priority)}：待跑任务 {pending} 个", flush=True)
        if pending == 0:
            print("[优先城市] 已无可跑任务 → 本轮自动回归全局轮转", flush=True)
        else:
            p_quota = int(round(quota * args.priority_share))
            runs.append({
                "name": f"抓取-优先城市 {'、'.join(priority)}（预算 {p_quota}）",
                "cmd": [py, fetch_py, "--city", ",".join(priority),
                        "--tier", args.tier, "--quota", str(p_quota), "--no-post"],
            })
            quota -= p_quota

    if quota > 0:
        runs.append({
            "name": f"抓取-全局轮转（预算 {quota}）",
            "cmd": [py, fetch_py, "--tier", args.tier, "--quota", str(quota), "--no-post"],
        })

    if not runs:
        return runs

    # 只有最后一段跑完后流水线 —— 中间段一律 --no-post，避免重复重建派生层
    runs[-1]["cmd"].remove("--no-post")
    return runs


def main() -> int:
    parser = argparse.ArgumentParser(description="BeaconMFG 每日定时采集")
    parser.add_argument("--priority-cities",
                        help="优先抓取城市，逗号分隔（覆盖配置文件）")
    parser.add_argument("--priority-share", type=float, default=None,
                        help="当日配额分配给优先城市的比例（默认 1.0 = 全给优先城市）")
    parser.add_argument("--daily-quota", type=int, default=None,
                        help="当日请求预算（默认 1000）")
    parser.add_argument("--tier", default=None,
                        choices=["core", "extended", "service", "retail", "tech",
                                 "leisure", "all"],
                        help="采集层级（默认 all）")
    parser.add_argument("--no-publish", action="store_true",
                        help="不发布 R2 / Pages（数据仍本地落盘）")
    parser.add_argument("--no-git", action="store_true", help="不推 GitHub")
    parser.add_argument("--english", action="store_true",
                        help="强制跑英文镜像（默认已按配置开启，此参数用于覆盖配置为关的情况）")
    parser.add_argument("--no-english", action="store_true",
                        help="本轮不跑英文镜像（覆盖配置文件里的 english:true）")
    parser.add_argument("--autoprofile-cities",
                        help="要给哪些城市补能力卡，逗号分隔（默认取优先城市；"
                             "都为空则本轮不补卡）")
    parser.add_argument("--autoprofile-limit", type=int, default=None,
                        help="每城最多补多少张能力卡（默认 300）")
    parser.add_argument("--no-autoprofile", action="store_true",
                        help="本轮不补能力卡（覆盖配置文件里的 autoprofile:true）")
    parser.add_argument("--config", help=f"配置文件路径（默认 {CONFIG.name}）")
    parser.add_argument("--log-dir", help=f"日志目录（默认 {DEFAULT_LOG_DIR}）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不实际抓取")
    args = parser.parse_args()

    # 配置：命令行 > JSON > 默认值
    cfg_path = Path(args.config) if args.config else CONFIG
    cfg: dict = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:  # 配置坏了不能让定时任务整个挂掉
            print(f"[警告] 配置文件解析失败，回退默认值：{cfg_path} -> {e}")

    args.priority_cities = ([c.strip() for c in args.priority_cities.split(",") if c.strip()]
                            if args.priority_cities else None)
    cities_cfg = list(cfg.get("priority_cities") or DEFAULTS["priority_cities"])
    share = args.priority_share if args.priority_share is not None else float(
        cfg.get("priority_share", DEFAULTS["priority_share"]))
    args.priority_share = min(1.0, max(0.0, share))
    quota = args.daily_quota if args.daily_quota is not None else int(
        cfg.get("daily_quota", DEFAULTS["daily_quota"]))
    args.tier = args.tier or cfg.get("tier", DEFAULTS["tier"])

    # 能力卡：--no-autoprofile 关掉；否则看配置；城市默认回退优先城市
    do_autoprofile = (not args.no_autoprofile) and bool(
        cfg.get("autoprofile", DEFAULTS["autoprofile"]))
    ap_limit = args.autoprofile_limit if args.autoprofile_limit is not None else int(
        cfg.get("autoprofile_limit", DEFAULTS["autoprofile_limit"]))
    args.autoprofile_cities = (
        [c.strip() for c in args.autoprofile_cities.split(",") if c.strip()]
        if args.autoprofile_cities else None)
    do_publish = (not args.no_publish) and bool(cfg.get("publish", DEFAULTS["publish"]))
    do_git = (not args.no_git) and bool(cfg.get("git", DEFAULTS["git"]))
    do_english = ((not args.no_english)
                  and (args.english or bool(cfg.get("english", DEFAULTS["english"]))))

    env = load_env()
    if not env.get("AMAP_KEY") and not os.environ.get("AMAP_KEY"):
        print("[致命] 拿不到 AMAP_KEY（检查 .env 是否存在且可读）")
        return 1

    log_dir = Path(args.log_dir) if args.log_dir else DEFAULT_LOG_DIR
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"=== BeaconMFG 每日采集 {stamp} ===")
    print(f"    仓库       : {ROOT}")
    print(f"    配置       : {cfg_path}（{'存在' if cfg_path.exists() else '缺失，用默认值'}）")
    print(f"    日配额     : {quota} 请求 ≈ {quota // PER_TASK_REQUESTS} 个任务")
    print(f"    层级       : {args.tier}")
    print(f"    发布 / git : {do_publish} / {do_git}")
    print(f"    英文镜像   : {'开（增强段最前，随当轮发布）' if do_english else '关'}")
    _ap_cities = (args.autoprofile_cities
                  or list(cfg.get("autoprofile_cities") or [])
                  or list(cities_cfg))
    print(f"    能力卡     : {'开' if do_autoprofile else '关'}"
          f"（{'、'.join(_ap_cities) if _ap_cities else '无城市'}，每城 ≤{ap_limit} 家）")

    log_fp = None
    if not args.dry_run:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_fp = (log_dir / f"cron_beacon_{stamp}.log").open("a", encoding="utf-8")

    try:
        runs = build_plan(args, cities_cfg, quota)
        if not runs:
            print("\n无可跑阶段（配额被切成 0）。调整 --daily-quota 或配置文件。")
            return 1

        codes = []
        for step in runs:
            codes.append(run_step(step["name"], step["cmd"], args.dry_run, log_fp))

        busy = [c for c in codes if c == 3]
        if len(busy) == len(codes):
            print("\n== 后处理流水线")
            print("   ⊘ 全部抓取阶段都因「另一个抓取进程正在运行」退出（exit 3），"
                  "本次没有产生新数据，跳过后处理")
            return 3

        # 核心流水线：英文与能力卡都挪到增强段，这里只跑「抓取 → 中文派生层 → 发布」。
        # 理由：英文调 LLM 很慢（约 5,000~6,500 条/小时），挂在核心段会把当天的发布一起拖住。
        # 挪走之后核心段尽快上线当天中文数据，英文/能力卡由增强段补跑并二次发布。
        skip = ["english"]
        skip.append("autoprofile")
        if not do_publish:
            skip += ["r2", "pages"]
        if not do_git:
            skip.append("git")
        run_step("后处理流水线（核心段）",
                 [sys.executable, str(SCRIPTS / "postfetch.py"), "--skip", ",".join(skip)],
                 args.dry_run, log_fp)

        # 增强段：补能力卡 + 重新切分片/发布。
        # 单独跑的意义：补卡失败也只损失「这轮没增强」，核心段已发布的当天数据不受影响。
        ap_cities = (args.autoprofile_cities
                     or list(cfg.get("autoprofile_cities") or [])
                     or list(cities_cfg))
        # 英文必须排在本段最前：step_english 要求它在 shards / manifest 之前，
        # 先把清单算出来再补英文 = 清单里那批英文分片是空的。
        # --only 按 ALL_STEPS 原序执行，而 english 本来就排在 autoprofile 之前，故直接拼接即可。
        steps: list[str] = []
        if do_english:
            steps.append("english")
        if do_autoprofile and ap_cities:
            steps += ["autoprofile", "capability"]
        steps += ["shards", "manifest", "readme", "validate"]
        if do_publish:
            steps += ["r2", "pages"]
        if do_git:
            steps.append("git")

        if steps:
            cmd = [sys.executable, str(SCRIPTS / "postfetch.py"),
                   "--only", ",".join(steps)]
            if do_english:
                cmd.append("--english")
            if do_autoprofile and ap_cities:
                cmd += ["--autoprofile-cities", ",".join(ap_cities),
                        "--autoprofile-limit", str(ap_limit)]
            bits = []
            if do_english:
                bits.append("英文镜像")
            if do_autoprofile and ap_cities:
                bits.append("能力卡 " + "、".join(ap_cities))
            run_step("后处理流水线（增强段·%s）" % (" + ".join(bits) or "仅重建分片/发布"),
                     cmd, args.dry_run, log_fp)
        elif do_autoprofile:
            print("\n[提示] 开了补卡但没有城市（配置 autoprofile_cities 与 priority_cities 都为空），"
                  "本轮跳过能力卡")

        if any(c != 0 for c in codes):
            print("\n[提示] 有抓取阶段非 0 退出（3=被别的抓取挡住；其它见其输出）")
        return 0
    finally:
        if log_fp:
            log_fp.close()


if __name__ == "__main__":
    raise SystemExit(main())
