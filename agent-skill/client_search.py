#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 薄框架客户端：按需拉取供应商分片，不为不看的分片付费。

设计原则（薄框架）：
- 安装本技能只需 SKILL.md + 本文件，**不需要 clone 整个仓库的 data/**。
- 数据全部从 CDN 按需拉取（默认 https://beacon-mfg.pages.dev）。
- ⚠ 必须带 User-Agent：Cloudflare 对无 UA 请求返回 403（error 1010），现象就是「源不通」。
- 平均每次查询传输 < 1 MB：manifest(143KB,可缓存) + 命中 fp 分片(~47KB) + 详情(按需)。

查询流程：
  1) 澄清供应商「类型 + 地域」（用户没说清就先问，别盲拉）。
  2) 类型 → 国标码（数字码直用；采购词查别名表；行业名查 gb4754）。
  3) 只拉该码的 L0 指纹分片（skills/registry/fingerprint/.../{码}.jsonl）。
  4) 客户端按城市过滤 + 工艺/材料收敛。
  5) 按契合度（关键词命中 > 资料完整度 sc > 有电话 > 凭证等级）排序，显示前 N。
  6) 用户点开某家 → 才拉 L1/L2（zh 分片或 vendors/{id}/SKILL.md）。

用法：
  python client_search.py --industry 3525 --city 宁波 --limit 5
  python client_search.py --keyword "CNC加工" --city 深圳
  python client_search.py --industry 3525 --city 宁波 --proc cnc_milling --mat 铝合金6061
  python client_search.py --industry 3525 --detail CN-MFG-0001234   # 拉完整档案(L2)
  python client_search.py --industry 3525 --city 宁波 --local --repo /path/to/beacon-mfg   # 离线读仓库
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ── 数据源 ────────────────────────────────────────────────────────────────
# 主源是 Cloudflare Pages（手机会 403 的那个坑就在这里）。备用源是 jsDelivr/raw，
# 给能直连 GitHub 的环境兜底。所有请求都带 User-Agent，绕开 1010。
BASE_URL = "https://beacon-mfg.pages.dev"
MIRRORS = [
    "https://fastly.jsdelivr.net/gh/eiry16/beacon-mfg@main/",
    "https://cdn.jsdelivr.net/gh/eiry16/beacon-mfg@main/",
    "https://raw.githubusercontent.com/eiry16/beacon-mfg/main/",
]
MANIFEST_REL = "data/manifest.json"
CACHE_DIR = Path.home() / ".cache" / "beaconmfg-shards"
TRANSFERRED = 0  # 仅统计真实下载字节（证明「只为命中的分片付费」）

# 国标码速查（常见采购词兜底，避免每次都联网拉别名表）。完整解析仍走 CDN 别名表。
EMBEDDED_CODES = {
    "3484": "机械零部件加工（CNC/数控）", "3525": "模具制造", "3391": "黑色金属铸造",
    "3392": "有色金属铸造（压铸）", "3311": "金属结构制造（钣金/冲压）", "3360": "金属表面处理及热处理加工",
    "2929": "塑料零件及其他塑料制品制造", "2913": "橡胶零件制造", "3451": "滚动轴承制造",
    "3453": "齿轮及齿轮减、变速箱", "3482": "紧固件制造", "3483": "弹簧制造",
    "2651": "初级形态塑料及合成树脂", "3982": "电子电路制造（PCB）", "3989": "其他电子元件制造",
    "3660": "汽车零部件及配件制造", "5164": "金属及金属矿批发",
    "6210": "正餐服务", "6220": "快餐服务", "6232": "咖啡馆服务", "6231": "茶馆服务",
    "6291": "小吃服务", "6110": "旅游饭店", "6130": "民宿服务", "8040": "理发及美容服务",
    "8030": "洗染服务", "8111": "汽车修理与维护", "8121": "计算机和辅助设备修理",
    "8930": "健身休闲活动", "9011": "歌舞厅娱乐活动（KTV）", "9013": "网吧活动",
    "8760": "电影放映", "9020": "游乐园", "6513": "应用软件开发", "6531": "信息系统集成服务",
    "6440": "互联网安全服务", "7452": "检测服务", "7453": "计量服务", "7491": "工业设计服务",
    "5213": "便利店零售", "5212": "超级市场零售", "5251": "西药零售",
}
CL_RANK = {"L3": 3, "L2": 2, "L1": 1, "L0": 0}


def _fmt(n: float) -> str:
    if n < 1024:
        return "%d B" % n
    if n < 1048576:
        return "%.1f KB" % (n / 1024)
    return "%.2f MB" % (n / 1048576)


def _ua() -> dict:
    return {"User-Agent": "BeaconMFG-Agent/1.0 (+https://beacon-mfg.pages.dev)"}


def fetch(rel: str, base: str | None = None, local_repo: str | None = None) -> bytes:
    """取一个文件。本地模式直接读盘；远程模式走 HTTPS + ETag 缓存 + UA。"""
    global TRANSFERRED
    if local_repo:
        return (Path(local_repo) / rel).read_bytes()

    base = base or BASE_URL
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = rel.replace("/", "_")
    body = CACHE_DIR / key
    meta = CACHE_DIR / ("%s.meta.json" % key)
    headers = {}
    if meta.exists() and body.exists():
        headers["If-None-Match"] = json.loads(meta.read_text(encoding="utf-8"))["etag"]

    roots = ([base] + MIRRORS) if base == BASE_URL else [base]
    last_err = None
    for root in roots:
        req = urllib.request.Request("%s/%s" % (root.rstrip("/"), rel), headers=headers)
        for k, v in _ua().items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                etag = resp.headers.get("ETag") or ""
                body.write_bytes(raw)
                meta.write_text(json.dumps({"etag": etag}), encoding="utf-8")
                TRANSFERRED += len(raw)
                return raw
        except urllib.error.HTTPError as e:
            if e.code == 304 and body.exists():
                return body.read_bytes()  # 命中缓存，零传输
            last_err = e
            if base == BASE_URL:
                continue  # 主源失败，试下一个镜像
            raise
    raise RuntimeError("拉取失败 %s: %s" % (rel, last_err))


def load_manifest(base: str | None, local_repo: str | None) -> dict:
    return json.loads(fetch(MANIFEST_REL, base, local_repo).decode("utf-8"))


def _load_alias_file(rel: str, local_repo: str | None) -> dict:
    """gb-alias.json / gb-alias-curated.json 可能是 [["metadata",..],["alias",{..}]]
    列表，也可能是直接 {keyword:[...]} 字典。两种都兼容。"""
    raw = json.loads(fetch(rel, None, local_repo))
    if isinstance(raw, list):
        for pair in raw:
            if isinstance(pair, list) and len(pair) == 2 and pair[0] == "alias":
                return pair[1]
        return {}
    if isinstance(raw, dict):
        return raw.get("alias", raw)
    return {}


def resolve_industry(kw: str, local_repo: str | None) -> list[tuple[str, str]]:
    """采购词/行业名 → [(国标码, 名称), ...]。数字直用；别名表优先；gb4754 兜底。"""
    kw = kw.strip()
    if kw.isdigit():
        return [(kw, EMBEDDED_CODES.get(kw, kw))]
    kw_l = kw.lower()
    # 1) 别名表（数据推导 + 人工策展，两层合并）
    alias: dict[str, list] = {}
    for rel in ("data/gb-alias.json", "data/gb-alias-curated.json"):
        for k, v in _load_alias_file(rel, local_repo).items():
            alias.setdefault(k.lower(), v)
    hits = []
    for k, v in alias.items():
        if k == kw_l or kw_l in k or k in kw_l:
            hits.extend(v)
    if hits:
        hits.sort(key=lambda x: -(x.get("hits") or 0))  # 真实命中数多的排前
        return [(h["code"], h.get("name", h["code"])) for h in hits]
    # 2) gb4754 行业名子串匹配（覆盖「公司名里没有、但国标名里有」的词）
    g = json.loads(fetch("data/gb4754-full.json", None, local_repo))
    classes: dict[str, str] = {}
    for pair in g:
        if isinstance(pair, list) and len(pair) == 2 and pair[0] in ("classes", "groups", "divisions"):
            classes.update(pair[1])
    return [(c, n) for c, n in classes.items() if kw in n or n in kw]


def pick_shards(manifest: dict, code: str, stype: str = "fp") -> list[dict]:
    return [s for s in manifest["shards"] if s.get("t") == stype and s.get("c", "").startswith(code)]


def load_rows(shard: dict, base: str | None, local_repo: str | None) -> list[dict]:
    raw = fetch(shard["p"], base, local_repo).decode("utf-8")
    if shard["p"].endswith(".jsonl"):
        return [json.loads(l) for l in raw.splitlines() if l.strip()]
    return json.loads(raw)


def _norm_city(c: str) -> str:
    return (c or "").replace("省", "").replace("市", "").replace("特别行政区", "").strip()


def relevance(r: dict, kw: str) -> tuple:
    co = (r.get("co") or "")
    kws = r.get("keywords") or []
    hit = 0
    if kw:
        kl = kw.lower()
        if kl in co.lower() or any(kl in (k or "").lower() for k in kws):
            hit = 1
    return (-hit, -(r.get("sc") or 0), -(r.get("tel") or 0), -CL_RANK.get(r.get("cl"), 0))


def main() -> int:
    ap = argparse.ArgumentParser(description="BeaconMFG 薄框架客户端：按需拉取供应商分片")
    ap.add_argument("--industry", help="国标码（支持前缀）：3525 模具 / 34 通用设备大类 / C 制造业")
    ap.add_argument("--keyword", help="采购词/行业名，自动解析成国标码（如 CNC加工 / 精密机械加工）")
    ap.add_argument("--city", help="城市（按 fp 记录的 city 字段过滤）")
    ap.add_argument("--proc", help="工艺码，逗号分隔任一命中")
    ap.add_argument("--mat", help="材料关键词")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--detail", help="供应商 ID：拉完整档案(L2)并显示")
    ap.add_argument("--base-url", help="远程基址，不给则默认 beacon-mfg.pages.dev + 镜像兜底")
    ap.add_argument("--local", action="store_true", help="读本地仓库文件（离线），需配合 --repo")
    ap.add_argument("--repo", help="本地仓库根目录（--local 时用）")
    a = ap.parse_args()

    if a.local and not a.repo:
        print("[错误] --local 需要 --repo 指向 beacon-mfg 仓库根目录")
        return 2

    # ── 类型 → 国标码 ──
    code = a.industry
    code_names: list[str] = []
    if not code and a.keyword:
        resolved = resolve_industry(a.keyword, a.repo if a.local else None)
        if not resolved:
            print("[未解析] 采购词「%s」在别名表与 gb4754 中都找不到对应国标码，请换说法或给国标码" % a.keyword)
            return 1
        code = resolved[0][0]
        code_names = [n for _, n in resolved]
        print("[解析] 「%s」→ 国标码 %s（候选：%s）"
              % (a.keyword, code, "、".join("%s %s" % (c, n) for c, n in resolved[:4])))
    elif not code:
        print("[用法] 需要 --industry <国标码> 或 --keyword <采购词> 之一")
        return 1

    manifest = load_manifest(a.base_url, a.repo if a.local else None)
    shards = pick_shards(manifest, code, "fp")
    if not shards:
        print("[无分片] manifest 里没有匹配国标码 %s 的 fp 分片" % code)
        return 1
    print("[分片] 本次命中 %d 个 fp 分片：%s"
          % (len(shards), "、".join("%s %s" % (s["c"], s.get("n", "")) for s in shards[:4])))

    # ── 详情模式：拉 zh 分片，取该 ID 完整档案 ──
    if a.detail:
        zh = pick_shards(manifest, code, "zh")
        for s in zh:
            for r in load_rows(s, a.base_url, a.repo if a.local else None):
                if r.get("id") == a.detail:
                    print(json.dumps(r, ensure_ascii=False, indent=2))
                    print("\n本次传输 %s（全量 clone 需要 %s）"
                          % (_fmt(TRANSFERRED), _fmt(manifest["metadata"]["total_bytes"])))
                    return 0
        print("[未找到] %s 不在国标码 %s 的分片里" % (a.detail, code))
        return 1

    # ── 普通检索：拉 fp 分片 → 过滤 → 排序 ──
    rows: list[dict] = []
    for s in shards:
        rows.extend(load_rows(s, a.base_url, a.repo if a.local else None))
    print("[载入] %d 条指纹" % len(rows))

    city = _norm_city(a.city) if a.city else None
    procs = [p.strip() for p in a.proc.split(",")] if a.proc else []
    hits = []
    for r in rows:
        if city and _norm_city(r.get("city")) != city:
            continue
        if procs and not (set(r.get("proc") or []) & set(procs)):
            continue
        if a.mat and not any(a.mat in m for m in (r.get("mat") or [])):
            continue
        hits.append(r)

    hits.sort(key=lambda r: relevance(r, a.keyword or ""))
    print("\n[命中] %d 家（按契合度排序，显示前 %d）：\n" % (len(hits), a.limit))
    for r in hits[:a.limit]:
        tags = []
        if r.get("cl"):
            tags.append("凭证%s" % r["cl"])
        if r.get("tel"):
            tags.append("有电话")
        if r.get("mf") == 0:
            tags.append("贸易/批发")
        print("  %s  %s（%s）" % (r["id"], r.get("co"), r.get("city") or "-"))
        print("    国标 %s %s · 工艺 %s%s"
              % (r.get("gb") or code, EMBEDDED_CODES.get(r.get("gb") or code, ""),
                 ",".join(r.get("proc") or []) or "-",
                 " · " + " · ".join(tags) if tags else ""))

    print("\n本次传输 %s（全量 clone 需要 %s）"
          % (_fmt(TRANSFERRED), _fmt(manifest["metadata"]["total_bytes"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
