#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Beacon-MFG 只读 MCP 服务（stdio 传输，零第三方依赖）

设计边界：
  - **只读**：只检索「已发布数据」—— 公开 CDN 端点（默认）或本地 git 仓库的
    已提交快照。绝不写入、绝不调用任何后端写入脚本，绝不持有任何密钥。
  - **不污染仓库**：本地模式只通过 `git show HEAD:<path>` 读取已提交内容，
    不触碰本地副本与索引；本服务**绝不**对仓库执行任何写操作。
  - 手机号为隐私字段：已提交版本是隐藏态（138****0000）；部署版可能是全号。
    对隐私敏感场景，把 BEACON_REPO 指向本地仓库（默认即读取隐藏态）即可，无需联网。

协议：JSON-RPC 2.0 over stdio（newline-delimited）。兼容 Claude Desktop / Cline /
Continue / 常见 MCP 客户端 等主流 MCP 客户端。

数据源解析：
  BEACON_SOURCE  HTTP 基址（默认 https://beacon-mfg.pages.dev）
  BEACON_REPO    本地 git 仓库路径；设了就用 `git show HEAD:` 读（推荐：离线 + 隐私安全）
                缺省时自动探测 cwd 所在 git 仓库（若含 data/gb 就用它）

暴露的 tool：
  search_vendors       按 关键词 / 城市 / 国标码 检索
  get_vendor           按 id（+ 国标码）取完整中文档案
  get_capability_card  按 id 取能力卡
