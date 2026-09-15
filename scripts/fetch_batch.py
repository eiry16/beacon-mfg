#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 批量抓取 —— 按 GB/T 4754-2017 国标小类抓（合规绿区：高德开放 API）

2026-09-08 改造：抓取矩阵从「8 个采购品类」改为「国标小类代码 × 关键词 × 城市」。

为什么要改：
    旧矩阵按品类组织，品类是*采购视角*（找注塑的人不一定在意企业国标代码是 2929 还是 3525），
    结果抓回来的企业没有统一的行业口径 —— 检索「模具制造」要在 3 个品类文件里翻，
    而「铸造/锻造/橡胶/齿轮/阀门/汽配」这些国标里明确存在的行业**一个都没抓过**。
    现在按国标小类组织，抓的时候就知道自己要什么行业，落库时存到最近的品类文件
    （映射见 scripts/industry_taxonomy.py 的 CATEGORY_OF_CODE）。

关键词写法沿用踩过的坑：
    - 高德上注塑厂极少叫「注塑」，叫「塑料制品/塑胶制品/塑料模具」（实测嘉兴「注塑加工」
      只召回 1 条，「塑料制品」满 60 条）
    - 不限 types 全召回，噪声交给下游；所以关键词本身要挑**高德上真实存在的叫法**

分层：
    core     —— 对应原有 8 个品类覆盖的行业（日常增量维护跑这个）
    extended —— 国标里存在但此前完全没抓的行业（补覆盖时跑）

用法:
    export AMAP_KEY=你的key
    python scripts/fetch_batch.py --dry-run                 # 打印任务计划
    python scripts/fetch_batch.py                           # core 全矩阵
    python scripts/fetch_batch.py --tier extended           # 只补新行业
    python scripts/fetch_batch.py --industry 3525           # 只抓模具制造
    python scripts/fetch_batch.py --industry 33 --city 苏州  # 大类 33（金属制品业）前缀匹配
    python scripts/fetch_batch.py --limit 20                # 每任务限量 20 条（默认 60）

注意：
- 尊重配额：任务间默认 1s 延时；高德个人开发者每日有请求上限
- 抓取完自动补写 industry 字段（见 classify_industry.backfill），
  然后必须重建 data/industry-index.json，否则新数据在企业检索里搜不到
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_gaode_poi as fetcher  # noqa: E402
from industry_taxonomy import CODES, category_of, self_check  # noqa: E402

# ---------------------------------------------------------------- 城市分组
# 长三角优先（嘉兴重点），珠三角次之
YRD = ["上海", "苏州", "无锡", "常州", "宁波", "嘉兴", "昆山"]
YRD_WIDE = YRD + ["南京", "杭州", "南通", "温州", "绍兴"]
PRD = ["深圳", "东莞", "广州", "佛山"]
PRD_WIDE = PRD + ["中山", "惠州", "珠海"]
ALL_CITIES = ["上海", "苏州", "无锡", "常州", "宁波", "嘉兴", "昆山",
              "深圳", "东莞", "广州", "佛山"]

