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

暴露的 tool（8 个，只读）：
  search_vendors       按 关键词 / 城市 / 国标码 检索
  get_vendor           按 id（+ 国标码）取完整中文档案
  get_capability_card  按 id 取能力卡
  suggest_filters      用户原话 → 候选国标小类码
  list_industries      国标小类货架（只列有数据的类）
  start_sourcing       多轮寻源起手：品类识别 → 宽召回 → 澄清问题
  answer_sourcing      续接澄清轮次 / 返回初选供应商
  refine_sourcing      推荐轮交互：details / more / best
"""

import os
import re
import sys
import math
import copy
import json
import hashlib
import concurrent.futures
import subprocess
import tarfile
import tempfile
import io
import gzip
import time
import hmac
import urllib.request
import urllib.error
import urllib.parse
import threading

# [utf8-guard] 中文 Windows 上，宿主若没注入 PYTHONUTF8/PYTHONIOENCODING，stdout 会落回
# cp936；而 TOOLS 的 tool 描述里含 `⚠`(U+26A0) 这类 GBK 根本编不出的字符 ——
# initialize 全是 ASCII 能过，一到 `tools/list` 就 UnicodeEncodeError **当场崩进程**。
# 强制 UTF-8 + 出错降级替换：宁可显示成 '?'，也绝不因为一次打印而断开协议。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# [no-bytecode-guard] 本文件随 npm 包分发（package.json 的 files 含 server.py），
# 可能被 `python server.py` 直接拉起 —— 那条路径**不经** bin/ 启动器，
# 启动器注入的 PYTHONDONTWRITEBYTECODE 不生效。这里显式禁写 __pycache__，
# 避免在全局 npm 包目录 / 只读盘上留字节码（或 EPERM 噪音）。本服务短命，
# 无字节码缓存收益。
sys.dont_write_bytecode = True
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

# --------------------------------------------------------------------------- #
# id token（D，2026-10-28）
#
# 对外 id 是 HMAC 置换后的 8 字符 token，不是 `CN-MFG-0000163` 这种顺序编号 ——
# 顺序编号等于「从 1 遍历到 222875 就能把全库重建一遍」。
#
# 为什么客户端也要有这份算法：MCP 的检索是**本地**跑的（拉分片、本地打分），
# 它得把结果里的内部 id 换成 token 才能对外给。算法文件与
# scripts/vid_token.py **逐字相同**（sync_vid_block.py 维护），密钥来自
# BEACON_VID_KEY。没有密钥时（npm 客户端默认情况）**不改写** —— 见 has_key()。
# --------------------------------------------------------------------------- #
try:
    from vid_token import (          # 同目录副本（npm 只分发 mcp/）
        has_key as _vid_has_key,
        encode_deep as _vid_encode_deep,
        decode_arg as _vid_decode_arg,
        vid_secret as _vid_secret,
    )
except Exception:                    # 副本缺失 → 退化成「不改写 id」，绝不影响其余功能
    _vid_has_key = None

# 哪些入参是「供应商 id」。只认这几个键，**不要**把 query/kw 也塞进来：
# query 是自然语言，里面出现 8 位大写串的概率虽低，但一旦误判就会把
# 正常检索词当 token 解，解出个不存在的编号去查。
# ⚠ schema 里实际叫 `id`（get_vendor / get_capability_card 都是），不是 vid ——
#   漏了 "id" 的表现是「检索正常，点开某家报 找不到 id=<token> 对应的国标码」。
_VID_IN_ARGS = ("id", "vid", "vendor_id", "supplier_id", "company_id", "card_id")


def _vid_ready() -> bool:
    return bool(_vid_has_key and _vid_has_key())


def _vid_remote_data() -> bool:
    """数据源是远端（HTTP）吗。

    这决定了入参里的 id 要不要解码：
      · **远端模式**（BEACON_REPO 未设）：拉下来的分片已经 token 化过 ——
        分片的 `id`、name-index、idmap 的键全都是 token。此时若再解码成
        内部 id 去查，等于拿内部 id 去查一张 token 键的索引，**必然查不到**
        （症状：检索正常，点开某家报「找不到 id 对应的国标码」）。
      · **本地模式**（BEACON_REPO 指向仓库）：读的是仓库里的内部 id，
        入参的 token 必须解码后才能命中。

    所以：远端模式下 id 一路原样透传，解码只发生在本地模式。
    """
    return not REPO


def _vid_in(args: Dict[str, Any]) -> Dict[str, Any]:
    """入参里的 token → 内部 id（仅本地模式需要；远端模式的数据已是 token）。"""
    if not _vid_ready() or not isinstance(args, dict):
        return args
    if _vid_remote_data():
        return args
    sec = _vid_secret()
    out = dict(args)
    for k in _VID_IN_ARGS:
        v = out.get(k)
        if isinstance(v, str) and v:
            out[k] = _vid_decode_arg(v, sec)
    return out


def _vid_out(res: Any) -> Any:
    """结果树里的内部 id → token（含路径、正文、嵌套列表）。"""
    if not _vid_ready() or res is None:
        return res
    return _vid_encode_deep(res, _vid_secret())


def _vid_url(vid: str) -> str:
    """拼对外 URL 时用 token，别用内部 id。

    两条路径必须分开看：
      · **本地逻辑**（分片定址、idmap 查 gb、清单短路）要内部 id —— 分片是按
        内部 id 命名/分桶的；
      · **对服务端的 HTTP 请求**要 token —— worker 那边是硬切，裸 id 在
        BEACON_ID_LEGACY=0 之后一律 404。
    混用一次的后果是「本机检索正常、点开卡片 404」，而且只在翻旗之后才炸。
    """
    if not _vid_ready():
        return vid
    from vid_token import encode as _enc
    return _enc(vid, _vid_secret())


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
        # 先挑出候选片，**并发预取**，再逐片解析（2026-10-30）。
        # 原来是 `for s in _shards_of_type("fp")` 里逐片 `_read_fp_shard`：
        # 一片 1.5~2.5s，start_sourcing 首查实测 179 个请求 / 91s，
        # 其中绝大部分就耗在这条串行链上（并发度实测只有 1.69）。
        # 预取只负责把内容放进缓存层，逐片判定的语义与顺序完全不变。
        cand = []
        for s in _shards_of_type("fp"):
            c = str(s.get("c") or "")
            if c in gbs or c[:2] in gbs:
                cand.append(s)
        if len(cand) > 1:
            try:
                _fetch_many([s.get("p") for s in cand])
            except Exception:
                pass
        for s in cand:
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
# 版本号单一来源：同目录 package.json（npm 发布包的 version）。
# 全项目所有版本号（MCP / HTTP 服务 / Python API / Android APK）
# 都对齐到这个值，`scripts/check_version.py` 在发布前校验它们没有漂移。
# 读不到 package.json 时的兜底值 —— 改版本时**必须**和 package.json 一起改，
# 否则 check_version.py 会报错（这就是它的用途）。
VERSION_FALLBACK = "1.11.7"


def _pkg_version() -> str:
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "package.json")
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f).get("version", VERSION_FALLBACK)
    except Exception:
        return VERSION_FALLBACK


VERSION = _pkg_version()

DEFAULT_SOURCE = "https://beacon-mfg.pages.dev"
BEACON_SOURCE = os.environ.get("BEACON_SOURCE", DEFAULT_SOURCE).rstrip("/")
BEACON_REPO = os.environ.get("BEACON_REPO")  # None -> 自动探测 / 退回 HTTP

CACHE_DIR = os.path.join(tempfile.gettempdir(), "beacon-mcp-cache")
UA = "BeaconMFG-MCP/%s (+https://beacon-mfg.pages.dev/)" % VERSION

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

# 陈旧判定的节流门。relpath -> (slot_sig, checked_at)，`BEACON_STALE_GATE` 秒内不重判。
#
# 为什么必须有：`_cap_term_of()` 在 `_hay_parts()` 里对**每条记录的每个能力键**调用一次，
# 一次「加工」查询就是 47 万次，每次都进 `_cache_stale()` 去问「cap.json 变了没」。
# 而一次查询内这些派生文件不可能变 —— 实测这 47 万次 time.time() + dict 查找 + 函数栈
# 占单次查询耗时的 45%（5 条查询 18.3s → 10.1s）。
#
# 为什么不按缓存槽判：`_cap_index_cache` 与 `_CAP_TERMS_SLOT` 共用 cap.json，按槽判会
# 互相失效。按「文件 + slot 当前签名」判才是正确粒度：签名没变 = 这个槽没被重建过 =
# 文件在这个签名下已经确认过不陈旧。
#
# 语义代价：文件变更最长延后 `BEACON_STALE_GATE` 秒才被看见（默认 1.0s，与 `_mtime_cache`
# 的 2s 同级）。设为 0 可完全关闭，恢复逐次判定。
BEACON_STALE_GATE = float(os.environ.get("BEACON_STALE_GATE", "1.0") or "0")
_STALE_GATE: Dict[str, tuple] = {}


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
    if relpath is None:
        return False
    key = relpath if isinstance(relpath, str) else "|".join(relpath)
    now = time.time()
    if BEACON_STALE_GATE > 0:
        g = _STALE_GATE.get(key)
        # 签名一致 = 这个槽没有重建过，且刚才已确认文件在该签名下无变化 → 仍不陈旧。
        # 签名不一致（槽被重建）或超过节流窗口 → 落下去真判一次。
        if g is not None and g[0] == slot[2] and (now - g[1]) < BEACON_STALE_GATE:
            return False
    m = _wt_mtime_any(relpath) if isinstance(relpath, (list, tuple, set)) else _wt_mtime(relpath)
    if m is not None and slot[2] != m:
        # 陈旧：不记门。否则「文件变了但 loader 拒绝更新缓存」（取数失败时不写缓存）会让
        # 门在窗口内一直回「不陈旧」，变成持续漏检。
        return True
    if BEACON_STALE_GATE > 0:
        _STALE_GATE[key] = (slot[2], now)
    return False


# HTTP 基址列表。默认数据源为 Cloudflare Pages；可用 BEACON_MIRRORS 追加自定义镜像，
# 主源不可达时按顺序回退。
def _http_bases() -> List[str]:
    bases = [BEACON_SOURCE]
    m = os.environ.get("BEACON_MIRRORS")
    if m:
        for x in m.split(","):
            x = x.strip().rstrip("/")
            if x:
                bases.append(x)
    return bases


# --------------------------------------------------------------------------- #
# 使用量埋点
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

# 兜底 op：不属于任何 tool 调用的取数（启动预热、跨线程池丢失上下文、
# 后台补数）用它，避免被 worker 当成旧版客户端 401。
#
# 为什么带 pid：worker 按 op 去重计量，同一进程共用一个是**想要**的效果
# （一次启动算一次操作）；而带上 pid 能让服务端台账里区分不同进程，
# 排查「谁在拉」时不会所有客户端糊成一条。时间戳只取进程启动那一刻，
# 不是每条请求一个新值 —— 那等于把去重完全废掉。
_BOOT_OP = "boot-%d-%d" % (os.getpid(), int(time.time()))


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


def _header_value(v: Any) -> str:
    """把任意值收敛成**可安全放进 HTTP 头**的 ASCII 串。

    ⚠ HTTP 头只能是 latin-1（`http.client.putheader` 会做 `.encode("latin-1")`），
    而 `X-Beacon-Tokens` 装的是**产品词**——中文用户查「海伦钢琴」，token 就是
    `海伦`，一进头就在**建连之前**抛 `UnicodeEncodeError`。

    后果远比「少记一条埋点」严重：`_beacon_headers()` 是**所有** HTTP 读共用
    （manifest、索引桶、指纹分片全走它），于是任何含中文关键词的检索在冷缓存下
    **整体不可用**，而报错文案指向「网络/代理不可达」。2026-10-24 由用户在另一台
    机器上实测定位（1.4.2 → 1.11.0 均存在；本机因 manifest 已进缓存而未复现）。

    处理方式取 **percent-encode** 而不是丢弃：中文产品词正是要观测的主体，
    过滤掉等于把中文场景的遥测清零。worker 侧用 `decodeURIComponent` 还原。
    """
    s = "" if v is None else str(v)
    if not s:
        return ""
    try:
        s.encode("latin-1")
        return s                      # 全 ASCII（含 latin-1 可编的西欧字符）
    except UnicodeEncodeError:
        return urllib.parse.quote(s, safe=",._-")


def _quote_path(relpath: str) -> str:
    """把可能含非 ASCII 的相对路径转成**可放进 HTTP 请求行**的 ASCII 串。

    ⚠ 这是与 `_header_value()` 并列的**第二处编码边界**，漏了同样致命：
    `http.client` 组装请求行时按 `'ascii'` 编码，路径里出现原始 UTF-8 字节
    就在**建连之前**抛 `UnicodeEncodeError: 'ascii' codec can't encode ...`。

    2026-10-24 实测路径：`_city_postings()` 拼 `skills/registry/index/city/贵阳.json`，
    城市名是发布侧的**原始中文文件名**（391 个城市全是中文名，没有哈希化）——
    中文城市 + 冷缓存首个查询必定在这一跳失败。

    为什么不在 `_city_postings()` 里就地 quote：`fetch_text()` 会**先用 relpath
    读本地工作树/dirgit HEAD**（本地仓库模式），把 relpath 提前编码会让本地读
    全部落空。所以编码只发生在「真正要发 HTTP」这一层，relpath 本身保持原样。

    只对含非 ASCII 的路径处理：纯 ASCII 路径（绝大多数分片、索引桶）走原样，
    零行为变化，也不会把已带 `%` 的路径二次编码。
    """
    if relpath.isascii():
        return relpath
    return urllib.parse.quote(relpath, safe="/")



def _beacon_headers() -> Dict[str, str]:
    """随请求带出的头。

    **身份头与遥测头必须分开**（2026-10-22）：
      - 身份（cid / op / Authorization）→ 不可关。关掉遥测只应少报「你查了
        什么」，不能连身份一起不发 —— 那样服务端会把用户当匿名客户端拒绝服务，
        而他以为只是关了个统计开关。
      - 遥测（tool / tokens / hits）→ 可被 BEACON_TELEMETRY=0 关闭。

    **所有值必须过 `_header_value()`**（2026-10-24）：中文 token 直接在
    `putheader` 抛 UnicodeEncodeError，等于让一条埋点把整次检索打挂。
    """
    h: Dict[str, str] = {}
    try:
        import identity  # 同目录模块；不可用时退回旧路径，不能因此丢身份
        h.update(identity.headers())
    except Exception:
        h["X-Beacon-Cid"] = _cid()

    cur = _cur_get() or {}
    # op = 一次 tool 调用。服务端按 op 去重计量配额：一次「上海」检索要拉
    # 178 个分片，按请求计费会把正常用户直接打死（见 pages/_worker.js 注释）。
    #
    # ⚠ **没有 op 就一定会 401**（2026-10-30 线上实测）。worker 门禁第 4 条是
    #   `enforce && !op → 401 client_outdated`，它把「缺 op」当成旧版客户端。
    #   而 op 只存在于 tool 调用期间（`_cur_set`），**启动预热、跨线程池、
    #   以及任何非 tool 触发的取数都没有它**。缺了就整片 401：
    #     · /data/manifest.json → 冷启动 8 次 401 重试（实测 67.7s）
    #     · /data/gb/**         → get_vendor 全部 fetch_failed
    #   所以这里必须有兜底 op。用 `boot-<pid>` 而不是给所有请求塞同一个常量：
    #   服务端按 op 去重，同一进程的多条预热/兜底请求计作一次操作，
    #   既不会把配额打爆，也不会让「一个进程」在台账里看起来像几百次调用。
    h["X-Beacon-Op"] = cur.get("op") or _BOOT_OP

    if not BEACON_TELEMETRY:
        return {k: _header_value(v) for k, v in h.items() if v not in ("", None)}

    h["X-Beacon-Tool"] = cur.get("tool") or ""
    # tokens = 分词后的**产品词**，不是整句（§14.2：记整句等于把用户输入落成明文台账）
    if cur.get("tokens"):
        h["X-Beacon-Tokens"] = ",".join(cur["tokens"])[:120]
    if cur.get("hits") is not None:
        h["X-Beacon-Hits"] = str(cur["hits"])
    return {k: _header_value(v) for k, v in h.items() if v not in ("", None)}


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
            # 2026-10-25 fix：**绝不继承父进程的 stdio**。stdio MCP 的 stdin 是
            # JSON-RPC 管道，git（及其 sh.exe 孙进程）继承该句柄后 communicate()
            # 会永远等不到 EOF → tool 调用整体挂死（Windows 上实测必现）。
            stdin=subprocess.DEVNULL, timeout=10,
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


def _cache_write(cap: str, etag: Optional[str], body: Optional[str]) -> None:
    """写内容缓存。**gzip + 不转义中文**。

    为什么（2026-10-30 实测）：原实现是
        json.dump({"etag":…, "body": body, "at": …}, f)
    两个开销叠在一起 ——
      ① 默认 ensure_ascii=True：中文每字变成 `\\u8d35` 六个字节，而 UTF-8 只要三字节；
      ② body 已经是 **gzip 解压后**的正文，缓存里存的是解压态而非线上那份压缩字节。
    结果是落盘量比实际下载量大 20 倍以上：一次冷启动下载 3.19 MB，
    缓存目录涨到 116.8 MB / 463 个文件 —— 用户只查一次就占了 117 MB 磁盘，
    与「尽可能少的落盘」正相反。gzip 后同一批内容约 10 MB。

    写临时文件再 replace：并发进程同时写同一 key 时不能出现半截文件
    （读到半截 JSON 会被当成"没有缓存"，下次又整片重下）。
    """
    if body is None:
        return
    try:
        raw = json.dumps({"etag": etag, "body": body, "at": time.time()},
                         ensure_ascii=False).encode("utf-8")
        tmp = cap + ".tmp"
        with open(tmp, "wb") as f:
            f.write(gzip.compress(raw, compresslevel=6, mtime=0))
        os.replace(tmp, cap)
    except Exception:
        pass


def _cache_read(cap: str) -> Optional[dict]:
    """读内容缓存；兼容旧格式（未压缩的 JSON）。"""
    try:
        with open(cap, "rb") as f:
            raw = f.read()
    except Exception:
        return None
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _git_show(relpath: str) -> Optional[str]:
    """只读已提交内容。绝不 checkout —— 不触发 maskphone smudge、不碰 index 锁。"""
    if not REPO:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", REPO, "show", "HEAD:" + relpath],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=20,   # 同上：不继承 stdio，且必须限时
        )
        if r.returncode == 0 and r.stdout:
            return r.stdout
    except Exception:
        pass
    return None


# 单基址 HTTP 超时（秒）。多镜像回退时每个基址各自计时，避免单一 CDN 卡死拖垮整体。
_HTTP_TIMEOUT = 30

# URL 级正文字缓存的**免网络**窗口（秒，2026-10-29）。
#
# 背景：`_http_get_once` 原本只做「带 If-None-Match 的条件请求」，命中也要付一个
# RTT。本机实测这条链路单次 RTT 就有 0.6~2.9 秒，于是**几十 KB** 的索引文件
# （gb-alias.json 24 KB / gb-alias-curated.json 40 KB / gb-class-abbr.json 33 KB）
# 每次进程各取一遍就要 3~9 秒 —— 与文件大小完全不成比例，纯是往返次数。
#
# 而这类索引随发布版本变化，不是每秒都在变：TTL 内直接用磁盘正文，一个请求都不发。
# 置 0 可关掉（恢复「每次条件请求」的旧行为）。
BEACON_HTTP_TTL = float(os.environ.get("BEACON_HTTP_TTL", "300") or "0")


# 记录最近一次全失败的诊断（供上层报错时引用，不进返回值以免破坏既有签名）
_HTTP_LAST_ERROR: List[str] = [""]
# 「取数被中途放弃」的独立槽（2026-10-24）：`_HTTP_LAST_ERROR` 会被后续每一次
# 失败请求覆盖，中止原因一写进去就被冲掉，于是返回体里只剩一句「HTTP 401」，
# 看不出「结果其实残缺」。分开存，两者都报。
_FETCH_ABORTED: List[str] = [""]


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
    """全部基址+全部连接方式都失败时，给出**指向真因**的诊断串。

    ⚠ 只有在**确实是网络层**失败时才谈代理。历史上这里无条件先说「可能是本机
    网络不可达」，而 1.4.x～1.11.0 的真实故障是请求头里的中文 token 触发
    `UnicodeEncodeError` —— 让人白去查代理/VPN（2026-10-24 用户实测反馈）。
    """
    # 编码问题：与网络、代理、CDN 全都无关，必须单独说清楚。
    # 且必须区分错在**请求行(URL)还是请求头**——两者排查方向完全不同：
    #   'ascii' codec  → 路径里混进了非 ASCII（如 city/贵阳.json）
    #   'latin-1' codec → 请求头里混进了非 ASCII（如 X-Beacon-Tokens: 海伦）
    # 早期把两者都写成「请求头」，用户按那个方向去查会一无所获（2026-10-24 反馈）。
    if "UnicodeEncodeError" in last_err or "latin-1" in last_err:
        if "'ascii'" in last_err:
            return ("请求路径 `%s` 含非 ASCII 字符，无法编码成 HTTP 请求行"
                    "（**本地编码问题，不是网络问题**）；最后错误: %s"
                    % (relpath, last_err))
        return ("请求头包含非 ASCII 字符，无法编码成 HTTP 头（**本地编码问题，"
                "不是网络问题**）；最后错误: " + last_err)
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
    # 兜底：Content-Encoding 只解一层，但中间层（代理 / 边缘）可能对**已压缩**
    # 的字节再压一次，此时解一层仍是 gzip 魔数。历史上这会让 cap-map /
    # region-index 这类文件直接 UnicodeDecodeError → 被当"取数失败"静默返回空，
    # 排查成本极高。按魔数循环解到不是 gzip 为止（上限 3 层，防病态输入）。
    import gzip as _gz2
    for _ in range(3):
        if not raw[:2] == b"\x1f\x8b":
            break
        try:
            raw = _gz2.decompress(raw)
        except Exception:
            break
    return raw.decode("utf-8")


def _http_get(relpath: str) -> Tuple[Optional[str], Optional[str]]:
    """统一的 HTTP 读取：依次尝试 _http_bases() 中的基址（主源 + 自定义镜像），
    任一成功即返回；全部失败则用本地缓存兜底（保证离线可用）。

    **401 自愈一次**（2026-10-28，与服务端 C 配套）：服务端把「只有
    `X-Beacon-Cid`、没有令牌」从「放行」改成 `401 identity_unverified`。
    客户端的 `identity.headers()` 只在本地令牌 fresh 时带 Authorization，
    所以只要本地令牌被服务端判废（改密钥 / 身份被 suspend / 时钟偏快），
    就会陷入「本地认为身份正常、服务端一直 401」的死循环。
    这里在拿到 401 时强制续签并重试**一次**：能自愈的当场自愈，不能的
    照旧走原有的报错路径（不放大成 N 次重试）。

    为什么只在 `body is None` 时重试：如果本地有缓存分片可用，优先保证
    可用性，不为了一次身份刷新把已有缓存结果丢掉。
    """
    st = [0]
    body, etag = _http_get_once(relpath, st)
    if body is None and st[0] == 401:
        try:
            import identity as _idn
            _idn.ensure_token(force=True)
        except Exception:
            return body, etag
        st[0] = 0
        body, etag = _http_get_once(relpath, st)
    return body, etag


def _http_get_once(relpath: str, st: list) -> Tuple[Optional[str], Optional[str]]:
    """单次尝试（不含 401 自愈）。`st[0]` 回填最后一次 HTTP 状态码。"""
    cached_body = None
    cached_etag = None
    last_err = ""
    _fatal = False
    for base in _http_bases():
        url = base + "/" + _quote_path(relpath)
        cap = _cache_path(url)
        etag = None
        if os.path.exists(cap):
            try:
                blob = _cache_read(cap) or {}
                etag = blob.get("etag")
                cached_body = blob.get("body")
                cached_etag = etag
                # 免网络窗口（见 BEACON_HTTP_TTL）：正文还在 TTL 内就直接用，
                # 连条件请求都不发 —— 本机单次 RTT 实测 0.6~2.9 秒，省掉它是
                # 小索引文件唯一有意义的速度来源。
                _at = blob.get("at")
                if (BEACON_HTTP_TTL > 0 and cached_body is not None and _at
                        and (time.time() - float(_at)) < BEACON_HTTP_TTL):
                    return cached_body, etag
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
                _cache_write(cap, new_etag, body)
                return body, new_etag
            except urllib.error.HTTPError as e:
                if e.code == 304 and cached_body is not None:
                    # 304 也要回写时间戳：否则旧缓存文件永远没有 "at"，
                    # TTL 免网络窗口对它**永远不生效**（每次都还得付一个 RTT）。
                    _cache_write(cap, etag, cached_body)
                    return cached_body, etag
                st[0] = e.code
                last_err = "HTTP %s" % e.code
                break   # HTTP 层错误，换基址比重试连接方式有意义
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, e)
                if isinstance(e, UnicodeEncodeError):
                    # 确定性错误：头编不出来，换连接方式或换基址结果完全一样，
                    # 继续循环只会把一次失败放大成 N 次（并拖长客户端等待）。
                    _fatal = True
                    break
                continue   # 连接层失败（典型：代理不可用）→ 换连接方式
        if _fatal:
            break
    # 全部基址失败：若有任何本地缓存也返回，保证离线可用
    if cached_body is not None:
        return cached_body, cached_etag
    _HTTP_LAST_ERROR[0] = _http_diagnose(relpath, last_err)
    return None, None


def _fetch_warn(total: int) -> Dict[str, Any]:
    """取数故障对本次结果的影响，供返回体如实告知调用方。

    - `total == 0` 且有过取数错误 → `fetch_error`：**0 条不等于没有这家企业**，
      原本的返回体没有任何字段能区分「库里真没有」和「取数挂了」。
    - 中途放弃（"已中止"）→ 额外 `partial: true`：结果是残缺的，别当全量。
    正常查询返回 {}，对存量输出零影响。
    """
    err = _HTTP_LAST_ERROR[0]
    aborted = _FETCH_ABORTED[0]
    if not err and not aborted:
        return {}
    detail = "；".join(x for x in (aborted, err) if x)
    if aborted:
        # 中途放弃过 → 结果是残缺的，无论是否碰巧有命中都要标出来
        return {"partial": True, "fetch_error": detail}
    if total == 0:
        return {"fetch_error": detail}
    return {}


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


# 分片并发上限（2026-10-29 由硬编码 6 改为可调，默认 12）。
#
# 原来固定 6，理由是「配合 CDN 限速」。但线上实测一次检索要拉 4~7 个 200~900 KB
# 的分片，低并发把延迟**叠加**成 7~11 秒；而总耗时本应接近「最慢的那一片」。
# 提高到 12 后，冷检索里分片取数这一段基本收敛到单片耗时。
# 若某台客户端/CDN 出现限速（429/超时），用 BEACON_FETCH_WORKERS 调回低值即可。
_FETCH_WORKERS = max(1, int(os.environ.get("BEACON_FETCH_WORKERS", "12") or "12"))


def _fetch_many(paths: List[str], workers: int = 0) -> List[Optional[str]]:
    """并发取多份**分片**文本，返回与入参同序的结果（取不到为 None）。

    走 `_shard_text`（按清单 sha1 缓存）而不是 `fetch_text`：命中缓存时连条件请求
    都省掉，重复查询同一国标码/邻近记录近乎零成本。

    为什么值得并发：一个国标码常有多个 zh 续片，串行取等于把延迟**相加**；
    并发取的总耗时接近最慢那一个。

    `workers` 传 0 表示用默认值 `_FETCH_WORKERS`（环境变量可调）。
    异常一律吞成 None —— 与 fetch_text 的语义一致（取不到就是取不到，
    由调用方决定是"换下一片"还是"报错"），绝不把线程异常泄漏成 tool 崩溃。
    """
    if len(paths) <= 1:
        return [_shard_text(p) for p in paths]
    n = max(1, int(workers or _FETCH_WORKERS))
    out: List[Optional[str]] = [None] * len(paths)
    # ⚠ ContextVar 不会自动继承到线程池的工作线程（2026-10-30 线上复现）。
    #   本次 tool 调用的 op 在 `_cur_get()` 里，`X-Beacon-Op` 头靠它发出；
    #   丢了 op → worker 判 `client_outdated` → **整片 401**。
    #   症状极具误导性：同一次 get_vendor 里，单读的 idmap 200、并发取的
    #   /data/gb/** 全 401 → 报 "fetch_failed"（看起来像企业不存在或网络抖）。
    #   每任务一份 `copy()`，因为同一个 Context 不能并发进入。
    import contextvars
    _base = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(n, len(paths))) as ex:
        futs = {ex.submit(_base.copy().run, _shard_text, p): i
                for i, p in enumerate(paths)}
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
            # gzip 存储（2026-10-30）：分片原文是 JSONL，gzip 后只剩 1/4~1/5。
            # 缓存的是**文本**，压缩不改变语义，只是别再让用户为「查过几次」
            # 付几十 MB 磁盘。旧格式（未压缩）仍可读。
            with open(cp, "rb") as f:
                raw = f.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8")
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
        tmp = cp + ".tmp"
        with open(tmp, "wb") as f:
            f.write(gzip.compress(txt.encode("utf-8"), compresslevel=6, mtime=0))
        os.replace(tmp, cp)
    except Exception:
        pass
    return txt


# --------------------------------------------------------------------------- #
# manifest + 分片访问
# --------------------------------------------------------------------------- #
# 清单的**跨进程**磁盘缓存（2026-10-29）。
#
# 为什么必须做：线上每次新进程加载清单要 2~3 秒（582 KB + 身份令牌 + 一次条件
# 请求），而清单是**读任何分片的前置条件** —— 拿不到清单，连「这个分片的内容
# sha1 是多少」都无从判断，`_shard_text` 的内容哈希缓存就永远无从命中。
# 结果就是「跨进程复用」形同虚设：同一条查询（素粉/贵阳）在三个全新进程里实测
# 11151 / 5713 / 7788 ms，而分片本身早已缓存（95 个 shard-*.json）。
# 换句话说，卡的不是下载，是**每进程一次的清单取数**。
#
# 因此给它一条真正的旁路：清单连同取到时间落盘，TTL 内完全不联网。
# 只在「数据只能来自网络」时启用（REPO 存在时本地文件 / git 更快也更准，
# 不能让 TTL 掩盖本地派生层重建 —— 那条路径仍按 mtime 判新）。
BEACON_MANIFEST_TTL = float(os.environ.get("BEACON_MANIFEST_TTL", "300") or "0")


def _manifest_disk_path() -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, "manifest-fresh.json")


def _manifest_disk_read() -> Optional[Dict[str, Any]]:
    """读跨进程清单缓存；未启用 / 过期 / 损坏 → None。"""
    if BEACON_MANIFEST_TTL <= 0:
        return None
    try:
        with open(_manifest_disk_path(), "r", encoding="utf-8") as f:
            blob = json.load(f)
        at = float(blob.get("at") or 0)
        man = blob.get("manifest")
        if isinstance(man, dict) and man.get("shards") is not None \
                and (time.time() - at) < BEACON_MANIFEST_TTL:
            return man
    except Exception:
        pass
    return None


def _manifest_disk_write(man: Dict[str, Any]) -> None:
    try:
        with open(_manifest_disk_path(), "w", encoding="utf-8") as f:
            json.dump({"at": time.time(), "manifest": man}, f, ensure_ascii=False)
    except Exception:
        pass


# 预热完成信号（2026-10-29）。为什么要等：预热和首次查询会**抢同一批表**
# （别名三表 / 切词表 / 清单）。没有锁时两边各算一遍，等于把 4 秒翻倍；
# 而实测「initialize 之后立刻发查询」的客户端确实会撞上这个窗口
# （三个新进程 4624 / 4857 / 35745 ms —— 那个 35s 就是两倍工作 + 网络重传叠出来的）。
# 所以查询侧**等预热完成**，而不是并行重复。
_WARM_DONE = threading.Event()
_WARM_WAIT_S = float(os.environ.get("BEACON_WARM_WAIT", "20") or "20")


def _await_warm() -> None:
    """等待启动预热完成（最多 `_WARM_WAIT_S` 秒）。未启动/已完成为无操作。"""
    if not _WARM_DONE.is_set():
        _WARM_DONE.wait(_WARM_WAIT_S)


def _warm_caches() -> None:
    """启动后异步预热「所有检索的前置」。

    放在后台线程里做，是因为 stdio 的 `initialize` 不能等这些（客户端会判服务
    不可用）；预热完，首次 `tools/call` 就直接命中，用户感知不到这一跳。

    清单里每一项都对应一个**进程内首次查询才建**的表，线上冷启动实测合计
    约 6~7 秒（2026-10-29，各分项为单进程冷调）：
      · load_manifest        2.0~3.0 s（跨进程 TTL 缓存上线后降到 ~20 ms）
      · alias_codes          2.07 s（三张别名表：data / curated / abbr）
      · _segment_terms       1.91 s（最长优先切词表，由别名+类名+能力词合成）
      · _city_names             25 ms
      · _province_map            3 ms
    每项都可失败：真出错时查询路径会自己重建，不让预热把进程带崩。
    """
    try:
        for fn in (load_manifest, _shard_hash_index, _city_names, _province_map,
                   load_gb_alias, _seg_patterns):
            try:
                fn()
            except Exception:
                pass
        try:
            _cap_term_of("cuisine_noodles")     # 预热能力词面表（cap.json）
        except Exception:
            pass
    finally:
        _WARM_DONE.set()


def load_manifest() -> Dict[str, Any]:
    global _manifest_cache
    if _cache_stale(_manifest_cache, "data/manifest.json"):
        # 只有「数据必须从网络来」时才走磁盘旁路；本地存在副本时保持 mtime 判新。
        use_disk = not REPO
        if use_disk:
            man = _manifest_disk_read()
            if man is not None:
                _manifest_cache = (man, time.time(), None)
                return _manifest_cache[0]
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
        _man = json.loads(txt)
        _manifest_cache = (_man, time.time(), _wt_mtime("data/manifest.json"))
        if use_disk:
            _manifest_disk_write(_man)
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
    """词 → 条目。三层表合并（data > curated > abbr，setdefault 保序）：
      gb-alias.json         数据推导层（企业关键词实际落在哪些小类）
      gb-alias-curated.json 人工策展层（口语俗称：律所、饭店、保洁…）
      gb-class-abbr.json    机械简称层（类名切短：法律服务、正餐、快递…）
    前两层优先级更高：abbr 层显式跳过与前两层冲突的词（见 gen_class_abbr.py）。"""
    global _alias_cache
    _alias_relpaths = ("data/gb-alias.json", "data/gb-alias-curated.json",
                       "data/gb-class-abbr.json")
    if _cache_stale(_alias_cache, _alias_relpaths):
        # ⚡ 两张表互相独立 → 一次并发取齐。逐个 fetch 各付一次 CDN RTT
        #    （并发取齐可省下数次串行往返）。
        #    同时借 _read_many_text 落到 _shard_text：清单里登记了 h 的表按内容缓存，
        #    内容没变就完全不走网络；没 h 的表（如 gb-alias-curated.json）自动退回 fetch。
        raw = _read_many_text(list(_alias_relpaths))
        # 三张表都在磁盘上；只要有一张没取到就**不提交缓存**（同 `_city_names`），
        # 否则一次抖动会让本进程此后所有口语词（律所/输送线/保洁…）都推不出国标码。
        if all(raw.get(rel) for rel in _alias_relpaths):
            merged: Dict[str, Any] = {}
            for rel in _alias_relpaths:
                for k, v in _load_alias_file(rel, raw.get(rel)).items():
                    merged.setdefault(str(k).lower(), v)
            _alias_cache = (merged, time.time(), _wt_mtime_any(_alias_relpaths))
    return _alias_cache[0] if _alias_cache else {}


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
        if names:      # 取不到不写缓存（同 `_city_names`）
            _gb_name_cache = (names, time.time(), _wt_mtime("data/gb4754-full.json"))
    return _gb_name_cache[0] if _gb_name_cache else {}


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


# 检索的**固定前置索引**（2026-10-30）。
#
# 为什么列成常量：这些文件每次检索都要读，但它们互不依赖，原来是「用到才读」
# —— 于是变成一条纯串行的 RTT 链。实测一次冷启动检索的时间线：
#   t=5.5s manifest → 7.1 city → 8.3 province-city → 9.7 gb-alias
#   → 9.9 gb-class-abbr → 9.9 gb-alias-curated → 11.9 gb-index → 13.1 cap
#   → 15.3 gb4754-full → 16.5 meta …… 到 17.7s 才开始读真正的分片。
# 即**12 秒 / 30 秒的 40% 花在 9 个各 1~2.3s 的串行小请求上**，
# 而它们的总字节量并不大。并发预取后这一段只剩最慢那一个的量级。
#
# 只预取、不改任何读取点：命中缓存后各调用方仍走自己原来的路径与校验。
_WARM_INDEX_PATHS = (
    "skills/registry/index/city.json",
    "skills/registry/index/province-city.json",
    "skills/registry/index/cap.json",
    "skills/registry/index/meta.json",
    "data/gb-alias.json",
    "data/gb-alias-curated.json",
    "data/gb-class-abbr.json",
    "data/gb-index.json",
    "data/gb4754-full.json",
)


def _warm_indexes() -> None:
    """并发预热固定索引。失败一律吞掉 —— 预热只影响速度，不能影响结果。"""
    if REPO:
        return          # 本地副本：读文件是微秒级，没必要开线程池
    try:
        _shard_hash_index()          # 先备好清单，否则预取也要排队等它
        _fetch_many(list(_WARM_INDEX_PATHS))
    except Exception:
        pass


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
    _GB_SHELF = rows if rows else None   # 取数失败不缓存空货架（同 `_city_names`）
    return rows


def _seg_patterns() -> List[Tuple[str, str]]:
    """长词优先的匹配表 [(词, 来源)]，来源 ∈ alias / gb_name / cap。"""
    global _SEG_PATTERNS
    if _SEG_PATTERNS is None:
        # 跨进程磁盘缓存（2026-10-29）。冷建实测 4.4 秒，几乎全花在
        # `_cap_term_index()`（大 JSON 解析 + 建词表）和 2757 条排序上，
        # 都是**纯 CPU**，与网络无关；不做持久化的话每个新进程都要重付一次。
        # 键里带数据版本号：一发布新数据版本就变，不会读到陈旧切词表。
        _cp = None
        try:
            _ver = _data_version() or ""
            if _ver:
                os.makedirs(CACHE_DIR, exist_ok=True)
                _cp = os.path.join(CACHE_DIR, "seg-" + hashlib.sha1(
                    _ver.encode("utf-8")).hexdigest() + ".json")
                if os.path.exists(_cp):
                    with open(_cp, "r", encoding="utf-8") as f:
                        _d = json.load(f)
                    if isinstance(_d, list) and _d:
                        _SEG_PATTERNS = [(str(a), str(b)) for a, b in _d]
                        return _SEG_PATTERNS
        except Exception:
            _cp = None
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
        # 全空说明取数链路整个挂了：不缓存，否则词条切分此后永久失效
        _SEG_PATTERNS = sorted(pats.items(), key=lambda x: -len(x[0])) if pats else None
        if _SEG_PATTERNS and _cp:
            try:
                with open(_cp, "w", encoding="utf-8") as f:
                    json.dump([[w, s] for w, s in _SEG_PATTERNS], f,
                              ensure_ascii=False)
            except Exception:
                pass
    return _SEG_PATTERNS or []


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


# 业态后缀：只表达「这是一家店」这种经营形态，不携带品类信息。
# 「牛肉粉店」与「牛肉粉」在名录里指的是同一批主体，而名录侧的写法普遍不带后缀。
_STORE_SUFFIX = "店馆铺摊吧坊"


def _strip_store_suffix(tok: str) -> str:
    """剥掉 token 末尾的业态后缀（可连续剥：「拉面店馆」→「拉面」）。

    护栏（必须保留）：原词 <3 字或剥后 <2 字 → 原样返回。
    「面馆」剥成「面」、「书店」剥成「书」都属于把精确查询退化成整城扫，
    比召回不足更糟 —— 这类查询的正确解法是走 gb= 或 list_industries。
    """
    orig = tok or ""
    t = orig
    while len(t) >= 3 and t[-1] in _STORE_SUFFIX:
        t = t[:-1]
    return t if len(t) >= 2 else orig


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
                           "province": "",
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
    # ②bis 省级抽取（2026-10-29）。省名**不能当关键词做 AND**：「贵州 面馆」原样
    #     拆成两个 token → 要求厂名同时含「贵州」与「面馆」→ 实测 0 条，而全省
    #     确实有面馆。省名必须变成**过滤条件**（记录的城市反查所属省），
    #     不能留在关键词里。直辖市（北京/上海）走城市那条路，这里不重复处理。
    prov_found = ""
    if not city_found:
        try:
            cand = [p for p in _province_map()["provinces"] if p and p.lower() in low]
        except Exception:
            cand = []
        if cand:
            prov_found = max(cand, key=len)
    resid = low
    if city_found:
        resid = resid.replace(city_found.lower(), " ")
    if prov_found:
        resid = resid.replace(prov_found.lower(), " ")
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

    # ③ 业态后缀归一（2026-10-29）。用户说的是「牛肉粉店」，名录里写的是「牛肉粉」；
    #    在纯子串 AND 语义下，多一个「店」字就把召回打掉九成 —— 贵阳实测
    #    「牛肉粉」42 条 vs「牛肉粉店」3 条（−93%），「面馆」13 条 vs「粉面馆」…
    #    「店/馆/铺/摊/吧/坊」这类**业态后缀不携带品类信息**，剥掉是**纯放宽**：
    #    任何含「牛肉粉店」的检索面必然也含「牛肉粉」，所以不可能引入假阳性。
    #
    #    护栏：原词 ≥3 字、剥后 ≥2 字才剥。否则「面馆」→「面」、「书店」→「书」
    #    会退化成「整城扫含面/书的记录」，比重排更糟。被改写的词原样回显在
    #    `query_parsed.token_fixed`，让调用方看得见 MCP 是**怎么放宽**的。
    #
    #    另有一条既有缺陷一并处理：能力词被消费后残留的**孤立业态字**。
    #    「米线馆」里「米线」是能力词（cuisine_rice_noodles）会被消费掉，只剩
    #    一个「馆」进 tokens，于是要求每条记录的检索面都含「馆」——实测昆明
    #    「米线馆」只剩 1 条。单字业态后缀不携带任何信息，与 `_noinfo` 同一口径丢弃。
    token_fixed: List[Dict[str, Any]] = []
    fixed: List[str] = []
    for t in toks:
        if len(t) == 1 and t in _STORE_SUFFIX:
            token_fixed.append({"from": t, "to": "", "why": "孤立业态字不携带信息，已丢弃"})
            continue
        s = _strip_store_suffix(t)
        if s != t:
            token_fixed.append({"from": t, "to": s, "why": "去掉业态后缀后按放宽形式检索"})
            fixed.append(s)
        else:
            fixed.append(t)
    toks = fixed

    out["tokens"] = list(dict.fromkeys(toks + [w.lower() for w, _ in segs]))
    out["token_fixed"] = token_fixed
    # 省名只有在「另外还有检索线索」时才当过滤条件。若整句就是个光秃秃的省名
    # （「贵州」），退回去当关键词做厂名匹配 —— 否则 token 为空会触发全国全扫
    # （188 片 / 四十多秒），而用户要的往往只是「名字里带贵州的那些」。
    if prov_found and not (out["tokens"] or cap_hits or alias_hits):
        out["tokens"] = [prov_found.lower()]
        prov_found = ""
    out["province"] = prov_found
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


# _shelf_recall 单字回溯用的泛字：这些字在国标类名里随处可见，命中了不说明
# 查询词与该类有任何关系（「店」命中正餐服务、咖啡馆服务……全部）。
_SHELF_STOP_CHARS = set("服务业公厂商中心部所集团有限材料产品设备加工制造技术"
                        "供销买卖经营承包租赁代理经纪咨询开发建设设计安装"
                        "装修装饰工程施工承包勘察监理检测检验")


def _shelf_recall(q: str, limit: int = 5) -> List[Dict[str, Any]]:
    """L3 兜底：查询词与任何类名/别名都不对字时，把货架初筛递到 agent 手边。

    与 suggest_filters 的分工：那条链路只认「子串完整对上」（ql in nm）；
    这里用 **bigram 重合度** ——「法律咨询」对「律师及相关法律服务」共享
    bigram「法律」，子串链路全军覆没时这里还能给方向。完全无重合时退化为货架
    records top（why 里明码标注「字面无关联，仅供语义参考」），让 agent 至少
    知道名录里有什么、按语义挑一个 gb=，或者如实告诉用户「该查询不在覆盖范围」。
    """
    q = (q or "").strip()
    if not q:
        return []
    ql = q.lower()
    grams = {g for g in _grams(q, query_mode=True) if len(g) >= 2 and g != ql}
    # 单字回溯：bigram 全灭时启用（「律政」→ 7231 律师及相关法律服务共享单字「律」）。
    # 泛字单字（服/务/业/公/司…）在类名里到处都是，命中了也不说明问题 —— 剔除。
    chars = {c for c in q if "\u4e00" <= c <= "\u9fff" and c not in _SHELF_STOP_CHARS}
    scored: List[tuple] = []
    for r in _gb_shelf():
        nm = (r["name"] or "").lower()
        path = (r["path"] or "").lower()
        s = 100 if (ql and ql in nm) else 0
        bi = sum(1 for g in grams if g in nm) if grams else 0
        if bi:
            s += min(bi * 10, 40)
        elif chars:
            # 单字回溯分级：类名**首字**命中是强信号（「律师…」对「律政」的律），
            # 腰部命中降权（「邮政/市政」里的政与「律政」无关）。不叠加，取最高。
            head = nm[:1] if nm else ""
            if any(c in chars for c in head):
                s += 25
            elif any(c in nm for c in chars):
                s += 8
        s += min(r["records"] / 5000.0, 4)
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    out: List[Dict[str, Any]] = []
    for s, r in scored[:limit]:
        out.append({"code": r["code"], "name": r["name"], "path": r["path"],
                    "records": r["records"],
                    "why": "货架字面初筛（bigram 重合，非精确子串）：请按语义判断是否对口"})
    if not out:
        # 完全不对字 → 货架热销榜。给信息但不装懂：why 明说这只是 records 最高。
        for r in sorted(_gb_shelf(), key=lambda r: -r["records"])[:limit]:
            out.append({"code": r["code"], "name": r["name"], "path": r["path"],
                        "records": r["records"],
                        "why": "查询词与货架字面零关联 —— 这只是名录里 records 最高的类，"
                               "仅供语义参考，不对味就如实告诉用户没收录"})
    return out


def _geo_rank_key(x: Dict[str, Any]) -> tuple:
    """距离排序键：无坐标记录恒排档内末位（None 不参与比较，用 (1, 0.0) 占位）。"""
    d = x.get("distance_km")
    return (1, 0.0) if d is None else (0, d)


def _geo_apply(matches: List[Dict[str, Any]], lat_v: Optional[float],
               lng_v: Optional[float], rad: Optional[float]) -> tuple:
    """给结果算球面距离；rad 非空时按圈过滤。

    返回 (保留列表, 圈外剔除数, 有坐标条数)。

    无坐标记录在 rad 模式下**剔除** —— 无法证明在圈内，就是不在（与项目
    「宁可留空不编造」同源：拿 1.5% 的空值去猜「应该在圈内」，用户按导航
    过去发现是隔壁市，比没有结果更糟）。只排序模式（rad=None）下保留、
    distance_km=None、排档内末位。

    调用时机：分页**之前**（与 _collapse_brands 同理，见该函数注释）。
    """
    if lat_v is None:
        return matches, 0, 0
    kept: List[Dict[str, Any]] = []
    dropped = 0
    n_coord = 0
    for m in matches:
        la, ln = m.get("lat"), m.get("lng")
        if la is None or ln is None:
            if rad is not None:
                dropped += 1
                continue
            kept.append(m)
            continue
        d = _haversine_km(lat_v, lng_v, la, ln)
        n_coord += 1
        if rad is not None and d > rad:
            dropped += 1
            continue
        m["distance_km"] = round(d, 2)
        kept.append(m)
    return kept, dropped, n_coord


def _geo_note(rad: Optional[float]) -> str:
    """坐标查询时追加在 order_note 后的口径说明（无坐标查询返回空串）。"""
    if rad is not None:
        return (" 本次传了坐标+半径：圈内按召回纯度分档、档内按球面距离升序；"
                "distance_km 为直线距离（非路程）；圈外与无坐标记录已剔除，"
                "剔除数见 geo_filtered_out。")
    return (" 本次传了坐标（无半径）：按球面距离升序为第一键，等距才按召回纯度；"
            "distance_km 为直线距离（非路程）；无坐标记录 distance_km=null 排末位。")


def _attach_routing_hint(out: Dict[str, Any], q: str, city: str) -> Dict[str, Any]:
    """命中偏少时，把「怎么把口语词换成国标码」这条链路内联进结果。

    只在 query 非空且命中 < 5 时触发 —— 命中充足说明用户说的词本身就是数据里的
    词，不需要打扰 agent。内联（而不是让 agent 再调一次）是为了省一个 RTT：
    agent 看到 hint 就能直接改参数重试。
    """
    if not q or out.get("total_matched", 0) >= 5:
        return out
    # 坐标圈内 0 条（2026-10-23）：先于一切候选码建议 —— 圈内没数据时，
    # 「改查 gb= 重试」是纯噪音（换什么码圈内都还是没有），该说的是
    # 「检索命中 N 条但都不在圈内 / 无坐标」。这类信息差最容易让 agent
    # 对用户说出「这个区域没有便利店」这种**没有数据支撑**的结论。
    if out.get("geo_filtered_out"):
        out["hint"] = ("坐标圈内 0 条：检索本身命中 %d 条，但都超出半径或无坐标"
                       "（无坐标记录无法证明在圈内，已剔除）。**不要对用户说"
                       "「这一带没有」**——如实说「圈内无收录，全城/全库有 N 条，"
                       "最近的也超出半径」。可放宽 radius_km、或去掉坐标按城市查。"
                       % out["geo_filtered_out"])
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
        # L3：货架初筛兜底。别名/类名子串都推不出码时，把 bigram 初筛（或热销榜）
        # 直接递给 agent —— 语义归一这一步归 LLM，但「候选从哪来」不必再让它
        # 多花一轮 list_industries。
        shelf = _shelf_recall(q, limit=5)
        if shelf:
            out["suggested_gb"] = shelf
            out["hint"] = ("0 条：你的词与名录里的写法（企业名/国标类名/别名表）都对不上字。"
                           "名录按国标小类归档，我已按字面重合度初筛了货架上最接近的类目"
                           "（见 suggested_gb）—— 请按语义挑 1~3 个 code 改传 gb= 重试；"
                           "都不对味就如实告诉用户该查询不在名录覆盖范围，不要换词硬猜。")
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
    # L3 同上：货架初筛兜底（这批词多半只是字面撞词，真正的行业要 agent 按语义挑）。
    shelf = _shelf_recall(q, limit=5)
    if shelf:
        out["suggested_gb"] = shelf
        out["hint"] = ("命中偏少且推不出国标码：这几条多半只是**字面撞词**，未必真是你要的行业。"
                       "我已按字面重合度初筛了货架上最接近的类目（见 suggested_gb）—— 请按语义挑 "
                       "1~3 个 code 改传 gb= 重试，召回和精度都会好很多。")
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
    {terms:{code:词面串}, shards:{code:{shard_name:count}}}。

    ⚠ 与 `_city_names` 同一类坑：取不到时若把 `{}` 写进缓存，此后整个进程的
    能力（工艺/技术标签）召回全部失效，而且返回值看着「正常」（只是永远没命中）。
    """
    global _cap_index_cache
    if _cache_stale(_cap_index_cache, "skills/registry/index/cap.json"):
        txt = _index_text("skills/registry/index/cap.json")
        if txt:
            try:
                _cap_index_cache = (json.loads(txt), time.time(),
                                    _wt_mtime("skills/registry/index/cap.json"))
            except Exception:
                pass          # 半成品 → 不缓存，下次重取
    return _cap_index_cache[0] if _cap_index_cache else None


