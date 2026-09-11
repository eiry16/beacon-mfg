#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 数据抓取框架 —— 高德开放 API（合规绿区，官方 POI 数据）

用途：按品类关键词 + 城市拉取制造业企业 POI 骨架（名称/地址/经纬度/电话），
作为供应商名录的"骨架"，业务联系方式后续从企业官网补充核实。

合规说明：
- 高德开放平台是官方开发者 API，个人开发者免费额度够用（每日限次）
- 仅拉取企业公开 POI 信息，不涉及个人隐私
- 需要你去 https://lbs.amap.com 注册开发者账号获取 Web 服务 Key

用法:
    export AMAP_KEY=你的key   (或直接改下面的常量)
    python scripts/fetch_gaode_poi.py --keyword "CNC加工" --city 东莞 --limit 50
"""
import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

AMAP_KEY = ""  # TODO: 填入高德 Web 服务 Key，或通过环境变量 AMAP_KEY 传入
BASE = "https://restapi.amap.com/v3/place/text"

REQUEST_COUNT = 0  # 本次运行累计 API 请求次数（配额统计用）

# 本次运行的请求硬上限（None = 不限）。给调用方（如 GUI）设置，用来保证
# 「翻页过程中」也不会越过日配额——只在任务之间检查是不够的：一个任务最多
# 能翻 ceil(limit/offset) 页，任务前检查通过也可能把配额打穿几个请求。
MAX_REQUESTS = None

# 高德明确返回配额用尽（info 含 OVER/LIMIT/QUOTA）时置 True。
# 调用方（GUI / CLI）据此提前结束整轮，而不是继续用无效 key 空转。
QUOTA_EXHAUSTED = False

ROOT = Path(__file__).resolve().parent.parent


def fetch(keyword, city, limit, offset=20, delay=0.5, types=None):
    """分页拉取高德 POI，直到拿满 limit 或没有更多数据。自动统计请求次数（REQUEST_COUNT）。

    types: POI 类型过滤。默认 None = 不限制（全召回）。
      历史默认值 "商务住宅|科教文化服务|公司企业" 问题很大：
      - 漏召回：制造业小厂常被高德归到「生活服务」，实测「CNC加工」上海
        不限 types 能多抓到「电火花中走丝精密CNC加工中心」这类真加工点
      - 引入噪声：「商务住宅」会把住宅小区、公寓抓进来
      故改为默认全召回 + 保存 type/typecode，把噪声判断交给下游 auto_profile。
    """
    global REQUEST_COUNT, QUOTA_EXHAUSTED
    pois_all = []
    page = 1
    while len(pois_all) < limit:
        # 翻页内的配额护栏：任务间检查会漏掉同一任务的多页请求
        if MAX_REQUESTS is not None and REQUEST_COUNT >= MAX_REQUESTS:
            print(f"  >>> 已达本次请求上限 {MAX_REQUESTS}，停止翻页（已取 {len(pois_all)} 条）")
            break
        params = {
            "key": AMAP_KEY,
            "keywords": keyword,
            "city": city,
            "citylimit": "true",
            "offset": offset,
            "page": page,
            "extensions": "all",  # base 只给骨架；all 才有 type/typecode/website/email/alias
        }
        if types:
            params["types"] = types
        url = BASE + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"请求失败: {e}")
            break
        REQUEST_COUNT += 1  # 成功请求才计数（避免空结果也扣配额）
        if data.get("status") != "1":
            info = data.get("info", "")
            print(f"API 返回错误: {info}")
            if "OVER" in info.upper() or "LIMIT" in info.upper() or "QUOTA" in info.upper():
                QUOTA_EXHAUSTED = True
                print(">>> 配额已用尽（次日 00:00 重置），本次停止抓取。")
            break
        pois = data.get("pois", [])
        pois_all.extend(pois)
        print(f"  第 {page} 页: +{len(pois)} 条（累计 {len(pois_all)} / 目标 {limit}，本次已用请求 {REQUEST_COUNT}）")
        if len(pois) < offset:
            break  # 没有更多数据
        page += 1
        time.sleep(delay)  # 尊重 API 配额
    return pois_all


def normalize_region(province, city):
    """规范省市名称：去掉 省/市/自治区 等后缀，保证与查询参数一致"""
    return (
        province.replace("省", "").replace("市", "").replace("自治区", "")
        .replace("壮族", "").replace("回族", "").replace("维吾尔", ""),
        city.replace("市", ""),
    )


def clean_phone(phone_str):
    """清洗高德 tel 字段：多个号码分号分隔逐号处理，保留完整号码。

    数据策略：联系方式为高德公开名录（POI）数据，直接完整展示，不做星号脱敏。
    """
    if not phone_str:
        return "待核实"
    parts = [p.strip() for p in re.split(r"[;；,，]", str(phone_str)) if p.strip()]
    return "; ".join(parts)


def _first(v):
    """高德常把空值返回成 []，统一成空串。"""
    if v in (None, [], ""):
        return ""
    if isinstance(v, list):
        return ";".join(str(x) for x in v if x)
    return str(v)


# ---------------------------------------------------------------- 数据状态判定
# SPEC §2.4：新记录一律写 status，is_template 只是兼容字段。
# 2026-09-09 修正：本函数此前直接写 is_template = (无电话)，等于把「真实企业但高德没
# 返回电话」标成「示例占位数据」——这正是 2026-09-08 已经修掉、却在抓取层复发的坑。
PENDING_PHONE = "待核实"
# 只认「示例」：「测试」会误伤真公司（实测 CN-MFG-0003331「…一测试架」是做测试治具的）
PLACEHOLDER_MARKS = ("示例",)


def has_real_number(phone):
    """电话里是否含至少一个真实号码。

    实测存在「待核实; 待核实」「0755-29005786; 待核实」这类多值电话，
    只做全等判断会把「有真号 + 一条待核实」的记录误判成待核实。
    """
    parts = [p.strip() for p in str(phone or "").replace("；", ";").split(";")]
    return any(ch.isdigit() for p in parts for ch in p if p and p != PENDING_PHONE)


def resolve_status(company, phone):
    """按 SPEC §2.4 判定状态：template / unverified_poi / verified。"""
    if any(m in (company or "") for m in PLACEHOLDER_MARKS):
        return "template"
    if not has_real_number(phone):
        return "unverified_poi"  # 真实企业，只是电话待核实——保留展示，不是占位数据
    return "verified"


def build_amap(poi, keyword):
    """抽取 extensions=all 才有的扩展字段。

    这些是下游推断能力的关键证据：
      type/typecode —— 三级分类（如「公司企业;工厂;工厂」），可判断是否为真实生产企业
      alias/tag     —— 别名与标签，常含工艺/材料线索
      website/email —— 供后续自动预填与认主联系
    """
    return {
        "poi_id": _first(poi.get("id")),
        "type": _first(poi.get("type")),
        "typecode": _first(poi.get("typecode")),
        "alias": _first(poi.get("alias")),
        "tag": _first(poi.get("tag")),
        "keytag": _first(poi.get("keytag")),
        "website": _first(poi.get("website")),
        "email": _first(poi.get("email")),
        "postcode": _first(poi.get("postcode")),
        "business_area": _first(poi.get("business_area")),
        "adcode": _first(poi.get("adcode")),
        "search_keyword": keyword,  # 抓到它时用的词，是「名义做什么」的弱证据
    }


def classify_at_ingest(rec, industry_code=None):
    """入库即打国标行业标签（GB/T 4754）。

    新建记录时调用；公司名是最强证据，抓它的目标代码只在名字毫无信号时兜底
    （confidence=low, source="search_keyword"）。拿不到信号就留 null，不硬贴。
    """
    try:
        from classify_industry import classify, build_industry
    except ImportError:  # 直接单跑本脚本时 scripts/ 可能不在 path 上
        return None, True
    code, conf, src, ev = classify(rec, expected_code=industry_code)
    if code is None:
        return None, True
    return build_industry(code, conf, src, ev), not code.startswith(("51", "52"))


def to_supplier(poi, category, keyword, seq, industry_code=None):
    """POI → 供应商记录（按 SPEC §2.4 写 status，is_template 仅作兼容）"""
    province, city = normalize_region(poi.get("pname", ""), poi.get("cityname", ""))
    phone = clean_phone(poi.get("tel"))  # 高德公开名录电话，完整入库
    status = resolve_status(poi.get("name", ""), phone)
    today = date.today().isoformat()
    rec = {
        "id": f"CN-MFG-{seq:07d}",
        "company": poi.get("name", ""),
        "category": category,
        "keywords": [keyword],
        "region": {"province": province, "city": city},
        "address": poi.get("address") or (poi.get("pname", "") + poi.get("adname", "")),
        "contact_phone": phone,
        "lat": float(poi.get("location", "").split(",")[1]) if poi.get("location") else None,
        "lng": float(poi.get("location", "").split(",")[0]) if poi.get("location") else None,
        "source": "public_directory",  # 公开名录
        "source_url": "",  # 不再点名数据源（去出处策略）
        # verified_at 已废弃，schema 里它的实际含义就是「导入日期」；
        # 这里曾经硬编码 "2026-08-13"，等于给每条新记录编一个核实日期——已改为当天。
        "verified_at": today,
        "imported_at": today,          # schema 现行字段：数据导入日期
        "last_verified_at": None,      # 未人工核验，POI 数据不等于已核实
        "status": status,              # SPEC §2.4 主状态字段
        "is_template": status != "verified",  # 兼容字段，语义与 status 对齐
        "note": "",  # 不记录抓取关键词与数据源（去出处策略）
        "amap": build_amap(poi, keyword),  # 高德扩展字段，供能力推断用
    }
    ind, is_mfr = classify_at_ingest(rec, industry_code)
    rec["industry"] = ind
    rec["is_manufacturer"] = is_mfr
    return rec


def save_suppliers(pois, category, keyword, industry_code=None):
    """把 POI 列表增量写入国标归档 data/gb/，返回新增条数。

    2026-09-08 起不再写 `data/suppliers/{品类}.json`（8 品类已废弃）：
    落位完全由记录的国标码决定，经 gb_store 统一推算。

    去重范围也随之改为**全库**：归档改按国标小类分文件后，同一家公司
    被两个关键词命中就会落到两个小类文件里各存一份（原按品类文件去重拦不住，
    实测全库 20265 条里重名就有 725 条）。所以这里用 gb_store 的全库名字索引。

    industry_code：本次任务的国标小类目标代码，入库时用来打行业标签。
    """
    import gb_store

    cache = gb_store.scan_cache()
    names = cache["names"]
    dirty: dict[str, list] = {}

    def rows_of(bucket):
        if bucket not in dirty:
            dirty[bucket] = gb_store.load_bucket(bucket)
        return dirty[bucket]

    new_pois = []
    updated = 0
    for p in pois:
        name = p.get("name")
        if not name:
            continue
        hit = names.get(name)
        if hit:
            bucket, sid = hit
            rec = next((r for r in rows_of(bucket) if r.get("id") == sid), None)
            if rec is None:
                # 索引与磁盘不一致（多半是别处改过盘），当新记录处理更稳妥
                new_pois.append(p)
                continue
            kws = rec.get("keywords") or []
            if keyword not in kws:
                kws.append(keyword)
                rec["keywords"] = kws
                updated += 1
            # 老数据是 extensions=base 抓的，没有 type/typecode，趁这次补上
            amap = build_amap(p, keyword)
            if not (rec.get("amap") or {}).get("type") and amap.get("type"):
                rec["amap"] = amap
                updated += 1
            # 同一家公司（按名字去重命中）这次带回电话 → 单向升级「待核实 → verified」。
            # 只填空不覆盖：已有号码是更强的证据，不能被后抓到的弱证据改写。
            if not has_real_number(rec.get("contact_phone")):
                new_tel = clean_phone(p.get("tel"))
                if has_real_number(new_tel):
                    rec["contact_phone"] = new_tel
                    rec["status"] = resolve_status(rec.get("company", ""), new_tel)
                    rec["is_template"] = rec["status"] != "verified"
                    updated += 1
            # 存量记录缺 status（2026-09-08 的迁移只覆盖到一部分），趁这次补齐
            if "status" not in rec:
                rec["status"] = resolve_status(rec.get("company", ""),
                                               rec.get("contact_phone"))
                rec["is_template"] = rec["status"] != "verified"
                updated += 1
            continue
        new_pois.append(p)

    if not new_pois:
        if updated:
            for bucket, rows in dirty.items():
                gb_store.save_bucket(bucket, rows)
            print(f"  无新增企业，但更新了 {updated} 处（累加关键词/补全扩展字段）")
        return 0

    seq_start = cache["max_id"] + 1
    added_by_bucket: dict[str, int] = {}
    for i, p in enumerate(new_pois):
        rec = to_supplier(p, category, keyword, seq_start + i, industry_code)
        bucket = gb_store.bucket_of(rec)
        rows_of(bucket).append(rec)
        names[p["name"]] = (bucket, rec["id"])
        added_by_bucket[bucket] = added_by_bucket.get(bucket, 0) + 1

    for bucket, rows in dirty.items():
        gb_store.save_bucket(bucket, rows)

    # 增量维护缓存，避免下一个任务又全量扫一遍 2 万条
    cache["max_id"] = seq_start + len(new_pois) - 1

    where = "、".join("%s+%d" % (b, n) for b, n in
                      sorted(added_by_bucket.items(), key=lambda x: -x[1])[:3])
    print(f"  新增 {len(new_pois)} 条（ID {seq_start}~{seq_start + len(new_pois) - 1}）"
          f"{f'，更新 {updated} 处' if updated else ''} → {where}")
    return len(new_pois)


def main():
    parser = argparse.ArgumentParser(description="高德 POI 抓取框架（制造业供应商骨架）")
    parser.add_argument("--keyword", required=True, help="搜索关键词，如 CNC加工 / 注塑 / 钣金")
    parser.add_argument("--city", required=True, help="城市，如 东莞 / 深圳")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--category", default="精密机械加工", help="映射到的品类名")
    parser.add_argument("--types", default="",
                        help="POI 类型过滤，如 '公司企业'。默认留空=不限（全召回，噪声交给下游过滤）")
    parser.add_argument("--no-post", action="store_true",
                        help="跳过抓后流水线（默认是跑的：重建索引/指纹/能力卡分片/清单/校验）")
    args = parser.parse_args()

    global AMAP_KEY
    import os
    AMAP_KEY = os.environ.get("AMAP_KEY", AMAP_KEY)
    if not AMAP_KEY:
        print("请先设置 API Key：export AMAP_KEY=你的key，或在 lbs.amap.com 注册获取")
        raise SystemExit(1)

    pois = fetch(args.keyword, args.city, args.limit, types=args.types or None)
    added = save_suppliers(pois, args.category, args.keyword)
    if added == 0:
        print("无新增（已全部存在或没有数据）。")
    print(f"归档位置：data/gb/<门类>/<大类>/<小类>.json（由国标码决定，见 data/gb-index.json）")
    print("提醒：无电话的 POI 记 status=unverified_poi（真实企业，保留展示，标注「电话待核实」），"
          "不是 template 占位数据")

    if added == 0:
        return 0
    if args.no_post:
        print("\n[警告] --no-post：派生层未重建（索引/指纹/能力卡分片/清单），新数据检索不到。")
        print("       手动补跑：python scripts/postfetch.py")
        return 0

    # 派生层必须跟着落库一起更新，否则「抓到了但客户搜不到」。
    # classify/index → fingerprint → shards → manifest → validate 的顺序由 postfetch 管。
    from postfetch import last_failed, run as postfetch_run

    failed = postfetch_run()
    if failed:
        print("\n[警告] 抓后流水线失败 %d 步 → %s（产物是旧值，别发布）"
              % (failed, "、".join(last_failed())))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