# ---------------------------------------------------------------- 抓取矩阵
# 国标小类代码 → (层级, 关键词列表, 城市列表)
JOBS = {
    # ============================================================ core
    "3484": ("core", ["CNC加工", "数控加工", "精密机械", "机械加工", "车铣复合", "加工中心"],
             ALL_CITIES),
    "3311": ("core", ["钣金加工", "激光切割", "冲压", "机箱机柜"],
             ["苏州", "无锡", "常州", "上海", "昆山", "嘉兴", "宁波", "南京",
              "深圳", "东莞", "佛山", "中山", "广州"]),
    "2929": ("core", ["注塑加工", "注塑成型", "塑料模具", "塑料制品", "塑胶制品", "塑料配件"],
             ["苏州", "昆山", "无锡", "嘉兴", "宁波", "上海", "杭州", "常州",
              "深圳", "东莞", "广州", "佛山"]),
    "3525": ("core", ["模具", "注塑模具", "冲压模具", "模具加工"],
             ["苏州", "昆山", "宁波", "上海", "嘉兴", "东莞", "深圳", "佛山"]),
    "3392": ("core", ["压铸", "铝合金压铸", "锌合金压铸"],
             ["苏州", "无锡", "常州", "宁波", "上海", "嘉兴", "昆山",
              "东莞", "佛山", "深圳"]),
    "3360": ("core", ["阳极氧化", "电镀", "喷涂", "热处理"],
             ["苏州", "无锡", "宁波", "上海", "嘉兴", "常州", "南京",
              "东莞", "深圳", "佛山", "惠州"]),
    "3982": ("core", ["PCB", "PCBA", "线路板", "电路板"],
             ["苏州", "无锡", "上海", "杭州", "嘉兴", "宁波", "南京",
              "深圳", "东莞", "惠州", "珠海", "广州"]),
    "3989": ("core", ["连接器", "线束", "端子", "接插件"],
             ["苏州", "无锡", "上海", "宁波", "深圳", "东莞"]),
    "3482": ("core", ["紧固件", "螺丝", "螺栓", "标准件"],
             ["嘉兴", "宁波", "温州", "上海", "苏州", "无锡", "常州", "海盐", "绍兴",
              "东莞", "深圳"]),
    "3451": ("core", ["轴承"], ["嘉兴", "宁波", "上海", "苏州", "无锡", "常州", "东莞", "深圳"]),
    "3483": ("core", ["弹簧"], ["嘉兴", "宁波", "上海", "苏州", "无锡", "常州", "深圳", "东莞"]),
    "3130": ("core", ["钢材", "不锈钢材料", "钢板"],
             ["上海", "苏州", "无锡", "宁波", "温州", "嘉兴", "南京", "佛山", "广州"]),
    "3252": ("core", ["铝型材", "铝板", "铝合金"],
             ["上海", "苏州", "无锡", "宁波", "佛山", "广州", "东莞"]),
    "3251": ("core", ["铜材", "铜管", "铜排"], ["上海", "苏州", "宁波", "温州", "佛山"]),
    "2651": ("core", ["工程塑料", "塑料原料", "改性塑料", "色母"],
             ["上海", "苏州", "宁波", "东莞", "深圳"]),

    # ============================================================ extended
    # 国标里明确存在、但旧矩阵一个关键词都没抓过的行业
    "3391": ("extended", ["铸造", "精密铸造", "铸铁", "翻砂"],
             ["苏州", "无锡", "常州", "宁波", "嘉兴", "佛山"]),
    "3393": ("extended", ["锻造", "锻件", "粉末冶金"],
             ["苏州", "无锡", "宁波", "常州", "上海", "嘉兴"]),
    "2913": ("extended", ["橡胶制品", "硅胶制品", "密封圈", "油封"],
             ["宁波", "上海", "苏州", "东莞", "深圳", "嘉兴"]),
    "2922": ("extended", ["塑料板材", "塑料管", "亚克力板"],
             ["上海", "苏州", "宁波", "东莞", "广州"]),
    "2921": ("extended", ["塑料薄膜", "缠绕膜", "吹膜"],
             ["上海", "苏州", "宁波", "广州", "东莞"]),
    "2926": ("extended", ["吸塑", "吹塑", "塑料包装"],
             ["上海", "苏州", "宁波", "深圳", "东莞"]),
    "3453": ("extended", ["齿轮", "减速机", "齿条", "链轮"],
             ["苏州", "无锡", "常州", "宁波", "上海", "嘉兴", "深圳", "东莞"]),
    "3481": ("extended", ["机械密封", "密封件"], ["苏州", "宁波", "上海", "深圳"]),
    "3321": ("extended", ["刀具", "铣刀", "钻头", "刀模"],
             ["苏州", "上海", "东莞", "深圳", "常州"]),
    "3443": ("extended", ["阀门"], ["上海", "苏州", "温州", "宁波"]),
    "3444": ("extended", ["液压", "油缸", "气缸"],
             ["上海", "苏州", "无锡", "宁波", "佛山"]),
    "3491": ("extended", ["工业机器人", "机械手"],
             ["上海", "苏州", "深圳", "东莞", "无锡"]),
    "4011": ("extended", ["自动化设备", "非标自动化", "流水线"],
             ["苏州", "上海", "深圳", "东莞", "无锡"]),
    "3591": ("extended", ["环保设备", "水处理设备", "除尘设备"],
             ["苏州", "上海", "无锡", "佛山", "深圳"]),
    "3831": ("extended", ["电线电缆", "线束加工"],
             ["上海", "苏州", "宁波", "东莞", "深圳"]),
    "3872": ("extended", ["LED照明", "灯具制造"], ["中山", "深圳", "东莞", "上海", "宁波"]),
    "3841": ("extended", ["锂电池", "电池制造"], ["深圳", "东莞", "苏州", "上海", "宁波"]),
    "3812": ("extended", ["电机制造", "马达"], ["上海", "苏州", "深圳", "东莞", "常州"]),
    "3660": ("extended", ["汽车配件", "汽车零部件", "汽配"],
             ["上海", "苏州", "宁波", "无锡", "深圳", "佛山"]),
    "3059": ("extended", ["钢化玻璃", "玻璃加工"], ["上海", "苏州", "深圳", "东莞", "佛山"]),
    "3072": ("extended", ["工业陶瓷", "特种陶瓷"], ["上海", "苏州", "东莞", "佛山"]),
    # 注意：高德搜「医疗器械」会召回药店/诊所，量小试点，噪声交给下游
    "3584": ("extended", ["医疗器械制造"], ["上海", "苏州", "深圳", "常州"]),
    "3983": ("extended", ["传感器"], ["上海", "苏州", "深圳", "无锡"]),
    "3973": ("extended", ["集成电路", "半导体", "芯片"], ["上海", "苏州", "深圳", "无锡"]),
    "3467": ("extended", ["包装机械", "包装设备"], ["上海", "苏州", "温州", "佛山", "深圳"]),
    "3399": ("extended", ["五金制品", "金属制品", "五金加工"],
             ["上海", "苏州", "宁波", "佛山", "深圳"]),

    # ============================================================ service（非制造新门类 第一波：住宿餐饮 + 居民服务/修理）
    # 2026-09-11 起从纯制造(C)/批发(F) 扩到实体门店服务门类。高德 POI 对实体店召回好，
    # 与制造/批发互补——客户 Agent 查「附近火锅/汽车维修」能直接命中。
    # 关键词用高德上的真实叫法（餐厅/火锅/奶茶/汽车维修…），不写行业书面语。
    # 单独成 service tier：默认 run_fetch 跑 core，不会误伤配额；要扩就 `--tier service` 或 `--tier all`。
    "6210": ("service", ["餐厅", "酒楼", "中餐馆", "火锅", "家常菜", "私房菜"],
             ["上海", "苏州", "深圳", "广州", "杭州", "宁波", "东莞", "佛山", "南京", "无锡", "常州", "嘉兴"]),
    "6220": ("service", ["快餐", "炸鸡", "汉堡", "便当", "简餐", "小吃店"],
             ["上海", "苏州", "深圳", "广州", "杭州", "宁波", "东莞", "佛山", "南京", "无锡"]),
    "6232": ("service", ["咖啡", "咖啡馆", "咖啡店"],
             ["上海", "苏州", "深圳", "广州", "杭州", "南京", "成都", "武汉"]),
    "6231": ("service", ["奶茶", "茶饮", "茶室", "茶馆"],
             ["上海", "苏州", "深圳", "广州", "杭州", "宁波", "东莞", "佛山"]),
    "6233": ("service", ["酒吧", "清吧", "餐吧"],
             ["上海", "深圳", "广州", "成都", "杭州"]),
    "6291": ("service", ["烘焙", "面包", "蛋糕", "西点", "甜品"],
             ["上海", "苏州", "深圳", "广州", "杭州", "宁波", "东莞", "佛山"]),
    "6110": ("service", ["酒店", "饭店", "宾馆", "度假酒店"],
             ["上海", "苏州", "深圳", "广州", "杭州", "南京", "成都", "武汉", "西安"]),
    "6130": ("service", ["民宿", "客栈", "精品民宿"],
             ["苏州", "杭州", "成都", "大理", "丽江", "莫干山"]),
    "8030": ("service", ["干洗", "洗衣", "洗衣店", "洗染"],
             ["上海", "苏州", "深圳", "广州", "杭州", "宁波", "南京", "无锡"]),
    "8040": ("service", ["美容", "美发", "理发", "美发店", "理发店"],
             ["上海", "苏州", "深圳", "广州", "杭州", "宁波", "东莞", "佛山", "南京"]),
    # 805 中类拆成 3 个真实小类：8050 不是合法小类码（已废弃，会落库失败）
    "8051": ("service", ["洗浴", "温泉", "水疗", "澡堂"],
             ["上海", "苏州", "深圳", "广州", "杭州", "南京", "武汉"]),
    "8052": ("service", ["足浴", "泡脚", "修脚"],
             ["上海", "苏州", "深圳", "广州", "杭州", "南京", "武汉"]),
    "8053": ("service", ["养生", "按摩", "SPA", "保健"],
             ["上海", "苏州", "深圳", "广州", "杭州", "南京", "武汉"]),
    "8111": ("service", ["汽车维修", "汽修", "汽车修理", "修车"],
             ["上海", "苏州", "深圳", "广州", "杭州", "宁波", "东莞", "佛山", "南京", "无锡", "常州", "嘉兴"]),
    "8121": ("service", ["电脑维修", "笔记本维修", "电脑上门维修"],
             ["上海", "深圳", "广州", "杭州", "南京", "武汉", "成都"]),
    "8122": ("service", ["家电维修", "空调维修", "洗衣机维修", "冰箱维修"],
             ["上海", "深圳", "广州", "杭州", "南京", "武汉", "成都", "苏州"]),

    # ============================================================ retail（F 批发和零售业）
    # 2026-09-14 补：F 门类此前只有历史遗留的 1806 条，JOBS 里一个任务都没有。
    # 制造业的「卖」与「造」采购方都要找，缺了批发零售链路是断的。
    "5165": ("retail", ["建材批发", "板材批发", "装饰材料批发"],
             ["上海", "苏州", "宁波", "东莞", "佛山", "深圳", "广州"]),
    "5174": ("retail", ["五金批发", "工具批发", "水暖批发"],
             ["上海", "苏州", "宁波", "东莞", "佛山", "深圳", "广州", "温州"]),
    "5164": ("retail", ["钢材批发", "不锈钢材料批发", "铜材批发", "铝材批发"],
             ["上海", "苏州", "无锡", "宁波", "佛山", "天津"]),
    "5281": ("retail", ["五金店", "五金机电", "五金工具"],
             ["上海", "苏州", "宁波", "东莞", "佛山", "深圳", "广州"]),
    "5251": ("retail", ["药店", "药房"],
             ["上海", "苏州", "深圳", "广州", "杭州", "南京", "武汉"]),
    "5213": ("retail", ["便利店"],
             ["上海", "深圳", "广州", "杭州", "南京", "苏州"]),

    # ============================================================ tech（I 信息技术 + M 科研技术）
    # 2026-09-14 补：I/M 门类已登记 schema（it-service / tech-research），但零抓取任务。
    # 客户 Agent 找「软件外包」「第三方检测」时不能一无所获。
    "6513": ("tech", ["软件开发", "软件公司", "软件外包"],
             ["上海", "深圳", "北京", "杭州", "广州", "南京", "成都", "武汉", "苏州"]),
    "6531": ("tech", ["系统集成", "信息系统集成"],
             ["上海", "深圳", "北京", "杭州", "广州", "南京", "成都"]),
    "6440": ("tech", ["网络安全", "信息安全"],
             ["上海", "深圳", "北京", "杭州", "成都"]),
    "6450": ("tech", ["大数据", "数据服务"],
             ["上海", "深圳", "北京", "杭州", "成都"]),
    "6540": ("tech", ["运维服务", "IT运维"],
             ["上海", "深圳", "北京", "杭州", "广州"]),
    "7452": ("tech", ["第三方检测", "检测技术", "检测服务"],
             ["上海", "苏州", "深圳", "广州", "宁波", "东莞", "佛山", "无锡"]),
    "7453": ("tech", ["计量校准", "计量检测"],
             ["上海", "苏州", "深圳", "广州", "宁波"]),
    "7455": ("tech", ["认证服务", "体系认证"],
             ["上海", "深圳", "广州", "杭州", "苏州"]),
    "7491": ("tech", ["工业设计", "产品设计"],
             ["上海", "深圳", "广州", "杭州", "苏州", "东莞"]),
    "7461": ("tech", ["环境检测", "环境监测"],
             ["上海", "苏州", "深圳", "广州", "南京", "武汉"]),

    # ============================================================ leisure（R 文化体育娱乐）
    # 2026-09-14 补：R 门类已登记 schema（entertainment），零抓取任务。
    "8930": ("leisure", ["健身房", "健身中心", "健身会所"],
             ["上海", "深圳", "广州", "杭州", "南京", "成都", "武汉", "苏州"]),
    "8929": ("leisure", ["羽毛球馆", "游泳馆", "篮球馆", "球馆"],
             ["上海", "深圳", "广州", "杭州", "南京", "成都", "武汉"]),
    "8760": ("leisure", ["电影院", "影城", "影院"],
             ["上海", "深圳", "广州", "杭州", "南京", "成都", "武汉"]),
    "9011": ("leisure", ["KTV", "量贩式KTV", "歌舞厅"],
             ["上海", "深圳", "广州", "杭州", "南京", "成都", "武汉"]),
    "9013": ("leisure", ["网吧", "网咖", "电竞馆"],
             ["上海", "深圳", "广州", "杭州", "南京", "武汉", "成都"]),
    "9020": ("leisure", ["游乐园", "游乐场", "水上乐园"],
             ["上海", "深圳", "广州", "杭州", "南京", "成都", "武汉"]),
}