"""

import os
import re
import sys
import math
import json
import hashlib
import concurrent.futures
import subprocess
import tarfile
import tempfile
import io
import time
import hmac
import urllib.request
import urllib.error

# [utf8-guard] 中文 Windows 上，宿主若没注入 PYTHONUTF8/PYTHONIOENCODING，stdout 会落回
# cp936；而 TOOLS 的 tool 描述里含 `⚠`(U+26A0) 这类 GBK 根本编不出的字符 ——
# initialize 全是 ASCII 能过，一到 `tools/list` 就 UnicodeEncodeError **当场崩进程**。
# 强制 UTF-8 + 出错降级替换：宁可显示成 '?'，也绝不因为一次打印而断开协议。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import socket
from typing import Any, Dict, List, Optional, Tuple
import sys as _sys
import uuid as _uuid

# --------------------------------------------------------------------------- #
# 寻源内核 桥接（G1/G2/G3：让 MCP 客户 agent 能跑多轮对话匹配）
# 路径相对本文件解析，与 cwd / BEACON_REPO 无关。桥接不可用时降级，不影响其余 tool。
# --------------------------------------------------------------------------- #
_MCP_DIR = os.path.dirname(os.path.abspath(__file__))
# 解析顺序：
#  ① 仓库平级目录 ../skills/寻源内核/src —— 本地 clone（git 仓库）优先用**活源**
#  ② 包内自带副本 mcp/寻源内核/src —— `npm i -g beacon-mfg-mcp` 安装后兜底
#     （prepack 会把 ../skills/寻源内核 拷进 mcp/寻源内核，见 package.json）
# 与 cwd / BEACON_REPO 无关。桥接不可用时降级，不影响其余 tool。
_RFK_CANDIDATES = [
    os.path.join(_MCP_DIR, "..", "skills", "rfq-kernel", "src"),
    os.path.join(_MCP_DIR, "rfq-kernel", "src"),
]
_RFK_SRC = next((p for p in _RFK_CANDIDATES if os.path.isdir(p)), None)
if _RFK_SRC and _RFK_SRC not in _sys.path:
    _sys.path.insert(0, _RFK_SRC)
try:
    import mcp_bridge as _bridge
except Exception:  # 桥接缺失/异常 → 降级，MCP 其余 3 个只读 tool 照常
    _bridge = None

_SESSIONS: Dict[str, Any] = {}


def _new_session_id() -> str:
    return _uuid.uuid4().hex


_gb_seed_cache: Dict[str, List[Dict[str, Any]]] = {}


def _gb_seed_records(pack_id: str) -> List[Dict[str, Any]]:
    """GB 种子：检测到某行业 pack 时，把该 pack 国标码（GB/T 4754）映射的企业补进召回。

    按 gb 分片精准取（与命中量相关，绝非全量扫描），进程内缓存复用。
    语义依据：beacon-mfg 以国标码为唯一行业判别信号，检测到行业即应召回其国标分类下的企业，
    即便其厂名不含召回词（如 conveyor 的 gb=3434 连续搬运设备厂「耐特斯传输设备」用『传输』
    而非『输送』，而『传输』df=1 未入预构建索引，常规召回捞不到）。加法召回，绝不误删真实企业。
    """
    cached = _gb_seed_cache.get(pack_id)
    if cached is not None:
        return cached
    out: List[Dict[str, Any]] = []
    try:
        gbs = _bridge.gbs_of_pack(pack_id)
    except Exception:
        gbs = set()
    if gbs:
        for s in _shards_of_type("fp"):
            c = str(s.get("c") or "")
            if c in gbs or c[:2] in gbs:
                try:
                    out.extend(_read_fp_shard(s))
                except Exception:
                    continue
    _gb_seed_cache[pack_id] = out
    return out


def _recall_for_sourcing(text: str, top_k: int = 200, pack_id: str | None = None) -> List[Dict[str, Any]]:
    """为匹配做宽召回：对『产品信号』（工艺/材料/认证/国标码）与『企业名』命中打分，取 top_k。

    **并集召回**：需求词被切成 bigram 后，任意一个命中即算候选（OR），不做交集。
    （旧的 `_candidate_shards` 对同一需求词内部的 bigram 求交，会把「钣金冲压」这类
    连写词缩到只剩字面全含的极少数分片，属隐性漏召回。）

    证据权重：
      - 产品信号命中（proc/mat/cert/gb）权重 3x；企业名 `co` 命中权重 1x。
        企业名是**合法证据**——大量长尾厂只把品类写在厂名里（如「中山市世通输送机械设备
        有限公司」的 proc 只有 cnc_milling），只信 proc/mat 会让这些真实企业永远搜不到。
      - 每个命中词按 IDF 加权：稀有词（「输送」df=6）权重大，泛词（城市名「上海」df≈1.3万、
        通名「厂家」）权重小 —— 需求句里的地区/通名不会带偏排序。
      - 传入 pack_id 时，国标码命中本行业的记录 +1000，确保本行业供应商排到 top_k 前列。

    性能：优先走预构建索引的快速路径；索引不可用或并集过大时回退全量扫描。只读。
    """
    toks = _collapse_prefixes({t for t in _grams(text, query_mode=True) if _usable_gram(t)})
    if not toks:
        return []

    idf = _gram_idf(toks)
    if not idf:                       # 索引不可用 -> 退化为均匀权重
        idf = {t: 1.0 for t in toks}
    recs = _recall_candidates(toks)
    if recs is None:
        recs = _build_fp_index()  # 兜底：全量扫描（首次构建并落盘缓存，后续进程内复用）

    # GB 种子：检测到的行业 pack，其国标码映射的企业（如 conveyor 的 gb=3434 连续搬运设备）
    # 即便厂名不含召回词（如「耐特斯传输设备」用『传输』而非『输送』，而『传输』df=1 未入索引），
    # 也作为语义对应补进召回（加法，绝不误删真实企业）。下方 +1000 国标命中加成自然覆盖。
    if pack_id and _bridge is not None:
        seed = _gb_seed_records(pack_id)
        if seed:
            seen_ids = {r.get("id") for r in recs if r.get("id")}
            for s in seed:
                sid = s.get("id")
                if sid and sid not in seen_ids:
                    recs.append(s)
                    seen_ids.add(sid)

    # 本 pack 的产品词面（小写）：用于给「厂名含产品词但 gb 未映射本行业」的长尾真实企业加成
    name_surfaces = [s.lower() for s in _bridge.pack_vocab_surfaces(pack_id)] if pack_id else []

    scored: List[tuple] = []
    for r in recs:
        prod_hay = (" ".join(r.get("proc") or []) + " " +
                    " ".join(r.get("mat") or []) + " " +
                    " ".join(r.get("cert") or []) + " " +
                    (r.get("gb") or "")).lower()
        name_hay = (r.get("co") or "").lower()
        w = 0.0
        for t in toks:
            if not t or t not in idf:      # 碎片/泛词不参与打分
                continue
            iw = idf[t]
            if t in prod_hay:
                w += 3.0 * iw
            elif t in name_hay:
                w += 1.0 * iw
        # 国标码命中检测行业 / 厂名含本 pack 产品词：本行业强信号，即便需求词全为
        # 稀有词（df<2，如「风送线」）导致 token 打分全为 0，也要保留——这些是
        # 真实供应商（GB 种子加法补召的 3434 搬运设备厂、或厂名含产品词的长尾厂），
        # 不能因为「没有高频区分词」就被 w<=0 一概滤除（beacon-mfg 红线：真实企业必须被看见）。
        gb_hit = pack_id and _bridge is not None and _bridge.pack_of_gb(r.get("gb")) == pack_id
        name_hit = pack_id and name_surfaces and any(s in name_hay for s in name_surfaces)
        if w <= 0 and not (gb_hit or name_hit):
            continue
        # 国标码命中检测行业 -> 加权，确保本行业供应商排到 top_k 前列
        if gb_hit:
            w += 1000.0
        # 厂名含本 pack 产品词、但 gb 未映射本行业的长尾真实企业（如输送厂 gb=3360/3451
        # 而非 3434）：同样视为本行业强信号给次高加成，避免被 GB 种子挤到池底
        # （beacon-mfg 红线：真实企业必须被看见）。
        elif name_hit:
            w += 500.0
        r2 = dict(r)
        r2["_recall_relevance"] = round(w, 4)
        scored.append((w, r2))
    scored.sort(key=lambda x: -x[0])
    return [r2 for _, r2 in scored[:top_k]]


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
DEFAULT_SOURCE = "https://beacon-mfg.pages.dev"
BEACON_SOURCE = os.environ.get("BEACON_SOURCE", DEFAULT_SOURCE).rstrip("/")
BEACON_REPO = os.environ.get("BEACON_REPO")  # None -> 自动探测 / 退回 HTTP

CACHE_DIR = os.path.join(tempfile.gettempdir(), "beacon-mcp-cache")
UA = "BeaconMFG-MCP/1.0 (+https://beacon-mfg.pages.dev/)"

_manifest_cache: Optional[Dict[str, Any]] = None
# 国标别名表（{word: entries}），懒加载一次；None 表示还没读过
_alias_cache: Optional[Dict[str, Any]] = None
# 国标码 → 中文名，懒加载一次
_gb_name_cache: Optional[Dict[str, str]] = None


# ───────────────────────────────────────────────────────────────
# 派生层缓存失效（让「后台上传 → 立刻可见」无需提交 / 无需重启）
#
# BEACON_WORKTREE=1（默认）下，后台流水线会把 fp / 预构建索引 / 清单等派生层重建到
# 本地副本。下面这组机制让 MCP 在不提交、不重启的前提下读到新版本：
#   - 磁盘索引缓存（idx-*）按「HEAD sha + 本地副本 mtime」判新（见 _index_text）
#   - 进程内缓存（manifest/alias/cap/gb_name/fp_index/桶/分片）按文件 mtime 判新
#     （见 _cache_stale），或按 BEACON_CACHE_TTL（秒）周期刷新。
#   - 批量读（_read_many_text，被 _candidate_shards / _records_from_paths 使用）
#     改为**本地副本优先**、git archive HEAD 兜底 —— 否则这两条关键词 / 城市检索的
#     关键路径会绕过上面的失效机制，永远读到已提交快照。
# 非本地副本模式（BEACON_WORKTREE=0）或 HTTP 模式：恢复旧的「提交后 / 重启后生效」。
BEACON_CACHE_TTL = float(os.environ.get("BEACON_CACHE_TTL", "0") or "0")

_mtime_cache: Dict[str, tuple] = {}  # relpath -> (checked_at, mtime)，2s 内复用避免热路径狂 stat


def _wt_mtime(relpath: str) -> Optional[float]:
    """本地副本文件 mtime（秒）。非本地副本模式或文件不存在 → None（不按 mtime 判新）。"""
    if not REPO or os.environ.get("BEACON_WORKTREE", "1") == "0":
        return None
    now = time.time()
    c = _mtime_cache.get(relpath)
    if c is not None and (now - c[0]) < 2.0:
        return c[1]
    p = os.path.join(REPO, relpath)
    try:
        m = os.stat(p).st_mtime
    except OSError:
        m = None
    _mtime_cache[relpath] = (now, m)
    return m


def _wt_mtime_any(relpaths) -> Optional[float]:
    """多文件取最大 mtime（任一不存在则忽略），全不存在 → None。"""
    mts = [m for m in (_wt_mtime(r) for r in relpaths) if m is not None]
    return max(mts) if mts else None


def _cache_stale(slot: tuple, relpath: Optional[str]) -> bool:
    """进程内缓存是否该失效。slot = (value, loaded_at, sig_mtime)。
    value 为空 / 超 BEACON_CACHE_TTL / 本地副本 mtime 变化（派生重建 重建）→ 失效。
    relpath 可为单路径或路径序列（取最大 mtime）。非本地副本模式（mtime 为 None）则只受
    TTL 与首次加载约束，即旧行为。
    """
    if slot is None or slot[0] is None:
        return True
    if BEACON_CACHE_TTL > 0 and (time.time() - slot[1]) > BEACON_CACHE_TTL:
        return True
    if relpath is not None:
        m = _wt_mtime_any(relpath) if isinstance(relpath, (list, tuple, set)) else _wt_mtime(relpath)
        if m is not None and slot[2] != m:
            return True
    return False


# 版本号单一来源：直接读同目录 package.json，避免 MCP 内部版本与发布包版本漂移。
def _pkg_version() -> str:
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "package.json")
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f).get("version", "1.5.6")
    except Exception:
        return "1.5.6"


# HTTP 基址列表（含兜底镜像）。默认部署 Cloudflare Pages 不可达时，自动回退到
# GitHub 兜底镜像（与 Android App 同策略：pages.dev 主源 + jsDelivr / raw 镜像），
# 保证「免 clone 检索」在单一 CDN 抖动/被墙时仍可工作。可用 BEACON_MIRRORS 追加自定义镜像。
def _http_bases() -> List[str]:
    bases = [BEACON_SOURCE]
    if BEACON_SOURCE == DEFAULT_SOURCE:
        bases.append("https://cdn.jsdelivr.net/gh/eiry16/beacon-mfg@main")
        bases.append("https://raw.githubusercontent.com/eiry16/beacon-mfg/main")
    m = os.environ.get("BEACON_MIRRORS")
    if m:
        for x in m.split(","):
            x = x.strip().rstrip("/")
            if x:
                bases.append(x)
    return bases


# --------------------------------------------------------------------------- #
# 使用量埋点（设计见 docs/MCP_USAGE_AUDIT.md §3.4 / §4 / §14）
# --------------------------------------------------------------------------- #
# 三条原则，改这里前先读文档：
#   1. **默认不出网**。本地台账永远只写本地文件；随请求带出去的只有下面四个
#      请求头，且里面没有 IP、没有 UA 原文、没有完整 query。
#   2. **可关闭**。`BEACON_TELEMETRY=0` 时不发任何头；`BEACON_USAGE_LOG=0` 时不写台账。
#   3. **失败静默**。埋点出任何问题都不许影响检索结果 —— 这是只读服务，
#      记账不能变成新的故障源。
#
# 为什么要有本地台账：本地 git 模式（BEACON_REPO）**完全不触网**，服务端看不见，
# 只有本地这一份能记。对服务端而言它统计到的永远是下限，这是架构决定的。

BEACON_TELEMETRY = os.environ.get("BEACON_TELEMETRY", "1") != "0"
BEACON_USAGE_LOG = os.environ.get("BEACON_USAGE_LOG", "1") != "0"
USAGE_LOG_PATH = os.path.join(os.path.expanduser("~"), ".beacon-mfg", "usage.jsonl")

# 当前调用的上下文（tool / tokens / hits），由 _dispatch 设置、_http_get 读取。
# 用 ContextVar 而不是全局变量：MCP server 理论上可能并发处理请求。
_CUR: Any = None
try:
    from contextvars import ContextVar
    _CUR = ContextVar("beacon_current_call", default=None)
except Exception:  # 极老的运行环境
    _CUR = None


def _cur_set(val: Any) -> Any:
    """返回 token 以便 finally 里 reset；无 ContextVar 时退化成全局变量。"""
    if _CUR is not None:
        return _CUR.set(val)
    global _CUR_FALLBACK
    _CUR_FALLBACK = val
    return None


def _cur_get() -> Any:
    if _CUR is not None:
        return _CUR.get()
    return globals().get("_CUR_FALLBACK")


_CUR_FALLBACK = None


def _client_id() -> str:
    """稳定的本机 id（首次调用时生成），**只用于派生不可逆的 cid**。

    文件里存的是随机 uuid；发到服务端的是 HMAC 结果，收不到原始 uuid。
    """
    p = os.path.join(os.path.expanduser("~"), ".beacon-mfg", "client_id")
    try:
        if os.path.exists(p):
            return open(p, "r", encoding="utf-8").read().strip()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        v = str(_uuid.uuid4())
        with open(p, "w", encoding="utf-8") as f:
            f.write(v)
        return v
    except Exception:
        return "unknown"


_CID_SALT = b"beacon-mfg-v1"   # 公开盐：目的是让 cid 不可逆，不是防攻击者


def _cid() -> str:
    return hmac.new(_CID_SALT, _client_id().encode("utf-8"), hashlib.sha256) \
               .hexdigest()[:16]


def _beacon_headers() -> Dict[str, str]:
    """随请求带出的四个头。**不含 IP / UA 原文 / 完整 query**。"""
    if not BEACON_TELEMETRY:
        return {}
    cur = _cur_get() or {}
    h = {"X-Beacon-Tool": cur.get("tool") or "", "X-Beacon-Cid": _cid()}
    # tokens = 分词后的**产品词**，不是整句（§14.2：记整句等于把用户输入落成明文台账）
    if cur.get("tokens"):
        h["X-Beacon-Tokens"] = ",".join(cur["tokens"])[:120]
    if cur.get("hits") is not None:
        h["X-Beacon-Hits"] = str(cur["hits"])
    return {k: v for k, v in h.items() if v not in ("", None)}


def _emit_usage(tool: str, ms: int, ok: bool, extra: Any = None) -> None:
    """写本地台账（jsonl）。默认开启，BEACON_USAGE_LOG=0 可关。"""
    if not BEACON_USAGE_LOG:
        return
    try:
        rec = {
            "ts": int(time.time() * 1000),
            "tool": tool,
            "ms": ms,
            "ok": ok,
            # REPO 可能指向一个不存在的路径（此时实际走的是 HTTP，只是 git show 静默失败），
            # 所以这里**必须判目录存在**再算 git，否则会把离线调用记成 http 或反之。
            "src": ("git" if (REPO and os.path.isdir(REPO)) else "http"),
            "cid": _cid(),
        }
        if extra:
            rec.update(extra)
        os.makedirs(os.path.dirname(USAGE_LOG_PATH), exist_ok=True)
        with open(USAGE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass   # 记账失败绝不能影响检索


# --------------------------------------------------------------------------- #
# 数据源抽象：只做只读取
# --------------------------------------------------------------------------- #
def _detect_repo() -> Optional[str]:
    if BEACON_REPO:
        return BEACON_REPO
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=os.getcwd(),
        )
        if out.returncode == 0:
            repo = out.stdout.strip()
            # 只在该仓库确实是 beacon-mfg（含 data/gb）时才用，避免误读别的仓库
            if os.path.isdir(os.path.join(repo, "data", "gb")):
                return repo
    except Exception:
        pass
    return None


REPO = _detect_repo()


def _cache_path(url: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, hashlib.sha1(url.encode("utf-8")).hexdigest() + ".json")


def _git_show(relpath: str) -> Optional[str]:
    """只读已提交内容。绝不 checkout —— 不触发 maskphone smudge、不碰 index 锁。"""
    if not REPO:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", REPO, "show", "HEAD:" + relpath],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout:
            return r.stdout
    except Exception:
        pass
    return None


# 单基址 HTTP 超时（秒）。多镜像回退时每个基址各自计时，避免单一 CDN 卡死拖垮整体。
_HTTP_TIMEOUT = 30


# 记录最近一次全失败的诊断（供上层报错时引用，不进返回值以免破坏既有签名）
_HTTP_LAST_ERROR: List[str] = [""]


def _openers() -> List[Tuple[str, Any]]:
    """构造 (标签, opener) 列表，**直连优先、代理兜底**。

    为什么需要：urllib 默认读取环境里的 `*_proxy` 变量。客户机器上若配了
    已失效的代理（VPN/Clash 退出、端口已关），所有请求都会被代理吃掉并失败，
    表现为「无法取得 manifest.json」——报错完全没指向真因，客户无从下手。
    这里把「绕过代理直连」作为**第一选择**，代理仅作兜底，并在全部失败时
    给出明确诊断（见 `_http_diagnose`）。

    仅在直连确实失败后才启用代理，因此不会破坏内网/反代等必须走代理的场景。
    """
    out: List[Tuple[str, Any]] = []
    # 1) 直连（显式 no-proxy opener，忽略环境里的 *_proxy）
    try:
        out.append(("direct", urllib.request.build_opener(urllib.request.ProxyHandler({}))))
    except Exception:
        pass
    # 2) 环境代理（若系统确实配置了可用代理，这里才会成功）
    try:
        if urllib.request.getproxies():
            out.append(("proxy", urllib.request.build_opener()))
    except Exception:
        pass
    # 3) 兜底：默认 opener
    if not out:
        out.append(("default", urllib.request.build_opener()))
    return out


def _http_diagnose(relpath: str, last_err: str = "") -> str:
    """全部基址+全部连接方式都失败时，给出**指向真因**的诊断串。"""
    hints = []
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}
    live = []
    for k, v in proxies.items():
        try:
            host = v.split("//")[-1].split(":")[0].split("@")[-1]
            port = v.rsplit(":", 1)[-1].split("/")[0]
            s = socket.create_connection((host, int(port)), timeout=1.5)
            s.close()
            live.append("%s=%s" % (k, v))
        except Exception:
            hints.append("代理 %s=%s **不可用**（已退出/端口未监听）" % (k, v))
    if live:
        hints.append("可用代理: " + ", ".join(live))
    if not hints:
        hints.append("未检测到代理配置；可能是本机网络不可达或域名被阻断")
    if last_err:
        hints.append("最后错误: " + last_err)
    return "；".join(hints)


def _decode_body(raw: bytes, resp) -> str:
    """按 Content-Encoding 解压再解码。

    **只声明 gzip**（标准库即可解）；不声明 brotli，避免声明了却解不开把请求打挂。
    """
    ce = ""
    try:
        ce = (resp.headers.get("Content-Encoding") or "").lower()
    except Exception:
        ce = ""
    if "gzip" in ce:
        try:
            import gzip as _gz
            raw = _gz.decompress(raw)
        except Exception:
            pass          # 解不开就当明文试（可能是中间层误标头）
    return raw.decode("utf-8")


def _http_get(relpath: str) -> Tuple[Optional[str], Optional[str]]:
    """统一的 HTTP 读取：依次尝试 _http_bases() 中的基址（主源 + 兜底镜像），
    任一成功即返回；全部失败则用本地缓存兜底（保证离线可用）。

    连接方式上「直连优先、代理兜底」——避免失效代理把检索整体打挂。
    """
    cached_body = None
    cached_etag = None
    last_err = ""
    for base in _http_bases():
        url = base + "/" + relpath
        cap = _cache_path(url)
        etag = None
        if os.path.exists(cap):
            try:
                with open(cap, "r", encoding="utf-8") as f:
                    blob = json.load(f)
                etag = blob.get("etag")
                cached_body = blob.get("body")
                cached_etag = etag
            except Exception:
                etag = None
        headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
        # 埋点头：服务端据此填 tool / tokens / hits 列。收不到也没关系 ——
        # worker 那边**照样计数**，只是这三列为空（文档 §3.4）。
        try:
            headers.update(_beacon_headers())
        except Exception:
            pass
        req = urllib.request.Request(url, headers=headers)
        if etag:
            req.add_header("If-None-Match", etag)
        for _tag, opener in _openers():
            try:
                resp = opener.open(req, timeout=_HTTP_TIMEOUT)
                body = _decode_body(resp.read(), resp)
                new_etag = resp.headers.get("ETag")
                try:
                    with open(cap, "w", encoding="utf-8") as f:
                        json.dump({"etag": new_etag, "body": body}, f)
                except Exception:
                    pass
                return body, new_etag
            except urllib.error.HTTPError as e:
                if e.code == 304 and cached_body is not None:
                    return cached_body, etag
                last_err = "HTTP %s" % e.code
                break   # HTTP 层错误，换基址比重试连接方式有意义
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, e)
                continue   # 连接层失败（典型：代理不可用）→ 换连接方式
    # 全部基址失败：若有任何本地缓存也返回，保证离线可用
    if cached_body is not None:
        return cached_body, cached_etag
    _HTTP_LAST_ERROR[0] = _http_diagnose(relpath, last_err)
    return None, None


def _worktree_read(relpath: str) -> Optional[str]:
    """本地 git 模式：读取本地副本文件，闭合『未提交/刚修改的数据搜不到』的缺口。

    优先于 git HEAD 返回本地副本版本（含**新增**与**刚修改**的记录）；仅当：
      - 文件不存在，或
      - 单对象 JSON（.json）无法解析（cron 写入中途的半成品）
    时返回 None，由 fetch_text 退回 git HEAD（已提交快照，安全），避免污染结果。
    注意：直接 open 本地副本文件、**不触发** git checkout / smudge，因此不会重隐藏手机号，
    也不会碰 git index 锁（与 server.py 硬规则 2 一致）。
    BEACON_WORKTREE=0 时彻底关闭，恢复『只读已提交快照』的严格行为。
    """
    if not REPO:
        return None
    if os.environ.get("BEACON_WORKTREE", "1") == "0":
        return None
    p = os.path.join(REPO, relpath)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return None
    if not txt.strip():
        return None
    # 单对象 JSON：解析失败（半成品）则退回 HEAD，避免返回损坏内容
    if relpath.endswith(".json"):
        try:
            json.loads(txt)
        except Exception:
            return None
    return txt


def fetch_text(relpath: str) -> Optional[str]:
    """统一的只读取入口。

    本地 git 模式（BEACON_WORKTREE 默认开启）：优先返回本地副本文件（含新增/刚修改），
    半成品或不存在时退回 git HEAD 已提交快照，再否则 HTTP——闭合检索缺口且不读损坏内容。
    BEACON_WORKTREE=0：恢复『只读已提交快照』的严格行为（git HEAD → HTTP）。
    """
    if REPO:
        if os.environ.get("BEACON_WORKTREE", "1") != "0":
            w = _worktree_read(relpath)
            if w is not None:
                return w
            g = _git_show(relpath)
            if g is not None:
                return g
        else:
            g = _git_show(relpath)
            if g is not None:
                return g
    return _http_get(relpath)[0]


def _fetch_many(paths: List[str], workers: int = 6) -> List[Optional[str]]:
    """并发取多份**分片**文本，返回与入参同序的结果（取不到为 None）。

    走 `_shard_text`（按清单 sha1 缓存）而不是 `fetch_text`：命中缓存时连条件请求
    都省掉，重复查询同一国标码/邻近记录近乎零成本。

    为什么值得并发：一个国标码常有多个 zh 续片，串行取等于把延迟**相加**；
    并发取的总耗时接近最慢那一个。
    单条路径时不做线程开销。

    上限 6 是有意的：客户端瓶颈在带宽不在连接数，开太多只会互相挤、还会撞上
    CDN 的限速。异常一律吞成 None —— 与 fetch_text 的语义一致（取不到就是取不到，
    由调用方决定是"换下一片"还是"报错"），绝不把线程异常泄漏成 tool 崩溃。
    """
    if len(paths) <= 1:
        return [_shard_text(p) for p in paths]
    out: List[Optional[str]] = [None] * len(paths)
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(workers, len(paths))) as ex:
        futs = {ex.submit(_shard_text, p): i for i, p in enumerate(paths)}
        for fut in concurrent.futures.as_completed(futs):
            i = futs[fut]
            try:
                out[i] = fut.result()
            except Exception:
                out[i] = None
    return out


_shard_hash_slot: Optional[tuple] = None    # (rel -> sha1, 数据版本号)


def _shard_hash_index() -> Dict[str, str]:
    """分片路径 → 清单登记的 sha1（h）。随数据版本重建，进程内只建一次。

    除 `shards` 外还合并 `metadata.idxfiles`：索引桶与别名表刻意不列入 `shards`，
    避免客户端同步时多下载。MCP 侧需要它们的摘要来做内容缓存。
    """
    global _shard_hash_slot
    ver = _data_version()
    if _shard_hash_slot is None or _shard_hash_slot[1] != ver:
        m: Dict[str, str] = {}
        try:
            man = load_manifest()
            for s in man.get("shards", []):
                p, h = s.get("p"), s.get("h")
                if p and h:
                    m[str(p)] = str(h)
            ix = (man.get("metadata") or {}).get("idxfiles") or {}
            d = ix.get("dir")
            for name, h in (ix.get("h") or {}).items():
                if d and name and h:
                    m["%s/%s" % (d, name)] = str(h)
            for rel, h in (ix.get("files") or {}).items():
                if rel and h:
                    m[str(rel)] = str(h)
        except Exception:
            m = {}
        _shard_hash_slot = (m, ver)
    return _shard_hash_slot[0]


def _shard_text(relpath: str) -> Optional[str]:
    """按**内容哈希**缓存的分片读取（热启动的关键）。

    为什么需要它 —— `_http_get` 的缓存是 URL 级的，命中也要发一次条件请求等 304：
    对大分片，多一个 RTT 是小事，但 ETag 一旦不匹配就得**整份重传**。
    而清单里本来就登记了每片内容的 sha1（h），拿它当缓存键正好：

      * 内容没变 → 缓存键不变 → **完全不走网络**（连 304 都省）；
      * 内容变了 → 键自动变 → 不会读到旧内容（这正是"不敢随便缓存"的顾虑所在）；
      * 命中后还能顺手**校验**拿到的内容 sha1 是否等于 h —— 清单的 field_note
        本来就建议客户端这么做，这里顺手把完整性也验了。哈希不符时不缓存、
        并在诊断里留痕（宁可每次重取，也不把对不上的内容当权威）。

    意义：同一国标码下的企业共享同一个 zh 分片，因此查过一家之后，
    再查同国标码下的任何一家，**这一层完全不再走网络**。
    """
    h = _shard_hash_index().get(relpath)
    cp = os.path.join(CACHE_DIR, "shard-" + h + ".json") if h else None
    if cp and os.path.exists(cp):
        try:
            with open(cp, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    txt = fetch_text(relpath)
    if txt is None or cp is None:
        return txt
    if h:
        got = hashlib.sha1(txt.replace("\r\n", "\n").encode("utf-8")).hexdigest()
        if got != h:
            _HTTP_LAST_ERROR[0] = (
                f"分片 {relpath} 内容哈希与清单不符（{got[:10]}… != {h[:10]}…），"
                "已按未缓存处理（可能是发布进行中，稍后重试即可）")
            return txt
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cp, "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception:
        pass
    return txt


# --------------------------------------------------------------------------- #
# manifest + 分片访问
# --------------------------------------------------------------------------- #
def load_manifest() -> Dict[str, Any]:
    global _manifest_cache
    if _cache_stale(_manifest_cache, "data/manifest.json"):
        txt = fetch_text("data/manifest.json")
        if not txt:
            # 报错必须指向真因：把 _http_get 记下的连接层诊断（失效代理 / 网络不可达 /
            # 域名阻断）带上，否则客户只看到「检查网络」却无从下手。
            detail = _HTTP_LAST_ERROR[0] or "无缓存且所有镜像均不可达"
            raise RuntimeError(
                "无法取得 manifest.json。诊断：" + detail
                + "。可尝试：① 检查系统代理/VPN 是否已退出或端口未监听；"
                + "② 设置 BEACON_SOURCE 指向自建镜像；"
                + "③ 设置 BEACON_REPO 指向本地仓库以完全离线使用。"
            )
        _manifest_cache = (json.loads(txt), time.time(), _wt_mtime("data/manifest.json"))
    return _manifest_cache[0]


def _shards_of_type(t: str) -> List[Dict[str, Any]]:
    return [s for s in load_manifest().get("shards", []) if s.get("t") == t]


def _zh_paths_for_gb(gb: str) -> List[str]:
    # 一个国标码可能拆成多个 zh 分片（如 3484.json + 3484-p2.json 续片），必须全扫
    return [s.get("p") for s in _shards_of_type("zh") if s.get("c") == gb]


# --------------------------------------------------------------------------- #
# 国标行业别名表（与 App 的 AliasIndex 同源）
#
# 别名表把「口语词 → 国标码」补上：「输送线」这类口语词在厂名/工艺/材料里
# 一个字都不出现，只能靠别名按码定向召回。
# --------------------------------------------------------------------------- #
def _load_alias_file(rel: str, txt: Optional[str] = None) -> Dict[str, Any]:
    """两种历史形态都兼容：[["metadata",..],["alias",{..}]] 和直接 {word: ...}。

    `txt` 可由调用方批量取好后传入（见 `load_gb_alias`）—— 免得逐文件各付一次 RTT。
    未传入时走 `_shard_text`：`data/gb-alias.json` 登记在清单里 ⇒ 按内容哈希缓存，
    跨进程也不重复下载。
    """
    if txt is None:
        txt = _shard_text(rel)
    if not txt:
        return {}
    try:
        raw = json.loads(txt)
    except Exception:
        return {}
    if isinstance(raw, list):
        for pair in raw:
            if isinstance(pair, list) and len(pair) == 2 and pair[0] == "alias":
                return pair[1] or {}
        return {}
    if isinstance(raw, dict):
        return raw.get("alias", raw) or {}
    return {}


def load_gb_alias() -> Dict[str, Any]:
    """词 → 条目。两层表合并：gb-alias.json（数据推导）+ gb-alias-curated.json（人工策展）。"""
    global _alias_cache
    _alias_relpaths = ("data/gb-alias.json", "data/gb-alias-curated.json")
    if _cache_stale(_alias_cache, _alias_relpaths):
        # ⚡ 两张表互相独立 → 一次并发取齐。逐个 fetch 各付一次 CDN RTT
        #    （并发取齐可省下数次串行往返）。
        #    同时借 _read_many_text 落到 _shard_text：清单里登记了 h 的表按内容缓存，
        #    内容没变就完全不走网络；没 h 的表（如 gb-alias-curated.json）自动退回 fetch。
        raw = _read_many_text(list(_alias_relpaths))
        merged: Dict[str, Any] = {}
        for rel in _alias_relpaths:
            for k, v in _load_alias_file(rel, raw.get(rel)).items():
                merged.setdefault(str(k).lower(), v)
        _alias_cache = (merged, time.time(), _wt_mtime_any(_alias_relpaths))
    return _alias_cache[0]


def _alias_entries(v: Any) -> List[Dict[str, Any]]:
    """条目的两种写法归一成 [{code,name,hits}]：
    gb-alias.json      → [{"code","name","hits"}, ...]
    gb-alias-curated   → {"codes":[...], "note":...}
    """
    out: List[Dict[str, Any]] = []
    if isinstance(v, dict):
        for c in (v.get("codes") or []):
            out.append({"code": str(c), "name": v.get("name", ""), "hits": int(v.get("hits") or 0)})
        if v.get("code"):
            out.append({"code": str(v["code"]), "name": v.get("name", ""), "hits": int(v.get("hits") or 0)})
    elif isinstance(v, list):
        for e in v:
            if isinstance(e, dict) and e.get("code"):
                out.append({"code": str(e["code"]), "name": e.get("name", ""),
                            "hits": int(e.get("hits") or 0)})
            elif isinstance(e, str):
                out.append({"code": e, "name": "", "hits": 0})
    return out


def _gb_names() -> Dict[str, str]:
    """国标码 → 中文名。取不到（离线/文件缺失）就返回空表，不影响主链路。"""
    global _gb_name_cache
    if _cache_stale(_gb_name_cache, "data/gb4754-full.json"):
        names: Dict[str, str] = {}
        txt = fetch_text("data/gb4754-full.json")
        if txt:
            try:
                raw = json.loads(txt)
            except Exception:
                raw = None
            # 两种形态都见过：{"classes":{...},"groups":{...}} 和 [["classes",{...}],...]；
            # 值本身也可能是 {"name":..,"desc":..}，只取 name 段。
            sections = raw.items() if isinstance(raw, dict) else (raw or [])
            for key, body in sections:
                if key in ("classes", "groups", "divisions") and isinstance(body, dict):
                    for k, v in body.items():
                        names[str(k)] = str(v.get("name", v)) if isinstance(v, dict) else str(v)
        _gb_name_cache = (names, time.time(), _wt_mtime("data/gb4754-full.json"))
    return _gb_name_cache[0]


def alias_codes(q: str, max_codes: int = 6) -> List[Dict[str, Any]]:
    """采购口语词 → 国标码。返回 [{code,name,word,exact,hits}]，按 精确>hits 排序。"""
    alias = load_gb_alias()
    q = (q or "").strip().lower()
    if not alias or not q:
        return []
    probes = [q] + [t for t in q.split() if len(t) >= 2]
    best: Dict[str, Dict[str, Any]] = {}
    for probe in dict.fromkeys(probes):
        if len(probe) < 2:
            continue
        for word, v in alias.items():
            wl = word.lower()
            if wl == probe:
                exact = True
            elif probe in wl or wl in probe:
                exact = False
            else:
                continue
            for e in _alias_entries(v):
                code = e["code"]
                if not code:
                    continue
                prev = best.get(code)
                rank = (1 if exact else 0, e["hits"])
                if prev is None or rank > prev["_rank"]:
                    best[code] = {"code": code, "name": e["name"], "word": word,
                                  "exact": exact, "hits": e["hits"], "_rank": rank}
    out = [v for v in best.values()]
    for v in out:
        v.pop("_rank", None)
        # curated 表只写 codes 不写 name，回填报一下 —— 返回体里带中文行业名，
        # 调用方（agent / 人）不用再自己查一遍码表。
        if not v["name"]:
            v["name"] = _gb_names().get(v["code"], "")
    out.sort(key=lambda x: (-int(x["exact"]), -x["hits"]))
    return out[:max_codes]


def _fp_paths_for_gbs(codes: List[str]) -> List[str]:
    return [s.get("p") for s in _shards_of_type("fp") if s.get("c") in set(codes)]


# --------------------------------------------------------------------------- #
# 语义路由层：不要求「服务端猜对用户的原话」，而是把**国标货架 + 候选码**交给
# 客户端 LLM，让它现场做语义映射。
#
# 设计思路：不要求服务端猜对用户的原话，而是把**国标货架 + 候选码**交给
# **客户端 LLM** 做语义映射：
#   · 零额外基础设施：服务端不调 LLM、不需要密钥、不引入延迟和成本；
#   · 天然覆盖长尾：说法千人千面，LLM 的知识面就是那张「说法表」；
#   · 可控可解释：挑码依据是货架上的类名 + 真实条数，不是黑盒。
# 别名表因此是**高频词加速器**（能命中就直接省一次往返），不是唯一入口。
#
# 三个出口：
#   list_industries()    → 国标货架（只列有数据的类，带条数）
#   suggest_filters()    → 拆词 + 别名 + 类名子串，给出候选码与下一步调用
#   search_vendors()     → 命中偏少时内联 hint，把这条链路推给 agent
# --------------------------------------------------------------------------- #

_GB_SHELF: Optional[List[Dict[str, Any]]] = None
_SEG_PATTERNS: Optional[List[Tuple[str, str]]] = None


def _gb_shelf() -> List[Dict[str, Any]]:
    """国标小类货架：[{code, name, group, path, records}]。

    数据源刻意选 `data/gb-index.json`（82 KB）而不是 `gb4754-full.json`（280 KB）：
    前者**只含真正有数据的类**（269 个），并直接带每类条数。对「让 LLM 挑码」
    这个用途，列一个 0 条的类只会误导它 —— 货架上摆不出来的东西没必要上架。
    """
    global _GB_SHELF
    if _GB_SHELF is not None:
        return _GB_SHELF
    rows: List[Dict[str, Any]] = []
    try:
        txt = fetch_text("data/gb-index.json")
        tree = (json.loads(txt) if txt else {}).get("tree") or {}
    except Exception:
        tree = {}
    for dcode, dv in tree.items():
        dname = str(dv.get("name") or "")
        for gcode, gv in (dv.get("divisions") or {}).items():
            gname = str(gv.get("name") or "")
            for mcode, mv in (gv.get("groups") or {}).items():
                mname = str(mv.get("name") or "")
                for ccode, cv in (mv.get("classes") or {}).items():
                    rows.append({
                        "code": str(ccode),
                        "name": str(cv.get("name") or ""),
                        "group": str(mcode),
                        "path": "%s/%s/%s" % (dname, gname, mname),
                        "records": int(cv.get("count") or 0),
                    })
    _GB_SHELF = rows
    return rows


def _seg_patterns() -> List[Tuple[str, str]]:
    """长词优先的匹配表 [(词, 来源)]，来源 ∈ alias / gb_name / cap。"""
    global _SEG_PATTERNS
    if _SEG_PATTERNS is None:
        pats: Dict[str, str] = {}
        for w in load_gb_alias():
            if len(w) >= 2:
                pats.setdefault(w, "alias")
        for r in _gb_shelf():
            if len(r["name"]) >= 2:
                pats.setdefault(r["name"], "gb_name")
        # 同时收 cap（能力词）：「人工智能 / 机器学习 / 大模型」是**跨门类能力词**，
        # 国标别名与类名两侧都不覆盖，必须单独纳入。
        # 只收 **纯汉字且长度 >= 2** 的能力词：ASCII 侧（ai / cnc / c / 3d…）数量大，
        # 且「c」「3c」这类单双字符做子串切分会把任意含 c 的串切碎。ASCII 能力词由
        # `_cap_terms_in` 用词边界单独扫，不进这张切分表。
        for w in _cap_term_index():
            if len(w) >= 2 and _is_cjk(w):
                pats.setdefault(w, "cap")
        _SEG_PATTERNS = sorted(pats.items(), key=lambda x: -len(x[0]))
    return _SEG_PATTERNS


def _segment_terms(text: str) -> List[Tuple[str, str]]:
    """把一段人话按「最长优先、不重叠」切成已知词条。

    这是「智能拆分需求」的确定性那一半：`不锈钢板激光切割折弯` →
    `激光切割 / 折弯 / 不锈钢板` 三个候选，各自再去映射国标码。
    剩下那一半（这几个词到底该挑哪个码、要不要并集）交给 LLM。
    """
    hay = (text or "").lower()
    if not hay:
        return []
    hits: List[Tuple[str, str]] = []
    i, n = 0, len(hay)
    while i < n:
        for w, src in _seg_patterns():
            if hay.startswith(w.lower(), i):
                hits.append((w, src))
                i += len(w)
                break
        else:
            i += 1
    return hits


# --------------------------------------------------------------------------- #
# 原话归一化：把「一句人话」切成可执行的检索参数
#
# 中文用户输入的自然语言没有空格，按空白切分时整句话就是一个 token；而关键词是
# 「每个 token 都必须出现在记录检索面里」（AND）语义，整句必然 0 条。
# 这一层负责把原话切成可执行的检索参数（城市 + 关键词 + 能力词），
# 让调用方可以直接把用户原话传进来。
# --------------------------------------------------------------------------- #
_STOPWORDS: Tuple[str, ...] = (
    # 通名/机构词：出现即无信息量（「企业」「公司」本身不是产品词）
    "生产厂家", "制造商", "加工厂", "制造厂", "供应商", "厂商",
    "企业", "公司", "工厂", "厂家",
    # 诉求词
    "有哪些", "哪几家", "哪家", "几家", "一家",
    "推荐", "介绍", "查找", "找一下", "查一下", "看一下", "看看",
    "找找", "查询", "搜索", "检索",
    # 指代/范围
    "有没有", "什么样的", "怎么样", "什么", "附近", "周边",
    "当地", "本地", "这边", "那边", "相关的", "有关的", "有关",
    # 单字诉求（整词替换，不会伤到词内部）
    "帮我", "给我", "我要", "我想", "一下",
)
_STOPWORDS_SET = frozenset(_STOPWORDS)
# 只削**两端**的功能字。刻意不含 厂/家/店/司（「酒厂」「酒店」「家电」是合法词），
# 也不含 有/用/能/会/可（「有色」「用友」会被削坏）。
_EDGE_FILLER = set("的了找做想要请给我你在和与或及是这那们吧呢吗把被让")
_RE_RUNSPLIT = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def _trim_edges(t: str) -> str:
    """削掉 token 两端的功能字，只削两端、不动中间（「做ai的」→「ai」）。"""
    i, j = 0, len(t)
    while i < j and t[i] in _EDGE_FILLER:
        i += 1
    while j > i and t[j - 1] in _EDGE_FILLER:
        j -= 1
    return t[i:j]


def _normalize_query(query: str, city: str = "") -> Dict[str, Any]:
    """把用户原话归一成 (city, keywords, cap_hits, alias_hits, segments)。

    一句话（「找一下苏州做AI的企业」）里混着四类东西：
      ① 城市     → 抽出来当 city 过滤（调用方没显式给 city 才抽）
      ② 已识别词 → 国标别名 / 国标类名 / 能力词（`_segment_terms` 切出）
      ③ 停用词   → 「的」「找」「企业」这类无信息量的填充
      ④ 真关键词 → 剔掉 ①②③ 后剩下的
    ② 保留进 keywords（不丢弃）：「厂名里真写了这个词」仍能被字面召回，与既有行为
    一致；③ 必须剔掉，否则「的企业」会参与 AND 匹配，必然 0 条。
    """
    raw = (query or "").strip()
    low = raw.lower()
    out: Dict[str, Any] = {"raw": raw, "city": city or "", "city_from_query": "",
                           "tokens": [], "cap_hits": [], "alias_hits": [],
                           "segments": []}
    if not low:
        return out

    # 停用词不参与任何「识别」——否则 '企业' 若恰好也是能力词/别名，会被当成
    # 一个正式检索词塞进 tokens，反而把查询扩大成「整城扫」。
    cap_hits = [h for h in cap_alias_codes(low)
                if str(h.get("word") or "").lower() not in _STOPWORDS_SET]
    alias_hits = [h for h in alias_codes(raw)
                  if str(h.get("word") or "").lower() not in _STOPWORDS_SET]
    segs = [(w, s) for (w, s) in _segment_terms(low)
            if w.lower() not in _STOPWORDS_SET]

    # ① 城市就地抽取（只在调用方没显式给 city 时）。取**最长**命中：「苏州」与「昆山」
    #    同时出现时长者信息量更大。省/自治区不在 city.json（那里只放市/县级），故
    #    「贵州的酒厂」这类由 agent 走 list_industries 语义挑码。
    city_found = ""
    if not out["city"]:
        try:
            names = [n for n in _city_names() if n and n.lower() in low]
        except Exception:
            names = []
        if names:
            city_found = max(names, key=len)
            out["city"] = city_found
            out["city_from_query"] = city_found

    # ② 把「已识别的」从原话里剔掉，剩下的才是通用关键词
    resid = low
    if city_found:
        resid = resid.replace(city_found.lower(), " ")
    for h in cap_hits + alias_hits:
        w = str(h.get("word") or "").lower()
        if w:
            resid = resid.replace(w, " ")
    for w, _src in segs:
        resid = resid.replace(w.lower(), " ")
    for w in sorted(_STOPWORDS, key=len, reverse=True):
        resid = resid.replace(w, " ")
    toks: List[str] = []
    # 纯结构字（厂/店/家/司…）不成词、没有产品信息，不能当关键词：它们会进入
    # **AND 过滤**，把厂名里恰好不写「厂」的「XX有限公司」整批误滤掉。
    # 例：cnc 被消费后只剩一个「厂」，纯文本召回会退化成「整城扫含厂字的记录」。
    _noinfo = _FUNC_CHARS | _ORG_SUFFIX
    for t in _RE_RUNSPLIT.sub(" ", resid).split():
        t = _trim_edges(t)
        if t and not all(ch in _noinfo for ch in t):
            toks.append(t)

    out["tokens"] = list(dict.fromkeys(toks + [w.lower() for w, _ in segs]))
    out["cap_hits"] = cap_hits
    out["alias_hits"] = alias_hits
    out["segments"] = [{"term": w, "source": s} for w, s in segs]
    return out


def list_industries(keyword: str = "", parent: str = "", limit: int = 30,
                    offset: int = 0) -> Dict[str, Any]:
    """国标货架：给 LLM 一张「有哪些行业、各有多少家」的菜单。

    用户原话与任何类名都不对字时（如「机加工」对不上「机械零部件加工」），
    唯一靠谱的办法是让 LLM 看货架自己挑 —— 这正是本工具存在的理由。
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    rows = _gb_shelf()
    parent = (parent or "").strip()
    if parent:
        rows = [r for r in rows
                if r["code"].startswith(parent) or r["group"].startswith(parent)
                or r["path"].startswith(parent)]
    keyword = (keyword or "").strip().lower()
    if keyword:
        rows = [r for r in rows
                if keyword in r["name"].lower() or keyword in r["code"]
                or keyword in r["path"].lower()]
    rows = sorted(rows, key=lambda r: -r["records"])
    return {
        "total": len(rows),
        "returned": len(rows[offset:offset + limit]),
        "note": "只列**已有企业**的国标小类（带条数）。请按语义挑 1~3 个最接近的 code，"
                "再调 search_vendors(gb=<code>, city=<城市>)。"
                "传 parent 可缩小货架（门类字母如 C、或大类两位码如 35）。",
        "industries": rows[offset:offset + limit],
    }


