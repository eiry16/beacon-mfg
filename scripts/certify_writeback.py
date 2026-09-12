# -*- coding: utf-8 -*-
"""把认证结果写回公开名录，让客户 Agent 真的能搜到已认证企业。

为什么必须做这一层：认证档案存在 `server/.data/certification/`（私有层，不进 Git）。
不做回流的话，认证做得再严，客户 Agent 按 SKILL.md 走（读 data/gb + 索引）
也永远搜不到这家企业 —— SKILL.md 承诺的 `cl` 字段在数据里一条都没有。

做什么：
1. 把审核通过的申请落成/更新为 data/gb/{门类}/{大类}/{小类}.json 里的记录
   （经 gb_store.upsert：按 industry_code 落位，跨桶迁移自动清理旧桶），
   写入 `cl`（凭证等级）与 `certification`（灯牌、有效期、审核员、profile、完成度）
2. 生成 data/certified-index.json：按灯牌列出已认证企业，客户 Agent 直接读这个即可
3. 重建 data/gb-index.json，保持四级树计数同步

红线：
- 灯牌只从申请档案读（evaluate 算出来的），本脚本不参与评级
- 电话只取 identity.contact_phone（已验证码通过），绝不写占位串
- 不适用的硬指标留空，不编造
- source 只能写枚举值 "certification"（见 validate.py SOURCE_ENUM），不写中文描述
- **不覆盖公开采集来的字段**：`gb_store.upsert` 是整条替换，而本脚本只产出认证
  相关的 18 个字段。所以写回前必须先 `merge_preserving` 与名录里的旧记录合并，
  否则一条带坐标/POI/skill 链接的记录会被换成"没有坐标"的版本。
  实测（2026-09-10）踩到：耐特斯那份记录丢了 `is_template`；按全库统计，
  这么写会丢 100% 记录的 `lat`/`lng`、83.9% 的 `amap`、17.4% 的 `agent`。
  合并后仍少字段则**中止不写盘**（宁可不发布，不可静默丢数据）。

用法：
    python scripts/certify_writeback.py            # 预览
    python scripts/certify_writeback.py --apply    # 落盘
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CERT_DIR = ROOT / "server" / ".data" / "certification"
OUT_INDEX = ROOT / "data" / "certified-index.json"

import gb_store  # noqa: E402
from industry_taxonomy import (  # noqa: E402
    name_of, path_of, level_of, is_manufacturer, category_of,
)

# 申报省份 → (province, city)。直辖市 province=city。
_MUNICIPAL = {"北京", "上海", "天津", "重庆"}


def split_region(claimed_address: str):
    """地址 → (省, 市)。返回值口径与归档 data/gb 一致：**不带「省」「市」后缀**。

    归档 24085 条的 region 是 {"province": "江苏", "city": "苏州"}；
    原实现直接返回正则捕获组，province 留着「省」字，且 city 的正则没锚定
    —— `([一-龥]{2,})(市|地区…)` 非贪婪不成立、会从串首贪婪吃掉省名，
    「江苏省苏州市…」被解析成 city="江苏省苏州"（2026-09-12 实测）。
    所以这里对 province 去后缀、city 从省名之后开始匹配。
    """
    addr = claimed_address or ""
    m = re.match(r"^(.+?[省]|.+?自治区|北京|上海|天津|重庆)", addr)
    province = (m.group(1) if m else "").replace("自治区", "").replace("省", "")
    if province in _MUNICIPAL:
        return province, province
    rest = addr[len(m.group(1)):] if m else addr
    m2 = re.match(r"^([一-龥]{2,}?)(市|地区|自治州|盟)", rest)
    return province, (m2.group(1) if m2 else "")


def load_existing(supplier_id: str) -> dict | None:
    """取这条 id 在名录里已有的记录。查不到返回 None（新企业，属常态）。"""
    try:
        bucket = gb_store.locate(supplier_id)
    except Exception:
        return None
    if not bucket:
        return None
    for r in gb_store.load_bucket(bucket):
        if r.get("id") == supplier_id:
            return r
    return None


def merge_preserving(old: dict, new: dict) -> dict:
    """认证层只改它负责的字段，**不覆盖公开采集来的字段**。

    为什么必须合并：`gb_store.upsert` 是**整条替换**（`rows[idx] = rec`），而
    `build_record` 只产出认证相关的 18 个字段。直接 upsert 的后果是——一条公开
    名录记录被替换成"没有坐标、没有 POI"的版本。实测（2026-09-10）：
    耐特斯那份记录丢了 `is_template`。

    这不是个别字段的问题，是全库规模的：名录里 **100%** 的记录带
    `lat`/`lng`/`is_template`、**83.9%** 带 `amap` POI 块、**17.4%** 带
    `agent`（供应商 skill 链接）块。其中 `agent` 一丢，客户 Agent 检索命中后
    就拿不到 `skill_url`，链路断在这里——而这整个过程**不报任何错**。

    列表字段取并集（去重）：认证带来的关键词不该抹掉公开采集的工艺/材料词。
    """
    merged = {**old, **new}
    for k in ("keywords", "certifications"):
        a, b = old.get(k), new.get(k)
        if isinstance(a, list) and isinstance(b, list):
            seen, out = set(), []
            for v in list(a) + list(b):
                key = (json.dumps(v, ensure_ascii=False, sort_keys=True)
                       if isinstance(v, dict) else str(v))
                if key not in seen:
                    seen.add(key)
                    out.append(v)
            merged[k] = out
    return merged


def build_record(app: dict) -> dict:
    ident = app.get("identity") or {}
    cap = app.get("capability") or {}
    cdata = cap.get("data") or {}
    review = app.get("review") or {}
    comp = cap.get("completeness") or {}

    province, city = split_region(ident.get("claimed_address", ""))
    industry = None
    code = cdata.get("industry_code") or app.get("industry_code")
    if code:
        industry = {
            "code": code,
            "name": name_of(code),
            "level": level_of(code),
            "path": path_of(code),
            "confidence": "high",
            "source": "certification",
        }

    keywords = []
    proc_en = cdata.get("processes") or []
    if proc_en:
        keywords.append("工艺:" + "/".join(proc_en))
    if cdata.get("materials"):
        keywords.append("材料:" + "/".join(cdata["materials"]))

    # category 是遗留字段（8 品类时代）：归档与检索已完全走国标（industry_code），
    # 但 validate.py 仍要求 category ∈ index.json 的品类名，所以这里用
    # category_of() 把国标码映射回旧品类名，而不是申请时随手填的值。
    cat = app.get("category")
    if code:
        cat = category_of(code)

    return {
        "id": app["supplier_id"],
        "company": app["company"],
        "category": cat or "未分类",
        "keywords": keywords or ["认证企业"],
        "region": {"province": province, "city": city},
        "address": ident.get("claimed_address", ""),
        "contact_phone": ident.get("contact_phone") or None,
        "website": None,
        "certifications": (cdata.get("quality") or {}).get("certifications") or [],
        "source": "certification",
        "source_url": None,
        "verified_at": (review.get("at") or "")[:10] or None,
        "status": "verified",
        "note": "已完成平台认证，灯牌由规则引擎算出",
        "industry": industry,
        "is_manufacturer": is_manufacturer(code) if code else bool(ident.get("business_nature") == "manufacturer"),
        "cl": app.get("badge"),
        "certification": {
            "app_id": app.get("app_id"),
            "badge": app.get("badge"),
            "issued_at": (app.get("badge_issued_at") or "")[:10] or None,
            "expires_at": (app.get("badge_expires_at") or "")[:10] or None,
            "reviewer": review.get("reviewer"),
            "profile": cap.get("profile"),
            "completeness": comp.get("score"),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写回 data/gb 并重建认证索引")
    a = ap.parse_args()

    if not CERT_DIR.exists():
        print("没有认证档案目录：%s" % CERT_DIR)
        return 0

    approved = []
    for f in sorted(CERT_DIR.glob("CERT-*.json")):
        try:
            app = json.load(open(f, encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print("  [跳过] %s 解析失败: %s" % (f.name, e))
            continue
        if app.get("stage") == "approved" and app.get("badge"):
            approved.append(app)

    if not approved:
        print("没有已通过审核的认证申请")
        return 0

    recs = []
    lost_report: list[str] = []
    for app in approved:
        rec = build_record(app)
        old = load_existing(rec["id"])
        note = ""
        if old:
            before = set(old.keys())
            rec = merge_preserving(old, rec)
            gone = before - set(rec.keys())
            if gone:
                # 合并之后还少了字段 = 合并逻辑本身有洞。**不许写盘**：
                # 写下去就是静默的数据丢失，谁也发现不了。
                lost_report.append("%s：%s" % (rec["id"], "、".join(sorted(gone))))
            kept = before & set(rec.keys())
            note = "  保留公开字段 %d 个" % len(kept - set(build_record(app).keys()))
        recs.append(rec)
        bucket = gb_store.bucket_of(rec)
        print("%-8s %-32s %s -> data/gb/%s.json  %s%%%s"
              % (app["badge"], app["company"][:30], app["supplier_id"],
                 bucket, (app.get("capability") or {}).get("completeness", {}).get("score"),
                 note))

    if lost_report:
        print("\n[ERROR] 回流会丢掉名录里已有的字段，已中止（不写盘）：")
        for line in lost_report:
            print("  - " + line)
        return 1

    if not a.apply:
        print("\n（预览模式，加 --apply 落盘）")
        return 0

    before = gb_store.stats()
    result = gb_store.upsert(recs)
    gb_store.rebuild_index()
    print("  落盘：新增 %d / 更新 %d / 跨桶迁移 %d（总数 %d → %d，应守恒）"
          % (result["added"], result["updated"], result["moved"],
             before["total"], gb_store.stats()["total"]))

    index = {}
    for rec in recs:
        b = rec["cl"]
        index.setdefault(b, []).append({
            "id": rec["id"], "company": rec["company"], "category": rec["category"],
            "region": rec["region"], "cl": b,
            "issued_at": rec["certification"]["issued_at"],
            "expires_at": rec["certification"]["expires_at"],
            "completeness": rec["certification"]["completeness"],
        })
    payload = {
        "metadata": {
            "description": "已认证企业索引。cl = 凭证等级（L0 未核验 / L1 企业自述 / "
                           "L2 平台已认证 / L3 第三方核验），灯牌由规则引擎算出，不可指定。",
            "generated_at": date.today().isoformat(),
            "counts": {b: len(v) for b, v in index.items()},
            "total": sum(len(v) for v in index.values()),
        },
        "index": index,
    }
    with open(OUT_INDEX, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    print("\n已写回 data/gb（%d 条），认证索引 %s（%d 家）"
          % (result["added"] + result["updated"], OUT_INDEX.name, payload["metadata"]["total"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