def _cap_term_index() -> Dict[str, List[str]]:
    """词面词 → 能力键**列表**（反向索引，缓存）。覆盖 cap.json terms 里的每个分词，
    例如 「AI」「人工智能」「机器学习」「大模型」「算法」→ [tech_ai]。

    **一个词可以指向多个键**（2026-10-23）。别名会跨域撞车：「物流」同时是
    retail-service 的 rdm_freight（零售商提供送货上门）和 civic-infra 的
    log_freight（货运主体）—— 两者都是合法语义，且**按用户意图二选一做不到**
    （「苏州物流」既可能找货运公司，也可能找送货运的商行）。
    旧版是 `setdefault`「首个码优先」：域名顺序里 retail-service 在 civic-infra
    前面，于是 rdm_freight 恒赢，G 门类 2178 家一个都召不回来。
    """
    idx: Dict[str, List[str]] = {}
    cap = _load_cap_index() or {}
    for code, termstr in (cap.get("terms") or {}).items():
        for w in str(termstr).lower().split():
            lst = idx.setdefault(w, [])
            if code not in lst:
                lst.append(code)
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
        for w, codes in _cap_term_index().items():
            if len(w) < 2:
                continue
            if not re.fullmatch(r"[a-z0-9\u4e00-\u9fff]+", w):
                continue
            rx = (re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])")
                  if w.isascii() else None)
            # 一词多码：每个 (词, 码) 各出一行，长词优先的排序不受影响
            for code in codes:
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
    # ① 整词（一词多码：全部收，见 `_cap_term_index` 的说明）
    for tok in dict.fromkeys(t for t in q.split() if t):
        for code in idx.get(tok) or []:
            hits.setdefault(code, {"cap": code, "word": tok,
                                   "name": _cap_name(code), "match": "exact"})
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