# ---------------------------------------------------------------- 欠采样城市补采
# 2026-09-08 实测发现：上面矩阵的城市配额极不均衡（按任务数算 上海 117 / 苏州 119 /
# 宁波 96，而 杭州仅 10 / 惠州 8 / 中山 6 / 绍兴 4 / 珠海 4）。
# 后果是杭州 480 家却只覆盖 3 个品类（钣金、压铸、表面处理、标准件、原材料全为 0），
# 客户 Agent 查「杭州模具」只能拿到 16 家、其中 14 家没电话。
# 这里统一给这 5 个城市铺满 core 行业，不改上面的原始定义（保留可追溯的编辑历史）。
REFILL_CITIES = ["杭州", "绍兴", "惠州", "中山", "珠海"]

for _code, (_tier, _kws, _cities) in JOBS.items():
    if _tier != "core":
        continue  # extended 是补新行业，先不动，避免一次跑爆配额
    for _c in REFILL_CITIES:
        if _c not in _cities:
            _cities.append(_c)


def resolve_industries(selector):
    """把 --industry 参数解析成代码列表。

    支持：4 位小类码（3525）/ 2-3 位前缀（33 → 3391,3392,3393…）/ 中文名子串（"模具"）。
    只在本矩阵 JOBS 里已定义的行业中匹配，避免拼错代码跑空。
    """
    if not selector:
        return None
    want = {s.strip() for s in selector.split(",") if s.strip()}
    hit = set()
    for s in want:
        if s in JOBS:
            hit.add(s)
            continue
        for code in JOBS:
            if code.startswith(s):
                hit.add(code)
        for code, (_, _k, _c) in JOBS.items():
            if s in CODES[code]["name"]:
                hit.add(code)
    return hit


