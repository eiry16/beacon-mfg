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
    return " ".join(str(rec.get(k, "")) for k in ("co", "city", "gb")) + " " + \
           " ".join(rec.get("proc", []) or []) + " " + \
           " ".join(rec.get("mat", []) or []) + " " + \
           " ".join(rec.get("cert", []) or [])


def _rec_summary(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": rec.get("id"),
        "company": rec.get("co"),
        "city": rec.get("city"),
        "gb": rec.get("gb"),
        "badge": rec.get("cl"),
        "score": rec.get("sc"),
        "has_phone": bool(rec.get("tel")),
        "process": rec.get("proc", []),
        "material": rec.get("mat", []),
        "cert": rec.get("cert", []),
    }


INDEX_DIR = "skills/registry/index"
_index_meta_cache: Optional[Dict[str, Any]] = None
_city_index_cache: Optional[Dict[str, Any]] = None
_bucket_cache: Dict[str, Dict[str, Any]] = {}

_RE_CJK = re.compile("[\u4e00-\u9fff\u3400-\u4dbf]+")
_RE_WORD = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    """切成可索引的词。必须与 scripts/gen_search_index.py 的 tokenize 完全一致，
    否则查询侧与索引侧口径不一致会直接把召回判成 0。
    CJK 取 1-gram + 2-gram；拉丁/数字取整词 + 长度 >= 2 的前缀。
    """
    t = (text or "").lower()
    out = set()
    for run in _RE_CJK.findall(t):
        for i, ch in enumerate(run):
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


def _index_meta() -> Optional[Dict[str, Any]]:
    global _index_meta_cache
    if _index_meta_cache is not None:
        return _index_meta_cache
    txt = fetch_text(INDEX_DIR + "/meta.json")
    if not txt:
        return None
    try:
        _index_meta_cache = json.loads(txt)
    except Exception:
        return None
    return _index_meta_cache


def _index_fresh() -> bool:
    """索引必须来自当前已发布快照，否则宁可回退全量扫描也不能用陈旧索引。

    git 模式比对 source_head 与当前 HEAD；HTTP 模式比对索引记录数与 manifest 的
    fp 记录总数。任一不一致 → 判定陈旧 → 回退。
    """
    meta = _index_meta()
    if not meta:
        return False
    try:
        if REPO:
            return meta.get("source_head") == (_head_sha() or "")
        tot = sum(s.get("k", 0) for s in _shards_of_type("fp"))
        return int(meta.get("records", -1)) == int(tot)
    except Exception:
        return False


def _city_gbs(city: str) -> Optional[set]:
    global _city_index_cache
    if _city_index_cache is None:
        txt = fetch_text(INDEX_DIR + "/city.json")
        if not txt:
            return None
        try:
            _city_index_cache = json.loads(txt)
        except Exception:
            return None
    m = _city_index_cache.get(city)
    return None if m is None else set(m.keys())


def _term_gbs(term: str) -> Optional[set]:
    """返回含该词的国标码集合。索引里没有该词 → 返回空集（确无命中），
    读不到桶 → 返回 None（视为不可用，触发回退）。
    """
    meta = _index_meta()
    if not meta:
        return None
    try:
        buckets = int(meta["buckets"])
    except Exception:
        return None
    b = int(hashlib.sha1(term.encode("utf-8")).hexdigest(), 16) % buckets
    key = str(b)
    if key not in _bucket_cache:
        txt = fetch_text("%s/terms/b%04d.json" % (INDEX_DIR, b))
        if not txt:
            return None
        try:
            _bucket_cache[key] = json.loads(txt)
        except Exception:
            return None
    m = _bucket_cache[key].get(term)
    return set(m.keys()) if m else set()


def _candidate_gbs(tokens: List[str], city: str) -> Optional[set]:
    """用预构建索引求候选国标码；索引不可用/陈旧时返回 None 交由调用方回退。"""
    if not _index_fresh():
        return None
    cands: Optional[set] = None
    if city:
        s = _city_gbs(city)
        if s is None:
            return None
        cands = s
        if not cands:
            return set()
    for tok in tokens:
        grams = _tokenize(tok)
        if not grams:
            return None
        tset: Optional[set] = None
        for g in grams:
            s = _term_gbs(g)
            if s is None:
                return None
            tset = s if tset is None else (tset & s)
            if not tset:
                break
        if tset is None:
            return None
        cands = tset if cands is None else (cands & tset)
        if not cands:
            return set()
    return cands


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
                if city and rec.get("city", "") != city:
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
    cands = _candidate_gbs(tokens, city) if (tokens or city) else None
    via_index = cands is not None

    if via_index and not cands:
        # 索引明确判定无候选国标码 —— 无需拉任何分片
        return {
            "total_matched": 0,
            "returned": 0,
            "shards_scanned": 0,
            "via_index": True,
            "results": [],
        }

    if via_index:
        fp_shards = [s for s in _shards_of_type("fp") if s.get("c") in cands]
    else:
        fp_shards = _shards_of_type("fp")

    matches: List[Dict[str, Any]] = []
    scanned = 0
    if via_index:
        # 并发拉取候选分片（通常 1~N 个）
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            for part in ex.map(_read_fp_shard, fp_shards):
                scanned += 1
                for rec in part:
                    if city and rec.get("city", "") != city:
                        continue
                    if tokens and not all(tok in _hay(rec).lower() for tok in tokens):
                        continue
                    matches.append(_rec_summary(rec))
    else:
        recs = _build_fp_index()
        scanned = len(fp_shards)
        for rec in recs:
            if city and rec.get("city", "") != city:
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
                "city": {"type": "string", "description": "城市名精确匹配，如 深圳 / 东莞"},
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
                    "serverInfo": {"name": "beacon-mfg-readonly", "version": "1.1.1"},
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
