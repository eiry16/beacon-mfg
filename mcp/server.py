#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Beacon-MFG 只读 MCP 服务（stdio 传输，零第三方依赖）

设计边界（与用户 2026-09-18 约定一致）：
  - **只读**：只检索「已发布数据」—— Cloudflare Pages 公开端点（默认）或本地 git 仓库的
    已提交快照。绝不写、绝不调用任何后端脚本（fetch_batch / postfetch / en_backfill …），
    绝不持有 ZHIPU / CLOUDFLARE 密钥。
  - **不污染仓库**：本地模式只通过 `git show HEAD:<path>` 读取已提交内容（不会触发
    maskphone 的 smudge 过滤器，也不会碰 index 锁）。本服务**绝不**对仓库执行
    `git checkout` / `git restore` / `git add` / `git commit` —— 那是历史「42578 个手机号
    被无声掩码」事故的根源，必须规避。
  - 手机号为隐私字段：git 已提交版本是掩码态（138****0000）；CF 部署版可能是全号。
    对隐私敏感场景，把 BEACON_REPO 指向本地仓库（默认即读取掩码态）即可，无需联网。

协议：JSON-RPC 2.0 over stdio（newline-delimited）。兼容 Claude Desktop / Cline /
Continue / WorkBuddy 等主流 MCP 客户端。

数据源解析：
  BEACON_SOURCE  HTTP 基址（默认 https://beacon-mfg.pages.dev）
  BEACON_REPO    本地 git 仓库路径；设了就用 `git show HEAD:` 读（推荐：离线 + 隐私安全）
                缺省时自动探测 cwd 所在 git 仓库（若含 data/gb 就用它）

暴露的 tool：
  search_vendors       按 关键词 / 城市 / 国标码 检索（走 fp 指纹分片）
  get_vendor           按 id（+ 国标码）取完整中文档案（走 zh 分片）
  get_capability_card  按 id 取能力卡（走 skills/registry/capability，由 R2 提供）
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
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple
import sys as _sys
import uuid as _uuid

# --------------------------------------------------------------------------- #
# rfq-kernel 桥接（G1/G2/G3：让 MCP 客户 agent 能跑多轮对话匹配）
# 路径相对本文件解析，与 cwd / BEACON_REPO 无关。桥接不可用时降级，不影响其余 tool。
# --------------------------------------------------------------------------- #
_RFK_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "skills", "rfq-kernel", "src")
if os.path.isdir(_RFK_SRC) and _RFK_SRC not in _sys.path:
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

    按 gb 分片精准取（O(命中分片)，绝非全量扫描），进程内缓存复用。
    语义依据：beacon-mfg 以国标码为唯一行业判别信号，检测到行业即应召回其国标分类下的企业，
    即便其厂名不含召回词（如 conveyor 的 gb=3434 连续搬运设备厂「耐特斯传输设备」用『传输』
    而非『输送』，而『传输』df=1 未入倒排索引，常规召回捞不到）。加法召回，绝不误删真实企业。
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
    连写词缩到只剩字面全含的极少数分片——实测 316/372 条缩到 1 个分片，属隐性漏召回。）

    证据权重：
      - 产品信号命中（proc/mat/cert/gb）权重 3x；企业名 `co` 命中权重 1x。
        企业名是**合法证据**——大量长尾厂只把品类写在厂名里（如「中山市世通输送机械设备
        有限公司」的 proc 只有 cnc_milling），只信 proc/mat 会让这些真实企业永远搜不到。
      - 每个命中词按 IDF 加权：稀有词（「输送」df=6）权重大，泛词（城市名「上海」df≈1.3万、
        通名「厂家」）权重小 —— 需求句里的地区/通名不会带偏排序。
      - 传入 pack_id 时，国标码命中本行业的记录 +1000，确保本行业供应商排到 top_k 前列。

    性能：优先走倒排索引的『区分词并集』快速路径 `_recall_candidates`（O(命中量)）；
    索引不可用/无区分词/并集过大时回退全量扫描（进程内与盘上均缓存，可复用）。只读。
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