# 全量兜底的**上限**（2026-10-29）。`_build_fp_index` 是「预构建索引不可用」时的
# 最后一道网：git archive 一次批量读 267~467 个 fp 分片，实测 210,555 条要 26.7 秒。
#
# 它本来只在「索引缺失/陈旧」时才会被走到，但 `get_vendor` 在 id 反查失败时会拿它
# 兜底——那是一条**用户正在等着看点开的那一家**的同步路径。实测绕过 `_vid_in`
# 直接调 `get_vendor(token_id)` 就掉进来了：31s / 38s 两次复现，客户端表现为「卡死」。
#
# 所以给它两道闸：时间闸（超时即返回已拿到的部分，并如实标记截断）+ 条数闸。
# 两道闸都在**返回前**判定，且截断结果绝不写进 `_fp_index`（进程级缓存，写脏等于
# 让后续每次查询都拿半份数据，见下方 fallback 分支的同类注释）。
#
# 默认值刻意**高于**本机全量构建实测耗时（26.7s / 21 万条）：这道闸的职责是拦
# 「系统病态」（曾经出现 479 次请求、2258s 才返回 0 条），不是把正常构建切残——
# 8s 的初版实测会把索引截成 6.1 万条（29%），普通检索于是悄悄少 2/3 记录。
# 需要更紧预算的地方自己传 deadline_s（`get_vendor` 就传 5s，见
# `_VENDOR_FP_DEADLINE`）。
_FP_BUILD_DEADLINE = float(os.environ.get("BEACON_FP_BUILD_DEADLINE", "40") or "0")
_FP_INDEX_MAX = int(os.environ.get("BEACON_FP_INDEX_MAX", "260000") or "0")
_fp_trunc_tls = threading.local()


def _fp_trunc_note() -> Dict[str, Any]:
    """本线程最近一次全量兜底是否被截断；未截断返回空 dict。"""
    return dict(getattr(_fp_trunc_tls, "note", {}) or {})


def _fp_trunc_set(note: Dict[str, Any]) -> None:
    _fp_trunc_tls.note = dict(note or {})