def suggest_filters(query: str, city: str = "", limit: int = 8) -> Dict[str, Any]:
    """把用户原话映射成候选国标码（给 LLM 做决策依据，而不是替它做决定）。

    三条来源按可信度排序：
      1 alias      别名表精确命中（人工策展 / 数据推导）—— 最可信
      2 gb_name    类名被完整说出（「机械零部件加工」）
      3 substring  类名子串（「轴承」→ 轴承制造）
      4 segmented  对原话做最长匹配拆出的词（覆盖「一句话多个诉求」）
    全都没命中时**明确告诉 agent 去查货架**，而不是返回一个空数组让它瞎猜。
    """
    q = (query or "").strip()
    if not q:
        return {"error": "缺少必填参数 query"}
    # 原话归一化：抽城市（调用方没给才抽）+ 剔停用词。用户给的是一句话时，
    # 「苏州做AI的企业」里的「苏州」应当变成 city 条件，而不是当成关键词去 AND。
    norm = _normalize_query(q, city)
    city = norm["city"] or city
    limit = max(1, min(int(limit), 30))
    by_code = {r["code"]: r for r in _gb_shelf()}
    cands: Dict[str, Dict[str, Any]] = {}

    def _add(code: str, score: int, why: str, term: str = "", hits: int = 0) -> None:
        row = by_code.get(code) or {}
        name = row.get("name") or _gb_names().get(code, "")
        # 名字里**直接含用户原话**是最强信号：「轴承」→ 3451 滚动轴承制造
        # 一定比「3399 其他未列明金属制品制造」更可能是正解。没有这一步，
        # 排序会被「类大 = 条数多」带偏（3399 有 4116 家，把正解压到第二名）。
        if term and term.lower() in (name or "").lower():
            score += 25
        cur = cands.get(code)
        item = {
            "code": code,
            "name": name,
            "path": row.get("path", ""),
            "records": row.get("records", 0),
            "why": why,
            "term": term or q,
            "hits": int(hits or 0),
            "_score": score,
        }
        if cur is None or score > cur["_score"]:
            cands[code] = item

    for h in alias_codes(q):
        _add(h["code"], 100 if h["exact"] else 80, "alias", h["word"], h.get("hits", 0))
    ql = q.lower()
    for r in _gb_shelf():
        nm = r["name"].lower()
        if nm == ql:
            _add(r["code"], 90, "gb_name", q)
        elif ql in nm:
            _add(r["code"], 60, "substring", q)
    for term, src in _segment_terms(q):
        if term.lower() == ql:
            continue
        if src == "alias":
            for h in alias_codes(term):
                _add(h["code"], 70 if h["exact"] else 55, "segmented", term,
                     h.get("hits", 0))
        for r in _gb_shelf():
            if r["name"] == term:
                _add(r["code"], 65, "segmented", term)

    # 词条级证据（hits = 该词在该类下真实出现过的企业数）优先于类规模。
    out = sorted(cands.values(), key=lambda x: (-x["_score"], -x["hits"], -x["records"]))
    for x in out:
        x.pop("_score", None)
    out = out[:limit]

    # 能力域候选（跨门类的工艺/技术标签，如 tech_ai=人工智能与算法）。
    # **绝不能混进 candidates**：那串是国标码，agent 会照着拿去调 search_vendors(gb=…)；
    # 把「tech_ai」塞进国标码字段会被当成国标码用，必然 0 条。所以单独字段 + 单独说明。
    # suggest_filters 同时看国标维与能力维，避免「人工智能」这种词两边都落空。
    cap_cands: List[Dict[str, Any]] = []
    try:
        for c in (norm.get("cap_hits") or cap_alias_codes(q)):
            cap_cands.append({"cap": c["cap"], "name": c["name"], "word": c["word"],
                              "records": _cap_count(c["cap"])})
    except Exception:
        cap_cands = []

    res: Dict[str, Any] = {
        "query": q,
        "city": city or "",
        "candidates": out,
        "how_to_use": "挑 1~3 个 code，逐个调 search_vendors(gb=<code>, city=<城市>)"
                      "（给了 gb 只扫对应分片，最快）；多个码的结果自行合并去重。"
                      "候选为空或都不对味时，调 list_industries 看货架自己挑。",
    }
    # 把「MCP 是怎么理解这句话的」摊开给 agent 看：抽走了哪个城市、剔了哪些停用词、
    # 认出了哪些词条。不透明的话，agent 无法判断候选为空到底是「没这行业」还是
    # 「这句话没被读懂」，只能瞎猜着换词重试。
    # 字段形状与 search_vendors 的 query_parsed **逐字一致** —— agent 从任一工具学到的
    # 字段名在另一个里都找得到，不必记两套形状（此前这里是扁平的 city_from_query /
    # keywords / recognized，与 search_vendors 的 query_parsed 不一致，属实现漂移）。
    res["query_parsed"] = {
        "city": city or "",
        "city_from_query": norm.get("city_from_query") or "",
        "keywords": norm.get("tokens") or [],
        "recognized": norm.get("segments") or [],
    }
    if cap_cands:
        res["cap_candidates"] = cap_cands
        res["cap_note"] = ("这些是**能力域**（跨门类的工艺/技术标签），不是国标码，"
                           "别拿去当 gb= 用。search_vendors(query=…) 已自动按它们展开并计数；"
                           "这里的用处是告诉你「该能力在名录里有多厚」——records 很小就是很薄。")
    if out:
        res["next_call"] = {"tool": "search_vendors",
                            "arguments": {"gb": out[0]["code"], "city": city or "",
                                          "limit": 20}}
    elif cap_cands:
        # 国标侧没候选、但能力域命中了 —— 别让 agent 空着手去翻货架
        res["next_call"] = {"tool": "search_vendors",
                            "arguments": {"query": q, "city": city or "", "limit": 20}}
        res["note"] = ("国标小类里没有这个词（GB/T 4754 本来就不保证有「%s」这一类），"
                       "但它命中能力域 %s。直接调 search_vendors(query=\"%s\") 即可，"
                       "能力域会被自动展开；结果偏少属正常 —— 那是数据厚度问题，"
                       "不是查询写法问题。%s"
                       % (q, "、".join(c["name"] for c in cap_cands), q,
                          ("另外能力域企业在空间上很稀疏（全库常只有几十家），"
                           "按 city=%s 过滤后若为 0 条，去掉 city 再试。" % city)
                          if city else ""))
    else:
        res["next_call"] = {"tool": "list_industries",
                            "arguments": {"keyword": q, "limit": 30}}
        res["note"] = ("没有字面/别名候选。请用你的语义理解先在货架上挑类："
                       "先 list_industries(keyword=\"…\") 看有哪些类，"
                       "必要时空 keyword 拉全货架（269 类）挑最贴近的 1~3 个 code。")
    return res