def _http_get(relpath: str) -> Tuple[Optional[str], Optional[str]]:
    url = BEACON_SOURCE + "/" + relpath
    cap = _cache_path(url)
    etag = None
    if os.path.exists(cap):
        try:
            with open(cap, "r", encoding="utf-8") as f:
                blob = json.load(f)
            etag = blob.get("etag")
        except Exception:
            etag = None
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        body = resp.read().decode("utf-8")
        new_etag = resp.headers.get("ETag")
        try:
            with open(cap, "w", encoding="utf-8") as f:
                json.dump({"etag": new_etag, "body": body}, f)
        except Exception:
            pass
        return body, new_etag
    except urllib.error.HTTPError as e:
        if e.code == 304 and os.path.exists(cap):
            with open(cap, "r", encoding="utf-8") as f:
                return json.load(f).get("body"), etag
        return None, None
    except Exception:
        # 网络不通时若本地有缓存也返回，保证离线可用
        if os.path.exists(cap):
            with open(cap, "r", encoding="utf-8") as f:
                return json.load(f).get("body"), etag
        return None, None


def fetch_text(relpath: str) -> Optional[str]:
    """统一的只读取入口：本地 git 优先（若可用），否则 HTTP。"""
    if REPO:
        g = _git_show(relpath)
        if g is not None:
            return g
        # 本地没有（如能力卡不进 git）再退回 HTTP
    return _http_get(relpath)[0]


# --------------------------------------------------------------------------- #
# manifest + 分片访问
# --------------------------------------------------------------------------- #
def load_manifest() -> Dict[str, Any]:
    global _manifest_cache
    if _manifest_cache is None:
        txt = fetch_text("data/manifest.json")
        if not txt:
            raise RuntimeError("无法取得 manifest.json（检查 BEACON_SOURCE / BEACON_REPO / 网络）")
        _manifest_cache = json.loads(txt)
    return _manifest_cache


def _shards_of_type(t: str) -> List[Dict[str, Any]]:
    return [s for s in load_manifest().get("shards", []) if s.get("t") == t]


def _zh_paths_for_gb(gb: str) -> List[str]:
    # 一个国标码可能拆成多个 zh 分片（如 3484.json + 3484-p2.json 续片），必须全扫
    return [s.get("p") for s in _shards_of_type("zh") if s.get("c") == gb]


# --------------------------------------------------------------------------- #
# tool 实现
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 全文索引：首次构建后常驻进程内存，并按 HEAD sha 缓存到磁盘，
# 避免每次搜索都全扫 267 个 fp 分片（此前逐分片 git show 约 100s）。
# --------------------------------------------------------------------------- #
_fp_index: Optional[List[Dict[str, Any]]] = None
_fp_index_key: Optional[str] = None


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


def _index_cache_path() -> Optional[str]:
    if not REPO:  # HTTP 模式不落盘缓存，避免读到陈旧的已发布快照
        return None
    key = (BEACON_REPO or BEACON_SOURCE) + "|" + (_head_sha() or "nosha")
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, "fpindex-" + h + ".json")


def _read_fp_shard(s: Dict[str, Any]) -> List[Dict[str, Any]]:
    txt = fetch_text(s["p"])
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

    只读已提交 object（本就是 maskphone 掩码态），不碰工作树、不触发 smudge、
    不碰 index 锁 —— 安全边界与逐分片 `git show` 一致，但把 267 次 subprocess
    降到 1 次，冷启动从 ~48s 降至数秒。任一环节失败都返回 None，由调用方退回
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


def _build_fp_index() -> List[Dict[str, Any]]:
    global _fp_index, _fp_index_key
    cp = _index_cache_path()
    key = cp or "mem"
    if _fp_index is not None and _fp_index_key == key:
        return _fp_index
    if cp and os.path.exists(cp):
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
            _fp_index = recs
            _fp_index_key = key
            if cp:
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
    _fp_index = recs
    _fp_index_key = key
    if cp:
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


def _hay(rec: Dict[str, Any]) -> str:
    return " ".join(str(rec.get(k, "")) for k in ("co", "city", "dist", "gb")) + " " + \
           " ".join(rec.get("proc", []) or []) + " " + \
           " ".join(rec.get("mat", []) or []) + " " + \
           " ".join(rec.get("cert", []) or []) + " " + \
           " ".join(rec.get("products", []) or [])


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
INDEX_VERSION = 3          # 倒排 key 为分片路径；v2 用国标码会漏掉 gb=null 的记录
_index_meta_cache: Optional[Dict[str, Any]] = None
_bucket_cache: Dict[str, Dict[str, Any]] = {}
_shard_rec_cache: Dict[str, List[Dict[str, Any]]] = {}

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
    也会被算成候选，白白多拉分片（上海+酒店实测 20 片）。2-gram 已足以保证
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
    """读索引文件。git 模式按 HEAD sha 落盘缓存（桶均 11KB），
    缓存后跨进程重复查询几乎零成本。
    """
    cp = None
    if REPO:
        h = _head_sha() or "nosha"
        cp = os.path.join(CACHE_DIR, "idx-" +
                          hashlib.sha1((h + "|" + relpath).encode("utf-8")).hexdigest() + ".json")
        if os.path.exists(cp):
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
        except Exception:
            pass
    return txt