def _head_sha() -> Optional[str]:
    if not REPO:
        return None
    try:
        r = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"],
                           capture_output=True, text=True,
                           # 2026-10-25 fix：这条在 `_data_version()` 热路径上，
                           # 每个 tool 调用都会经过。不指定 stdin 会让 git 继承
                           # stdio MCP 的 JSON-RPC 管道 → communicate() 死等 →
                           # 所有工具「假死」（1.11.2 实测）。必须 DEVNULL + 限时。
                           stdin=subprocess.DEVNULL, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _data_version() -> str:
    """数据版本号，用于给落盘缓存判新。

    git 模式 = HEAD sha（提交变则版本变）；
    HTTP 模式 = 清单**内容指纹**。

    2026-10-24 修：原来 HTTP 模式只取 `generated_at`，它是 `date.today()`（**日期精度**）。
    同一天内重复发布（重跑索引、补数据）版本号不变 → 落盘缓存永远命中旧快照 →
    线上索引卡在 210555 条，MCP 每次搜索都判 `index_behind_data` 并退化成扫全部
    467 个分片（单次 130s+），重跑 gen_search_index 也「看不出效果」。
    改为「generated_at + 各类型记录数 + 索引桶哈希聚合」，数据或索引任一变化即失效。

    取不到就返回空串，调用方据此**禁用**缓存（宁慢不脏）。
    """
    if REPO:
        return _head_sha() or "nosha"
    try:
        md = load_manifest().get("metadata", {}) or {}
        bt = md.get("by_type") or {}
        parts = ["%s:%s" % (k, (bt[k] or {}).get("records", 0)) for k in sorted(bt)]
        h = (md.get("idxfiles") or {}).get("h") or {}
        if h:
            parts.append("idx:" + hashlib.sha1(
                "".join(sorted(h.values())).encode("utf-8")).hexdigest()[:16])
        if not parts:
            return str(md.get("generated_at") or "")
        return str(md.get("generated_at") or "") + "|" + "|".join(parts)
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
# 7~10 位：编号上限从 9,999,999 抬到 4,294,967,295，与 vid token 的 32 bit 编号位
# 对齐（2026-10-28）。同一片内宽度必然相同，所以片内字典序仍等于数值序。
_VENDOR_ID_RE = re.compile(r"^CN-MFG-(\d{7,10})$")
_IDMAP_DIR = "skills/registry/index/idmap"
# 片内解析结果常驻内存：rel -> (id->gb, 数据版本号)
_idmap_slot: Dict[str, tuple] = {}
# meta.json（切片参数）常驻内存：({...}, 数据版本号)
_idmap_meta_slot: Optional[tuple] = None


def _idmap_bucket(vid: str) -> Optional[int]:
    """id 落在第几片；非 `CN-MFG-\\d{7,10}` 形态返回 None（归 other.jsonl）。

    只有**旧寻址**（数字整除 1000）用得上。token 寻址看 _idmap_rel 那条分支。
    """
    m = _VENDOR_ID_RE.match(vid or "")
    return int(m.group(1)) // _IDMAP_BUCKET if m else None


# 对外形态：8 字符 Crockford base32（去掉 I/L/O/U，与 vid_token 的编码一致）
_TOKEN_ID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{8}$")
# 寻址模式。老快照的清单里没有 `addr` → 客户端继续按旧的整除定址走，
# 行为与升级前**完全一致**（不多付请求、不会 404），所以可以安全先发客户端。
_IDMAP_ADDR_TOKEN = "prefix2"


def _idmap_addr() -> str:
    """清单里 idmap 的寻址模式。取不到 = 该快照的 idmap 还没换 token 寻址。"""
    return str(_idmap_meta().get("addr") or "bucket1000")