def _attach_routing_hint(out: Dict[str, Any], q: str, city: str) -> Dict[str, Any]:
    """命中偏少时，把「怎么把口语词换成国标码」这条链路内联进结果。

    只在 query 非空且命中 < 5 时触发 —— 命中充足说明用户说的词本身就是数据里的
    词，不需要打扰 agent。内联（而不是让 agent 再调一次）是为了省一个 RTT：
    agent 看到 hint 就能直接改参数重试。
    """
    if not q or out.get("total_matched", 0) >= 5:
        return out
    try:
        s = suggest_filters(q, city, limit=5)
    except Exception:
        s = {}
    cands = s.get("candidates") or []
    try:
        caps = cap_alias_codes(q)
    except Exception:
        caps = []

    def _emit_cap() -> None:
        out["cap_expanded"] = [{"cap": c["cap"], "name": c["name"], "word": c["word"],
                                "records": _cap_count(c["cap"])} for c in caps]
        txt = ("这个词命中能力域 %s（跨门类的工艺/技术标签，不是国标小类），"
               "search_vendors(query=…) 已自动展开并计入结果。命中偏少说明该能力"
               "在名录里确实很薄，**不要理解成「没这家厂」**。放宽可调 "
               "list_industries(keyword=…) 换近义类目再传 gb=。"
               % "、".join("%s %d 家" % (c["name"], _cap_count(c["cap"]))
                          for c in caps))
        # 0 条且带了城市时，最可能的元凶是城市而不是关键词 —— 能力域企业在空间上
        # 很稀疏（全库可能就十几家），叠一个城市筛掉全部是常态。这一句能省掉 agent
        # 一整轮「换个城市试」的瞎猜。
        if city and not out.get("total_matched", 0):
            txt += ("本次已按 city=%s 过滤；去掉 city 再试一次往往就有。" % city)
        out["hint"] = txt

    if out.get("total_matched", 0) == 0 and not cands:
        # 0 条 + 国标侧无候选 —— 但**能力域可能命中**，这是唯一还可执行的线索。
        # 此处必须给出 hint，否则 agent 看不到「换个说法 / 加城市就能召回」这条信息。
        if caps:
            _emit_cap()
            return out
        out["hint"] = ("关键词与名录里的写法对不上，且没有可推导的国标码。"
                       "建议调 list_industries 用语义挑码后改传 gb=。")
        return out
    if cands:
        out["hint"] = ("命中偏少：用户说的是口语/采购词，名录里按国标小类归档。"
                       "改用 gb= 重试通常能召回几十~几百条（见 suggested_gb）。")
        out["suggested_gb"] = [{"code": c["code"], "name": c["name"],
                                "records": c["records"], "why": c["why"]}
                               for c in cands]
        return out
    # 命中 1~4 条、别名与类名一条候选都推不出来 —— 最糟的一档，也是最容易被漏掉的一档：
    # 既有几条噪音（agent 会以为「就这些」直接交付），又没有任何可执行的下一步。
    # 因此「1~4 条且无候选」也要给出 hint，不能掉进空档。
    if caps:
        _emit_cap()
        return out
    out["hint"] = ("命中偏少且推不出国标码：这几条多半只是**字面撞词**，未必真是你要的行业。"
                   "建议调 list_industries(keyword=\"…\") 用语义挑 1~3 个国标小类，"
                   "再改传 gb= 重试；这样召回和精度都会好很多。")
    return out


# --------------------------------------------------------------------------- #
# 能力（cap）别名召回：把「AI / 人工智能 / 机器学习 / 大模型 / 算法 …」映射到
# 能力键（如 tech_ai），按能力键定向召回。与 GB 别名不同，cap 是跨门类能力、
# 不挂在某个国标码下，故走 cap.json 的 shards 表找分片，而非 _fp_paths_for_gbs。
#
# 关键点：命中 cap 别名的 token 从「通用子串 AND 校验」里**消费掉**，只走 cap
# 召回 —— 否则「AI」会作为子串命中 algebraist / Ashore 等英文名咖啡店，造成噪声。
# 匹配用整词精确（不分词组子串），同样是为了避免「ai」误扩到别的词里。
# --------------------------------------------------------------------------- #
_cap_index_cache: Optional[Dict[str, Any]] = None


def _load_cap_index() -> Optional[Dict[str, Any]]:
    """skills/registry/index/cap.json —— 发布产物（与 city.json 同构）：
    {terms:{code:词面串}, shards:{code:{shard_name:count}}}。"""
    global _cap_index_cache
    if _cache_stale(_cap_index_cache, "skills/registry/index/cap.json"):
        txt = _index_text("skills/registry/index/cap.json")
        try:
            _cap_index_cache = (json.loads(txt) if txt else {},
                               time.time(), _wt_mtime("skills/registry/index/cap.json"))
        except Exception:
            _cap_index_cache = ({}, time.time(), _wt_mtime("skills/registry/index/cap.json"))
    return _cap_index_cache[0]


def _cap_term_index() -> Dict[str, str]:
    """词面词 → 能力键（反向索引，缓存）。覆盖 cap.json terms 里的每个分词，
    例如 「AI」「人工智能」「机器学习」「大模型」「算法」→ tech_ai。"""
    idx: Dict[str, str] = {}
    cap = _load_cap_index() or {}
    for code, termstr in (cap.get("terms") or {}).items():
        for w in str(termstr).lower().split():
            idx.setdefault(w, code)   # 首个出现的码优先
    return idx


_CAP_SUBSTR_WORDS: Optional[List[Tuple[str, str, Any]]] = None


def _cap_substr_words() -> List[Tuple[str, str, Any]]:
    """能力词表里**可做子串扫描**的词 [(词, 键, 预编译边界正则|None)]，长词在前。

    过滤规则都是为了不误命中：
      · 长度 < 2 一律不要 —— 表里有 'c'、'/' 这种单字符键，子串扫会把任何含 c 的
        查询都映射成 tst_cpp（一个字符毁掉一条链路）；
      · 只保留「纯字母/数字/汉字」构成的词，带符号的（`.net` / `c++` / `250m汽车配件`
        里的斜杠）留给整词精确那一档；
      · ASCII 词预编译 `(?<![a-z0-9])词(?![a-z0-9])` 词边界 —— 否则 'ai' 会命中
        'chair'、'cnc' 会命中 'cncxx'。汉字不分词、天然成词，无需边界。
    """
    global _CAP_SUBSTR_WORDS
    if _CAP_SUBSTR_WORDS is None:
        rows: List[Tuple[str, str, Any]] = []
        for w, code in _cap_term_index().items():
            if len(w) < 2:
                continue
            if not re.fullmatch(r"[a-z0-9\u4e00-\u9fff]+", w):
                continue
            rx = (re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])")
                  if w.isascii() else None)
            rows.append((w, code, rx))
        rows.sort(key=lambda x: -len(x[0]))
        _CAP_SUBSTR_WORDS = rows
    return _CAP_SUBSTR_WORDS


def cap_alias_codes(q: str) -> List[Dict[str, Any]]:
    """能力口语词 → 能力键。返回 [{cap, word, name, match}]。

    两档匹配，整词精确优先：
      ① exact   查询按空白切开后某个 token 完全等于能力词（原行为）
      ② substr  能力词出现在查询里的**任意位置**（ASCII 词另要求词边界）

    真实用户说的是**一整句人话**：按空白切只有一个 token，'AI' 永远匹配不上，
    能力域召回对自然人话会失效。国标别名侧（`alias_codes`）是子串匹配的，
    两侧行为需保持一致。
    """
    q = (q or "").strip().lower()
    if not q:
        return []
    idx = _cap_term_index()
    hits: Dict[str, Dict[str, Any]] = {}
    # ① 整词
    for tok in dict.fromkeys(t for t in q.split() if t):
        code = idx.get(tok)
        if code:
            hits[code] = {"cap": code, "word": tok, "name": _cap_name(code),
                          "match": "exact"}
    # ② 子串（长词优先，一份查询最多认 6 个能力域，免得长句把货架扫爆）
    for w, code, rx in _cap_substr_words():
        if code in hits:
            continue
        if rx is not None:
            if not rx.search(q):
                continue
        elif w not in q:
            continue
        hits[code] = {"cap": code, "word": w, "name": _cap_name(code),
                      "match": "substr"}
        if len(hits) >= 6:
            break
    return list(hits.values())


def _cap_name(code: str) -> str:
    """能力键 → 人类可读名（取 terms 词面串的首段）。"""
    cap = _load_cap_index() or {}
    t = (cap.get("terms") or {}).get(code, "")
    return str(t).split()[0] if t else code


def _cap_count(code: str) -> int:
    """能力键 → 全库企业数（cap.json 的 shards[code] 是一堆「分片名 → 条数」，求和）。"""
    cap = _load_cap_index() or {}
    return sum((((cap.get("shards") or {}).get(code) or {})).values())


def _cap_shard_paths(cap_code: str) -> List[str]:
    """能力键 → 摘要分片路径（一个逻辑桶可能拆成多个物理分片，必须全收）。

    cap.json shards[code] 的键是**逻辑桶名**（如 `C/34/3484` / `_unclassified`），
    已剥离续片后缀、与 manifest 同口径。
    fp 分片按条数切成续片后，一个桶对应多条 manifest 记录（桶名相同、路径各异）
    ——只取一条会把续片里的企业整批丢掉，且不报错。
    """
    cap = _load_cap_index() or {}
    names = (cap.get("shards") or {}).get(cap_code, {})
    if not names:
        return []
    by_bucket: Dict[str, List[str]] = {}
    for s in _shards_of_type("fp"):
        b = str(s.get("b", ""))
        p = s.get("p")
        if b and p:
            by_bucket.setdefault(b, []).append(p)
    paths: List[str] = []
    for nm in names:
        ps = by_bucket.get(str(nm)) or []
        if not ps:
            # manifest 兜底：本地文件系统按主文件 + -pN 续片直拼
            cand = f"skills/registry/fingerprint/gb/{nm}.jsonl"
            if os.path.exists(cand):
                ps.append(cand)
            base_dir = os.path.dirname(cand)
            stem = os.path.basename(cand)[:-len(".jsonl")]
            if os.path.isdir(base_dir):
                for fn in sorted(os.listdir(base_dir)):
                    if fn.startswith(stem + "-p") and fn.endswith(".jsonl"):
                        ps.append(os.path.join(base_dir, fn))
        for p in ps:
            if p and p not in paths:
                paths.append(p)
    return paths



# --------------------------------------------------------------------------- #
# tool 实现
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 全文索引：首次构建后常驻进程内存，并按 HEAD sha 缓存到磁盘，
# 避免每次搜索都全扫 267 个 fp 分片（此前逐分片 git show 约 100s）。
# --------------------------------------------------------------------------- #
_fp_index: Optional[List[Dict[str, Any]]] = None
_fp_index_key: Optional[str] = None
_name_gb_slot: Optional[tuple] = None   # (id -> gb 映射, loaded_at, mtime)