def _git_read_many(paths: List[str]) -> Dict[str, str]:
    """git 模式：一次 `git archive` 批量取出多个文件。

    「每个文件一次 git show」是冷启动的主要开销（18 个分片 ≈ 5s），
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
    """批量取文本：git 模式 1 次 archive；HTTP 模式线程池并发。"""
    if not paths:
        return {}
    if REPO:
        got = _git_read_many(paths)
        if len(got) >= max(1, len(paths) // 2):   # archive 正常覆盖
            return got
    out: Dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        for path, txt in zip(paths, ex.map(fetch_text, paths)):
            if txt:
                out[path] = txt
    return out


def _index_meta() -> Optional[Dict[str, Any]]:
    global _index_meta_cache
    if _index_meta_cache is not None:
        return _index_meta_cache
    txt = _index_text(INDEX_DIR + "/meta.json")
    if not txt:
        return None
    try:
        _index_meta_cache = json.loads(txt)
    except Exception:
        return None
    return _index_meta_cache


def _index_fresh() -> bool:
    """索引可用且版本匹配、不落后于数据 → True；否则回退全量扫描。

    索引用等号判「记录数一致」会永远失败：它是提交前从工作树构建的，
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
    """倒排索引里出现过的城市名集合：地区词不作为相关性证据（只作筛选偏好）。"""
    global _city_names_cache
    if _city_names_cache is not None:
        return _city_names_cache
    names: set = set()
    txt = _index_text(INDEX_DIR + "/city.json")
    if txt:
        try:
            names = set(json.loads(txt).keys())
        except Exception:
            names = set()
    _city_names_cache = names
    return names


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
    """取倒排索引里某词的 postings {分片路径: 命中条数}；缺失/异常返回空 dict。"""
    rel = _bucket_rel(gram, buckets)
    b = _bucket_cache.get(rel)
    if b is None:
        txt = _index_text(rel)
        try:
            b = json.loads(txt) if txt else {}
        except Exception:
            b = {}
        _bucket_cache[rel] = b
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
    """按倒排索引的 df 给产品词估 IDF 权重 = 1/(1+ln(df))。

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
    """倒排索引『区分词并集』快速召回：取并集（OR）而非交集。

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
    """批量读取若干 fp 分片的记录，并在进程内按路径缓存（重复查询近乎零成本）。"""
    out: List[Dict[str, Any]] = []
    if not paths:
        return out
    todo = [p for p in paths if p not in _shard_rec_cache]
    for p in todo:
        _shard_rec_cache[p] = []
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
        _shard_rec_cache[_path] = recs
    for p in paths:
        out.extend(_shard_rec_cache.get(p, []))
    return out


