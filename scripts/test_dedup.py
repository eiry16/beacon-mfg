#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判重（去重）回归测试 —— 覆盖 2026-09-19 数据库质量检查发现的 4 个缺陷。

为什么要有这个测试
------------------
`save_suppliers()` 的判重逻辑改过一次就炸过一次：
  · 旧版用「全局精确公司名」判重 → 跨城市同名不同实体被静默丢弃
  · 中间版本把 `register_target()` 推迟到第二遍循环 → 同批次重复条目全部漏判，
    实测造成 856 条同一 POI 拿到两个编号
这两个都不是靠读代码能一眼看出来的「顺序敏感」缺陷，必须有自动化回归。

跑法：python scripts/test_dedup.py   （沙箱目录，不碰真实数据、不发网络请求）
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gb_store  # noqa: E402
import fetch_gaode_poi as F  # noqa: E402

_PASS = _FAIL = 0


def _poi(name, city, addr, pid=None, loc="121.000000,31.000000"):
    """构造一个高德 POI 的最小可用形态。"""
    return {"id": pid, "name": name, "address": addr, "tel": "13800000000",
            "location": loc, "pname": "某省", "cityname": city, "adname": "某区",
            "type": "公司企业;公司;公司", "typecode": "170200"}


def _run(pois, category="制造业", keyword="测试", code=None):
    """在临时目录里跑一次 save_suppliers，返回 (新增数, 全部记录)。"""
    tmp = Path(tempfile.mkdtemp(prefix="dedup_t_"))
    gb_store.GB_DIR = tmp / "gb"
    gb_store.GB_DIR.mkdir(parents=True)
    gb_store.invalidate_cache()
    try:
        n = F.save_suppliers(pois, category, keyword, code)
        rows = []
        for f in gb_store.GB_DIR.rglob("*.json"):
            rows.extend(json.load(open(f, encoding="utf-8")))
        return n, rows
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print("  PASS  %-46s %s" % (label, got))
    else:
        _FAIL += 1
        print("  FAIL  %-46s got=%s want=%s" % (label, got, want))


print("=" * 76)
print("判重回归测试")
print("=" * 76)

# ── 缺陷 1：批内不去重（id 分配前未登记 → 同一 POI 拿两个编号）
print("\n[缺陷1] 同一批 pois 内出现重复条目")
n, rows = _run([_poi("肆花社花园餐厅", "贵阳", "黔灵镇三桥村16号", "B0L0USVJZ3"),
                _poi("肆花社花园餐厅", "贵阳", "黔灵镇三桥村16号", "B0L0USVJZ3")],
               "住宿和餐饮业", "餐厅", "6210")
check("同批同 poi_id ×2 → 入库条数", len(rows), 1)
check("  只发了 1 个编号", n, 1)

n, rows = _run([_poi("云创广告图文装饰", "南京", "迈皋桥街道怡宁路183号"),
                _poi("云创广告图文装饰", "南京", "迈皋桥街道怡宁路183号")],
               "租赁和商务服务业", "亚克力加工")
check("同批无 poi_id、同城同名同址 ×2 → 入库条数", len(rows), 1)

# ── 缺陷 2：键不含 poi_id
print("\n[缺陷2] poi_id 生效")
n, rows = _run([_poi("甲公司", "深圳", "科技园1号", "X1"),
                _poi("甲公司深圳分公司", "深圳", "科技园1号", "X1")],
               "制造业", " consultant ")
check("同一 poi_id、名称不同 → 入库条数", len(rows), 1)

# ── 缺陷 3：键不含城市（跨城同名被误吞 —— 这是最隐蔽的数据丢失）
print("\n[缺陷3] 跨城市同名不同实体必须都保留")
n, rows = _run([_poi("固特异轮胎", "宁波", "中山东路1号", "G1"),
                _poi("固特异轮胎", "常州", "延陵中路2号", "G2")],
               "批发和零售业", "五金批发", "5174")
check("跨城同名 → 入库条数", len(rows), 2)
check("  城市都应留下", sorted({(r.get("region") or {}).get("city") for r in rows}),
      sorted(["常州", "宁波"]))

# ── 缺陷 4：无归一化（别名漏判）
print("\n[缺陷4] 别名归一化")
n, rows = _run([_poi("张三五金店", "苏州", "干将东路5号", "H1"),
                _poi("张三五金有限公司", "苏州", "干将东路5号", "H2")],
               "批发和零售业", "五金店", "5281")
check("同址 + 名称差后缀 → 入库条数", len(rows), 1)

# ── 反向护栏：连锁门店绝不能被合并
print("\n[护栏] 连锁门店（同名不同址）必须各自保留")
n, rows = _run([_poi("星巴克(南京西路店)", "上海", "南京西路1266号", "S1"),
                _poi("星巴克(人民广场店)", "上海", "人民大道100号", "S2"),
                _poi("星巴克(徐家汇店)", "上海", "肇嘉浜路1000号", "S3")],
               "住宿和餐饮业", "咖啡", "6232")
check("3 家门店 → 入库条数", len(rows), 3)
check("  地址互不相同", len({r.get("address") for r in rows}), 3)

# ── 合并语义：命中已有记录时累加关键词、不新增
print("\n[合并语义] 再次命中应累加关键词而非新建")
tmp = Path(tempfile.mkdtemp(prefix="dedup_t_"))
gb_store.GB_DIR = tmp / "gb"
gb_store.GB_DIR.mkdir(parents=True)
gb_store.invalidate_cache()
try:
    p = _poi("肆花社花园餐厅", "贵阳", "黔灵镇三桥村16号", "B0L0USVJZ3")
    F.save_suppliers([p], "住宿和餐饮业", "餐厅", "6210")
    n2, rows2 = 0, []
    F.save_suppliers([p], "住宿和餐饮业", "私房菜", "6210")
    for f in gb_store.GB_DIR.rglob("*.json"):
        rows2.extend(json.load(open(f, encoding="utf-8")))
    check("二次抓取后仍只有 1 条", len(rows2), 1)
    check("  keywords 累加", sorted(rows2[0].get("keywords") or []), ["私房菜", "餐厅"])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ── 编号连续性：新记录必须顺延，不能与已有冲突
print("\n[编号] ID 必须在已有最大值上递增")
tmp = Path(tempfile.mkdtemp(prefix="dedup_t_"))
gb_store.GB_DIR = tmp / "gb"
gb_store.GB_DIR.mkdir(parents=True)
gb_store.invalidate_cache()
try:
    F.save_suppliers([_poi("甲", "上海", "a1", "N1")], "制造业", "kw1")
    F.save_suppliers([_poi("乙", "上海", "a2", "N2")], "制造业", "kw2")
    rows = []
    for f in gb_store.GB_DIR.rglob("*.json"):
        rows.extend(json.load(open(f, encoding="utf-8")))
    ids = sorted(r["id"] for r in rows)
    check("两批次共 2 条", len(ids), 2)
    check("  ID 不重复且递增", ids, ["CN-MFG-0000001", "CN-MFG-0000002"])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 76)
print("通过 %d / 失败 %d" % (_PASS, _FAIL))
print("=" * 76)
sys.exit(1 if _FAIL else 0)