def _head_sha() -> Optional[str]:
    if not REPO:
        return None
    try:
        r = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _data_version() -> str:
    """数据版本号，用于给落盘缓存判新。

    git 模式 = HEAD sha（提交变则版本变）；
    HTTP 模式 = 清单里的 generated_at（**一发布新数据版本就变**，
    所以不会读到陈旧的已发布快照 —— 这正是当初不敢在 HTTP 模式落盘的顾虑）。

    取不到就返回空串，调用方据此**禁用**缓存（宁慢不脏）。
    """
    if REPO:
        return _head_sha() or "nosha"
    try:
        return str(load_manifest().get("metadata", {}).get("generated_at") or "")
    except Exception:
        return ""


def _index_cache_path() -> Optional[str]:
    """索引落盘缓存的路径。

    HTTP 模式也可落盘：缓存键包含「数据版本号」，一发布新数据键就变，
    因此不会读到陈旧的已发布快照。
    """
    sig = _data_version()
    if not sig:
        return None
    key = (BEACON_REPO or BEACON_SOURCE) + "|" + sig
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, "fpindex-" + h + ".json")


# ── id → 国标码 的定址分片 ────────────────────────────────────────────
# 为什么需要：`get_vendor(id)` 在调用方没给国标码时得先定位。
#
# id 是**密集自增**的 `CN-MFG-%07d` → 按 id 千位切片即可直接定址，
# 不需要额外目录表。
#     CN-MFG-0093680 → 93680 // 1000 = 93 → skills/registry/index/编号映射/093.jsonl
# 一片约 1000 行，一次只拉命中那一片。
#
# 附带一个重要性质：id 密集且分片完整时，**「分片取到了但 id 不在」= 这条 id 确实
# 不存在**（不是索引滞后）→ 可以直接给「查无此人」，不必退回全量索引。
# 这个区分（shard_ok）是下面 _编号映射_lookup 返回两元组的原因。
_IDMAP_BUCKET = 1000
_VENDOR_ID_RE = re.compile(r"^CN-MFG-(\d{7})$")
_IDMAP_DIR = "skills/registry/index/idmap"
# 片内解析结果常驻内存：rel -> (id->gb, 数据版本号)
_idmap_slot: Dict[str, tuple] = {}
# meta.json（切片参数）常驻内存：({...}, 数据版本号)
_idmap_meta_slot: Optional[tuple] = None


def _idmap_bucket(vid: str) -> Optional[int]:
    """id 落在第几片；非 `CN-MFG-\\d{7}` 形态返回 None（归 other.jsonl）。"""
    m = _VENDOR_ID_RE.match(vid or "")
    return int(m.group(1)) // _IDMAP_BUCKET if m else None


def _idmap_rel(vid: str) -> Optional[str]:
    b = _idmap_bucket(vid)
    if b is None:
        return (_IDMAP_DIR + "/other.jsonl") if vid else None
    return "%s/%03d.jsonl" % (_IDMAP_DIR, b)


def _idmap_meta() -> Dict[str, Any]:
    """编号映射 的切片参数（来自清单的 `metadata.编号映射`）。

    两个作用：

    1. **范围守卫**：越过已发布范围的 id（bucket >= buckets）如果照常去请求分片，
       会一路失败到超时。有 meta 就能先判定「这条 id 根本不存在」，
       把这段等待整段省掉。

    2. **开关**：清单里**没有**这个块 = 该快照的 编号映射 还没发布（老版本快照）。
       此时整体跳过 编号映射 路径、直接走原有定位方式 —— 于是「MCP 先升级、数据还没
       发布」的窗口期里，客户端行为与升级前**完全一致**，不会多付任何请求或 404。

    ⚠ 只从**清单**读（HTTP 模式）。清单为了定位分片本来就要拉，所以零额外请求；
      单拉一个 meta.json 在窗口期恰恰就是一个 404。本地模式（BEACON_REPO）下
      才补一次 编号映射/meta.json 的本地文件读 —— 那是磁盘读，不产生网络请求，
      方便「只重算了 编号映射、还没重算清单」的开发中间态。
    """
    global _idmap_meta_slot
    ver = _data_version()
    if _idmap_meta_slot is not None and _idmap_meta_slot[1] == ver:
        return _idmap_meta_slot[0]

    m: Dict[str, Any] = {}
    try:
        blk = load_manifest().get("metadata", {}).get("idmap")
        if isinstance(blk, dict) and blk:
            m = blk
    except Exception:
        m = {}
    if not m and REPO:
        try:
            txt = _index_text(_IDMAP_DIR + "/meta.json")
            if txt:
                o = json.loads(txt)
                if isinstance(o, dict):
                    m = o
        except Exception:
            m = {}
    _idmap_meta_slot = (m, ver)
    return m


def _idmap_lookup(vid: str) -> Tuple[Optional[str], bool]:
    """定位 id → 国标码。返回 (gb, shard_ok)。

    gb 为 `None` = 该片里没有这条 id；gb 为空串 = **未归类**（这是有效答案，
    客户端必须照样按空串走 _unclassified 那条路，而不是当成「没找到」）。
    shard_ok = 该片的**范围已被覆盖** → 此时 id 缺席可判定为「不存在」，
    调用方不必再去付全量兜底的代价。

    ⚠ 「片里没有」要**先过 max_id 这道闸**才能判不存在。抓取流水线与政府信源
    导入每天都在发新号，而 编号映射 只在 manifest 步重算、只在 pages 步上云 ——
    于是「刚导入、还没发布」的新 id 天然不在已发布的 编号映射 里。
    若只看「片存在但片里没这条」就判不存在，等于**把新导入的企业全部误杀**
    （政府名录一次能进上千家，症状还是静默的：查不到，不报错）。
    判据：id 数字 <= meta.max_id 才敢判「真不存在」；超过就退回慢路径兜底。
    """
    meta = _idmap_meta()
    nb = meta.get("buckets")
    if not isinstance(nb, int) or nb <= 0:
        return None, False          # 快照里没有 idmap（尚未发布）→ 调用方按老路径走

    # 已发布的最大 id 数字。老版本快照没有这个字段 → 退化成「只能靠 buckets 守卫」，
    # 此时新 id 会被误判，所以**取不到就整体不走 编号映射**（宁可慢，不可错）。
    mx = meta.get("max_id")
    if not isinstance(mx, int) or mx < 0:
        return None, False

    # ⚠ 两个守卫的**顺序不能反**。
    #   max_id 是比 buckets 更精确的上界（buckets 只精确到千位）：
    #   例如 buckets=183 / max_id=182302 时，id=0187302 的 bucket=187 已越界，
    #   但它其实只是「比已发布最大号大 5000」的新号 —— 若先走 buckets 守卫就会
    #   被判成不存在（误杀）。所以先用 max_id 收口，再谈 buckets。
    m_vid = _VENDOR_ID_RE.match(vid or "")
    if m_vid is not None and int(m_vid.group(1)) > mx:
        return None, False      # 尚未发布的新号 → 交回调用方走兜底

    # 范围守卫：连千位片都还没生成 = 远在未来的号，直接判定，
    # 不去请求那个必定 404 的分片。
    b = _idmap_bucket(vid)
    if b is not None and b >= nb:
        return None, True

    rel = _idmap_rel(vid)
    if not rel:
        return None, False
    ver = _data_version()
    slot = _idmap_slot.get(rel)
    if slot is not None and slot[1] == ver:
        m = slot[0]
        return (m.get(vid), True) if m else (None, False)
    txt = _index_text(rel)
    if txt is None:
        return None, False                    # 片没取到（网络问题）→ 允许慢路径兜底
    m: Dict[str, str] = {}
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        i, sep, g = line.partition(",")
        if sep:
            m[i] = g
    _idmap_slot[rel] = (m, ver)
    return m.get(vid), True


def _name_gb_index() -> Dict[str, str]:
    """`id -> 国标码` 映射，来源 `data/name-index.jsonl`（发布产物）。

    为什么要有它：`get_vendor` 在调用方没给 gb 时需要先定位；顺序遍历全部 fp
    分片去找那个 id；本地批量读看不出来，但远端的串行请求会非常慢。
    name-index 每条记录自带 id/co/city/gb，**一次读取**即可定位到目标分片：
    一次索引读 + 一次分片拉取。

    值可能是空串（该记录未归类），那不是缺失 —— 空串是有效答案，代表要直接去
    `_unclassified` 那条路径，而不是再扫 320 个 fp 分片。
    """
    global _name_gb_slot
    if _cache_stale(_name_gb_slot, "data/name-index.jsonl"):
        m: Dict[str, str] = {}
        try:
            txt = _index_text("data/name-index.jsonl")
            if txt:
                for line in txt.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    i = o.get("id")
                    if i:
                        m[i] = o.get("gb") or ""
        except Exception:
            m = {}
        _name_gb_slot = (m, time.time(), _wt_mtime("data/name-index.jsonl"))
    return _name_gb_slot[0]