def search_vendors(query: str = "", city: str = "", gb: str = "",
                   limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    q = (query or "").strip().lower()
    # 多词按空格分词，要求全部命中(AND)——这样「上海 酒店」也能正确匹配，
    # 而非必须作为连续子串出现。单关键词时退化为原行为。
    tokens = [t for t in q.split() if t]

    # 给了国标码：只扫对应分片（最快路径，不构建全量索引）
    if gb:
        fp_shards = [s for s in _shards_of_type("fp") if s.get("c") == gb]
        scanned = 0
        matches: List[Dict[str, Any]] = []
        for s in fp_shards:
            scanned += 1
            for rec in _read_fp_shard(s):
                # city 匹配「地级市 或 区县」：县级市（昆山/海盐…）在高德里归到地级市名下，
                # 记录里 city=苏州、dist=昆山。只比 city 的话查「昆山」永远 0 条。
                if city and city not in (rec.get("city", ""), rec.get("dist", "")):
                    continue
                if tokens and not all(tok in _hay(rec).lower() for tok in tokens):
                    continue
                matches.append(_rec_summary(rec))
        return {
            "total_matched": len(matches),
            "returned": len(matches[offset:offset + limit]),
            "shards_scanned": scanned,
            "results": matches[offset:offset + limit],
        }

    # 未给国标码：优先走「预构建倒排索引 → 只拉命中分片」（O(命中量)）。
    # 索引缺失或陈旧时回退进程内全量索引（O(总量)，慢但结果等价）。
    cands = _candidate_shards(tokens, city) if (tokens or city) else None
    via_index = cands is not None

    if via_index and not cands:
        # 索引明确判定无命中 —— 无需拉任何分片
        return {
            "total_matched": 0,
            "returned": 0,
            "shards_scanned": 0,
            "via_index": True,
            "results": [],
        }

    matches: List[Dict[str, Any]] = []
    if via_index:
        # 只拉候选分片（git 模式一次 archive 批量取，通常 1~N 个）
        scanned = len(cands)
        for rec in _records_from_paths(cands):
# city 匹配「地级市 或 区县」：县级市（昆山/海盐…）在高德里归到地级市名下，
            # 记录里 city=苏州、dist=昆山。只比 city 的话查「昆山」永远 0 条。
            if city and city not in (rec.get("city", ""), rec.get("dist", "")):
                continue
            if tokens and not all(tok in _hay(rec).lower() for tok in tokens):
                continue
            matches.append(_rec_summary(rec))
    else:
        recs = _build_fp_index()
        scanned = len(_shards_of_type("fp"))
        for rec in recs:
# city 匹配「地级市 或 区县」：县级市（昆山/海盐…）在高德里归到地级市名下，
            # 记录里 city=苏州、dist=昆山。只比 city 的话查「昆山」永远 0 条。
            if city and city not in (rec.get("city", ""), rec.get("dist", "")):
                continue
            if tokens and not all(tok in _hay(rec).lower() for tok in tokens):
                continue
            matches.append(_rec_summary(rec))

    return {
        "total_matched": len(matches),
        "returned": len(matches[offset:offset + limit]),
        "shards_scanned": scanned,
        "via_index": via_index,
        "results": matches[offset:offset + limit],
    }


def get_vendor(vid: str, gb: str = "") -> Dict[str, Any]:
    vid = (vid or "").strip()
    if not vid:
        return {"error": "缺少必填参数 id"}
    # 没给国标码就先建 id->gb 索引（扫 fp 分片，HTTP 缓存加速）
    if not gb:
        for s in _shards_of_type("fp"):
            txt = fetch_text(s["p"])
            if not txt:
                continue
            for line in txt.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("id") == vid:
                    gb = rec.get("gb") or ""
                    break
            if gb:
                break
    if not gb:
        return {"error": f"找不到 id={vid} 对应的国标码，可能该记录尚未发布"}
    zh_paths = _zh_paths_for_gb(gb)
    if not zh_paths:
        return {"error": f"国标码 {gb} 没有对应的 zh 分片"}
    for zh_path in zh_paths:
        txt = fetch_text(zh_path)
        if not txt:
            continue
        try:
            arr = json.loads(txt)
        except Exception as e:
            return {"error": f"分片 {zh_path} 解析失败: {e}"}
        for rec in arr:
            if rec.get("id") == vid:
                return {"vendor": rec}
    return {"error": f"国标码 {gb} 的全部 zh 分片(共{len(zh_paths)}个)中均未找到 id={vid}"}


def get_capability_card(vid: str) -> Dict[str, Any]:
    vid = (vid or "").strip()
    if not vid:
        return {"error": "缺少必填参数 id"}
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
# rfq-kernel 桥接 tool：多轮对话式供应商匹配（G1/G2/G3）
# 入口只有一个：客户 agent 检测到采购/代工/找厂意图时自动调 start_sourcing。
# --------------------------------------------------------------------------- #
def start_sourcing(demand_text: str, audience_id: str = "domestic_downstream") -> Dict[str, Any]:
    """【客户 agent 自动触发】当对话中出现找厂/代工/采购意图时调用。

    内部：品类检测 → 指纹宽召回 → 解析需求 → 生成 1~2 轮澄清问题，把需求归一到
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
                       "返回精简档案(id/企业名/城市/国标码/认证等级/工艺/材料/认证/是否含电话)。",
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
]


def _dispatch(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    args = args or {}
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


def main() -> None:
    _log(f"start; source={BEACON_SOURCE} repo={REPO or '(http)'}")
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
                    "serverInfo": {"name": "beacon-mfg-readonly", "version": "1.3.0"},
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