def _idmap_rel(vid: str) -> Optional[str]:
    """id 落在哪一片。

    token 寻址：片名 = token 的前 2 字符（32×32 = 1024 片）。token 是随机
    排列的，前 2 字符在其上是**均匀**的 → 21 万条散进去每片约 205 行 / 3 KB，
    与旧的「1000 行 / 14 KB」同量级，而且不需要任何目录表。
    """
    if _idmap_addr() == _IDMAP_ADDR_TOKEN:
        if vid and _TOKEN_ID_RE.match(vid):
            return "%s/%s.jsonl" % (_IDMAP_DIR, vid[:2])
        return (_IDMAP_DIR + "/other.jsonl") if vid else None
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

    ⚠ token 寻址下**没有这道闸，也不需要**：清单、分片、这条索引是同一次
    发布出去的，token 不在片里就是没有这家 —— 不存在「号比索引新」这种
    中间态（旧寻址之所以需要，是因为 id 自增，新号天然落在索引之外）。
    """
    if _idmap_addr() == _IDMAP_ADDR_TOKEN:
        # 形态不是 token = 内部 id 漏出来了（旧客户端 / 未发布的数据），
        # 直接交回调用方走兜底，不去 other.jsonl 碰运气。
        if not vid or not _TOKEN_ID_RE.match(vid):
            return None, False
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
            # 片不存在 = 这个前缀下一条记录都没有 → 无从判断，交回兜底
            return None, False
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
        # 同 `_city_names`：取数失败不写缓存（否则整个进程的 gb 精确解析全废）
        if m:
            _name_gb_slot = (m, time.time(), _wt_mtime("data/name-index.jsonl"))
    return _name_gb_slot[0] if _name_gb_slot else {}


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


def _build_fp_index_via_archive(deadline: Optional[float] = None) -> Optional[List[Dict[str, Any]]]:
    """git 模式：用 `git archive HEAD -- <dir>` 一次性批量取出所有 fp 分片。

    只读已提交 object（本就是 maskphone 隐藏态），不碰本地副本、不触发 smudge、
    不碰 index 锁 —— 安全边界与逐分片 `git show` 一致，但把 267 次 subprocess
    降到 1 次，首次加载从 ~48s 降至数秒。任一环节失败都返回 None，由调用方退回
    逐分片 fallback。

    `deadline`（epoch 秒，可选）：解析阶段的**时间闸**。git archive 只花几百毫秒，
    真正吃时间的是 21 万行的 json.loads（实测全量构建 26.7s）。到点即返回**已解析
    的部分**并写入 `_fp_trunc_note()` 标记截断 —— 返回 None 会让调用方退回更慢的
    逐分片路径，反而更糟。
    """
    try:
        d = os.path.join("skills", "registry", "fingerprint")
        r = subprocess.run(
            ["git", "-C", REPO, "archive", "HEAD", "--", d],
            capture_output=True,
            stdin=subprocess.DEVNULL, timeout=120,   # 同上：不继承 stdio，且必须限时
        )
        if r.returncode != 0 or not r.stdout:
            return None
        recs: List[Dict[str, Any]] = []
        _cut = False
        with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:*") as tf:
            for m in tf.getmembers():
                if _cut:
                    break
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
                # 时间闸：按**分片**粒度判（一行一判的 time.time() 成本会吃掉
                # 相当一部分预算）。到点后把已解析的部分返回，别让它变成「卡死」。
                if deadline is not None and time.time() > deadline:
                    _cut = True
        if _cut:
            _fp_trunc_set({
                "stage": "archive_parse_timeout",
                "records": len(recs),
                "note": ("全量兜底在 git archive 解析阶段超时，只解析了部分分片（%d 条）。"
                         "**不代表没有相关企业**；请改用 query+city 或 gb= 走索引路径。"
                         % len(recs)),
            })
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


def _build_fp_index(deadline_s: Optional[float] = None) -> List[Dict[str, Any]]:
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
    _t_start = time.time()
    _fp_trunc_set({})          # 每轮构建先清掉上一轮的截断标记，避免串到本次回执里
    _budget = _FP_BUILD_DEADLINE if deadline_s is None else float(deadline_s)
    _deadline_abs = (_t_start + _budget) if _budget > 0 else None

    def _deadline_hit() -> bool:
        return _deadline_abs is not None and time.time() > _deadline_abs

    # git 模式优先用 `git archive` 一次性批量读（1 次 subprocess，远快于逐分片 git show）
    if REPO:
        recs = _build_fp_index_via_archive(_deadline_abs)
        if recs is not None:
            # 本地副本模式：用本地副本 fingerprint 分片覆盖/补充已提交快照，闭合检索缺口
            if wt:
                if _deadline_hit():
                    # 时间闸：archive 这一步已经吃满预算，别再把 merge 叠上去。
                    # 返回未合并的结果并标记，让调用方知道少了一层工作树覆盖。
                    _fp_trunc_set({
                        "stage": "worktree_merge_skipped",
                        "deadline_s": _FP_BUILD_DEADLINE,
                        "records": len(recs),
                        "note": ("全量兜底超时（%.1fs），已跳过工作树指纹覆盖层。"
                                 "结果可能不含本地副本里新增/修改的记录。"
                                 % _FP_BUILD_DEADLINE),
                    })
                else:
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
    # git show / HTTP GET 均为 I/O 密集，线程池并发拉取可大幅缩短首次构建耗时。
    # 分批 + 失败过半即停（2026-10-24）：全量回退是 467 个分片，若故障是系统性的
    # （401/429/断网），挨个试完只会把一次失败放大成几百次重试 —— 实测 479 次
    # 请求、累计 2258s 才返回 0 条，用户只看到「卡住然后没有结果」。
    _BATCH = 16
    _tried = 0
    _ok = 0
    _aborted = False
    _timeout = False
    # 与 _fetch_many 同一个坑：ContextVar 不进线程池 → 丢 op → 401。
    # 这里虽然有 _BOOT_OP 兜底不会挂，但兜底 op 会把「本次 tool 调用」
    # 记成「一次启动」，配额与台账都会失真，所以照旧显式带上下文。
    import contextvars
    _fpctx = contextvars.copy_context()

    def _read_fp_in_ctx(p):
        return _fpctx.copy().run(_read_fp_shard, p)

    with concurrent.futures.ThreadPoolExecutor(max_workers=_BATCH) as ex:
        for _i in range(0, len(fp_shards), _BATCH):
            _chunk = fp_shards[_i:_i + _BATCH]
            _parts = list(ex.map(_read_fp_in_ctx, _chunk))
            _tried += len(_chunk)
            _ok += sum(1 for _p in _parts if _p)
            for _p in _parts:
                recs.extend(_p)
            if _tried >= 32 and _tried < len(fp_shards) and _ok * 2 < _tried:
                _aborted = True
                _FETCH_ABORTED[0] = (
                    "取数失败过半（%d 个分片仅 %d 个读到），已中止剩余 %d 个"
                    % (_tried, _ok, len(fp_shards) - _tried))
                break
            # 时间闸：每批之间判一次。超时就带着**已拿到的部分**返回并标记截断，
            # 绝不无限期堵住调用方（get_vendor 会走这条兜底）。
            if _deadline_hit():
                _timeout = True
                break
    if _timeout:
        _fp_trunc_set({
            "stage": "shard_scan_timeout",
            "deadline_s": _FP_BUILD_DEADLINE,
            "shards_scanned": _tried,
            "shards_total": len(fp_shards),
            "records": len(recs),
            "note": ("全量兜底超时（%.1fs，已扫 %d/%d 个分片）。本次只基于已读到的部分，"
                     "**不代表没有相关企业**；请改用 query+city 或 gb= 走索引路径。"
                     % (_FP_BUILD_DEADLINE, _tried, len(fp_shards))),
        })
        return recs
    if _aborted:
        # ⚠ 绝不能把残缺结果写进 `_fp_index` / 磁盘缓存：那一层是**进程级**缓存，
        #   写进去等于让后续每一次查询都拿半份数据，直到重启。
        return recs
    if wt and REPO:
        recs = _merge_worktree_fp(recs)
    # 条数闸：兜底只为「找得到」，不必把整个库搬进内存。
    if _FP_INDEX_MAX and len(recs) > _FP_INDEX_MAX:
        _fp_trunc_set({
            "stage": "record_cap",
            "cap": _FP_INDEX_MAX,
            "records_total": len(recs),
            "note": ("全量兜底记录数超过上限 %d，已截断。结果可能不完整；"
                     "请改用 query+city 或 gb= 走索引路径。" % _FP_INDEX_MAX),
        })
        return recs[:_FP_INDEX_MAX]
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
        try:
            txt = _index_text("skills/registry/index/cap.json")
            if txt:
                terms = (json.loads(txt).get("terms") or {})
                if terms:     # 取数失败/空表不写缓存（同 `_city_names`）
                    _CAP_TERMS_SLOT = (terms, time.time(),
                                       _wt_mtime("skills/registry/index/cap.json"))
        except Exception:
            pass
    terms_now = _CAP_TERMS_SLOT[0] if _CAP_TERMS_SLOT else {}
    return str(terms_now.get(code) or code or "")


# 菜系级「互斥」：一个主体被显式标成韩餐／日料／西餐／东南亚／泰餐时，
# 不再把「面馆（cuisine_noodles）」的词面并进检索面。
#
# 为什么需要（2026-10-29 实测）：查「面馆」时首条是「Kim Pro辛普乐概念韩餐」。
# 它的 cap 是 ["cuisine_korean","cuisine_noodles","cuisine_barbecue","正餐"] ——
# 韩餐馆的 POI 标签里带「拉面」（韩式拉面），离线打标于是把 cuisine_noodles
# 也挂给了它。`面馆` 便经 cuisine_noodles 的中文词面命中它，且 via 被提到
# cap 档（最高档），压过所有真面馆，且无法靠翻页绕开。
#
# 为什么不改词表：删掉 cuisine_noodles 的「拉面」会让兰州拉面那批真面馆一起
# 掉出该能力域（它们的厂名往往只写「拉面」）。所以修在**检索面构造**这一步：
# 记录自带更强的菜系标签时，面条词面不参与匹配。这是纯收益——
# 真面馆不会被标成 cuisine_korean，所以不会误伤。
_CAP_CUISINE_CONFLICT: Dict[str, tuple] = {
    "cuisine_noodles": ("cuisine_korean", "cuisine_japanese", "cuisine_western",
                        "cuisine_southeast_asian", "cuisine_thai"),
}


def _cap_terms_of(rec: Dict[str, Any]) -> List[str]:
    """记录的能力键 → 实际参与检索的词面（已按菜系互斥剔除）。

    只是过滤能力键，不改记录本身：`cap` 字段原样保留，`via=cap` 的判定
    （`want & set(rec["cap"])`）与能力卡查询都不受影响。
    """
    caps = [c for c in (rec.get("cap") or []) if c]
    if not caps:
        return []
    capset = set(caps)
    kept: List[str] = []
    for c in caps:
        blocked = _CAP_CUISINE_CONFLICT.get(c)
        if blocked and any(b in capset for b in blocked):
            continue
        kept.append(c)
    return kept


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
        # 走 `_cap_terms_of`：菜系互斥后再摊成中文词面（否则韩餐馆会靠
        # cuisine_noodles 的词面冒充面馆，见 `_CAP_CUISINE_CONFLICT`）。
        parts.append(" ".join(_cap_term_of(c) for c in _cap_terms_of(rec)))
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
    """关键词在**厂名**里的贴合度，0/1/2/3。用于同一召回档内部的次级排序。

    只有档位是不够的：档内顺序原本就是分片里的记录顺序 —— 等于随机。
    还需要判断关键词是否**成片出现在厂名里**：只判断「是否出现」不够
    （括号里的商圈名也会撞上），必须是连片的词面命中才区分得开。

    第 4 档（2026-10-23）拆「成片命中」：连锁门店名是「品牌(门店限定)」，
    关键词落在**门店限定**里与落在**品牌名**里是两种相关性。
    实测搜「医院」：返回前 10 条全是「蓝天酒店(贵州医科大学附属医院店)」这类
    酒店/民宿/餐厅，真正的医院被挤到第 23 位之后 —— 两者同名次（text）、同名分
    （「医院」都成片在厂名里），于是按分片路径排，H 住宿餐饮恒在 Q 卫生前面。
    拆档后「贵州省人民医院」（品牌名命中）压过「…(…医院店)」（仅限定词命中）。

    `query+city` 与 `gb=` 两条路由共用本函数，保证同一批记录两条路排出来的顺序一致
    （体检的「C 路由一致性」就是查这个）。
    """
    if not tokens:
        return 0
    co = str(rec_or_sum.get("company") or rec_or_sum.get("co") or "").lower()
    if not co:
        return 0
    joined = "".join(tokens)
    if joined in co:
        # 成片命中再分一档：去掉门店限定后**仍在**，说明是主体本身的名字
        return 3 if joined in _brand_of(co) else 2
    return 1 if all(t in co for t in tokens) else 0


_RE_TAIL_BRACKET = re.compile(r"[\s]*[\(（][^\(（）\)]*[\)）]\s*$")


def _brand_of(name: str) -> str:
    """企业名 → 品牌键：去掉末尾的括号限定。

    连锁在名录里是**一家一条**（东吴面馆 9 条、瑞幸咖啡在苏州 267 条），
    对「找谁能做这个」是纯噪声。门店名的规律是「品牌(门店限定)」，去掉括号就是品牌。

    刻意**不做**更激进的归并（去「店/馆/楼」后缀、模糊匹配）：那会把「陆振兴」和
    「陆正兴」这种一字之差的不同主体并成一家 —— 错比噪声严重。
    与 scripts/gen_brand_index.py 的 brand_of 同一口径。
    """
    s = (name or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = _RE_TAIL_BRACKET.sub("", s).strip()
    return s


def _brand_key(sum_rec: Dict[str, Any]) -> Optional[str]:
    """归并键：城市 + 国标小类 + 品牌。跨城同名是不同主体，跨小类也是不同业态。"""
    b = _brand_of(sum_rec.get("company") or "")
    if len(b) < 3:
        return None
    return "%s|%s|%s" % (sum_rec.get("city") or "", sum_rec.get("gb") or "", b)


def _collapse_brands(matches: List[Dict[str, Any]], keep_all: bool = False
                     ) -> tuple[List[Dict[str, Any]], int]:
    """把同一品牌的多家门店折成一行（代表行带 `branches` 与 `branch_ids`）。

    为什么在**结果集上现算**而不是发布一份品牌索引：发布侧算过一次的版本有 911 KB，
    每个 MCP 客户端都得为它多下一份；而检索本来就把候选记录读进内存了，这里零成本。
    少发的字节比多发的索引值钱。

    副作用（可接受）：分片是按需分批拉的，跨分片连锁只统计到「本次加载到的部分」，
    `branches` 可能偏小。宁可少报一个数，也不让所有人多付 900KB。

    返回 (折叠后的列表, 主体数)。顺序保持不变 —— 代表行取该品牌在结果里最靠前的那条，
    所以档位排序（text → cap_text → alias → cap）不被破坏。
    """
    if keep_all:
        return matches, len(matches)
    out: List[Dict[str, Any]] = []
    pos: Dict[str, int] = {}
    for m in matches:
        k = _brand_key(m)
        if k is None:
            out.append(m)
            continue
        i = pos.get(k)
        if i is None:
            pos[k] = len(out)
            m["branches"] = 1
            out.append(m)
        else:
            row = out[i]
            row["branches"] = int(row.get("branches") or 1) + 1
            ids = row.setdefault("branch_ids", [])
            if len(ids) < 20:                       # 有上限：别让一条结果带 267 个 id
                ids.append(m.get("id"))
            # 坐标查询（2026-10-23）：品牌行报**最近门店**的距离，不是代表店。
            # 「全家 2.1km」而拐角就有一家 400m 的全家，是最容易被用户当成 bug 的
            # 一种错。代表行本身不动（档位/name_fit 仍按它），只把距离换成最近的。
            dm, dr = m.get("distance_km"), row.get("distance_km")
            if dm is not None and (dr is None or dm < dr):
                row["distance_km"] = dm
    return out, len(pos)


# 指纹 et 码 → 主体经济类型名。0 = 门类判不出来（老数据无国标码），不贴标签。
_ETYPE_NAMES = {0: "", 1: "生产", 2: "流通", 3: "服务"}


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两地球面直线距离（公里）。半径取 6371.0。

    只用于「X 公里内」这类**排序与粗筛**，不是测绘精度 —— GCJ-02（高德）与
    WGS-84（GPS）坐标混用时有数十到数百米偏差，对公里级半径无影响。
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


# 中国经纬度范围（与 scripts/gen_fingerprint.py 的坐标写入校验同口径）。
# 超出即视为调用方传错（常见根因：经纬度顺序写反、用了海外国标 API）。
_GEO_LAT_RANGE = (3.0, 54.0)
_GEO_LNG_RANGE = (73.0, 136.0)
_GEO_RADIUS_MAX_KM = 500.0


def _geo_params(lat: Any, lng: Any, radius_km: Any) -> tuple:
    """坐标参数校验 → (lat_v, lng_v, radius_v)。任一不合法返回 error 字典占位。

    地标名（「青创港」）→ 坐标由**调用方**解析：MCP 不持地图 API，也不猜地名。
    传错时不静默忽略 —— 坐标查询的失败模式必须是显式的，否则 agent 会把
    「按全城排的结果」误当成「按距离排的结果」交付给用户。
    """
    if (lat is None) != (lng is None):
        return None, None, None, {
            "error": "bad_geo",
            "message": "lat 与 lng 必须同时提供。「青创港」这类地标名请先用地图工具"
                       "解析成坐标再传（MCP 不持地图 API，不做地名猜测）。",
        }
    if lat is None:
        return None, None, None, None
    try:
        lat_v, lng_v = float(lat), float(lng)
    except (TypeError, ValueError):
        return None, None, None, {"error": "bad_geo",
                                  "message": "lat/lng 必须是数字，收到 %r / %r" % (lat, lng)}
    if not (_GEO_LAT_RANGE[0] <= lat_v <= _GEO_LAT_RANGE[1]
            and _GEO_LNG_RANGE[0] <= lng_v <= _GEO_LNG_RANGE[1]):
        return None, None, None, {
            "error": "bad_geo",
            "message": "坐标超出中国范围（lat %.1f~%.1f, lng %.1f~%.1f），收到 %r / %r。"
                       "常见根因：经纬度顺序写反。" % (
                           _GEO_LAT_RANGE[0], _GEO_LAT_RANGE[1],
                           _GEO_LNG_RANGE[0], _GEO_LNG_RANGE[1], lat, lng),
        }
    rad = None
    if radius_km is not None:
        try:
            rad = float(radius_km)
        except (TypeError, ValueError):
            return None, None, None, {"error": "bad_geo",
                                      "message": "radius_km 必须是数字，收到 %r" % (radius_km,)}
        if rad <= 0:
            return None, None, None, {"error": "bad_geo",
                                      "message": "radius_km 必须 > 0（公里）"}
        if rad > _GEO_RADIUS_MAX_KM:
            return None, None, None, {
                "error": "bad_geo",
                "message": "radius_km 上限 %.0f 公里（覆盖全国任意两点），收到 %r。"
                           "要更大范围请去掉坐标按城市/全国查。" % (
                               _GEO_RADIUS_MAX_KM, radius_km),
            }
    return lat_v, lng_v, rad, None


def _rec_summary(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": rec.get("id"),
        "company": rec.get("co"),
        "city": rec.get("city"),
        "district": rec.get("dist"),
        "gb": rec.get("gb"),
        # 行业归类的证据来源（2026-10-24）。指纹分片带 isrc 时才有值；
        # 没有就留 null —— 调用方要精确证据等级请用 get_vendor 的 industry_evidence。
        # ⚠ 读的是 isrc 不是 src：src 是**数据信源**（gov_list/public_directory），
        #   两件事必须分开，混用会让 App 的绿色 G 标和这里的证据等级互相干扰。
        "industry_source": rec.get("isrc"),
        # 主体经济类型：生产(A/B/C/D/E) / 流通(F/G) / 服务(H~S)。
        # 全行业口径下这是唯一能跨门类表达「这家主体在干什么性质的事」的信号 ——
        # 旧的「是否制造商」只能区分 C 与非 C，服务类主体占六成却全挤在一筐里。
        "etype": _ETYPE_NAMES.get(rec.get("et") or 0, ""),
        "badge": rec.get("cl"),
        "score": rec.get("sc"),
        "has_phone": bool(rec.get("tel")),
        "process": rec.get("proc", []),
        "material": rec.get("mat", []),
        "cert": rec.get("cert", []),
        # 坐标（2026-10-23 接入）：指纹层 98.5% 有值，无值为 null。
        # 给 agent 自己排序/换算用；检索侧的距离过滤见 search_vendors 的
        # lat/lng/radius_km 参数（那里才会写 distance_km）。
        "lat": rec.get("lat"),
        "lng": rec.get("lng"),
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
                           capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=120)  # 同上：不继承 stdio
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
    # ⚠ 必须把调用方的 ContextVar **显式带进线程池**（2026-10-24 实测踩坑）。
    #   本次 tool 调用的 op 存在 `_cur_get()` 里，`X-Beacon-Op` 头就是靠它发出去的；
    #   而 ContextVar **不会**自动继承到 ThreadPoolExecutor 的工作线程（线程各有空
    #   context）。子线程发出的请求于是只有 cid、没有 op → worker 判 identity_required
    #   → 401 → 文本为空。症状极隐蔽：city.json 是主线程单读（正常），terms/*.json
    #   走这里（全 401）→ 候选分片集为空 → 搜索返回 0 条、shards_scanned=0，
    #   看起来像「索引没建好」，实际是身份在跨线程时丢了。
    import contextvars
    _ctx = contextvars.copy_context()

    def _one(p: str) -> Optional[str]:
        return _ctx.copy().run(_shard_text, p)   # 每个任务一份副本，可并发进入

    # 分批提交 + **快速放弃**（2026-10-24）。
    # 为什么必须分批：全量回退最多要拉 467 个分片。若失败是**系统性**的
    # （断网、代理死、身份 401、429 限流），挨个试完只是把「一次失败」放大成
    # 几百次重试 —— 用户侧看到的就是「工具卡了 100~300 秒然后返回 0 条」，
    # 而且完全不知道是真没有这家企业还是取数挂了。实测过一次 479 次请求
    # 累计 2258s 才返回 0 条。
    # 判据：跑满 32 个后统计**失败占比**，超过一半就停 —— 此时哪怕剩下的全成功，
    # 结果也已经残缺不全，继续拉没有意义，不如把故障如实报给调用方。
    _BATCH = 16
    _tried = 0
    _ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=_BATCH) as ex:
        for _i in range(0, len(paths), _BATCH):
            _chunk = paths[_i:_i + _BATCH]
            _got = list(ex.map(_one, _chunk))
            _tried += len(_chunk)
            _ok += sum(1 for _t in _got if _t)
            for _p, _t in zip(_chunk, _got):
                if _t:
                    out[_p] = _t
            if _tried >= 32 and _tried < len(paths) and _ok * 2 < _tried:
                _FETCH_ABORTED[0] = (
                    "取数失败过半（%d 个中仅 %d 个成功），已中止剩余 %d 个分片"
                    % (_tried, _ok, len(paths) - _tried))
                break
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


def _index_reason() -> str:
    """索引为什么没被用上 —— 回给调用方，别再让人靠猜。

    2026-10-23 实测踩过：线上 MCP 一次「面馆」查询 `shards_scanned=320`（全量扫），
    但返回体只有 `via_index:false` 一个布尔，看不出是「索引没部署」「版本不符」
    还是「落后于数据」。排查要靠翻代码 —— 这个字段就是为这件事加的。
    """
    meta = _index_meta()
    if not meta:
        return ("index_meta_unavailable（skills/registry/index/meta.json 读不到："
                "未部署 / 401 / 网络不通）")
    if int(meta.get("version", 0)) != INDEX_VERSION:
        return "index_version_mismatch（索引 v%s，客户端要 v%d）" % (
            meta.get("version"), INDEX_VERSION)
    try:
        tot = sum(s.get("k", 0) for s in _shards_of_type("fp"))
        rec = int(meta.get("records", -1))
    except Exception:
        return "index_meta_malformed"
    if not (rec >= tot > 0):
        return "index_behind_data（索引 %d 条 < 数据 %d 条，需重跑 gen_search_index）" % (rec, tot)
    return "index_ok"       # 索引本身没问题 → 是取候选分片那一步失败了


def _bucket_rel(term: str, buckets: int) -> str:
    b = int(hashlib.sha1(term.encode("utf-8")).hexdigest(), 16) % buckets
    return "%s/terms/b%04d.json" % (INDEX_DIR, b)


# --- 自由文本需求 -> 可靠产品词：过滤 + 加权 -------------------------------- #
# df < MIN 的 bigram 几乎必是分词碎片（「送线」df=0），不作证据。
# 泛词（城市名/企业通名）已由 `_usable_gram` 拦掉，故不另设 df 上限。
_RECALL_DF_MIN = 2

# --- 候选分片硬上限（保险丝，不是性能方案）--------------------------------- #
# 老实现是「并集 >= 60% 分片就 return None → 调用方全量扫」。那个阈值是**相对量**，
# 分片数一涨就必然触发（282 片时不会，8,448 片时任何中频词都会）—— 而触发代价是
# 把整库拉下来。实测（scripts/audit_recall_cost.py，24 条真实查询，467 片）：
# 最大 128 片（「加工」27.4%），60% 阈值一次都没撞上；但**索引路径本身**就能拉到
# 13~16 MB，因为倒排粒度是分片级，不是行级。这件事只能靠行级倒排解决（见
# docs/两步走落地方案.md 的 P2），本上限只负责让「最坏情况」有界：
# 宁可少拉几片并如实上报，也绝不静默全量。
# 400 的取值：当前 467 片的 85%，给「数据翻倍到 ~900 片」留出不误伤的余量。
_RECALL_SHARD_CAP = 400

# 本进程最近一次召回是否被截断。用 threading.local：工具调用是并发处理的，
# 用模块级字典会让 A 线程的截断标记串到 B 线程的回执里（这个坑 `_HTTP_LAST_ERROR`
# 已经踩过一次）。
_recall_tls = threading.local()


def _recall_trunc_note() -> Dict[str, Any]:
    """当前线程最近一次召回的截断回执；没截断返回空 dict。"""
    return dict(getattr(_recall_tls, "trunc", {}) or {})


def _cap_shards(picked, weights: Dict[str, float], where: str) -> List[str]:
    """候选分片超过 `_RECALL_SHARD_CAP` 时按命中密度截断，并记下截断事实。

    按权重（`idf × 该片命中数`）降序，而不是按路径字典序：密度高的片更可能给出
    相关记录，按路径截断等于随机丢弃 —— 撞「真实企业必须被看见」的红线。

    返回排序后的路径列表；截断与否都返回，调用方**不需要**分支。
    """
    _recall_tls.trunc = {}
    paths = sorted(picked)
    if len(paths) <= _RECALL_SHARD_CAP:
        return paths
    ranked = sorted(paths, key=lambda p: (-weights.get(p, 0.0), p))
    kept = sorted(ranked[:_RECALL_SHARD_CAP])
    _recall_tls.trunc = {
        "where": where,
        "cap": _RECALL_SHARD_CAP,
        "shards_total": len(paths),
        "shards_kept": len(kept),
        "shards_dropped": len(paths) - len(kept),
        "note": ("候选分片超过上限，已按「词在该片里的命中密度」保留前 "
                 f"{_RECALL_SHARD_CAP} 片。本次结果可能不完整（被丢弃的片里若有"
                 "相关企业不会出现）。这是**有界截断**，不是全量扫描。"),
    }
    return kept


# 以功能字开头/结尾、或以企业通名结尾的 bigram 不是产品词（「的输」「线厂」「厂家」）。
_FUNC_CHARS = set("的了要找做想帮我你在和与或及是有能可会请给把被用让需个些这那们吧呢吗来去上下就到从对为")
_ORG_SUFFIX = set("厂家司部行店商社")
_city_names_cache: Optional[set] = None
_province_map_cache: Optional[dict] = None


def _city_names() -> set:
    """预构建索引里出现过的城市名集合：地区词不作为相关性证据（只作筛选偏好）。

    ⚠ 取数失败**绝不可**把空集写进缓存（2026-10-24 修）。原实现 `if txt:` 不成立时
    仍然写 `(set(), ...)`，而 `_cache_stale` 只认 `value is None`，于是**一次**网络
    抖动/401 就会让这个进程此后**所有**查询都拿不到城市表：
    城市抽不出来 → 「贵阳肠旺面」不再拆成 city=贵阳 + 关键词=肠旺面，整句被当成
    一个 token → 候选分片从 4 个掉到 1 个、结果从 21 条掉到 3 条，且**没有任何报错**。
    实测复现过：同一台机器同一份代码，冷启动首次查询正常、偶发一次就只有 3 条。
    """
    global _city_names_cache
    if _cache_stale(_city_names_cache, INDEX_DIR + "/city.json"):
        txt = _index_text(INDEX_DIR + "/city.json")
        if txt:
            try:
                names = set(json.loads(txt).keys())
            except Exception:
                names = None      # 半成品/损坏 → 不缓存，下次重取
            if names:
                _city_names_cache = (names, time.time(),
                                     _wt_mtime(INDEX_DIR + "/city.json"))
    return _city_names_cache[0] if _city_names_cache else set()


def _province_map() -> Dict[str, Any]:
    """「省名单 + 城市→省 反查表」（index/province-city.json，由 gen_region_index 生成）。

    为什么需要它：指纹分片里只有 `city` / `dist`，**没有 province**，所以「贵州的
    面馆」无法在记录层直接判省；而 city.json 只收有数据的城市（贵州仅 贵阳/遵义），
    拿它展开省份会漏掉全省绝大多数记录。反过来，用「城市 → 省」反查就能在过滤记录时
    就地判省，不必新增一遍全国扫描。

    与 `_city_names` 同样的坑：取数失败**绝不能**把空表写进缓存，否则一次网络抖动
    会让此后所有省级查询都退化成「把省名当关键词 AND」，结果是**静默 0 条**。
    """
    global _province_map_cache
    rel = INDEX_DIR + "/province-city.json"
    if _cache_stale(_province_map_cache, rel):
        txt = _index_text(rel)
        if txt:
            try:
                d = json.loads(txt)
                provs = {str(p) for p in (d.get("provinces") or []) if p}
                c2p = {str(k): str(v) for k, v in (d.get("city2prov") or {}).items()}
                val = {"provinces": provs, "city2prov": c2p}
            except Exception:
                val = None          # 半成品 → 不缓存
            if val:
                _province_map_cache = (val, time.time(), _wt_mtime(rel))
    return _province_map_cache[0] if _province_map_cache else {"provinces": set(),
                                                               "city2prov": {}}


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
    与 `_gram_idf` 阶段剔除，故这里只做并集。

    返回 None **只在真的召不回来时**（索引不可用、无采信词）。并集过大不再回退全量：
    改为按「idf × 片内命中数」截断到 `_RECALL_SHARD_CAP`，并把截断事实写进
    `_recall_trunc_note()`，由 tool 回执如实告知调用方。
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
    weights: Dict[str, float] = {}
    for g in toks:
        if not g:
            continue
        d = _bucket_df(g, buckets)
        if d < _RECALL_DF_MIN:   # 碎片 -> 不作锚
            continue
        iw = 1.0 / (1.0 + math.log(d))          # 与 _gram_idf 同口径
        for p, n in _bucket_postings(g, buckets).items():
            if p not in known:
                continue
            picked.add(p)
            try:
                weights[p] = weights.get(p, 0.0) + iw * float(n)
            except Exception:
                weights[p] = weights.get(p, 0.0) + iw
    if not picked:
        return None
    return _records_from_paths(_cap_shards(picked, weights, "recall_candidates"))



def _city_postings(city: str) -> Optional[Dict[str, int]]:
    """某个城市的 {分片路径: 条数}。索引不可用 → None；确无该城市 → {}。

    先读 `index/city/<城市>.json`（几 KB），读不到才退回 `index/city.json`（700 KB 全表）。
    拆分的理由：一次查询只关心一个城市，却要为全国 527 个城市付流量 ——
    那是冷启动首查的主要成本。旧索引没有单城文件，所以全表兜底保留。

    城市名做**后缀无关**匹配（「姑苏」/「姑苏区」），避免索引写法与用户输入不一致时
    把「有」误判成「无」（返回空结果比返回慢结果更糟）。
    """
    if not city:
        return {}
    safe = city.replace("/", "").replace("\\", "")
    err_before = _HTTP_LAST_ERROR[0]          # 记住进入前的错误，供兜底成功后撤销
    txt = _index_text(INDEX_DIR + "/city/" + safe + ".json")
    if txt:
        try:
            d = json.loads(txt)
            if isinstance(d, dict):
                return {str(k): int(v) for k, v in d.items()}
        except Exception:
            pass
    txt = _index_text(INDEX_DIR + "/city.json")
    if not txt:
        return None
    # 走到这里说明**全表兜底取到了**。单城文件只是「省流量」的可选快路径，
    # 它失败（4xx / 编码错误 / 该城市无文件）不影响结果完整性 —— 所以不能让它
    # 冒到 `fetch_error` 里。实测代价：0 条结果时返回体夹一条「请求头编码错误」，
    # 调用方会判定「工具坏了」，而真实结论只是「库里确实没有这家企业」。
    # 只撤销**本函数内新产生**的错误，外层（manifest / 分片哈希不符）照旧保留。
    if _HTTP_LAST_ERROR[0] != err_before:
        _HTTP_LAST_ERROR[0] = err_before
    try:
        full = json.loads(txt)
    except Exception:
        return None
    m = full.get(city)
    if not m:
        alt = _norm_district(city)
        for k, v in full.items():
            if _norm_district(k) == alt:
                m = v
                break
    return {str(k): int(v) for k, v in (m or {}).items()}


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
        postings = _city_postings(city)
        if postings is None:
            return None
        if not postings:               # 索引里没这个城市 → 确无命中
            return []
        cands = set(postings.keys())

    # 每片的「词命中密度」累加：只用于超上限时的截断排序（见 `_cap_shards`）。
    wacc: Dict[str, float] = {}
    for gs in gram_groups:
        tset: Optional[set] = None
        for g in gs:
            d = docs.get(_bucket_rel(g, buckets))
            m = d.get(g) if isinstance(d, dict) else None
            s = set(m.keys()) if m else set()   # 桶里没这个词 → 确无命中
            if m:
                for _p, _n in m.items():
                    try:
                        wacc[_p] = wacc.get(_p, 0.0) + float(_n)
                    except Exception:
                        wacc[_p] = wacc.get(_p, 0.0) + 1.0
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
    picked = {p for p in cands if p in known}
    if not picked:
        return []
    return _cap_shards(picked, wacc, "candidate_shards")


def _records_from_paths(paths: List[str]) -> List[Dict[str, Any]]:
    """批量读取若干 fp 分片的记录，并在进程内按路径缓存（重复查询近乎零成本）。

    缓存按本地副本 mtime 判新（见 _wt_mtime）：派生重建 重建 fp 分片后，对应分片 mtime
    变化即重读，无需重启；非本地副本模式保持旧行为（按路径存在性判新）。
    """
    out: List[Dict[str, Any]] = []
    if not paths:
        return out
    todo: List[str] = []
    _seen_path: set = set()
    for p in paths:
        if p in _seen_path:      # 同一次调用里重复的路径只读一次
            continue
        _seen_path.add(p)
        m = _wt_mtime(p)
        slot = _shard_rec_cache.get(p)
        if slot is not None and (m is None or slot[0] == m):
            continue
        todo.append(p)
        # ⚠ 这里**不能**先写 `(m, [])` 占位（2026-10-24 修）。
        #   tool 调用改成并发处理后（见 main()），占位会被**另一个线程**当成
        #   「已缓存、内容为空」跳过 → 该分片的记录整体丢失。症状是并发跑两个
        #   检索时结果变少（实测「贵阳肠旺面」21 条 → 3 条），而串行时永远正常，
        #   极难复现。去掉占位后：读失败也不会把空结果写进缓存（下次还会重试），
        #   代价只是并发撞同一分片时多拉一次。另外下方 `_read_many_text` 本身
        #   已按内容哈希缓存，重复拉取通常连网络都不走。
        # 缓存只在**读成功之后**写入（见下方赋值处）。
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


def _norm_district(s: str) -> str:
    """区县比较用的归一化键：去掉末尾一个 区/县/市。

    数据层已经在入库时统一成短写法（scripts/normalize_district.py），这里再兜一层，
    因为：① 存量分片可能还没重生成；② 用户输入的是「吴中区」这种官方全名，
    而记录里是「吴中」。等值比较会让这两种写法互相看不见 —— 数据是全的，
    用户却看到半个城市。归一化后「吴中」==「吴中区」，「浦东新」==「浦东新区」。
    """
    s = (s or "").strip()
    return s[:-1] if len(s) > 2 and s[-1] in "区县市" else s


def _city_ok(rec: Dict[str, Any], city: str, province: str = "") -> bool:
    """city 匹配「地级市 或 区县」：县级市（昆山/海盐…）在地图数据里归到地级市名下，
    记录里 city=苏州、dist=昆山。只比 city 的话查「昆山」永远 0 条。

    区县侧做**后缀无关**比较（见 `_norm_district`），
    所以「姑苏」「姑苏区」「嘉善」「嘉善县」都能命中同一批记录。

    `province`：省级过滤。指纹分片没有 province 字段，只能用「记录的城市 →
    省」反查（index/province-city.json）。取不到映射时**放行**（宁可多给不可错杀：
    滤错是静默 0 条，比多几条严重得多）。
    """
    if province and not _province_ok(rec, province):
        return False
    if not city:
        return True
    if city == rec.get("city", ""):
        return True
    return _norm_district(city) == _norm_district(rec.get("dist", ""))


def _province_ok(rec: Dict[str, Any], province: str) -> bool:
    """记录是否属于该省：按 city（没有则 dist）反查 province-city.json。"""
    if not province:
        return True
    m = _province_map().get("city2prov") or {}
    if not m:
        return True           # 表没加载成功 → 不过滤
    want = province.strip()
    for key in ("city", "dist"):
        c = str(rec.get(key) or "").strip()
        if not c:
            continue
        p = m.get(c)
        if p is None:
            # 记录里可能写全称（贵阳市/贵州省），或反过来只有简称
            for cand in (c + "市", c[: -1] if c.endswith("市") else c):
                p = m.get(cand)
                if p:
                    break
        if p:
            return p.strip() == want
    return True               # 判不出来 → 放行，避免误杀


OFFSET_MAX = 200   # 分页深度上限（10 页 × 20 条）。服务端 X-Beacon-Offset-Max 同值。


def _page_slice(matches: list, offset: int, limit: int, raw_offset: int):
    """分页切片 + 越界说明。三处 return 共用，避免各写一遍导致口径不一致。"""
    page = matches[offset:offset + limit]
    if raw_offset <= OFFSET_MAX:
        return page, {}
    return page, {"offset_clamped": {
        "requested": raw_offset, "max": OFFSET_MAX,
        "note": ("分页深度上限 %d 条。要更完整的结果请**收窄检索条件**"
                 "（加城市 / 国标码 / 能力词），而不是加大 offset。" % OFFSET_MAX)}}


def search_vendors(query: str = "", city: str = "", gb: str = "",
                   limit: int = 20, offset: int = 0,
                   group_branches: bool = True, etype: str = "",
                   lat: Optional[float] = None, lng: Optional[float] = None,
                   radius_km: Optional[float] = None) -> Dict[str, Any]:
    """按关键词 / 城市 / 国标码 / 坐标检索全国各行业主体。

    `group_branches`：同一品牌的连锁门店是否折成一行（默认折）。连锁在名录里是一家一条，
    不折的话「面馆」这类查询会被门店淹没（苏州瑞幸一家就 267 条）。
    要看全部门店时传 false。

    `etype`：主体经济类型过滤 —— 生产 / 流通 / 服务。按国标门类推导：
       生产 = A 农林牧渔、B 采矿、C 制造、D 电力热力燃气水、E 建筑
       流通 = F 批发零售、G 交通运输仓储邮政
       服务 = H 住宿餐饮 … S 公共管理
    「找能做的厂」用生产，「找附近的店/服务商」用服务，别用「非制造商」反向排除 ——
    那会把六成主体混成一个筐。

    `lat`/`lng`/`radius_km`：坐标检索（2026-10-23 接入，数据来自指纹层 98.5% 覆盖）。
    地标名（「青创港」）→ 坐标由**调用方**解析（MCP 不持地图 API）；GCJ-02/WGS-84
    混用无碍（偏差远小于公里级半径）。语义：
      - 只传 lat/lng：结果档内按球面距离升序（无坐标记录排末位，distance_km=null）；
      - 加 radius_km：圈外与无坐标记录剔除（无法证明在圈内=不在圈内），
        total_matched 变为圈内数，被剔除数见 geo_filtered_out；
      - 连锁品牌行报**最近门店**距离（_collapse_brands）。
    """
    limit = max(1, min(int(limit), 200))
    # etype 过滤：中文名 → 指纹 et 码。传了不认识的值不静默忽略，见下方校验。
    et_want = {"生产": 1, "流通": 2, "服务": 3}.get((etype or "").strip(), 0)
    if (etype or "").strip() and not et_want:
        return {
            "error": "bad_etype",
            "message": "etype 只接受 生产 / 流通 / 服务 三个值（按国标门类推导），收到 %r" % etype,
        }
    lat_v, lng_v, rad, geo_err = _geo_params(lat, lng, radius_km)
    if geo_err:
        return geo_err
    # ── 分页深度上限（2026-10-22）────────────────────────────────────────
    # 这不是性能优化，是**反截取**。翻页遍历是从公开接口搬走整个库的唯一
    # 可行路径：`total_matched=4684` 时 offset += 200 循环 24 次就拉干了。
    # 而真实用户翻不到第 11 页 —— 他要的是「找几家能做的」，不是「第 4000 条是谁」。
    # ⚠ 这条只是体验层的一道闸：search 本身在客户端本地过滤，改客户端即可绕过。
    #   真正的防线是服务端的数据面鉴权（pages/_worker.js 的 quotaGuard）。
    raw_offset = max(0, int(offset))
    offset = min(raw_offset, OFFSET_MAX)
    # 固定索引并发预热（2026-10-30）：放在任何取数之前，把 9 个串行 RTT
    # 压成一批。见 `_WARM_INDEX_PATHS` 上方的时间线实测。
    _warm_indexes()
    q = (query or "").strip().lower()
    # 原话归一化：用户输入的是**一句人话**，不是空格分隔的关键词表。
    # 归一化把它切成可执行的检索参数，并顺带把原话里的城市抽成 city 过滤条件。
    # 详见 `_normalize_query`。
    norm = _normalize_query(query, city) if q else None
    province = ""          # 省级过滤（省名不能当关键词，见 _normalize_query ②bis）
    if norm:
        city = norm["city"] or city
        province = norm.get("province") or ""
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
    if norm and (norm["city_from_query"] or norm.get("province") or norm["segments"]
                 or norm.get("token_fixed")
                 or tokens != [t for t in q.split() if t]):
        parsed["query_parsed"] = {
            "city": city or "",
            "city_from_query": norm["city_from_query"] or "",
            "province": province or "",
            "keywords": gen_tokens,
            "recognized": norm["segments"],
        }
        # 业态后缀被剥过才回显：调用方需要知道「牛肉粉店」实际是按「牛肉粉」检索的，
        # 否则会以为召回变宽是数据变了。
        if norm.get("token_fixed"):
            parsed["query_parsed"]["token_fixed"] = norm["token_fixed"]

    # 给了国标码：只扫对应分片（最快路径，不构建全量索引）
    if gb:
        fp_shards = [s for s in _shards_of_type("fp") if s.get("c") == gb]
        scanned = 0
        matches: List[Dict[str, Any]] = []
        # 并发预取再逐片判定（2026-10-30 实测）：这是首查最大的单点。
        # 逐片串行时 34 片 × 1.5~2.5s ≈ 51s（一次「苏州 注塑」冷查 41s，
        # 并发度只有 1.69）。_fetch_many 带 12 路并发，且已缓存的分片直接命中
        # 磁盘缓存，不产生请求。
        if len(fp_shards) > 1:
            try:
                _fetch_many([s.get("p") for s in fp_shards])
            except Exception:
                pass
        for s in fp_shards:
            scanned += 1
            for rec in _read_fp_shard(s):
                if not _city_ok(rec, city, province):
                    continue
                if et_want and (rec.get("et") or 0) != et_want:
                    continue
                if gen_tokens and not all(tok in _hay(rec).lower() for tok in gen_tokens):
                    continue
                s = _rec_summary(rec)
                s["via"] = "gb"
                matches.append(s)
        # 与 query 路由同一套档内排序（`gb=` 只走一档，故按厂名贴合度降序即可），
        # 否则同一批记录经两条路进来顺序不同，agent 会以为「换了个写法结果就变了」。
        # 2026-10-23：传了坐标时距离优先于 name_fit（「附近」是显式意图），
        # 无坐标记录排末位；档间次序该路径只有一档，无分档问题。
        if lat_v is not None:
            matches, geo_dropped, geo_ncoord = _geo_apply(matches, lat_v, lng_v, rad)
            matches.sort(key=lambda x: (_geo_rank_key(x), -_name_fit(x, gen_tokens),
                                        str(x.get("id") or "")))
        else:
            geo_dropped = 0
            geo_ncoord = 0
        # 品牌归并放在**分页之前**：否则一页 20 条里可能 9 条是同一条街上的门店，
        # 而第 21 条才是另一个品牌 —— 分页之后再折叠等于白折。
        records_total = len(matches)
        matches, brands_total = _collapse_brands(matches, keep_all=not group_branches)
        # 同 query 路由：折行后按距离稳定重排（品牌行距离折行时才换成最近门店，
        # 位置会停在代表店那）。等距组内保持 name_fit/id 序。
        if lat_v is not None:
            matches.sort(key=_geo_rank_key)
        page, pmeta = _page_slice(matches, offset, limit, raw_offset)
        out = {
            "total_matched": records_total,
            "total_brands": brands_total,
            "returned": len(page),
            "shards_scanned": scanned,
            "results": page,
            **parsed,
            **pmeta,
            # 同 query 路由的口径：档内按「关键词在厂名里成片出现」优先，score 不是相关度。
            "order_note": ("results 按「关键词在企业名里成片出现」优先排列；同一品牌的连锁门店"
                           "已折成一行（`branches` = 门店数，`branch_ids` 前 20 个）。"
                           "score 是**能力画像分**（有已发布能力卡才有分，无卡为 0），"
                           "**不是相关度**，不要拿它反推排序。")
                          + (_geo_note(rad) if lat_v is not None else ""),
            # 坐标回显：让 agent 能核对「服务端确实按我给的坐标排了」。
            **({"geo": {"lat": lat_v, "lng": lng_v, "radius_km": rad,
                        "records_with_coords": geo_ncoord}} if lat_v is not None else {}),
            **({"geo_filtered_out": geo_dropped} if geo_dropped else {}),
        }
        return _attach_routing_hint(out, q, city)

    # 未给国标码：优先走「预构建索引 → 只拉命中分片」。
    # 索引缺失或陈旧时回退进程内全量索引（与命中量相关，慢但结果等价）。
    cands = _candidate_shards(gen_tokens, city) if (gen_tokens or city) else None
    via_index = cands is not None
    recall_trunc = _recall_trunc_note()

    if via_index and not cands and not alias_paths and not cap_hits:
        # 索引明确判定无命中、别名也没给方向 —— 无需拉任何分片
        return _attach_routing_hint({
            "total_matched": 0,
            "returned": 0,
            "shards_scanned": 0,
            "via_index": True,
            "results": [],
            **_fetch_warn(0),   # 索引桶取数失败也会让候选为空，别把故障说成「没有」
            **parsed,
        }, q, city)

    matches: List[Dict[str, Any]] = []
    seen: set = set()

    def _collect(recs, relax_tokens: bool = False) -> None:
        for rec in recs:
            if not _city_ok(rec, city, province):
                continue
            if et_want and (rec.get("et") or 0) != et_want:
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
            # 能力命中压过纯字面撞名（2026-10-23）。
            # 查询词是能力别名时（「医院」「银行」「药店」「煤矿」），**主体本身
            # 就是干这个的**比**名字里恰好有这个词**相关得多。只靠 name_fit 分不开：
            # 「贵黔医院负一楼营养餐厅」（gb 6210，医院开的食堂）品牌名里也有
            # 「医院」，与「贵州省人民医院」同名次同名分，照样排在前面。
            # 所以这里显式把 cap 含该键的记录提到最高档，与下方 cap 定向召回同档。
            if cap_hits and rec.get("cap"):
                want = {h["cap"] for h in cap_hits}
                if want & {str(c) for c in rec["cap"]}:
                    s["via"] = "cap"
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
    # ⚠ 一次批量读，不要逐片调（2026-10-30 实测）。
    #   原来是 `for p in alias_paths: for rec in _records_from_paths([p])`——
    #   每次只传 1 个路径，`_read_many_text` 的并发完全用不上，34 个别名分片
    #   就串行 34 × 1.5s ≈ 51s。这正是「苏州 注塑」冷启动 41s 的主因
    #   （整次检索的实测并发度只有 1.69）。批量后同批 12 路并发。
    #   `alias_scanned` 仍按**记录数**计，与改前口径一致。
    for rec in _records_from_paths(alias_paths or []):
        alias_scanned += 1
        if not _city_ok(rec, city, province):
            continue
        # etype 必须在这里也筛一遍：别名召回**绕过了 `_collect`**，
        # 漏这一步就会出现「传 etype=生产，返回的全是流通主体」
        # ——总数按 etype 变了、行却没变，比不筛更难发现（2026-10-23 修）。
        if et_want and (rec.get("et") or 0) != et_want:
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
    # 同上的串行问题：内层每片单独一次 `_records_from_paths([p])`。
    # 这里不动结构，只在前面并发预取一遍 —— 预取走后仍命中同一缓存层，
    # 逐片逻辑与顺序完全不变，风险最小。
    _cap_all_paths = [p for h in cap_hits for p in _cap_shard_paths(h["cap"])]
    if len(_cap_all_paths) > 1:
        try:
            _fetch_many(_cap_all_paths)
        except Exception:
            pass
    for h in cap_hits:
        for p in _cap_shard_paths(h["cap"]):
            for rec in _records_from_paths([p]):
                cap_scanned += 1
                # 该分片可能含多条记录，只收真正带此能力键的（cap.json 的 shards
                # 表只是「哪些分片含此 cap」，分片内还需按 cap 成员过滤）。
                if h["cap"] not in (rec.get("cap") or []):
                    continue
                if not _city_ok(rec, city, province):
                    continue
                if et_want and (rec.get("et") or 0) != et_want:
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

    # 分档排序（稳定）：cap → text → cap_text → alias。
    # 为什么必须显式排：cap_text 与 text 是**同一趟**文本召回里 append 进去的，
    # 不重排两档会交错，order_note 里「按召回来源分档排列」就成了假承诺。
    #
    # 2026-10-23：cap 从末档提到首档。原口径 text 最高，制造业时代成立（厂名写了
    # 「CNC」的大概率真是 CNC 厂）；全行业口径下不成立 —— 搜「医院」时前 10 条
    # 全是「蓝天酒店(贵州医科大学附属医院店)」「贵黔医院负一楼营养餐厅」，
    # 真正的医院在第 23 位之后。查询词是能力别名时，**主体本身干这个** > **名字里
    # 有这个词**，这一条跨门类通用（药店/银行/物流/煤矿同此）。
    # cap 与 text 同名次时仍按 name_fit 细分（品牌名命中 > 仅门店限定命中）。
    _RANK = {"cap": 0, "text": 1, "city": 1, "cap_text": 2, "alias": 3}

    # 坐标过滤与距离计算（2026-10-23）：分页之前，与 _collapse_brands 同理。
    # radius 模式下圈外与无坐标记录在这里就已剔除，后续所有计数（total_matched /
    # total_brands / 分页）都是圈内口径；被剔除数单独走 geo_filtered_out，
    # 让 agent 能对用户说清「全城有 176 条、圈内 3 条」而不是只说「有 3 条」。
    matches, geo_dropped, geo_ncoord = _geo_apply(matches, lat_v, lng_v, rad)

    # 档内再按厂名贴合度排（见 `_name_fit` 的说明）。这是**档内**微调，不影响档间
    # 次序，也不影响 total_matched。末尾按 id 收尾，保证同档同分的顺序稳定
    #（否则等于按分片路径排，H 住宿餐饮恒在 Q 卫生前面）。
    # 2026-10-23：坐标查询两种语义，排序键不同（这是刻意的，不是疏漏）——
    #   ① 只传 lat/lng（无 radius）：意图 =「离我最近的先看」→ **距离第一键**，
    #      档序降为次级（等距时才按召回纯度分高下）。否则「便利店」会出现
    #      15km 的 cap 档压在 10.8km 的 text 档前面，用户只会当 bug。
    #   ② 传 radius_km：圈已经限定候选，纯度优先、档内距离 —— 圈内都是
    #      「能用的」，先看最对口的。
    # 无坐标查询时距离键恒等，排序退化为原有 (_RANK, name_fit, id)，零影响。
    if lat_v is not None and rad is None:
        def _geo_sort(x):
            return (_geo_rank_key(x), _RANK.get(x.get("via"), 9),
                    -_name_fit(x, gen_tokens), str(x.get("id") or ""))
    else:
        def _geo_sort(x):
            return (_RANK.get(x.get("via"), 9), _geo_rank_key(x),
                    -_name_fit(x, gen_tokens), str(x.get("id") or ""))
    matches.sort(key=_geo_sort)

    # 品牌归并（分页之前，理由见 `_collapse_brands`）。
    records_total = len(matches)
    matches, brands_total = _collapse_brands(matches, keep_all=not group_branches)
    # 折行后按距离稳定重排（2026-10-23）：品牌行的 distance_km 是在折行时才换成
    # 「最近门店」的，位置却停在代表店那里 —— 不重排就会出现「排第 3 的品牌比
    # 排第 1 的还近」，用户只会把它当 bug。键序与折行前一致（无 radius 距离
    # 第一、有 radius 档序第一）；stable + 等距组内保持折行前序（name_fit/id）。
    # 无坐标查询时距离键恒等，排序结果与不传坐标完全一致（存量零影响）。
    if lat_v is not None:
        if rad is None:
            matches.sort(key=lambda x: (_geo_rank_key(x), _RANK.get(x.get("via"), 9)))
        else:
            matches.sort(key=lambda x: (_RANK.get(x.get("via"), 9), _geo_rank_key(x)))
    page, pmeta = _page_slice(matches, offset, limit, raw_offset)
    out = {
        "total_matched": records_total,
        "total_brands": brands_total,
        "returned": len(page),
        "shards_scanned": scanned,
        "via_index": via_index,
        **({"via_index_reason": _index_reason()} if not via_index else {}),
        # 召回被截断时如实上报（见 _cap_shards）：结果可能不完整，不能装作完整。
        **({"recall_truncated": recall_trunc} if recall_trunc else {}),
        # 全量兜底被时间闸/条数闸截断时同样要如实上报（见 _build_fp_index）：
        # 走兜底路径时结果天然不完整，不说清楚就等于让调用方以为「库里就这些」。
        **({"scan_truncated": _fp_trunc_note()} if _fp_trunc_note() else {}),
        # 0 条 ≠ 没有这家企业（2026-10-24）：取数整体失败时也是 0 条，而返回体
        # 原本没有任何字段能区分两者 —— 用户会得出「数据里没有」的错误结论。
        **_fetch_warn(records_total),
        "results": page,
        **parsed,
        **pmeta,
        # 结果顺序是**有意设计的分档**，不是随机的：按召回纯度递减排列。
        # 而 `score` 是**能力画像分**（有已发布能力卡才有分，无卡为 0），**不是相关度**。
        # 两者一起看极易误判，
        # 所以在这里把口径写死，让 agent 自己能分辨「谁更值得先看」。
        "order_note": ("results 按召回来源分档排列：via=cap(能力定向：主体本身干这个) → "
                       "via=text(厂名/工艺/材料等**字面**命中) → "
                       "via=cap_text(厂名里其实没这个词，只是撞上了能力键的中文词面) → "
                       "via=alias(别名定向)；**同一档内**再按「关键词在"
                       "厂名里成片出现」优先（所以「商务酒店」会把 龙翔商务酒店 排在 "
                       "某酒店(中央商务区店) 前面）；score 是**能力画像分**"
                       "（有已发布能力卡才有分，无卡为 0），**不是相关度**，不要拿它反推排序。")
                       + (_geo_note(rad) if lat_v is not None else ""),
        # 行业证据：search 只返回指纹摘要，拿不到完整的 industry.source。
        # 但 agent 很容易把 results[].gb 当成「核实过的行业」直接讲给客户 ——
        # 全库 28.5% 的归类其实是按采集关键词反推的。把口径写死在这里，
        # 和 order_note 同一立场：别让调用方猜。
        "industry_note": ("results[].gb 是这家**归档所在的国标小类**，不等于已核实的行业。"
                          "要拿行业下结论前，先 get_vendor 看 industry_evidence.tier："
                          "A=工商登记/人工认证、B=公司名推断、C=词表兜底、D=采集关键词反推。"
                          "C/D 级别的行业只能当线索，向用户表述时应带上「行业待核」。"),
        # 坐标回显 + 圈外剔除数（仅坐标查询时出现）。
        **({"geo": {"lat": lat_v, "lng": lng_v, "radius_km": rad,
                    "records_with_coords": geo_ncoord}} if lat_v is not None else {}),
        **({"geo_filtered_out": geo_dropped} if geo_dropped else {}),
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


# ── 行业归类的证据等级（2026-10-24）──────────────────────────────────
# 全库 210,555 条里只有 34 条的行业有权威依据（工商登记 / 人工认证），
# 98.4% 是推断的。但检索结果里这两者长得一模一样 —— agent 会把
# 「用采集关键词反推出来的码」当成「确认过的行业」讲给客户。
# 那比答"没有"伤害更大：答"没有"用户会自己去找，答错了用户会拿它做采购决定。
#
# 所以给每条记录附一个等级，让调用方自己决定信到什么程度。
# 等级**完全由 industry.source 推导、不落盘**：source 一变等级自动跟着变，
# 不会出现「数据里存着旧等级、source 已经改过」的自相矛盾。
#
#   A 权威  工商登记 / 人工认证
#   B 名称  公司名推断（"苏州某某精密机械有限公司" → 通用设备制造）
#   C 兜底  国标特征词 / POI 标签 / 原品类
#   D 采集  按抓取时的搜索词反推 —— 最弱的一档，占全库 28.5%
_EVIDENCE_TIER = {
    "gov_registry": ("A", "工商登记"),
    "registration": ("A", "工商登记"),
    "certification": ("A", "人工认证"),
    "name": ("B", "公司名称推断"),
    "gb_token": ("C", "国标特征词匹配"),
    "keyword": ("C", "POI 标签词"),
    "category_fallback": ("C", "原品类兜底"),
    "company_website": ("C", "企业官网"),
    "search_keyword": ("D", "采集关键词反推"),
}
_EVIDENCE_CAVEAT = {
    "C": "行业依据较弱，可能只判到门类；可作线索，不宜当精确分类引用。",
    "D": "按采集时的搜索词反推，与这家实际做什么可能无关"
         "（当年用「软件开发」搜地图，整栋软件大厦里的公司都被打了 6513）。"
         "多结果并列时请降权，或向用户明示「行业待核」。",
}


def _industry_evidence(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 industry.source 折成可读的证据等级；没有 industry 就返回 None。"""
    ind = rec.get("industry")
    if not isinstance(ind, dict) or not ind.get("code"):
        return None
    src = (ind.get("source") or "").strip()
    tier, label = _EVIDENCE_TIER.get(src, ("C", src or "未知来源"))
    out: Dict[str, Any] = {
        "tier": tier,
        "label": label,
        "source": src,
        "confidence": ind.get("confidence"),
        "code": ind.get("code"),
        "name": ind.get("name"),
    }
    cav = _EVIDENCE_CAVEAT.get(tier)
    if cav:
        out["caveat"] = cav
    return out


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
    # 行业证据等级：只增不删的新字段，老调用方忽略即可（见 _EVIDENCE_TIER）
    ev = _industry_evidence(rec)
    if ev:
        out["industry_evidence"] = ev
    if not out.get("gb"):
        out["gb_unclassified"] = True
    return out