def _read_fp_shard(s: Dict[str, Any]) -> List[Dict[str, Any]]:
    # 走 _shard_text：摘要分片同样登记在清单里，按内容 sha1 缓存后，
    # 重复检索（换关键词、换城市）不再重复下载 fp 层。
    txt = _shard_text(s["p"])
    out: List[Dict[str, Any]] = []
    if not txt:
        return out
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _build_fp_index_via_archive() -> Optional[List[Dict[str, Any]]]:
    """git 模式：用 `git archive HEAD -- <dir>` 一次性批量取出所有 fp 分片。

    只读已提交 object（本就是 maskphone 隐藏态），不碰本地副本、不触发 smudge、
    不碰 index 锁 —— 安全边界与逐分片 `git show` 一致，但把 267 次 subprocess
    降到 1 次，首次加载从 ~48s 降至数秒。任一环节失败都返回 None，由调用方退回
    逐分片 fallback。
    """
    try:
        d = os.path.join("skills", "registry", "fingerprint")
        r = subprocess.run(
            ["git", "-C", REPO, "archive", "HEAD", "--", d],
            capture_output=True,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        recs: List[Dict[str, Any]] = []
        with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:*") as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                try:
                    data = tf.extractfile(m).read().decode("utf-8", "replace")
                except Exception:
                    continue
                for line in data.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        continue
        return recs if recs else None
    except Exception:
        return None


def _build_fp_index_from_worktree() -> List[Dict[str, Any]]:
    """本地 git 模式 + BEACON_WORKTREE!=0：直接扫本地副本 fingerprint 分片，闭合检索缺口。

    逐行解析（每行非法 JSON 跳过，避免读到 cron 写入中途的半成品），按 id 去重后返回。
    仅补充/覆盖『已提交快照之外』的本地副本记录，不读非 fingerprint 目录。
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not REPO:
        return []
    if os.environ.get("BEACON_WORKTREE", "1") == "0":
        return []
    d = os.path.join(REPO, "skills", "registry", "fingerprint")
    if not os.path.isdir(d):
        return []
    for root, _dirs, files in os.walk(d):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            fp = os.path.join(root, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = f.read()
            except Exception:
                continue
            for line in data.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                rid = r.get("id") if isinstance(r, dict) else None
                if rid:
                    out[rid] = r
    return list(out.values())


def _merge_worktree_fp(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """用本地副本 fingerprint 记录覆盖/补充已提交快照（按 id；本地副本胜出），闭合检索缺口。"""
    wt_recs = _build_fp_index_from_worktree()
    if not wt_recs:
        return recs
    by_id: Dict[str, Dict[str, Any]] = {
        r["id"]: r for r in recs if isinstance(r, dict) and r.get("id")
    }
    for r in wt_recs:
        if isinstance(r, dict) and r.get("id"):
            by_id[r["id"]] = r   # 工作树覆盖已提交
    return list(by_id.values())


def _build_fp_index() -> List[Dict[str, Any]]:
    global _fp_index, _fp_index_key
    cp = _index_cache_path()
    wt = os.environ.get("BEACON_WORKTREE", "1") != "0"
    key = (cp or "mem") + ("|wt" if wt else "")
    if wt:
        # 本地副本模式：manifest 由 派生重建 每次重建（mtime 随之变），把它并进键 →
        # fp 分片被重建后 fp_index 自动翻新，无需提交 / 重启。
        mm = _wt_mtime("data/manifest.json")
        key += "|m%.0f" % (mm if mm is not None else 0)
    if _fp_index is not None and _fp_index_key == key:
        return _fp_index
    if cp and not wt and os.path.exists(cp):
        try:
            with open(cp, "r", encoding="utf-8") as f:
                _fp_index = json.load(f)
                _fp_index_key = key
                return _fp_index
        except Exception:
            pass
    fp_shards = _shards_of_type("fp")
    # git 模式优先用 `git archive` 一次性批量读（1 次 subprocess，远快于逐分片 git show）
    if REPO:
        recs = _build_fp_index_via_archive()
        if recs is not None:
            # 本地副本模式：用本地副本 fingerprint 分片覆盖/补充已提交快照，闭合检索缺口
            if wt:
                recs = _merge_worktree_fp(recs)
            _fp_index = recs
            _fp_index_key = key
            # 本地副本模式不落盘缓存（内容随本地副本变化，落盘会陈旧）
            if cp and not wt:
                try:
                    with open(cp, "w", encoding="utf-8") as f:
                        json.dump(recs, f, ensure_ascii=False)
                    for old in os.listdir(CACHE_DIR):
                        if old.startswith("fpindex-") and old != os.path.basename(cp):
                            try:
                                os.remove(os.path.join(CACHE_DIR, old))
                            except Exception:
                                pass
                except Exception:
                    pass
            return recs
    # fallback：逐分片读取（HTTP 模式，或 git archive 异常时）
    recs = []
    # git show / HTTP GET 均为 I/O 密集，线程池并发拉取可大幅缩短首次构建耗时
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        for part in ex.map(_read_fp_shard, fp_shards):
            recs.extend(part)
    if wt and REPO:
        recs = _merge_worktree_fp(recs)
    _fp_index = recs
    _fp_index_key = key
    if cp and not wt:
        try:
            with open(cp, "w", encoding="utf-8") as f:
                json.dump(recs, f, ensure_ascii=False)
            for old in os.listdir(CACHE_DIR):
                if old.startswith("fpindex-") and old != os.path.basename(cp):
                    try:
                        os.remove(os.path.join(CACHE_DIR, old))
                    except Exception:
                        pass
        except Exception:
            pass
    return recs


_CAP_TERMS_SLOT: Optional[tuple] = None


def _cap_term_of(code: str) -> str:
    """能力键 → 中文词面（`tech_ai` → `人工智能与算法 人工智能 AI 算法 …`）。

    词面表来自 `skills/registry/index/cap.json`（发布产物，与 city.json 同构）——
    刻意**不**把词面写进摘要：那一层按字节计费（约 139k 条），而词面表只有几十 KB，
    且改词面（加别名）不需要重建摘要。

    取不到时退回键本身：`caps_index` 允许出现码表之外的裸值（材料「不锈钢」、
    品类「正餐」），它们本身就是可检索词，丢掉等于让这类查询瞎掉。
    """
    global _CAP_TERMS_SLOT
    if _cache_stale(_CAP_TERMS_SLOT, "skills/registry/index/cap.json"):
        terms: Dict[str, Any] = {}
        try:
            txt = _index_text("skills/registry/index/cap.json")
            if txt:
                terms = (json.loads(txt).get("terms") or {})
        except Exception:
            terms = {}
        _CAP_TERMS_SLOT = (terms, time.time(), _wt_mtime("skills/registry/index/cap.json"))
    return str(_CAP_TERMS_SLOT[0].get(code) or code or "")


def _hay_parts(rec: Dict[str, Any], with_cap: bool) -> str:
    parts = [
        # `or ""` 不是多余：gb 为 None（未归类）时 str() 会产出字面量 "None"，
        # 2271 条 gb=null 的记录于是每条都带一个 "None" 词面 —— 查 "none" 能命中
        # 整个未归类批（假阳性），而且白占检索面字节。空值就是空值。
        " ".join(str(rec.get(k) or "") for k in ("co", "city", "dist", "gb")),
        " ".join(rec.get("proc", []) or []),
        " ".join(rec.get("mat", []) or []),
        " ".join(rec.get("cert", []) or []),
        " ".join(rec.get("products", []) or []),
    ]
    if with_cap:
        parts.append(" ".join(_cap_term_of(c) for c in (rec.get("cap") or [])))
    return " ".join(p for p in parts if p)


def _hay(rec: Dict[str, Any]) -> str:
    """检索面：query 的每个词都必须出现在这里（AND 全命中）。

    另有 `cap`（跨门类能力键）：其余字段都表达不了「能力」，
    而能力键必须摊成中文词面才能被中文命中。
    """
    return _hay_parts(rec, True)


def _hay_lit(rec: Dict[str, Any]) -> str:
    """**字面**检索面 = `检索面` 去掉 cap 词面。

    存在的理由：`检索面` 把厂名/工艺与 cap 词面混在一起，于是「query 词出现在
    检索面里」这一条**分不清**是「厂名/工艺真写了这个词」还是「只是能力键的中文
    词面撞上了」；而 `via=text` 会让 agent 误以为「这家厂自己写了 AI」。
    因此 text 的判定走这里。
    """
    return _hay_parts(rec, False)


def _name_fit(rec_or_sum: Dict[str, Any], tokens: List[str]) -> int:
    """关键词在**厂名**里的贴合度，0/1/2。用于同一召回档内部的次级排序。

    只有档位是不够的：档内顺序原本就是分片里的记录顺序 —— 等于随机。
    还需要判断关键词是否**成片出现在厂名里**：只判断「是否出现」不够
    （括号里的商圈名也会撞上），必须是连片的词面命中才区分得开。

    `query+city` 与 `gb=` 两条路由共用本函数，保证同一批记录两条路排出来的顺序一致
    （体检的「C 路由一致性」就是查这个）。
    """
    if not tokens:
        return 0
    co = str(rec_or_sum.get("company") or rec_or_sum.get("co") or "").lower()
    if "".join(tokens) in co:
        return 2                     # 关键词在厂名里成片出现 —— 最强
    return 1 if all(t in co for t in tokens) else 0


def _rec_summary(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": rec.get("id"),
        "company": rec.get("co"),
        "city": rec.get("city"),
        "district": rec.get("dist"),
        "gb": rec.get("gb"),
        "badge": rec.get("cl"),
        "score": rec.get("sc"),
        "has_phone": bool(rec.get("tel")),
        "process": rec.get("proc", []),
        "material": rec.get("mat", []),
        "cert": rec.get("cert", []),
    }


INDEX_DIR = "skills/registry/index"
INDEX_VERSION = 3          # 索引键为分片路径（用国标码会漏掉未归类的记录）
_index_meta_cache: Optional[Dict[str, Any]] = None
_bucket_cache: Dict[str, tuple] = {}
_shard_rec_cache: Dict[str, tuple] = {}

_RE_CJK = re.compile("[\u4e00-\u9fff\u3400-\u4dbf]+")
_RE_WORD = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    """切成可索引的词。必须与 scripts/gen_search_index.py 的 tokenize 保持一致。

    CJK 取 1-gram + 2-gram；拉丁/数字取整词 + 长度 >= 2 的前缀。
    """
    return _grams(text, query_mode=False)


def _grams(text: str, query_mode: bool = False) -> List[str]:
    """query_mode=True 时，长度 >= 2 的 CJK 串只用 2-gram。

    原因：查询侧若同时用 1-gram 求交，含「酒」和「店」但不含「酒店」的分片
    也会被算成候选，白白多拉分片。2-gram 已足以保证
    召回（含「酒店」的记录必然含 bigram「酒店」），单字查询仍走 1-gram。
    """
    t = (text or "").lower()
    out = set()
    for run in _RE_CJK.findall(t):
        long_run = len(run) >= 2
        for i, ch in enumerate(run):
            if not (query_mode and long_run):
                out.add(ch)
            if i + 2 <= len(run):
                out.add(run[i:i + 2])
    for w in _RE_WORD.findall(t):
        if len(w) >= 2:
            out.add(w)
            for n in range(2, min(len(w), 12)):
                out.add(w[:n])
        elif w:
            out.add(w)
    return sorted(out)


def _index_text(relpath: str) -> Optional[str]:
    """读索引文件。按**数据版本号**落盘缓存，缓存后跨进程重复查询几乎零成本。

    BEACON_WORKTREE=1（默认）：磁盘缓存额外按本地副本文件 mtime 判新 —— 派生重建
    重建索引后 mtime 变化，下次查询即重读本地副本版本，无需提交 HEAD 即可让关键词 /
    城市检索看到新数据（修 DSH 发现的「不提交就永远看不到」坑）。非本地副本模式保持
    旧的「HEAD 变化才刷新」行为。

    HTTP 模式也落盘：name-index 与 编号映射 的重复解析成本高，值得缓存。
    版本号取 _data_version()：一发布新数据即失效，不会读到陈旧快照。
    """
    cp = None
    ver = _data_version()
    if ver:
        cp = os.path.join(CACHE_DIR, "idx-" +
                          hashlib.sha1((ver + "|" + relpath).encode("utf-8")).hexdigest() + ".json")
        if os.path.exists(cp) and _index_sig_ok(cp, relpath):
            try:
                with open(cp, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
    txt = fetch_text(relpath)
    if txt and cp:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cp, "w", encoding="utf-8") as f:
                f.write(txt)
            _write_index_sig(cp, relpath)
        except Exception:
            pass
    return txt


def _index_sig_ok(cp: str, relpath: str) -> bool:
    """本地副本模式下，磁盘缓存里的 mtime 印记是否与当前本地副本文件一致。

    印记用 repr 精确往返（浮点不丢精度），避免 派生重建 在同一秒内重写索引文件时
    截断判成「未变」而误服陈旧缓存。
    """
    sig = _wt_mtime(relpath)
    if sig is None:
        return True                      # 非工作树：不按 mtime 判新
    sf = cp + ".sig"
    try:
        with open(sf, "r", encoding="utf-8") as f:
            return float(f.read().strip()) == sig
    except Exception:
        return False                     # 缺印记 / 损坏 → 重建


def _write_index_sig(cp: str, relpath: str) -> None:
    sig = _wt_mtime(relpath)
    sf = cp + ".sig"
    try:
        if sig is None:
            if os.path.exists(sf):
                os.remove(sf)
            return
        with open(sf, "w", encoding="utf-8") as f:
            f.write(repr(sig))
    except Exception:
        pass


def _git_read_many(paths: List[str]) -> Dict[str, str]:
    """git 模式：一次 `git archive` 批量取出多个文件。

    「每个文件一次 git show」是首次加载的主要开销（18 个分片 ≈ 5s），
    批量取把 N 次 subprocess 降到 1 次。只读已提交 object，安全边界不变。
    """
    if not REPO or not paths:
        return {}
    try:
        r = subprocess.run(["git", "-C", REPO, "archive", "HEAD", "--", *paths],
                           capture_output=True)
        if r.returncode != 0 or not r.stdout:
            return {}
        out: Dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:*") as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                try:
                    out[m.name.replace("\\", "/").lstrip("/")] = \
                        tf.extractfile(m).read().decode("utf-8", "replace")
                except Exception:
                    continue
        return out
    except Exception:
        return {}


def _read_many_text(paths: List[str]) -> Dict[str, str]:
    """批量取文本：本地副本优先 → git archive 兜底；HTTP 模式线程池并发。

    BEACON_WORKTREE=1（默认）：**必须本地副本优先**。索引与分片都走这里 ——
    若只用「git archive HEAD」就只会读到**已提交快照**，刚重建、尚未提交的
    派生层完全看不见。本地副本缺的文件再走已提交快照兜底，最后 HTTP。
    非本地副本模式（BEACON_WORKTREE=0）只读已提交快照。
    """
    if not paths:
        return {}
    if REPO:
        if os.environ.get("BEACON_WORKTREE", "1") != "0":
            out: Dict[str, str] = {}
            missing: List[str] = []
            for p in paths:
                w = _worktree_read(p)
                if w is not None:
                    out[p] = w
                else:
                    missing.append(p)
            if missing:                       # 工作树缺失/半成品 → 已提交快照兜底
                out.update(_git_read_many(missing))
            return out
        got = _git_read_many(paths)
        if len(got) >= max(1, len(paths) // 2):   # archive 正常覆盖
            return got
    # ⚠ HTTP 分支必须走 _shard_text（按清单 h 做**内容哈希缓存**），不能裸调 fetch_text。
    #   这里是 _candidate_shards（索引桶）与 _records_from_paths（fp 分片）的**公共取数口**：
    #   一次「模具」检索要拉大量 fp 分片。裸 fetch_text 只有 URL 级缓存，
    #   而 URL 缓存**不跨进程复用**（每次新进程都重下），于是热启动与首次加载同速
    #   —— 走缓存后不必重复付出这笔解析开销。
    #   换 _shard_text 后，内容没变就完全不走网络（连 304 都省），且跨进程有效。
    #   （没有 h 的路径——如索引桶——_shard_text 内部自动退回 fetch_text，行为不变。）
    _shard_hash_index()          # 预热：别让线程池里做首次构建
    out: Dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        for path, txt in zip(paths, ex.map(_shard_text, paths)):
            if txt:
                out[path] = txt
    return out


def _index_meta() -> Optional[Dict[str, Any]]:
    global _index_meta_cache
    if _cache_stale(_index_meta_cache, INDEX_DIR + "/meta.json"):
        txt = _index_text(INDEX_DIR + "/meta.json")
        if not txt:
            _index_meta_cache = (None, time.time(), _wt_mtime(INDEX_DIR + "/meta.json"))
        else:
            try:
                _index_meta_cache = (json.loads(txt), time.time(),
                                    _wt_mtime(INDEX_DIR + "/meta.json"))
            except Exception:
                _index_meta_cache = (None, time.time(), _wt_mtime(INDEX_DIR + "/meta.json"))
    return _index_meta_cache[0]


def _index_fresh() -> bool:
    """索引可用且版本匹配、不落后于数据 → True；否则回退全量扫描。

    索引用等号判「记录数一致」会永远失败：它是提交前从本地副本构建的，
    天然比已提交快照新。因此判「索引不落后于数据」即可 —— 索引更新只会让
    候选分片更全，真正的精确过滤仍在分片侧做，结果等价。
    """
    meta = _index_meta()
    if not meta:
        return False
    try:
        if int(meta.get("version", 0)) != INDEX_VERSION:
            return False
        tot = sum(s.get("k", 0) for s in _shards_of_type("fp"))
        rec = int(meta.get("records", -1))
        return rec >= tot > 0
    except Exception:
        return False


def _bucket_rel(term: str, buckets: int) -> str:
    b = int(hashlib.sha1(term.encode("utf-8")).hexdigest(), 16) % buckets
    return "%s/terms/b%04d.json" % (INDEX_DIR, b)


# --- 自由文本需求 -> 可靠产品词：过滤 + 加权 -------------------------------- #
# df < MIN 的 bigram 几乎必是分词碎片（「送线」df=0），不作证据。
# 泛词（城市名/企业通名）已由 `_usable_gram` 拦掉，故不另设 df 上限。
_RECALL_DF_MIN = 2
# 以功能字开头/结尾、或以企业通名结尾的 bigram 不是产品词（「的输」「线厂」「厂家」）。
_FUNC_CHARS = set("的了要找做想帮我你在和与或及是有能可会请给把被用让需个些这那们吧呢吗来去上下就到从对为")
_ORG_SUFFIX = set("厂家司部行店商社")
_city_names_cache: Optional[set] = None


def _city_names() -> set:
    """预构建索引里出现过的城市名集合：地区词不作为相关性证据（只作筛选偏好）。"""
    global _city_names_cache
    if _cache_stale(_city_names_cache, INDEX_DIR + "/city.json"):
        names: set = set()
        txt = _index_text(INDEX_DIR + "/city.json")
        if txt:
            try:
                names = set(json.loads(txt).keys())
            except Exception:
                names = set()
        _city_names_cache = (names, time.time(), _wt_mtime(INDEX_DIR + "/city.json"))
    return _city_names_cache[0]


def _is_cjk(s: str) -> bool:
    return bool(s) and all("\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" for ch in s)


def _usable_gram(t: str) -> bool:
    """该查询 gram 是否为可采信的产品词：滤掉单字功能词、分词碎片、地区词与企业通名。"""
    if not t:
        return False
    if len(t) == 1:
        return t not in _FUNC_CHARS                 # 单字功能词（的/要/找…）不作证据
    if _is_cjk(t):
        if t[0] in _FUNC_CHARS or t[-1] in _FUNC_CHARS or t[-1] in _ORG_SUFFIX:
            return False
        if t in _city_names():
            return False
    return True


def _collapse_prefixes(toks: set) -> set:
    """英文前缀 token 折叠：`_grams('iso9001')` 会派生 is/iso/iso9/…/iso9001。

    若全留着，一个「ISO9001」会被算成 6 次重复命中（分数被放大），还会误命中
    『BLU ISOLA cafe』这类把 is/iso 当子串的名字。这里只保留最长的那个前缀。
    """
    latin = sorted([t for t in toks if t.isascii() and t.isalnum()], key=len, reverse=True)
    keep: List[str] = []
    for t in latin:
        if not any(k.startswith(t) for k in keep):
            keep.append(t)
    keep_set = set(keep)
    return {t for t in toks if not (t.isascii() and t.isalnum()) or t in keep_set}


def _bucket_postings(gram: str, buckets: int) -> Dict[str, Any]:
    """取预构建索引里某词的 postings {分片路径: 命中条数}；缺失/异常返回空 dict。

    桶缓存按本地副本 mtime 判新（见 _wt_mtime）：派生重建 重建索引后，对应桶文件
    mtime 变化即自动重读，无需重启。
    """
    rel = _bucket_rel(gram, buckets)
    m = _wt_mtime(rel)
    slot = _bucket_cache.get(rel)
    if slot is not None and (m is None or slot[0] == m):
        b = slot[1]
    else:
        txt = _index_text(rel)
        try:
            b = json.loads(txt) if txt else {}
        except Exception:
            b = {}
        _bucket_cache[rel] = (m, b)
    post = b.get(gram) if isinstance(b, dict) else None
    return post if isinstance(post, dict) else {}


def _bucket_df(gram: str, buckets: int) -> int:
    """某词的文档频次 df = postings 各分片命中数之和。索引缺失返回 0。"""
    post = _bucket_postings(gram, buckets)
    if not post:
        return 0
    try:
        return sum(int(v) for v in post.values())
    except Exception:
        return len(post)


def _gram_idf(grams) -> Dict[str, float]:
    """按预构建索引的 df 给产品词估 IDF 权重 = 1/(1+ln(df))。

    稀有的真产品词（「输送」df=6 -> 0.36）权重高；泛词权重低、自然让位，
    碎片（df<MIN，如「送线」）直接剔除。索引不可用时返回空 dict，
    调用方退化为均匀权重 1.0。
    """
    meta = _index_meta()
    if not meta:
        return {}
    try:
        buckets = int(meta["buckets"])
    except Exception:
        return {}
    out: Dict[str, float] = {}
    for g in grams:
        if not g:
            continue
        d = _bucket_df(g, buckets)
        if d < _RECALL_DF_MIN:
            continue
        out[g] = 1.0 / (1.0 + math.log(d))
    return out


def _recall_candidates(toks) -> Optional[List[Dict[str, Any]]]:
    """预构建索引『区分词并集』快速召回：取并集（OR）而非交集。

    锚定所有采信的产品词（df >= _RECALL_DF_MIN）。碎片/地区词/通名已在 `_usable_gram`
    与 `_gram_idf` 阶段剔除，故这里只做并集。索引不可用、无采信词、或并集覆盖过大
    （≥60% 分片）时返回 None，交调用方全量扫描兜底（结果一致）。
    """
    if not _index_fresh():
        return None
    meta = _index_meta() or {}
    try:
        buckets = int(meta["buckets"])
    except Exception:
        return None

    known = {s.get("p") for s in _shards_of_type("fp")}
    picked: set = set()
    for g in toks:
        if not g:
            continue
        if _bucket_df(g, buckets) < _RECALL_DF_MIN:   # 碎片 -> 不作锚
            continue
        picked.update(p for p in _bucket_postings(g, buckets) if p in known)
    if not picked:
        return None
    if len(picked) >= max(1, int(len(known) * 0.6)):   # 并集过大，全量扫描更划算
        return None
    return _records_from_paths(sorted(picked))


def _candidate_shards(tokens: List[str], city: str) -> Optional[List[str]]:
    """用预构建索引求候选分片路径；索引不可用/版本不符/陈旧时返回 None 交回退。

    返回空列表表示「索引明确判定无命中」，无需拉任何分片。
    """
    if not _index_fresh():
        return None
    meta = _index_meta() or {}
    try:
        buckets = int(meta["buckets"])
    except Exception:
        return None

    gram_groups: List[List[str]] = []
    for tok in tokens:
        gs = _grams(tok, query_mode=True)
        if not gs:
            return None
        gram_groups.append(gs)

    need: List[str] = []
    if city:
        need.append(INDEX_DIR + "/city.json")
    for gs in gram_groups:
        for g in gs:
            need.append(_bucket_rel(g, buckets))
    need = sorted(set(need))

    raw = _read_many_text(need)
    docs: Dict[str, Any] = {}
    for k in need:
        t = raw.get(k)
        if not t:
            continue
        try:
            docs[k] = json.loads(t)
        except Exception:
            return None

    cands: Optional[set] = None
    if city:
        cd = docs.get(INDEX_DIR + "/city.json")
        if cd is None:
            return None
        m = cd.get(city)
        if not m:                      # 索引里没这个城市 → 确无命中
            return []
        cands = set(m.keys())

    for gs in gram_groups:
        tset: Optional[set] = None
        for g in gs:
            d = docs.get(_bucket_rel(g, buckets))
            m = d.get(g) if isinstance(d, dict) else None
            s = set(m.keys()) if m else set()   # 桶里没这个词 → 确无命中
            tset = s if tset is None else (tset & s)
            if not tset:
                break
        if tset is None:
            return None
        cands = tset if cands is None else (cands & tset)
        if not cands:
            return []

    if cands is None:
        return None
    # 与 manifest 求交：防止陈旧索引指向已删除的分片
    known = {s.get("p") for s in _shards_of_type("fp")}
    return sorted(p for p in cands if p in known)


def _records_from_paths(paths: List[str]) -> List[Dict[str, Any]]:
    """批量读取若干 fp 分片的记录，并在进程内按路径缓存（重复查询近乎零成本）。

    缓存按本地副本 mtime 判新（见 _wt_mtime）：派生重建 重建 fp 分片后，对应分片 mtime
    变化即重读，无需重启；非本地副本模式保持旧行为（按路径存在性判新）。
    """
    out: List[Dict[str, Any]] = []
    if not paths:
        return out
    todo: List[str] = []
    for p in paths:
        m = _wt_mtime(p)
        slot = _shard_rec_cache.get(p)
        if slot is not None and (m is None or slot[0] == m):
            continue
        todo.append(p)
        _shard_rec_cache[p] = (m, [])
    for _path, txt in _read_many_text(todo).items():
        recs: List[Dict[str, Any]] = []
        for line in txt.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
        _shard_rec_cache[_path] = (_wt_mtime(_path), recs)
    for p in paths:
        slot = _shard_rec_cache.get(p)
        if slot:
            out.extend(slot[1])
    return out


def _city_ok(rec: Dict[str, Any], city: str) -> bool:
    """city 匹配「地级市 或 区县」：县级市（昆山/海盐…）在高德里归到地级市名下，
    记录里 city=苏州、dist=昆山。只比 city 的话查「昆山」永远 0 条。
    """
    if not city:
        return True
    return city in (rec.get("city", ""), rec.get("dist", ""))


def search_vendors(query: str = "", city: str = "", gb: str = "",
                   limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    q = (query or "").strip().lower()
    # 原话归一化：用户输入的是**一句人话**，不是空格分隔的关键词表。
    # 归一化把它切成可执行的检索参数，并顺带把原话里的城市抽成 city 过滤条件。
    # 详见 `_normalize_query`。
    norm = _normalize_query(query, city) if q else None
    if norm:
        city = norm["city"] or city
        tokens = norm["tokens"]
        cap_hits = norm["cap_hits"]
        alias_hits = norm["alias_hits"]
    else:
        tokens, cap_hits, alias_hits = [], [], []
    # 能力（cap）别名：映射到能力键（如 tech_ai）后**消费**掉该 token（不再做通用
    # 子串 AND 校验），只走 cap 定向召回 —— 否则「AI」会子串命中 algebraist /
    # Ashore 等英文名咖啡店造成噪声。
    # 只消费「会造成子串噪声的短 ASCII token」（ai / cnc …），中文/较长 token 不消费
    # —— 保留其文本匹配（厂名含「人工智能」的企业不被误丢），最终是「文本 ∪ 能力」并集。
    cap_tokens = {h["word"].lower() for h in cap_hits
                  if len(h["word"]) <= 2 and h["word"].isascii()}
    gen_tokens = [t for t in tokens if t not in cap_tokens]
    # 口语词 → 国标码（别名表）。
    # 「输送线/流水线/PCB」这类词在任何记录的厂名/工艺/材料里都不出现，纯子串匹配
    # 必然 0 条。别名命中的记录按码定向召回，**豁免 AND token 校验** —— 不豁免的话
    # 刚拉进来就又被 检索面 判定「不含输送线」而滤掉，等于白接。
    alias_by_code = {a["code"]: a for a in alias_hits}
    alias_paths = _fp_paths_for_gbs(list(alias_by_code)) if alias_by_code else []

    # 只有「原话确实被拆过」时才回显解析结果：一句话查询（抽了城市 / 剔了停用词 /
    # 识别出词条）必须让 agent 看见 MCP 是怎么理解的，否则它无法判断结果为何是这样。
    # 普通关键词查询不额外增加返回体字节。
    parsed: Dict[str, Any] = {}
    if norm and (norm["city_from_query"] or norm["segments"]
                 or tokens != [t for t in q.split() if t]):
        parsed["query_parsed"] = {
            "city": city or "",
            "city_from_query": norm["city_from_query"] or "",
            "keywords": gen_tokens,
            "recognized": norm["segments"],
        }

    # 给了国标码：只扫对应分片（最快路径，不构建全量索引）
    if gb:
        fp_shards = [s for s in _shards_of_type("fp") if s.get("c") == gb]
        scanned = 0
        matches: List[Dict[str, Any]] = []
        for s in fp_shards:
            scanned += 1
            for rec in _read_fp_shard(s):
                if not _city_ok(rec, city):
                    continue
                if gen_tokens and not all(tok in _hay(rec).lower() for tok in gen_tokens):
                    continue
                s = _rec_summary(rec)
                s["via"] = "gb"
                matches.append(s)
        # 与 query 路由同一套档内排序（`gb=` 只走一档，故按厂名贴合度降序即可），
        # 否则同一批记录经两条路进来顺序不同，agent 会以为「换了个写法结果就变了」。
        matches.sort(key=lambda x: -_name_fit(x, gen_tokens))
        return _attach_routing_hint({
            "total_matched": len(matches),
            "returned": len(matches[offset:offset + limit]),
            "shards_scanned": scanned,
            "results": matches[offset:offset + limit],
            **parsed,
            # 同 query 路由的口径：档内按「关键词在厂名里成片出现」优先，score 不是相关度。
            "order_note": ("results 按「关键词在厂名里成片出现」优先排列；score 是"
                           "**能力画像分**（有已发布能力卡才有分，无卡为 0），"
                           "**不是相关度**，不要拿它反推排序。"),
        }, q, city)

    # 未给国标码：优先走「预构建索引 → 只拉命中分片」。
    # 索引缺失或陈旧时回退进程内全量索引（与命中量相关，慢但结果等价）。
    cands = _candidate_shards(gen_tokens, city) if (gen_tokens or city) else None
    via_index = cands is not None

    if via_index and not cands and not alias_paths and not cap_hits:
        # 索引明确判定无命中、别名也没给方向 —— 无需拉任何分片
        return _attach_routing_hint({
            "total_matched": 0,
            "returned": 0,
            "shards_scanned": 0,
            "via_index": True,
            "results": [],
            **parsed,
        }, q, city)

    matches: List[Dict[str, Any]] = []
    seen: set = set()

    def _collect(recs, relax_tokens: bool = False) -> None:
        for rec in recs:
            if not _city_ok(rec, city):
                continue
            # 只在有关键词时才算检索面（纯城市查询不需要，省一次 join/条）。
            hay = _hay(rec).lower() if gen_tokens else ""
            if not relax_tokens and gen_tokens and not all(tok in hay for tok in gen_tokens):
                continue
            rid = rec.get("id")
            if rid in seen:      # 别名召回与文本召回的并集要去重
                continue
            seen.add(rid)
            s = _rec_summary(rec)
            # 只有落在**字面**检索面（厂名/城市/工艺/材料/认证/产品）上才算 text；
            # 全靠 cap 词面撞上的如实标 cap_text —— 见 `检索面_lit` 的说明。
            # 没挂 cap 键的记录两张面完全等价（绝大多数），直接复用 hay 不额外算。
            if gen_tokens:
                lit = hay if not rec.get("cap") else _hay_lit(rec).lower()
                s["via"] = "text" if all(tok in lit for tok in gen_tokens) else "cap_text"
            else:
                s["via"] = "city"      # 没给关键词，纯城市筛选
            if relax_tokens:
                a = alias_by_code.get(rec.get("gb") or "")
                if a:
                    s["alias_match"] = {"word": a["word"], "gb": a["code"], "name": a["name"]}
            matches.append(s)

    if via_index:
        # 只拉候选分片（git 模式一次 archive 批量取，通常 1~N 个）。
        # 若全部 token 都被 cap 别名消费掉（gen_tokens 空），且本就有 cap 命中，
        # 则跳过文本召回（否则 city 命中会拉回整座城市的全部记录），只走下方 cap 召回。
        if gen_tokens or not cap_hits:
            scanned = len(cands or [])
            _collect(_records_from_paths(cands or []))
        else:
            scanned = 0
    else:
        if gen_tokens or not cap_hits:
            scanned = len(_shards_of_type("fp"))
            _collect(_build_fp_index())
        else:
            scanned = 0

    # 别名定向召回（后追加，纯度次之）
    alias_scanned = 0
    for p in alias_paths:
        for rec in _records_from_paths([p]):
            alias_scanned += 1
            if not _city_ok(rec, city):
                continue
            rid = rec.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            s = _rec_summary(rec)
            s["via"] = "alias"         # 别名定向召回（企业自己没写过这个词）
            a = alias_by_code.get(rec.get("gb") or "")
            if a:
                s["alias_match"] = {"word": a["word"], "gb": a["code"], "name": a["name"]}
            matches.append(s)

    # 能力（cap）别名定向召回（消费 token，纯度最高，最后追加并去重）。
    # 按能力键扫对应摘要分片，只收 cap 含该键且城市命中的记录；其余通用 token
    # （gen_tokens）仍做 AND 校验，保证「AI 喷涂」这类组合查询不跑偏。
    cap_scanned = 0
    for h in cap_hits:
        for p in _cap_shard_paths(h["cap"]):
            for rec in _records_from_paths([p]):
                cap_scanned += 1
                # 该分片可能含多条记录，只收真正带此能力键的（cap.json 的 shards
                # 表只是「哪些分片含此 cap」，分片内还需按 cap 成员过滤）。
                if h["cap"] not in (rec.get("cap") or []):
                    continue
                if not _city_ok(rec, city):
                    continue
                if gen_tokens and not all(tok in _hay(rec).lower() for tok in gen_tokens):
                    continue
                rid = rec.get("id")
                if rid in seen:
                    continue
                seen.add(rid)
                s = _rec_summary(rec)
                s["via"] = "cap"       # 能力定向召回（跨门类能力键，如 tech_ai）
                s["alias_match"] = {"word": h["word"], "cap": h["cap"],
                                    "name": h["name"], "type": "cap"}
                matches.append(s)

    # 分档排序（稳定）：text → cap_text → alias → cap。
    # 为什么必须显式排：cap_text 与 text 是**同一趟**文本召回里 append 进去的，
    # 不重排两档会交错，order_note 里「按召回来源分档排列」就成了假承诺。
    _RANK = {"text": 0, "city": 0, "cap_text": 1, "alias": 2, "cap": 3}

    # 档内再按厂名贴合度排（见 `_name_fit` 的说明）。这是**档内**微调，不影响档间
    # 次序，也不影响 total_matched。
    matches.sort(key=lambda x: (_RANK.get(x.get("via"), 9), -_name_fit(x, gen_tokens)))

    out = {
        "total_matched": len(matches),
        "returned": len(matches[offset:offset + limit]),
        "shards_scanned": scanned,
        "via_index": via_index,
        "results": matches[offset:offset + limit],
        **parsed,
        # 结果顺序是**有意设计的分档**，不是随机的：按召回纯度递减排列。
        # 而 `score` 是**能力画像分**（有已发布能力卡才有分，无卡为 0），**不是相关度**。
        # 两者一起看极易误判，
        # 所以在这里把口径写死，让 agent 自己能分辨「谁更值得先看」。
        "order_note": ("results 按召回来源分档排列：via=text(厂名/工艺/材料等**字面**命中) → "
                       "via=cap_text(厂名里其实没这个词，只是撞上了能力键的中文词面) → "
                       "via=alias(别名定向) → via=cap(能力定向)；**同一档内**再按「关键词在"
                       "厂名里成片出现」优先（所以「商务酒店」会把 龙翔商务酒店 排在 "
                       "某酒店(中央商务区店) 前面）；score 是**能力画像分**"
                       "（有已发布能力卡才有分，无卡为 0），**不是相关度**，不要拿它反推排序。"),
    }
    if alias_hits:
        out["alias_expanded"] = [{"word": a["word"], "gb": a["code"], "name": a["name"]}
                                 for a in alias_hits]
        out["alias_shards_scanned"] = len(alias_paths)
        out["alias_records_seen"] = alias_scanned
    if cap_hits:
        out["cap_expanded"] = [{"word": h["word"], "cap": h["cap"], "name": h["name"]}
                               for h in cap_hits]
        out["cap_records_seen"] = cap_scanned
    return _attach_routing_hint(out, q, city)


def _pick(*vals: Any) -> Any:
    """取第一个「非空」值。None/""/[]/{} 都算空 —— setdefault 会把 None 当已填，
    旧结构的 `city: null` 于是永远盖住新结构里的 region.city。
    """
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def normalize_vendor(rec: Dict[str, Any]) -> Dict[str, Any]:
    """zh 分片存在两套历史结构，下游 agent 只认一种，在这里展平（只增不删）。

    已归类：co / city / dist / gb ...            （扁平）
    未归类：company / region.{province,city} / category / keywords ...
    manufacturer_id / industry / tel 两种写法都有，一并归一。
    """
    out = dict(rec)
    region = rec.get("region") or {}
    out["co"] = _pick(rec.get("co"), rec.get("company"))
    out["company"] = _pick(rec.get("company"), rec.get("co"))
    out["city"] = _pick(rec.get("city"), region.get("city"))
    out["dist"] = _pick(rec.get("dist"), region.get("district"), region.get("dist"))
    out["province"] = _pick(rec.get("province"), region.get("province"))
    out["gb"] = rec.get("gb") if rec.get("gb") not in (None, "") else None
    out["keywords"] = _pick(rec.get("keywords"), rec.get("products"))
    if not out.get("gb"):
        out["gb_unclassified"] = True
    return out


def get_vendor(vid: str, gb: str = "") -> Dict[str, Any]:
    vid = (vid or "").strip()
    if not vid:
        return {"error": "缺少必填参数 id"}
    gb = (gb or "").strip()
    # 没给国标码就先解析 id -> gb。
    #
    # 定位策略（三级，见下）：先小后大，绝不默认读全量索引。
    #
    # 现在分三级：
    #   ① 编号映射 定址分片（id 千位切片）：常数级 IO
    #   ② 片取到了但 id 不在 → id 密集自增且分片与 zh 同批发布，可判定「不存在」，
    #      直接返回，不走任何慢路径
    #   ③ 片没取到（超出已发布范围）或本地本地副本比索引新 → name-index / 并发全量兜底
    if not gb:
        got, shard_ok = _idmap_lookup(vid)
        if got is not None:
            gb = got               # 归属已确定（可能是空串 = 未归类，那是有效答案）
        elif shard_ok and not REPO:
            return {"error": f"找不到 id={vid}：id 索引里没有这条记录"
                             "（可能尚未发布到云端，或 id 拼写有误）"}
        else:
            idx = _name_gb_index()
            if vid in idx:
                gb = idx[vid]
            else:
                try:
                    for rec in _build_fp_index():   # 已并发，且落盘缓存
                        if rec.get("id") == vid:
                            gb = rec.get("gb") or ""
                            break
                except Exception:
                    pass

    if gb:
        zh_paths = _zh_paths_for_gb(gb)
        if not zh_paths:
            return {"error": f"国标码 {gb} 没有对应的 zh 分片"}
        # 一个国标码常有 2~3 个 zh 续片（主片 + 续片），
        # 串行取等于把延迟相加。并发取、按原顺序判定，命中即返回。
        texts = _fetch_many(zh_paths)
        for zh_path, txt in zip(zh_paths, texts):
            if not txt:
                continue
            try:
                arr = json.loads(txt)
            except Exception as e:
                return {"error": f"分片 {zh_path} 解析失败: {e}"}
            for rec in arr:
                if rec.get("id") == vid:
                    v = normalize_vendor(rec)
                    # zh 记录自身常常**不写 gb**（只留 industry.code），但它就归档在
                    # 这个 gb 分片里。不按分片归属回填的话会出现自相矛盾：
                    # search_vendors 能搜到它、get_vendor 却说
                    # gb=null + gb_unclassified=true。
                    if not v.get("gb"):
                        v["gb"] = gb
                        v["gb_source"] = "shard"   # 记录没写，取自分片归属
                        v.pop("gb_unclassified", None)
                    return {"vendor": v}
        return {"error": f"国标码 {gb} 的全部 zh 分片(共{len(zh_paths)}个)中均未找到 id={vid}"}

    # 兜底：gb 为 null（未归类）—— 这些记录躺在 c=='' 的 zh 分片里
    # （主要是 _unclassified.json，外加几个 xxx/_partial.json）。
    # 未归类企业也要能被检索到，不能直接报「找不到国标码」。
    loose = [s.get("p") for s in _shards_of_type("zh") if not s.get("c")]
    loose_txt = _fetch_many(loose)
    for p, txt in zip(loose, loose_txt):
        if not txt:
            continue
        try:
            arr = json.loads(txt)
        except Exception:
            continue
        for rec in arr:
            if rec.get("id") == vid:
                v = normalize_vendor(rec)
                v["_from"] = p
                return {"vendor": v}
    return {"error": f"找不到 id={vid} 对应的国标码，可能该记录尚未发布"}


_CAP_CARD_IDS: Optional[set] = None


def _card_count() -> int:
    return int(((load_manifest().get("metadata") or {})
                .get("cap_cards") or {}).get("count") or 0)


def _cap_card_ids() -> Optional[set]:
    """清单里登记的「已有能力卡的 id 集合」（None = 清单没这块，走原路径）。

    并非所有供应商都有能力卡；逐个去远端探测会非常慢。
    清单带上这张 id 表之后，没卡的可以直接短路，一次网络都不发。
    """
    global _CAP_CARD_IDS
    if _CAP_CARD_IDS is None:
        ids = ((load_manifest().get("metadata") or {}).get("cap_cards") or {}).get("ids")
        _CAP_CARD_IDS = set(ids.split(",")) if ids else None
    return _CAP_CARD_IDS


def get_capability_card(vid: str) -> Dict[str, Any]:
    vid = (vid or "").strip()
    if not vid:
        return {"error": "缺少必填参数 id"}
    # 短路：清单里查得到这张表时，没卡的 id 立刻回，别去撞三基址 404。
    want = _cap_card_ids()
    if want is not None:
        num = vid.split("-")[-1]
        if not num.isdigit() or num not in want:
            return {
                "id": vid, "has_card": False,
                "note": "该供应商暂无已发布能力卡（全库仅 %d 家有卡）。"
                        "需要档案请先调 get_vendor。" % _card_count(),
            }
    rel = f"skills/registry/capability/{vid}.json"
    txt = fetch_text(rel)
    if not txt:
        return {
            "id": vid,
            "has_card": False,
            "note": "无已发布能力卡：该供应商为 L0 未认证，或能力卡尚未生成/尚未发布到云端。"
                    "能力卡不进 git，仅经 Cloudflare R2 按需提供。",
        }
    try:
        card = json.loads(txt)
    except Exception as e:
        return {"error": f"能力卡解析失败: {e}"}
    return {"id": vid, "has_card": True, "card": card}


# --------------------------------------------------------------------------- #
# 寻源内核 桥接 tool：多轮对话式供应商匹配（G1/G2/G3）
# 入口只有一个：客户 agent 检测到采购/代工/找厂意图时自动调 start_sourcing。
# --------------------------------------------------------------------------- #
def start_sourcing(demand_text: str, audience_id: str = "domestic_downstream") -> Dict[str, Any]:
    """【客户 agent 自动触发】当对话中出现找厂/代工/采购意图时调用。

    内部：品类检测 → 摘要宽召回 → 解析需求 → 生成 1~2 轮澄清问题，把需求归一到
    beacon-mfg 供应商数据结构做初级筛选。返回 session_id 供后续轮次续接。
    """
    if _bridge is None:
        return {"error": "rfq-kernel 桥接未就绪（skills/rfq-kernel 缺失）"}
    demand_text = (demand_text or "").strip()
    if not demand_text:
        return {"error": "缺少必填参数 demand_text"}
    pack_id = _bridge.detect_industry(demand_text)
    # 召回 query 并入检测行业的「精准」召回扩词（recall_terms），使检索只拉相关行业供应商
    # （收敛跨行业噪声），同时避免宽泛 2 字词把 OR 召回池冲爆而挤出长尾真实企业。
    # 未声明 recall_terms 的 pack 回退到全量 vocab（旧行为）。
    recall_query = demand_text
    if pack_id:
        vocab = _bridge.pack_recall_terms(pack_id)
        if vocab:
            recall_query = demand_text + " " + " ".join(vocab)
    recs = _recall_for_sourcing(recall_query, top_k=200, pack_id=pack_id)
    if not recs:
        return {"stage": "clarifying", "session_id": None, "candidates_found": 0,
                "clarifying_questions": [],
                "note": "未从名录中召回相关工厂，请换更具体的产品/工艺描述。"}
    state, resp = _bridge.build_session(demand_text, recs, pack_id, audience_id)
    sid = _new_session_id()
    _SESSIONS[sid] = state
    resp["session_id"] = sid
    return resp


def answer_sourcing(session_id: str, answers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """续接澄清轮次：把客户回答写回，返回下一轮澄清问题或直接给出初选推荐。"""
    if _bridge is None:
        return {"error": "rfq-kernel 桥接未就绪"}
    state = _SESSIONS.get(session_id or "")
    if state is None:
        return {"error": f"session {session_id} 不存在或已过期（请重新 start_sourcing）"}
    state, resp = _bridge.answer_session(state, answers or {})
    _SESSIONS[session_id] = state
    resp["session_id"] = session_id
    return resp


def refine_sourcing(session_id: str, action: str = "", value: Optional[str] = None) -> Dict[str, Any]:
    """推荐轮次的交互：details(带 supplier_id 看详情/RFQ 入口) / more(带 N) / best。"""
    if _bridge is None:
        return {"error": "rfq-kernel 桥接未就绪"}
    state = _SESSIONS.get(session_id or "")
    if state is None:
        return {"error": f"session {session_id} 不存在或已过期（请重新 start_sourcing）"}
    resp = _bridge.refine_session(state, action or "", value)
    resp["session_id"] = session_id
    return resp


# --------------------------------------------------------------------------- #
# MCP 协议层（JSON-RPC 2.0 over stdio）
# --------------------------------------------------------------------------- #
TOOLS = [
    {
        "name": "search_vendors",
        "description": "检索灯塔工厂供应商名录。可按关键词(企业名/工艺/材料/认证)、城市、国标码(GB/T 4754)过滤。"
                       "返回精简档案(id/企业名/城市/国标码/认证等级/工艺/材料/认证/是否含电话)。"
                       "⚠ 关键词是**字面子串**匹配：用户的口语词/采购词若与名录写法不同（如「机加工」"
                       "vs 归档用的「机械零部件加工」），命中会极少。此时不要直接报「没有」——"
                       "看返回体里的 hint / suggested_gb，或先调 suggest_filters 拿到候选码，"
                       "再用 gb=<码> 重试（召回通常多几十倍，且只扫对应分片更快）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词：企业名/工艺/材料/认证子串"},
                "city": {"type": "string",
                         "description": "城市名精确匹配，如 深圳 / 东莞；"
                                        "也接受区县或县级市（如 昆山、海盐），"
                                        "这些地方在高德里归地级市名下，靠 district 字段命中"},
                "gb": {"type": "string", "description": "国标码，如 3484(机械零部件加工)。给了就只扫对应分片"},
                "limit": {"type": "integer", "default": 20, "description": "返回条数上限(1-200)"},
                "offset": {"type": "integer", "default": 0, "description": "分页偏移"},
            },
        },
    },
    {
        "name": "get_vendor",
        "description": "按供应商 id(+可选国标码)取完整中文档案：企业名/地址/经纬度/电话/行业路径/认证/关键词等。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "供应商 id，如 CN-MFG-0000163（必填）"},
                "gb": {"type": "string", "description": "国标码；不填会自动从指纹分片反查(稍慢)"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "get_capability_card",
        "description": "按供应商 id 取能力卡（工艺位/设备/产能/认证/起订量等）。"
                       "全库仅少数供应商有已发布能力卡，无卡的会秒回 has_card=false"
                       "（不必重试）。"
                       "无已发布卡片时返回 has_card=false 及原因说明。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "供应商 id（必填）"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "start_sourcing",
        "description": "【客户 agent 自动触发】当对话中出现找厂/代工/采购/询价意图时调用，"
                       "例如『帮我找个能做不锈钢保温杯的厂』『哪家能做铝合金压铸』。"
                       "内部做品类识别→指纹宽召回→解析需求→生成 1~2 轮澄清问题，"
                       "把需求归一到 beacon-mfg 供应商数据结构做初级筛选，返回 session_id。"
                       "后续用 answer_sourcing 续接澄清、refine_sourcing 看推荐详情。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "demand_text": {"type": "string",
                                "description": "客户的原始需求描述（必填），如『想找东莞做ISO9001的钣金厂』"},
                "audience_id": {"type": "string", "description": "客户视图：domestic_downstream(国内下游)/intl_buyer(国际采购商)，默认国内下游"},
            },
            "required": ["demand_text"],
        },
    },
    {
        "name": "answer_sourcing",
        "description": "续接 start_sourcing 的澄清轮次：把客户对澄清问题的回答写回，"
                       "返回下一轮澄清问题，或（1~2 轮后）直接给出按需求匹配度初选的供应商列表。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "start_sourcing 返回的会话 id（必填）"},
                "answers": {"type": "object",
                            "description": "澄清答案，键为问题里的 field（如 certifications_required/material/process/region），值为选项"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "refine_sourcing",
        "description": "推荐轮次的交互：details(带 supplier_id 看详情与 RFQ 在线入口) / more(带数字 N 看更多) / best(看最匹配一家)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话 id（必填）"},
                "action": {"type": "string", "description": "details / more / best"},
                "value": {"type": "string", "description": "details 时为 supplier_id；more 时为数量 N"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "suggest_filters",
        "description": "把用户原话（口语/采购词）映射成候选国标小类码，供你改用 search_vendors(gb=…)。"
                       "用户说的是「机加工」，名录里按「机械零部件加工」归档——别名表覆盖不了所有说法，"
                       "所以本工具把「拆词 + 别名 + 类名」的候选和每类条数给你，由你做最终语义选择。"
                       "search_vendors 命中偏少时也会内联 suggested_gb。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户原话，如 机加工 / 不锈钢板激光切割折弯（必填）"},
                "city": {"type": "string", "description": "可选，用于拼出下一步调用参数"},
                "limit": {"type": "integer", "default": 8, "description": "候选码条数上限(1-30)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_industries",
        "description": "列出国标小类货架（只列已有企业的类，附企业条数），供你在没有字面候选时"
                       "按语义自己挑码。可传 keyword 模糊筛、parent 按门类字母/大类码缩小范围。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "可选，类名/码的子串"},
                "parent": {"type": "string", "description": "可选，门类字母(C)或大类两位码(35)"},
                "limit": {"type": "integer", "default": 30, "description": "返回条数(1-200)"},
                "offset": {"type": "integer", "default": 0},
            },
        },
    },
]


def _chain_grams(grams: List[str], max_len: int = 8) -> List[str]:
    """把重叠的 2-gram 链回原词：`流水` + `水线` → `流水线`。

    为什么需要这一步：`_tokenize` 出于召回考虑会同时产出 1-gram 和 2-gram，
    直接取前 N 个得到的是「厂 / 家 / 水 / 流」这种碎片 —— 拿来当需求信号毫无意义。
    链回原词之后才是「流水线」这种能直接翻译成抓取矩阵的词。
    """
    gs = sorted(set(g for g in grams if len(g) == 2))
    if not gs:
        return []
    succ: Dict[str, List[str]] = {}
    for g in gs:
        succ.setdefault(g[0], []).append(g)
    has_pred = {g[1] for g in gs}
    out: List[str] = []
    for h in [g for g in gs if g[0] not in has_pred]:
        best = h
        stack = [(h, h)]
        while stack:
            cur, acc = stack.pop()
            if len(acc) > len(best):
                best = acc
            if len(acc) >= max_len:
                continue
            for nxt in succ.get(cur[-1], []):
                stack.append((nxt, acc + nxt[1:]))
        out.append(best)
    return out


def _product_tokens(name: str, args: Dict[str, Any]) -> List[str]:
    """从入参里抽出**产品词**（供 X-Beacon-Tokens）。

    刻意不记录整句 query（§14.2）：用户什么都可能输入，原样记等于落成明文台账。
    流程：分词 → 只留 `_usable_gram` 认可的（滤掉城市、企业通名、单字功能词）
    → **只取长度 ≥ 2**（这一条同时干掉了单字碎片）→ 2-gram 链回原词。
    """
    text = ""
    if name == "search_vendors":
        text = args.get("query") or ""
    elif name == "start_sourcing":
        text = args.get("demand_text") or ""
    if not text:
        return []
    try:
        toks = [t for t in _tokenize(str(text)) if _usable_gram(t)]
    except Exception:
        toks = [t for t in str(text).split() if t]

    city = (args.get("city") or "").strip()
    cjk: List[str] = []
    latin: List[str] = []
    for t in toks:
        if not t or len(t) < 2 or t == city:
            continue
        (cjk if _is_cjk(t) else latin).append(t)

    # 英文/数字取整词（不链）；中文走 2-gram 链还原
    out: List[str] = []
    for t in sorted(set(latin), key=len, reverse=True):
        if t not in out:
            out.append(t)
    for t in _chain_grams(cjk):
        if t not in out:
            out.append(t)
    return out[:6]


def _dispatch(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """tool 调用的唯一入口 —— 埋点就挂在这里（文档 §4）。

    顺序有讲究：**先**把 tokens 放进上下文，再调用。因为 HTTP 请求发生在
    被调用函数内部，tokens 必须在请求发出前就位；而 hits 只能等结果出来后
    回填，供**后续**请求带上（一次检索的头几个请求因此没有 hits 列，属正常）。
    """
    args = args or {}
    tok = _cur_set({"tool": name, "tokens": _product_tokens(name, args)})
    t0 = time.time()
    err: Optional[BaseException] = None
    r: Any = None
    try:
        r = _dispatch_inner(name, args)
        if isinstance(r, dict) and "total_matched" in r:
            cur = _cur_get() or {}
            cur["hits"] = r.get("total_matched")
            _cur_set(cur)
        return r
    except BaseException as e:      # noqa: BLE001 —— 埋点不能吞掉协议层行为
        err = e
        raise
    finally:
        cur = _cur_get() or {}
        # _dispatch_inner 把业务异常包成了 {"error": ...}，那也算失败
        ok = err is None and not (isinstance(r, dict) and "error" in r)
        _emit_usage(name, int((time.time() - t0) * 1000), ok,
                    {"tokens": cur.get("tokens"), "hits": cur.get("hits")})
        try:
            if tok is not None and _CUR is not None:
                _CUR.reset(tok)
        except Exception:
            pass


def _dispatch_inner(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if name == "search_vendors":
            return search_vendors(
                query=args.get("query", ""),
                city=args.get("city", ""),
                gb=args.get("gb", ""),
                limit=args.get("limit", 20),
                offset=args.get("offset", 0),
            )
        if name == "get_vendor":
            return get_vendor(args.get("id", ""), args.get("gb", ""))
        if name == "get_capability_card":
            return get_capability_card(args.get("id", ""))
        if name == "suggest_filters":
            return suggest_filters(args.get("query", ""), args.get("city", ""),
                                   int(args.get("limit") or 8))
        if name == "list_industries":
            return list_industries(args.get("keyword", ""), args.get("parent", ""),
                                   int(args.get("limit") or 30),
                                   int(args.get("offset") or 0))
        if name == "start_sourcing":
            return start_sourcing(args.get("demand_text", ""), args.get("audience_id", "domestic_downstream"))
        if name == "answer_sourcing":
            return answer_sourcing(args.get("session_id", ""), args.get("answers"))
        if name == "refine_sourcing":
            return refine_sourcing(args.get("session_id", ""), args.get("action", ""), args.get("value"))
    except Exception as e:  # 任何异常都包成文本，避免协议崩
        return {"error": f"{name} 执行异常: {e}"}
    return {"error": f"未知 tool: {name}"}


def _send(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(msg: str) -> None:
    sys.stderr.write("[beacon-mcp] " + msg + "\n")
    sys.stderr.flush()


def _maybe_spawn_selfupdate() -> None:
    """启动时踢一次自更新检查（detached、不阻塞、失败一律静默）。

    修掉的盲区：自更新原先只挂在 npm 启动器（bin/beacon-mfg-mcp.js）上，
    凡是以 `python server.py` 直接接入的用户（包括本地仓库用户）永远不会
    收到“有新版 / 仓库落后”的提示。这里让**任何接入方式**都会检查一次，
    并把结果写进 ~/.beacon-mfg/update-check.json、把提示写到 stderr
    （MCP 客户端会把它记进日志，因此用户可见）。
    """
    try:
        if os.environ.get("BEACON_MCP_NO_UPDATE") == "1":
            return
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selfupdate.py")
        if not os.path.isfile(script):
            return
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：不随本进程退出而结束
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=None,          # 继承：提示进入客户端日志
            **kwargs,
        )
    except Exception:
        pass


def main() -> None:
    _log(f"start; source={BEACON_SOURCE} repo={REPO or '(http)'}")
    _maybe_spawn_selfupdate()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            _send({
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "beacon-mfg-readonly", "version": _pkg_version()},
                },
            })
        elif method == "notifications/initialized":
            continue  # 通知无需回复
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            res = _dispatch(name, params.get("arguments", {}))
            _send({
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
                    "isError": "error" in res,
                },
            })
        else:
            # 未知方法：若有 id 则回空结果，通知则忽略
            if mid is not None:
                _send({"jsonrpc": "2.0", "id": mid, "result": {}})


if __name__ == "__main__":
    main()