def plan(tier="core", industries=None, category=None, keyword=None, city=None):
    tasks = []
    for code, (t, kws, cities) in JOBS.items():
        if tier != "all" and t != tier:
            continue
        if industries is not None and code not in industries:
            continue
        cat = category_of(code)
        if category and cat != category:
            continue
        for kw in kws:
            if keyword and kw != keyword:
                continue
            for c in cities:
                if city and c != city:
                    continue
                tasks.append((code, cat, kw, c))
    return tasks


def main():
    parser = argparse.ArgumentParser(description="BeaconMFG 批量抓取（按 GB/T 4754 国标小类）")
    parser.add_argument("--limit", type=int, default=200,
                        help="每个任务抓取上限。默认 200 = 高德同参数翻页硬上限（翻到底）。"
                             "以前默认 60/40 只翻 2 页，抓到的永远是排序最前那一批")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    parser.add_argument("--tier", default=None,
                        choices=["core", "extended", "service", "retail", "tech", "leisure", "all"],
                        help="core=现有制造覆盖；extended=补制造新行业；service=住宿餐饮+居民服务修理；"
                             "retail=F 批发零售；tech=I 信息技术+M 科研技术；leisure=R 文体娱乐；all=全部。"
                             "默认 **all** —— 设计面就是 7 个门类，默认只跑 core 等于把 H/I/M/O/R"
                             "永久排除在外（2026-09-14 前的实际情况：餐饮抓了 4 个月一条都没有）。"
                             "配额不再被 core 独占：账本已按门类轮转（见 fetch_cursor._round_robin_by_gate），"
                             "每个门类分到大致相等的请求数。指定 --industry 时同样默认 all")
    parser.add_argument("--industry", help="国标代码，逗号分隔。支持前缀（33）或中文名（模具）")
    parser.add_argument("--category", help="只跑落到该存储品类的任务（兼容旧用法）")
    parser.add_argument("--keyword", help="只跑该关键词的任务")
    parser.add_argument("--city", help="只跑指定城市（逗号分隔多个）")
    parser.add_argument("--types", default="",
                        help="POI 类型过滤，默认留空=不限（全召回，噪声交给下游过滤）")
    parser.add_argument("--no-classify", action="store_true",
                        help="抓完不自动补写 industry 字段（会跳过流水线里的 classify 步）")
    parser.add_argument("--no-post", action="store_true",
                        help="抓完跳过整条抓后流水线（索引/指纹/能力卡分片/清单/校验都不重建）。"
                             "旧写法 --no-index 等价于跳其中的 index 步")
    parser.add_argument("--no-index", action="store_true",
                        help="只跳过行业/地域索引重建（兼容旧用法，新脚本请用 --no-post）")
    parser.add_argument("--no-schedule", action="store_true",
                        help="关闭抓取账本调度（默认开启）。关掉就退回改造前的固定顺序："
                             "每次都从 JOBS 字典序第一个任务开始，配额只喂给头部那批任务")
    parser.add_argument("--no-district", action="store_true",
                        help="不按区县 adcode 分片展开。默认开启：一个组合翻到底（200 条）"
                             "且召回量很大时，自动拆成区县再抓 —— 官方 200 条上限是按"
                             "请求参数算的，换 adcode 就是全新的 200 条")
    parser.add_argument("--quota", type=int, default=1000,
                        help="本轮请求上限（默认 1000，即高德日配额）。配合账本调度使用："
                             "跑不完的任务下一轮接着跑，且只挑能完整跑完的任务，"
                             "不会让最后一个任务翻页翻到一半被掐断")
    parser.add_argument("--autoprofile", action="store_true",
                        help="抓取完给本轮新抓到的城市自动补「未认证」能力卡（调 batch_auto_profile，"
                             "烧 LLM、慢；默认关）。仅增量补本轮城市、不清空已有卡")
    parser.add_argument("--no-lock", action="store_true",
                        help="跳过单实例抓取锁（不推荐：并发会撞 id）")
    args = parser.parse_args()

    # 2026-09-14：默认从 core 改为 all。设计面含 7 个已登记门类，默认只跑 core
    # 会让 H/I/M/O/R 永远排不上号（账本里 1914 个「从未跑过」绝大多数是它们）。
    # 担心配额被新门类挤占是多余的——账本按门类轮转，各门类分到等额请求。
    tier = args.tier or "all"
    industries = resolve_industries(args.industry)
    cities = {c.strip() for c in args.city.split(",")} if args.city else None

    tasks = []
    if cities:
        for c in cities:
            tasks += plan(tier, industries, args.category, args.keyword, c)
    else:
        tasks = plan(tier, industries, args.category, args.keyword)

    # ---------------------------------------------------------------- 账本调度
    # 不排序的话，每次运行都从 JOBS 字典序第一个任务开始，配额全喂给头部那批任务，
    # 后面 60%（extended / service 全部新行业都在里面）一次都轮不到。
    # 账本按「从未跑过 > 久未跑且上次产量高」排序，并跳过冷却期内的分片，
    # 把「一轮跑完」改成「多天分批跑完」。详见 scripts/fetch_cursor.py 顶部说明。
    cursor = None
    ledger = None
    if not args.no_schedule:
        import fetch_cursor as cursor  # noqa: E402
        ledger = cursor.load()

        # 翻到底还不够的组合，按区县 adcode 再切片。官方 200 条上限是**按请求参数**算的，
        # 换 adcode 就是全新的 200 条（实测全市 vs 虎丘区第 1 页只重叠 1 条）。
        # dry-run 不展开：那会真的去查行政区划接口，白花请求。
        if not args.no_district and not args.dry_run:
            tasks, n_exp = cursor.expand(tasks, ledger, key=fetcher.AMAP_KEY)
            if n_exp:
                print("[账本] %d 个组合已翻到底且货很多 → 拆成区县分片（各再得 200 条）"
                      % n_exp)
                cursor.save(ledger)  # expanded 标记要落盘，否则下轮会重复展开

        before = cursor.summary(tasks, ledger)
        print("[账本] 分片 %d 个（其中区县层 %d）：从未跑过 %d / 已到期 %d / 冷却中跳过 %d"
              % (before["total"], before["districts"], before["never"],
                 before["due"], before["cooling"]))
        # 单任务请求数：ceil(min(limit,200)/25)，上限 8 页
        per_task = max(1, min(cursor.DEEP_PAGES,
                              -(-min(int(args.limit or 200), 200) // cursor.PAGE_SIZE)))
        tasks = cursor.order(tasks, ledger, budget=args.quota, per_task=per_task)
        print("[账本] 本轮预算 %d 请求，选中 %d 个任务（没跑过的先跑）"
              % (args.quota, len(tasks)))
        if before["orphan"]:
            print("[账本] 提示：%d 个分片已不在当前 JOBS 矩阵里（矩阵改过？可忽略）"
                  % before["orphan"])

    missing, bad = self_check()
    # 说明：self_check 查的是 legacy 采购品类表 CATEGORY_OF_CODE 的覆盖率，
    # 与「落库目录」无关——落库目录由 gb_store.bucket_of 按 gate 推导。
    # 所以 missing 是非致命的（按大类兜底标签），只有 bad（path_of 渲染失败）才值得警觉。
    if bad:
        print("[警告] %d 个小类 path_of 渲染为空（人类可读路径缺失；落库目录由 gate 推导、不受影响，"
              "但建议补 taxonomy 映射）：%s%s"
              % (len(bad), ", ".join(bad[:20]), " …" if len(bad) > 20 else ""))
    if missing:
        print("[提示] %d 个小类未在 CATEGORY_OF_CODE 登记 legacy 采购品类（非致命："
              "按大类兜底标签，仅影响 legacy 品类字段，不影响按 gate 的落库目录）：%s%s"
              % (len(missing), ", ".join(missing[:15]), " …" if len(missing) > 15 else ""))

    # JOBS 键必须是合法 4 位小类码（落在 data/gb4754-full.json 的 CLASSES 里），
    # 否则落库路由 bucket_of() 会取不到 gate，运行到打印/落盘时才崩、且整批中断。
    # 提前拦下，报出具体是哪个键非法，比 KeyError 友好得多。
    bad_jobs = [c for c in JOBS if c not in CODES]
    if bad_jobs:
        print("[致命] fetch_batch.JOBS 含非法国标小类码，已终止：%s" % ", ".join(bad_jobs))
        print("        这些码不在 GB/T 4754-2017 小类表中，请改成真实 4 位小类码"
              "（可用  python -c \"import industry_taxonomy as t; print(t.CLASSES.get('XXXX'))\"  核对）。")
        raise SystemExit(2)

    if args.dry_run:
        print("计划任务 %d 个（tier=%s）：" % (len(tasks), tier))
        by_code = {}
        for code, cat, kw, c in tasks:
            by_code.setdefault(code, []).append(cat)
        for code in sorted(by_code, key=lambda c: -len(by_code[c])):
            print("  %-6s %-26s → %-8s %3d 任务"
                  % (code, CODES[code]["name"][:26], by_code[code][0], len(by_code[code])))
        return

    if not fetcher.AMAP_KEY:
        import os
        fetcher.AMAP_KEY = os.environ.get("AMAP_KEY", "")
    if not fetcher.AMAP_KEY:
        print("请先设置 API Key：export AMAP_KEY=你的key")
        raise SystemExit(1)
    # 本轮请求硬上限。翻页过程中也要检查（fetcher 内部已有护栏），
    # 只在任务之间看一眼是不够的——一个多页任务照样能打穿上限。
    if args.quota:
        fetcher.MAX_REQUESTS = args.quota

    if not getattr(args, "no_lock", False):
        if not fetcher.acquire_fetch_lock():
            print("⛔ 另一抓取进程已在进行（持有 data 写入锁），本次退出以避免 id 区间重叠碰撞。")
            print("   如需强制并发（不推荐），加 --no-lock。")
            raise SystemExit(3)

    done = 0
    total_new = 0
    touched = set()
    cities = set()  # 本轮抓到的城市（城市层，供「自动补能力卡」用）
    for code, cat, kw, c in tasks:
        # 配额护栏放在任务开头：跑完才发现超了，那个任务的请求已经花出去了
        if getattr(fetcher, "QUOTA_EXHAUSTED", False):
            print("\n>>> 高德返回配额已用尽，停止采集（未跑完的下一轮接着跑）")
            break
        if fetcher.MAX_REQUESTS and fetcher.REQUEST_COUNT >= fetcher.MAX_REQUESTS:
            print("\n>>> 已达本轮请求上限 %d，停止采集（未跑完的下一轮接着跑）"
                  % fetcher.MAX_REQUESTS)
            break
        print("\n[%d/%d] %s %s × %s × %s → %s"
              % (done + 1, len(tasks), code, CODES[code]["name"], kw, c, cat))
        pois = []
        added = 0
        try:
            pois = fetcher.fetch(kw, c, args.limit, types=args.types or None)
            added = fetcher.save_suppliers(pois, cat, kw, industry_code=code)
            total_new += added
            touched.add(cat)
            if not (c.isdigit() and len(c) == 6):
                cities.add(c)  # 城市层任务（非区县 adcode）才纳入自动补卡范围
        except Exception as e:
            print(f"  失败: {e}")
        # 写账本：抓多抓少都要记。不记的话这个分片下轮还被当成「从未跑过」排到队首，
        # 又重复占用配额 —— 那账本就白做了。
        if cursor is not None:
            # 用 fetch() 自己报的元信息，别靠 len(pois) < limit 猜：
            # 撞上 200 条上限时 pois 正好等于 limit，猜会误判成「还没到底」
            meta = getattr(fetcher, "LAST_FETCH", None) or {}
            exhausted = meta.get("exhausted", len(pois) < args.limit)
            cursor.record(cursor.key_of(code, kw, c), len(pois), added, exhausted,
                          int(meta.get("requests", 0) or 0), ledger)
            if (done + 1) % cursor.FLUSH_EVERY == 0:
                cursor.save(ledger)
        done += 1
        time.sleep(1)

    if cursor is not None and ledger is not None:
        cursor.save(ledger)
        print("\n[账本] 已落盘 %s（累计记录 %d 个分片）；"
              "想看还剩多少没跑过：python scripts/fetch_cursor.py --stats --tier all"
              % (cursor.LEDGER.name, len(ledger.get("shards", {}))))

    print(f"\n完成：{done} 个任务，新增 {total_new} 条。")

    if not touched:
        return 0

    # ------------------------------------------------------------ 抓后流水线
    # 只写 data/gb/** 的原始名录是不够的：客户 Agent / Pages / App 读的是派生层
    # （指纹分片、能力卡分片、清单、行业地域索引），漏跑不会报错只会静默过期。
    # 统一交给 scripts/postfetch.py，别在这里手搓调用顺序。
    if args.no_post:
        print("\n[警告] --no-post 已跳过抓后流水线：索引 / 指纹 / 能力卡分片 / 清单"
              "全部没重建 —— 新数据在客户检索里搜不到。务必手动跑：")
        print("        python scripts/postfetch.py")
        return 1

    from postfetch import last_failed, run as postfetch_run

    skip = []
    if args.no_classify or not total_new:
        skip.append("classify")
    if args.no_index:
        skip.append("index")
    if not args.autoprofile:
        skip.append("autoprofile")
    autoprofile_cities = ",".join(sorted(cities)) if args.autoprofile else None
    failed = postfetch_run(skip=skip, autoprofile_cities=autoprofile_cities)

    if failed:
        print("\n[警告] 抓后流水线有 %d 步失败 → %s" % (failed, "、".join(last_failed())))
        print("       对应产物仍是旧值，先别发布；看上面每个步骤的 ✗ 行")
        return 1

    if total_new:
        print("\n可选：python scripts/en_backfill.py --workers 4（补齐英文镜像，较慢）")
    return 0


def sync_index_counts():
    """已迁移到 scripts/postfetch.py（保留转发以免外部调用断掉）。"""
    from postfetch import sync_index_counts as _impl
    _impl()


if __name__ == "__main__":
    # 必须把 main() 的返回值透出去：抓后流水线失败要靠 exit code 告诉
    # 定时任务 / CI。写成裸 main() 会永远返回 0，又是「静默成功」。
    raise SystemExit(main())