# ── get_vendor 解析结果缓存（2026-10-29）─────────────────────────────────────
# 实测（贵阳「牛肉粉」首条 Z3XMNP7Y，本地工作树）：不带 gb 1.61s、带 gb 1.03s；
# 同一个 id 连打 5 次是 979/980/981/967/970ms —— **零收敛**。
# 原因是这条路只缓存了分片的**原始文本**，每次都重做：
#   `_idmap_lookup`（~700ms，只在不给 gb 时）+ 命中片的 `json.loads`（~150-190ms/片，
#   zh 分片 5-7 MB / 5000 条）+ normalize。
# 而「点开一家详情」是最频繁的交互动作，1 秒/次直接落在用户感知上。
#
# 这里缓存**最终回执**，用清单 mtime 当全局签名：派生层任何重建都会重写
# data/manifest.json，所以一次 stat（_mtime_cache 2s 内复用）就能判定整份缓存
# 是否还对得上当前数据 —— 与 `_fp_index` 的 key 口径一致。
# 只缓存成功结果（有 vendor 的），错误不缓存（否则一次偶发 fetch_failed 会钉住）。
_VENDOR_CACHE: Dict[tuple, tuple] = {}   # (vid, gb) -> (resp, sig)
_VENDOR_CACHE_MAX = int(os.environ.get("BEACON_VENDOR_CACHE_MAX", "1024") or "0")

# get_vendor 走全量兜底时的**专属预算**（比通用 8s 更紧）：这条路径是用户正在
# 等着看「点开的那一家」，通用预算下 26.7s 的构建会原样砸到交互上。
_VENDOR_FP_DEADLINE = float(os.environ.get("BEACON_VENDOR_FP_DEADLINE", "5") or "0")


def _vendor_cache_sig() -> float:
    m = _wt_mtime("data/manifest.json")
    return m if m is not None else -1.0


def _vendor_cache_get(ck: tuple, sig: float) -> Optional[Dict[str, Any]]:
    hit = _VENDOR_CACHE.get(ck)
    if hit is None or hit[1] != sig:
        return None
    # 回执会被调用方整块塞进 MCP 响应，深拷贝防止上游改坏缓存本体。
    return copy.deepcopy(hit[0])


def _vendor_cache_put(ck: tuple, sig: float, resp: Dict[str, Any]) -> None:
    if not _VENDOR_CACHE_MAX or not isinstance(resp, dict) or "vendor" not in resp:
        return
    if len(_VENDOR_CACHE) >= _VENDOR_CACHE_MAX:
        # 简单 FIFO（dict 保序）：这不是热点数据集，LRU 的记账成本不划算。
        try:
            _VENDOR_CACHE.pop(next(iter(_VENDOR_CACHE)))
        except (StopIteration, KeyError):
            pass
    _VENDOR_CACHE[ck] = (resp, sig)


# `/v1/rec` 端点不存在时的静默期截止时间（unix ts）。见 _v1_rec 里的说明：
# 目的是「线上还没部署时，别让每条详情多付一个 404 往返」。
_REC_PROBE_UNTIL = [0.0]


def _v1_rec(vid: str) -> Optional[Dict[str, Any]]:
    """GET /v1/rec/<id> —— 向服务端按条取一条完整记录；取不到返回 None。

    为什么单开一条路：原先「看一家企业」= 定国标码 → 拉 2~3 个 zh 续片
    （合计 5~7MB）→ 本地 JSON 解析整片再线性扫。实测 get_vendor 冷调用
    2.4~3.2s，其中大头是分片解析。服务端有了按条寻址（v2 定长块 + R2 Range）
    以后，同一步是 1 个请求、约 1.1KB。

    **不允许它把详情链路搞挂**：任何异常 / 非 200 / 结构不符都退化成 None，
    调用方随即回落到原来的分片路径。这样在 v2 块尚未发布、或线上还是老
    worker 的机器上，行为与改动前**完全一致**，不会出现「升级 MCP 后详情全空」。

    自证：返回的 id 必须与请求的 id 相等。这不是多余的一步 —— 按条取依赖
    idmap 的 rowid 与 zh 块内的偏移，偏移错一位不会报错，只会返回**另一家
    企业**的记录（2026-10-30 那个 zh 块未排序的 bug 就是这个形态：返回的东西
    看起来完全正常）。这里做一次往返校验，代价为零。
    """
    if not vid:
        return None
    # 纯本地模式（REPO 存在）不走这里：那时其余数据都读本地副本，
    # 单为一条详情去联网既慢又会让「断网可用」失效。
    if REPO:
        return None
    # 端点还不存在时的静默期。
    #
    # 为什么必须有：线上 worker 没部署 /v1/rec 之前，每次 get_vendor 都会先付
    # 一个 404 往返。本机实测单次 RTT 0.6~2.9 秒，这等于**给每条详情加一秒**，
    # 而这一秒换不到任何东西 —— 是纯粹的回归。
    # 探测到端点不存在后安静 5 分钟：既不让每条详情付钱，又能在部署后
    # 自己恢复（不需要重启 MCP 子进程）。
    if time.time() < _REC_PROBE_UNTIL[0]:
        return None
    _st = [0]
    try:
        body, _etag = _http_get_once("/v1/rec/" + vid, _st)
        if body is None and _st[0] == 401:
            try:
                import identity as _idn
                _idn.ensure_token(force=True)
            except Exception:
                return None
            _st[0] = 0
            body, _etag = _http_get_once("/v1/rec/" + vid, _st)
    except Exception:
        return None
    if not body:
        # 404/405 = 这个端点还不存在（老 worker），不是「这条记录不存在」。
        # 只对这两种状态进静默期；其余（网络错误等）下次照常重试。
        if _st[0] in (404, 405, 501):
            _REC_PROBE_UNTIL[0] = time.time() + 300
        return None
    try:
        o = json.loads(body)
    except Exception:
        return None
    if not isinstance(o, dict) or o.get("error"):
        return None
    # 自证：返回的必须就是请求的那一条。
    #
    # ⚠ 比对两个字段（2026-10-30 线上实测）：v2 块按**内部 id** 定长索引，
    #   worker 原样透出的 `id` 是 `CN-MFG-0020882` 这种内部编号，而客户端手里
    #   的 id 是 token（`2BB5DR0Q`，与 search_vendors 返回的一致）。
    #   只比 `id` 会让**每一次**按条取都判为"不是同一条"→ 静默回落 → 这个
    #   优化在线上等于从未生效（而日志上看起来一切正常）。
    #   同时比 `token`：worker 回写的 token 就是请求的那个值。
    #   自证本身不能省 —— 偏移错一位不会报错，只会返回另一家企业。
    _got_id = str(o.get("id") or "")
    _got_tok = str(o.get("token") or "")
    if _got_id != vid and _got_tok != vid:
        return None
    return o


def _rec_gb(rec: Dict[str, Any]) -> str:
    """从一条 zh 记录里取国标码。顶层没有就取 industry.code。"""
    g = rec.get("gb")
    if g:
        return str(g)
    ind = rec.get("industry") or {}
    if isinstance(ind, dict):
        return str(ind.get("code") or "")
    return ""


def get_vendor(vid: str, gb: str = "") -> Dict[str, Any]:
    vid = (vid or "").strip()
    if not vid:
        return {"error": "缺少必填参数 id"}
    gb = (gb or "").strip()
    # 命中缓存就直接回，省掉 id 反查 + 分片解析（三次实测均为 970ms 量级）。
    _ck = (vid, gb)
    _sig = _vendor_cache_sig()
    _hit = _vendor_cache_get(_ck, _sig)
    if _hit is not None:
        return _hit
    # ⓿ 按条取（2026-10-30）：详情的主路径。
    #
    # 一次 HTTP 拿 1 条 ≈ 1.1KB，替代下面「定国标码 → 拉 2~3 个 zh 续片
    # （5~7MB）→ 解析整片 → 线性扫」。走不通就静默回落到原路径，
    # 因此这一步失败**不会**让详情不可用，只会慢回去。
    _fast = _v1_rec(vid)
    if _fast is not None:
        _rgb = _rec_gb(_fast)
        if _rgb and not _fast.get("gb"):
            # zh 记录常常不写顶层 gb（只留 industry.code），但它确实归档在这个
            # 国标码下。不回填就会出现「search 能搜到、get_vendor 说 gb=null」。
            _fast["gb"] = _rgb
            _fast["gb_source"] = "record"
        _resp = {"vendor": normalize_vendor(_fast)}
        _vendor_cache_put(_ck, _sig, _resp)
        return _resp
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
                    # 已并发，且进程内缓存；带专属预算，超时即用已解析的部分
                    # （否则这条兜底会把 26.7s 的全量构建原样砸到交互上）。
                    for rec in _build_fp_index(deadline_s=_VENDOR_FP_DEADLINE):
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
        missing = [p for p, t in zip(zh_paths, texts) if not t]
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
                    _resp = {"vendor": v}
                    _vendor_cache_put(_ck, _sig, _resp)
                    return _resp
        # ⚠ 「取不到分片」与「分片里没有这一条」是两件事，不能合并成一句
        #   「全部 zh 分片中均未找到」。2026-10-29 实测：一次并发取数里
        #   p2 分片返回 None（身份令牌刚过期、401 自愈与并发抢跑），结论直接
        #   变成"这家不存在"，而重试一次就正常 —— 这种话术会把排障带偏，
        #   也会让调用方对用户说"查无此企业"。
        if missing:
            return {
                "error": "fetch_failed",
                "message": f"国标码 {gb} 的分片有 {len(missing)}/{len(zh_paths)} 个没取到，"
                           "**这不代表没有这家企业**；本次未能在已取到的分片里命中。",
                "missing": missing[:3],
                "hint": "重试一次通常即可（并发取数时偶发身份令牌刷新）。持续失败请看 /data 可达性与身份。",
            }
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
                _resp = {"vendor": v}
                _vendor_cache_put(_ck, _sig, _resp)
                return _resp
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
        # 集合里的元素**就是发布面上的对外 id**：token 化发布之后是 8 字符
        # token，之前是内部编号的数字后缀。
        #
        # ⚠ 旧实现一律取 `vid.split("-")[-1]` 当"数字后缀"：token 里没有连字符，
        #   取出来还是它自己，`isdigit()` 直接为假 → **任何 token 都被判成"没卡"**，
        #   连一次请求都不发。2026-10-29 实测：token WE694FVV 明明
        #   `/skills/registry/capability/WE694FVV.json` 返回 200，MCP 却回
        #   has_card=false —— 客户端 Agent 由此彻底拿不到 L1。
        #   判据改为"形态即键"：token 形态按原值比，其余才退回数字后缀（兼容本地
        #   git 模式与未 token 化的老快照）。
        key = vid if _TOKEN_ID_RE.match(vid) else vid.split("-")[-1]
        if key not in want:
            return {
                "id": vid, "has_card": False,
                "note": "该供应商暂无已发布能力卡（全库仅 %d 家有卡）。"
                        "需要档案请先调 get_vendor。" % _card_count(),
            }
    rel = f"skills/registry/capability/{_vid_url(vid)}.json"
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
        "description": "检索灯塔名录里的**全国各行业主体**（按 GB/T 4754 全门类收录：农林牧渔、采矿、"
                       "制造、电热燃水、建筑、批发零售、交通运输、住宿餐饮、信息软件、金融、"
                       "房地产、租赁商务服务、科研技术服务、水利环境、居民服务、教育、卫生、"
                       "文化体育娱乐、公共管理……）。可按关键词(名称/工艺/材料/认证/服务内容)、"
                       "城市或区县、国标码过滤。支持坐标检索：传 lat/lng 按距离排序、"
                       "加 radius_km 只留圈内（地标名需调用方先解析成坐标）。"
                       "返回精简档案(id/名称/城市/区县/国标码/认证等级/"
                       "工艺/材料/认证/是否含电话/连锁门店数/距离)。"
                       "⚠ 关键词是**字面子串**匹配：用户的口语词若与名录写法不同（如「机加工」"
                       "vs 归档用的「机械零部件加工」、「面馆」vs「苏式面馆」），命中会偏少。"
                       "此时不要直接报「没有」——看返回体里的 hint / suggested_gb，"
                       "或先调 suggest_filters 拿到候选码，再用 gb=<码> 重试"
                       "（召回通常多几十倍，且只扫对应分片更快）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "关键词：企业名/品牌/工艺/材料/认证/服务内容子串"},
                "city": {"type": "string",
                         "description": "城市名，如 深圳 / 东莞 / 苏州；"
                                        "也接受区县或县级市（如 昆山、海盐、姑苏），"
                                        "这些地方在地图数据里归地级市名下，靠 district 字段命中"},
                "gb": {"type": "string", "description": "国标码，如 3484(机械零部件加工)、"
                                                        "6210(正餐服务)。给了就只扫对应分片"},
                "limit": {"type": "integer", "default": 20, "description": "返回条数上限(1-200)"},
                "offset": {"type": "integer", "default": 0, "description": "分页偏移"},
                "group_branches": {"type": "boolean", "default": True,
                                   "description": "同一品牌的连锁门店是否折成一行（默认折）。"
                                                  "折后代表行带 branches(门店数) 与 branch_ids；"
                                                  "传 false 可展开全部门店"},
                "etype": {"type": "string", "enum": ["生产", "流通", "服务"],
                          "description": "主体经济类型过滤，按国标门类推导："
                                         "生产 = A农林牧渔/B采矿/C制造/D电力热力燃气水/E建筑；"
                                         "流通 = F批发零售/G交通运输仓储邮政；"
                                         "服务 = H住宿餐饮…S公共管理。"
                                         "「找能做的厂」用生产，「找附近的店/服务商」用服务"},
                "lat": {"type": "number",
                        "description": "纬度（GCJ-02/WGS-84 均可）。**地标名（如「青创港」）"
                                       "请先用地图工具解析成坐标再传 —— MCP 不持地图 API、"
                                       "不猜地名。与 lng 必须同时提供"},
                "lng": {"type": "number",
                        "description": "经度。与 lat 同时提供后，结果档内按球面距离升序，"
                                       "每条带 distance_km（直线距离，非路程）；无坐标记录排末位"},
                "radius_km": {"type": "number",
                              "description": "半径（公里，上限 500）。给了就只保留圈内主体："
                                             "圈外与无坐标记录剔除（无坐标=无法证明在圈内），"
                                             "剔除数见返回体 geo_filtered_out；连锁品牌行报最近门店距离"},
            },
        },
    },
    {
        "name": "get_vendor",
        "description": "按主体 id(+可选国标码)取完整中文档案：名称/地址/经纬度/电话/行业路径/认证/关键词等。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "主体 id，如 CN-MFG-0000163（必填）"},
                "gb": {"type": "string", "description": "国标码；不填会自动从指纹分片反查(稍慢)"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "get_capability_card",
        "description": "按主体 id 取能力卡（工艺位/设备/产能/认证/起订量等；服务业主体多为空）。"
                       "全库仅少数主体有已发布能力卡，无卡的会秒回 has_card=false"
                       "（不必重试）。"
                       "无已发布卡片时返回 has_card=false 及原因说明。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "主体 id（必填）"},
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
    {
        "name": "account",
        "description": "查看/提升本机调用身份与配额（匿名装完即用，无需注册）。"
                       "数据本身对所有档位全量可见；绑定手机号只**提高配额**，不解锁内容。"
                       "action=status 看当前档位与剩余额度；"
                       "action=request 需传 phone（中国大陆 11 位），发短信验证码；"
                       "action=confirm 需传 phone + code，验证通过后本机升级为 verified，"
                       "配额 200→2000 次/滚动 24 小时；"
                       "action=logout 清除本地令牌回到匿名档。"
                       "⚠ 只有当你**确实遇到** quota_exceeded / l2_daily_limit，"
                       "或用户主动问「额度/绑定手机号」时才调用；不要一上来就催用户绑手机。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "request", "confirm", "logout"],
                           "description": "默认 status"},
                "phone": {"type": "string", "description": "request/confirm 必填：中国大陆手机号，如 13800138000"},
                "code": {"type": "string", "description": "confirm 必填：收到的 6 位验证码"},
            },
        },
    },
    {
        "name": "vendor_onboarding",
        "description": "给供应商开通「入驻 / 认领」入口：返回一个**可直接点开**的控制台链接与步骤。"
                       "当用户说「我是某某公司的」「我想入驻 / 上架产品 / 让客户搜到我」"
                       "「我要认领我的企业」「怎么注册成供应商」「我的审核到哪一步了」时调用。"
                       "⚠ 本工具**只给入口，绝不代办**：短信验证码与法人手机核验必须由用户"
                       "在自己的浏览器里输入 —— 让他把验证码念给你，等于把「证明他是这家企业」"
                       "的凭证交给第三方，还会进入对话上下文与日志。"
                       "⚠ 不要替用户编造企业信息，不要承诺审核结果或时效，"
                       "也不要把未认领的企业说成「已经属于你」。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "supplier_id": {"type": "string",
                                "description": "可选。已在检索结果里拿到的企业 ID（如 CN-MFG-0020317），用于生成直达深链，省得用户再搜一遍公司名"},
                "company": {"type": "string", "description": "可选。企业名称，用于把步骤说得更具体"},
                "intent": {"type": "string", "enum": ["claim", "register", "status"],
                           "description": "claim=认领库里已收录的企业（默认）；register=库里没有、要新建档；status=查自己申请的审核进度"},
            },
        },
    },
]


CONSOLE_BASE = "https://beacon-mfg.pages.dev/console/"


def vendor_onboarding(supplier_id: str = "", company: str = "",
                      intent: str = "claim") -> Dict[str, Any]:
    """给供应商一个**能自己走完**的入驻入口。

    为什么 MCP 只做到「给链接」为止
    ------------------------------
    Agent 拿得到 HTML 也没用（它不会填表单）。但更要紧的是**凭证不能进对话**：
    认领要过短信验证码与法人手机核验，让用户把验证码念给 Agent，等于把
    「证明我是这家企业」的东西交给第三方，还顺带落进 LLM 上下文与日志。
    所以这里只返回链接 + 步骤 + 明确的边界，由用户在浏览器里自己完成。

    同源红线：即便将来 Agent 能代填字段，也必须企业自己确认过才算「企业自述」——
    否则就是拿平台推断冒充企业说的话。
    """
    from urllib.parse import quote as _q

    sid = (supplier_id or "").strip()
    name = (company or "").strip()
    if sid and not name:
        # 顺带把公司名查出来，好让 Agent 说「帮您找到 XX 公司」而不是干巴巴一个 ID
        try:
            v = get_vendor(sid)
            # get_vendor 的外层是 {"vendor": {...}}，公司名在 vendor 顶层或 identity 里
            vv = (v.get("vendor") if isinstance(v, dict) and v.get("vendor") else v) or {}
            name = str(
                vv.get("company")
                or (vv.get("identity") or {}).get("company")
                or ""
            ).strip()
        except Exception:
            pass

    url = CONSOLE_BASE + ("?id=" + _q(sid, safe="") if sid else "")
    intent = (intent or "claim").strip().lower()

    if intent == "register":
        steps = [
            "打开链接，用手机号收验证码登录（同一个号在 App 与网页是同一账号，草稿也共享）",
            "在「找到我的企业」里搜公司名；搜不到就走「新建档」",
            "填主体 / 联系 / 产能 / 质量 / 服务与产品条目（单价可不填，对外显示「面议」）",
            "提交后进入人工审核；不通过会带**逐字段理由**退回，改完可重新提交",
        ]
    elif intent == "status":
        steps = [
            "打开链接，用提交时那个手机号登录",
            "页面会列出「我的申请」：待审核 / 已通过 / 已驳回（驳回带理由）",
        ]
    else:
        steps = [
            "打开链接，用手机号收验证码登录",
            "搜到自己的企业并选中（已在库里的话，公开信息会自动带成底稿，不用重填）",
            "第一步：填 18 位统一社会信用代码（按 GB 32100-2015 校验位当场验）",
            "第二步：填工商登记的法人手机号并收码",
            "提交认领申请 → **人工复核通过后**才算认领成功",
        ]

    return {
        "url": url,
        "company": name or None,
        "supplier_id": sid or None,
        "intent": intent,
        "steps": steps,
        "boundary": "验证码、法人手机号这类凭证只能由用户在本页面自己输入。"
                    "**不要让用户把它们告诉你**，也不要替他填写。",
        "what_not_to_do": [
            "不要代替用户完成认领或注册",
            "不要索取、转述或回显任何验证码",
            "不要承诺审核结果或时效",
            "不要把未认领的企业说成「已经属于该用户」",
        ],
        "why_not_here": "USCC 是企业公开信息，单凭它认领等于把企业送人。"
                        "所以我们不用「填个代码就算认领」的做法，而是提交后人工复核。",
        "next": "把 url 原样给用户，让他在浏览器里走完；完成后可以回来让你查他企业的能力卡。",
    }


def account(action: str = "status", phone: str = "", code: str = "") -> Dict[str, Any]:
    """身份与配额自助。**任何异常都转成可读错误**，不让对话里冒出堆栈。"""
    try:
        import identity
    except Exception as e:      # noqa: BLE001
        return {"error": "identity_unavailable", "detail": str(e)[:120]}
    a = (action or "status").strip().lower()
    try:
        if a == "status":
            return identity.status()
        if a == "request":
            return identity.request_code(phone)
        if a == "confirm":
            return identity.confirm_code(phone, code)
        if a == "logout":
            return identity.logout()
    except Exception as e:      # noqa: BLE001
        return {"error": "account_failed", "detail": str(e)[:160]}
    return {"error": "bad_action", "hint": "action 只能是 status/request/confirm/logout"}


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
    args = _vid_in(args or {})
    # 每次调用重置「取数失败原因」，避免上一次的超时/401 文案串到这一次的正常结果上
    _HTTP_LAST_ERROR[0] = ""
    _FETCH_ABORTED[0] = ""
    # op = 本次 tool 调用的唯一 id，服务端按它去重计配额。
    # 必须**每次调用新生成**：复用同一个 op 会被服务端的「单 op 路径上限」拦住
    # （一次检索横跨上百个分片是正常的，但同一个 op 不该无限扩）。
    tok = _cur_set({"tool": name, "tokens": _product_tokens(name, args),
                    "op": _uuid.uuid4().hex})
    t0 = time.time()
    err: Optional[BaseException] = None
    r: Any = None
    try:
        r = _dispatch_inner(name, args)
        if isinstance(r, dict) and "total_matched" in r:
            cur = _cur_get() or {}
            cur["hits"] = r.get("total_matched")
            _cur_set(cur)
        # 出口统一换 token：只改 `id` 字段是不够的 —— 路径
        # （skills/vendors/<id>/SKILL.md）、markdown 正文、LLM 会照抄的自然语言
        # 里都写着内部编号，漏一处就等于把编号送出去。
        return _vid_out(r)
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
                group_branches=bool(args.get("group_branches", True)),
                etype=args.get("etype", ""),
                # 2026-10-24 fix：v1.9.0 的坐标参数一度漏转发（schema 里有、
                # search_vendors 签名里也有，但 dispatch 没传 → 三个参数恒为 None，
                # 表现为 radius_km 静默失效、结果无 distance_km / geo、
                # 且「只传 lat」不报 bad_geo）。坐标必须转发。
                lat=args.get("lat"),
                lng=args.get("lng"),
                radius_km=args.get("radius_km"),
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
        if name == "account":
            return account(args.get("action", "status"), args.get("phone", ""), args.get("code", ""))
        if name == "vendor_onboarding":
            return vendor_onboarding(args.get("supplier_id", ""), args.get("company", ""),
                                     args.get("intent", "claim"))
    except Exception as e:  # 任何异常都包成文本，避免协议崩
        return {"error": f"{name} 执行异常: {e}"}
    return {"error": f"未知 tool: {name}"}


_SEND_LOCK = threading.Lock()


def _send(obj: Dict[str, Any]) -> None:
    # 并发处理 tool 调用后，stdout 是**多线程共享**资源：整行必须一次写完，
    # 否则两条响应会交错成一行非法 JSON，客户端直接判定协议错误。
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _SEND_LOCK:
        sys.stdout.write(line)
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


def _serve_tool_call(params: Dict[str, Any], mid: Any) -> None:
    """在工作线程里跑一次 tools/call，并**保证**回一条响应（含异常路径）。

    必须做成独立函数而不是就地 try：`_dispatch` 会重新抛业务异常，若异常发生在
    线程里又没人接，客户端会永远等这个 id（表现为工具「假死」）。
    """
    name = (params or {}).get("name", "")
    try:
        # 等启动预热收尾（最多 BEACON_WARM_WAIT 秒）。不这样做的话，预热线程与
        # 首次查询会各建一遍同样的表（别名三表 ~2s、切词表 ~1.9s），等于把冷启动
        # 成本翻倍 —— 实测出现过 35745 ms 这种远超单项之和的离群值。
        _await_warm()
        res = _dispatch(name, (params or {}).get("arguments") or {})
        payload = {
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
                "isError": isinstance(res, dict) and "error" in res,
            },
        }
    except BaseException as e:      # noqa: BLE001 —— 线程里必须兜住一切
        payload = {
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": {"code": "internal", "tool": name,
                               "message": "%s: %s" % (type(e).__name__, e)}},
                    ensure_ascii=False)}],
                "isError": True,
            },
        }
    try:
        _send(payload)
    except Exception:
        pass


def main() -> None:
    _log(f"start; source={BEACON_SOURCE} repo={REPO or '(http)'}")
    _maybe_spawn_selfupdate()
    # 后台静默取一次身份令牌。**必须异步**：bootstrap 最坏等 3 秒超时，
    # 而 stdio 的 initialize 如果卡 3 秒，客户端会判定服务不可用。
    # 拿不到就只带 cid 继续跑（服务端 fail-open），不影响任何查询。
    try:
        import identity
        identity.warmup()
    except Exception:
        pass
    # 预热「清单 + 分片哈希索引」（异步，理由同上：initialize 不能等）。
    # 这两样是所有检索的前置：清单不到位，连分片的内容哈希都无从判断，
    # 内容哈希缓存也就无从命中。线上实测清单取数 2~3 秒，先在这里付掉。
    try:
        threading.Thread(target=_warm_caches, name="beacon-warm", daemon=True).start()
    except Exception:
        pass
    # ⚠ 读循环与 tool 执行必须**解耦**（2026-10-24）：
    #   原来 `for raw in sys.stdin` 里直接同步跑 _dispatch，是严格串行 ——
    #   一个慢检索（冷缓存扫全库 / 网络抖动）会把后面**所有**调用堵住，
    #   连 `account`、`ping` 这种本该立刻返回的也要排队到客户端超时；
    #   更糟的是客户端超时后不再读管道，服务端写 stdout 会被阻塞，
    #   进程从此「假死」，只能被杀。改为：协议类方法就地处理（极快、保序），
    #   只有 tools/call 丢进小线程池，响应按 JSON-RPC 的 id 匹配、允许乱序。
    _pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
    try:
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
                if _pool is None:
                    _pool = concurrent.futures.ThreadPoolExecutor(
                        max_workers=3, thread_name_prefix="beacon-tool")
                _pool.submit(_serve_tool_call, msg.get("params", {}), mid)
            else:
                # 未知方法：若有 id 则回空结果，通知则忽略
                if mid is not None:
                    _send({"jsonrpc": "2.0", "id": mid, "result": {}})
    finally:
        # stdin 关闭后必须等在飞的调用写完再退，否则最后一条响应会丢
        if _pool is not None:
            _pool.shutdown(wait=True)


# --------------------------------------------------------------------------- #
# 命令行自助（`npx beacon-mfg-mcp login|status|logout`）
# --------------------------------------------------------------------------- #
# 为什么要有 CLI：MCP 的 `account` 工具只能在**已经接进 agent** 的场景里用。
# 但用户大概率是在终端里第一次接触这个包（`npx` 装完先跑一下看看），此时
# 还没有任何 agent 会替他调 tool。两条路并存，覆盖两类入口。
CLI_COMMANDS = {"login": "login", "bind": "login", "status": "status",
                "whoami": "status", "me": "status", "logout": "logout"}


def _cli_ask(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    return (line or "").strip()


def _cli_main(cmd: str, argv: List[str]) -> int:
    """返回进程退出码。**不写 stdout 的 JSON-RPC 通道** —— 这是终端模式。"""
    import identity

    def show(d: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
        sys.stdout.flush()

    # --phone / --code 供脚本化调用（CI、一键脚本），缺省走交互式提问
    opt: Dict[str, str] = {}
    for i, a in enumerate(argv):
        if a in ("--phone", "-p") and i + 1 < len(argv):
            opt["phone"] = argv[i + 1]
        elif a in ("--code", "-c") and i + 1 < len(argv):
            opt["code"] = argv[i + 1]

    if cmd == "status":
        show(identity.status())
        return 0

    if cmd == "logout":
        show(identity.logout())
        return 0

    # login
    phone = opt.get("phone") or _cli_ask("手机号（中国大陆 11 位）：")
    r = identity.request_code(phone)
    if not r.get("ok"):
        show(r)
        # 把「为什么不行」翻成人话：这些错误码用户看不懂，但后果很具体
        hint = {
            "sms_not_configured": "服务端还没接短信通道（运营侧待配置），请稍后再试",
            "too_frequent": "同一号码 60 秒内只能发一条，请稍等再试",
            "daily_limit": "该号码今天验证次数已用完，请明天再试",
            "ip_limit": "当前网络验证过于频繁，请换网络或稍后",
            "bad_phone": "手机号格式不对（只支持中国大陆 11 位）",
        }.get(r.get("error") or "")
        if hint:
            sys.stderr.write("\n提示：" + hint + "\n")
        return 2

    sys.stderr.write("\n验证码已发送到 %s（%d 秒内有效）\n" %
                     (r.get("phone", ""), int(r.get("ttl") or 300)))
    if r.get("dev_code"):
        sys.stderr.write("【本地 dev 通道】验证码：%s\n" % r["dev_code"])
    code = opt.get("code") or _cli_ask("验证码（6 位）：")
    c = identity.confirm_code(phone, code)
    show(c)
    if c.get("ok"):
        sys.stderr.write("\n✅ 已升级为 verified：配额 %s 次/滚动 24 小时，"
                         "能力卡等 L2 资产已解锁。\n" %
                         (c.get("quota", {}) or {}).get("day", "-"))
        return 0
    return 2


if __name__ == "__main__":
    _argv = sys.argv[1:]
    _cmd = CLI_COMMANDS.get(_argv[0].lstrip("-").lower()) if _argv else None
    if _cmd:
        sys.exit(_cli_main(_cmd, _argv[1:]))
    main()
