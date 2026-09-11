# -*- coding: utf-8 -*-
"""
BeaconMFG 采集会话
==================

把「品类 Profile + 通用采集流程」编成一次分模块的对话，
逐轮把供应商的口语回答归一化成结构化字段，最后交给 render.py 生成 Skill。

设计原则：
1. 一次只问一个字段，不让供应商面对 40 个输入框
2. 每轮都返回 (抽取到的字段, 归一化说明)，供面板实时展示
3. 归一化失败不阻塞流程，标记待补充，留给表单兜底通道
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

from .normalize import _CN_DIGITS, is_bool_no, is_empty_answer, normalize

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = REPO_ROOT / "skills" / "profiles"
CODES_FILE = REPO_ROOT / "skills" / "schema" / "process-codes.json"

# 能力码表按域分文件，schema 装配与采集归一化共用同一个加载器（scripts/cap_codes.py）。
# server 启动时已把 scripts/ 放进 sys.path；直接跑脚本时补一次。
_SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import cap_codes  # noqa: E402

# 证照按门类分组。制造业问 ISO9001/IATF16949，餐饮问食品经营许可证/HACCP ——
# 用同一份清单会让两边都识别不出东西（实测：餐饮口述「有食品经营许可证」在
# 制造业清单下返回「未匹配到已知认证」）。
CERT_GROUPS = {
    "mfg": [
        "ISO9001", "IATF16949", "ISO14001", "ISO13485", "AS9100", "ISO45001",
        "高新技术企业", "专精特新", "RoHS", "REACH", "UL", "CE",
    ],
    "food": [
        "食品经营许可证", "食品生产许可证", "HACCP", "ISO22000", "量化分级A级",
        "量化A级", "清真认证", "绿色食品", "有机认证", "SC认证",
    ],
    "entertainment": [
        "娱乐经营许可证", "网络文化经营许可证", "电影放映经营许可证",
        "营业性演出许可证", "消防验收合格证", "消防安全检查合格证",
        "高危险性体育项目经营许可证", "食品经营许可证", "卫生许可证",
    ],
    "retail": [
        "食品经营许可证", "烟草专卖零售许可证", "药品经营许可证",
        "医疗器械经营许可证", "酒类流通备案", "烟花爆竹经营许可证",
        "出版物经营许可证", "成品油零售经营批准证书",
    ],
    "service": [
        "机动车维修经营备案", "特种设备安装改造维修许可证",
        "再生资源回收经营者备案", "道路运输经营许可证",
        "家政服务经营备案", "人力资源服务许可证",
    ],
    "it": [
        "ISO27001", "ISO20000", "CMMI", "高新技术企业", "双软认证",
        "信息系统集成资质", "ITSS", "ISO9001", "等保测评",
    ],
    "lab": [
        "CMA", "CNAS", "ISO17025", "检验检测机构资质认定", "实验室认可",
        "工程勘察资质", "工程设计资质", "工程监理资质", "高新技术企业", "ISO9001",
    ],
}

# 证照名前的否定词。**必须先判否定再判命中** —— 原实现是「先找证书名，找不到才看否定词」，
# 于是「没有 ISO9001」会命中 ISO9001 并被记成「拥有该认证」（2026-09-11 发现）。
# 这不是措辞问题，是往名录里写假数据 —— 「数据宁可留空也不编造」这条红线直接踩了。
_NEG_CERT = ("没有", "没", "无", "不具备", "未取得", "未办", "不是", "不含")

# 兼容别名：制造业证照清单。既有调用方按旧名读，别改。
KNOWN_CERTS = CERT_GROUPS["mfg"]

# 单字工艺别名（"车"、"铣"）极易误伤——"汽车件"、"车间" 都含"车"。
# 命中时必须检查上下文，落在这些词里则不算。
_NEG_CONTEXT = ("汽车", "车间", "车库", "车位", "停车", "车险", "车棚", "车床工")


def _alias_hit(text: str, word: str) -> bool:
    """词是否命中，且不在负面上下文中。"""
    for m in re.finditer(re.escape(word), text):
        ctx = text[max(0, m.start() - 1): m.end() + 1]
        if not any(n in ctx for n in _NEG_CONTEXT):
            return True
    return False


# ---------------------------------------------------------------- 采集问句分层
#
# 问句按四层组装（2026-09-11 门类扩展）：
#
#     STEPS_HEAD      全门类通用开头：谁、多大、在哪
#     STEPS_BY_GATE   门类专属主体：制造业问工艺与公差，餐饮问菜系与席位
#     档案 profile     品类 / 业态细分字段，插在门类主体第 2 步之后
#     STEPS_TAIL      全门类通用收尾：询价通道 + 主动声明边界
#
# **为什么要分层**：改造前 COMMON_STEPS 把 33 个字段写死成一套，其中公差、最大工件
# 尺寸、打样周期、材质单、首件报告、PPAP 全是制造专属且多为必填。问餐厅「最薄能压多
# 薄的壁」不只是尴尬 —— 它会因为必填门禁把整个采集流程卡死在门口（赤兔那类非制造企业
# 真机实测就是这么卡住的）。
#
# **绝不回归**：HEAD + BY_GATE["C"] + TAIL 组装出的 `COMMON_STEPS` 与改造前**逐字段
# 一致**（顺序、required、kind 全同），由 scripts/test_gate_expansion.py 逐项断言。
#
# 门类专属措辞用 `label_by_gate` / `question_by_gate` / `hint_by_gate` 覆盖，不覆盖时
# 用默认值 —— 制造业的默认值就是改造前原文，所以 C 门类的问句一个字节都没动。

STEPS_HEAD: list[dict] = [
    {
        "title": "企业身份",
        "fields": [
            {"path": "identity.founded", "label": "成立年份", "kind": "integer",
             "question": "咱们是哪年开的？", "hint": "例：2010", "required": False},
            {"path": "identity.employee_count", "label": "员工人数", "kind": "integer",
             "question": "现在有多少人？", "hint": "例：80", "required": False},
            {"path": "identity.scale", "label": "规模档位", "kind": "enum",
             "enum": ["<50人", "50-100人", "100-500人", "500-1000人", ">1000人"],
             "question": "按人数算属于哪个档？（50人以下 / 50-100 / 100-500 / 500以上）",
             "hint": "用于客户 Agent 判断承接能力", "required": False},
            {"path": "identity.floor_area_m2", "label": "厂房面积", "kind": "integer",
             "question": "厂房多大面积？", "hint": "例：3000（指 3000 平米）", "required": False,
             # 同一个字段、同一个语义（能承接生意的物理空间），只是各门类对它叫法不同。
             # 不拆成两个字段：拆了检索层就要按门类分支，而这正是本次改造要避免的。
             "label_by_gate": {"H": "营业面积", "R": "营业面积", "F": "门店面积",
                               "O": "门店面积", "A": "用地面积", "I": "办公面积",
                               "M": "办公面积"},
             "question_by_gate": {
                 "H": "店里营业面积多大？", "R": "营业面积多大？",
                 "F": "门店多大？", "O": "门店多大？",
                 "A": "种养用地多少亩（说「亩」或平米都行）？",
                 "I": "办公面积多大？", "M": "办公面积多大？"}},
            {"path": "identity.export_markets", "label": "销售市场", "kind": "list",
             "question": "主要做国内还是也出口？",
             "hint": "例：国内、东南亚、欧美", "required": False,
             "label_by_gate": {"H": "客源范围", "R": "客源范围", "F": "客源范围",
                               "O": "服务范围", "A": "销货范围",
                               "I": "客户范围", "M": "客户范围"},
             "question_by_gate": {
                 "H": "客人主要是周边还是全城都来？", "R": "客人主要是周边还是全城都来？",
                 "F": "客户主要是周边的还是全市都来？",
                 "O": "客户主要是周边小区还是全城都接？",
                 "I": "客户主要是本地还是外地？", "M": "客户主要是本地还是外地？",
                 "A": "货主要卖到本地还是外地？"}},
        ],
    },
]


STEPS_BY_GATE: dict[str, list[dict]] = {

    # ------------------------------------------------------------ 制造业
    "C": [
        {
            "title": "工艺能力",
            "fields": [
                {"path": "processes", "label": "主营工艺", "kind": "processes",
                 "question": "咱们厂主要做什么工艺？",
                 "hint": "例：CNC 车和铣都做，氧化是外发给别人做的", "required": True},
                {"path": "materials", "label": "常用材料", "kind": "list",
                 "question": "常做的材料有哪些？",
                 "hint": "例：铝合金6061、不锈钢304、POM、钛合金TC4", "required": True},
                {"path": "highlights", "label": "擅长场景", "kind": "list",
                 "question": "有没有特别擅长的场景？比如医疗件、薄壁件、大件、急单？",
                 "hint": "写具体场景，别写形容词。这段直接进 Skill 的「我们特别擅长」",
                 "required": False},
            ],
        },
        {
            "title": "硬边界",
            "fields": [
                {"path": "limits.tolerance_mm", "label": "常规公差", "kind": "tolerance_mm",
                 "question": "常规公差能控制在多少？",
                 "hint": "说「一丝」就是 0.01mm", "required": True},
                {"path": "limits.max_part_size_mm", "label": "最大工件尺寸", "kind": "size3",
                 "question": "最大能做多大的件（长宽高）？",
                 "hint": "例：八百乘六百，高度四百", "required": True},
                {"path": "limits.min_order_qty", "label": "最小起订量", "kind": "moq",
                 "question": "最少接多少件的单？",
                 "hint": "「一件也做」就填 1", "required": True},
                {"path": "limits.lead_time_days.sample", "label": "打样周期", "kind": "days",
                 "question": "打个样要几天？", "hint": "例：五天", "required": True},
                {"path": "limits.lead_time_days.batch_100", "label": "百件交期", "kind": "days",
                 "question": "做一百件呢？", "hint": "例：十二天", "required": False},
                {"path": "limits.current_load_pct", "label": "当前产线负荷", "kind": "percent",
                 "question": "现在产线忙不忙？大概几成负荷？",
                 "hint": "例：七八成", "required": False},
                {"path": "limits.rush_available", "label": "能否加急", "kind": "bool",
                 "question": "急单能插吗？", "hint": "能 / 不能", "required": False},
            ],
        },
        {
            "title": "质量资质",
            "fields": [
                {"path": "quality.certifications", "label": "资质认证", "kind": "cert_list",
                 "question": "有什么认证？ISO9001 有没有？",
                 "hint": "有多个一起说就行", "required": False},
                {"path": "quality.inspection_equipment", "label": "检测设备", "kind": "list",
                 "question": "检测设备有哪些？三坐标、二次元这些？",
                 "hint": "直接影响良率可信度", "required": False},
                {"path": "quality.first_article_report", "label": "首件报告", "kind": "bool",
                 "question": "能出首件报告吗？", "hint": "能 / 不能", "required": False},
                {"path": "quality.material_certificate", "label": "材质单", "kind": "bool",
                 "question": "材质单（MTC）能提供吗？", "hint": "能 / 不能", "required": False},
            ],
        },
        {
            "title": "商务服务",
            "fields": [
                {"path": "service.quote_inputs_required", "label": "询价所需材料",
                 "kind": "list",
                 "question": "客户询价一般要提供什么东西？",
                 "hint": "例：3D 图、材质、数量、表面处理", "required": True},
                {"path": "service.quote_response_hours", "label": "报价响应", "kind": "hours",
                 "question": "多久能回价？", "hint": "例：一天内", "required": True},
                {"path": "service.sample_policy", "label": "打样政策", "kind": "text",
                 "question": "打样怎么收费？", "hint": "例：收费打样，批量下单返还",
                 "required": False},
                {"path": "service.payment_terms", "label": "付款方式", "kind": "list",
                 "question": "付款方式是什么？", "hint": "例：三成预付，发货前付清",
                 "required": False},
            ],
        },
    ],

    # ------------------------------------------------------------ 住宿和餐饮
    "H": [
        {
            "title": "经营业态",
            "fields": [
                {"path": "capability.cuisines", "label": "菜系", "kind": "cap_multi",
                 "cap_from": "restaurant:菜系",
                 "question": "你们家主要做什么菜？川菜、日料、火锅这些，可以多说几个。",
                 "hint": "可多选。菜系是客户 Agent 最常用的筛选条件",
                 "required": True},
                {"path": "capability.service_modes", "label": "经营形态", "kind": "cap_multi",
                 "cap_from": "restaurant:服务形态",
                 "question": "除了堂食，还做哪些？比如包间、宴席、团餐、外卖、包场？",
                 "hint": "可多选。这决定哪类单子你能接",
                 "required": True},
                {"path": "capability.signature_dishes", "label": "招牌菜", "kind": "list",
                 "question": "有什么招牌菜或者拿手菜？",
                 "hint": "例：水煮鱼、毛血旺。客户选餐厅最先看这个",
                 "required": False},
            ],
        },
        {
            "title": "接待硬边界",
            "fields": [
                # 与 profile 的「总座位数」高度重合 —— 两个都必填等于让老板
                # 同一件事说两遍。座位数必填，这里只做「办宴席能超出多少」的补充。
                {"path": "limits.max_party_size", "label": "最大接待人数", "kind": "integer",
                 "question": "如果办宴席、包场，最多能接待多少人？",
                 "hint": "比座位数多就填多的那个，例：260", "required": False},
                {"path": "limits.max_tables", "label": "最大桌数", "kind": "integer",
                 "question": "同时最多能摆几桌？",
                 "hint": "宴席单必问", "required": False},
                {"path": "limits.min_order_qty", "label": "最少接几桌", "kind": "integer",
                 "question": "最少几桌起接？",
                 "hint": "「一桌也接」就填 1", "required": False},
                {"path": "limits.min_order_value_cny", "label": "最低消费", "kind": "number",
                 "question": "包间有最低消费吗？没有就说没有。",
                 "hint": "没有最低消费也是有效答案，直说「没有」",
                 "required": False, "zero_is_answer": True},
                {"path": "limits.advance_booking_days", "label": "宴席需提前几天订",
                 "kind": "integer",
                 "question": "办宴席一般要提前几天订？",
                 "hint": "例：7", "required": False},
                {"path": "limits.current_load_pct", "label": "当前上座率", "kind": "percent",
                 "question": "现在生意忙不忙？大概几成上座？",
                 "hint": "例：七八成", "required": False},
                {"path": "limits.rush_available", "label": "能否临时加桌", "kind": "bool",
                 "question": "临时加桌加位能安排吗？", "hint": "能 / 不能", "required": False},
            ],
        },
        {
            "title": "食品卫生",
            "fields": [
                {"path": "quality.certifications", "label": "资质证照", "kind": "cert_list",
                 "cert_group": "food",
                 "question": "有哪些证照？食品经营许可证肯定有的吧，HACCP、ISO22000 有吗？",
                 "hint": "有几个说几个", "required": False},
                {"path": "quality.hygiene_grade", "label": "量化分级", "kind": "enum",
                 "enum": ["A", "B", "C", "未评定"],
                 "question": "门口的卫生量化分级是 A 还是 B？不清楚就说未评定。",
                 "hint": "市场监管局的量化分级，A 级最能打", "required": False},
            ],
        },
        {
            "title": "接待服务",
            "fields": [
                {"path": "service.booking_channels", "label": "预订渠道", "kind": "list",
                 "question": "客人一般怎么订位？电话、微信、还是美团点评？",
                 "hint": "例：电话、微信、美团", "required": True},
                {"path": "service.invoice_available", "label": "能否开票", "kind": "bool",
                 "question": "能开票吗？",
                 "hint": "企业客户报销的硬条件", "required": True},
                {"path": "service.deposit_required", "label": "宴席是否收定金",
                 "kind": "bool",
                 "question": "办宴席要收定金吗？", "hint": "收 / 不收", "required": False},
                {"path": "capability.delivery_platforms", "label": "外卖平台",
                 "kind": "list",
                 "question": "做外卖的话，上的哪些平台？",
                 "hint": "例：美团、饿了么。不做外卖就直说没有",
                 "required": False, "empty_is_answer": True},
            ],
        },
    ],

    # ------------------------------------------------------------ 文化、体育和娱乐业
    # 与档案（skills/profiles/entertainment.json）分工：档案问「有多少包间、人均多少、
    # 营业到几点」这类**每家都不同、且是硬筛选条件**的数；这里问**能把场所归类到什么**
    # 的能力码。两边都问同一件事会让老板说两遍 —— 所以 body 里刻意不再出现
    # capacity / private_rooms / price_per_person_cny / business_hours 这几个字段。
    "R": [
        {
            "title": "场所业态",
            "fields": [
                {"path": "capability.venue_types", "label": "场所类型", "kind": "cap_multi",
                 "cap_from": "entertainment:场所类型",
                 "question": "你们主要是做什么的？KTV、网吧、影院、游乐园、密室，"
                             "还是别的？可以多说几个。",
                 "hint": "可多选。场所类型是客户 Agent 最常用的第一道筛选",
                 "required": True},
                {"path": "capability.service_modes", "label": "服务形态", "kind": "cap_multi",
                 "cap_from": "entertainment:服务形态",
                 "question": "除了散客，还接哪些？包场、团建、生日派对、团体票？",
                 "hint": "可多选。这决定哪类单子你能接 —— 团建单只找能包场的",
                 "required": True},
                {"path": "capability.venue_features", "label": "场地设施", "kind": "cap_multi",
                 "cap_from": "entertainment:场地设施",
                 "question": "场地有什么设施？比如停车位、无障碍通道、能供餐、有舞台音响？",
                 "hint": "可多选。客户 Agent 按设施做硬过滤，没有就跳过",
                 "required": False},
            ],
        },
        {
            "title": "接待硬边界",
            "fields": [
                # 容量与包间数在档案里必填（每个场所都不同、且是团建第一道筛选），
                # 这里只补「临时能不能多塞人、要提前多久订」这类**承接弹性**问题。
                {"path": "limits.max_party_size", "label": "最大接待人数", "kind": "integer",
                 "question": "办包场、团建的话，最多能同时接待多少人？",
                 "hint": "比日常容纳人数多就填多的那个，例：260", "required": False},
                {"path": "limits.max_private_rooms", "label": "同时开放包间上限",
                 "kind": "integer",
                 "question": "最多能同时开几个包间？",
                 "hint": "整层包场时最有用", "required": False},
                {"path": "limits.min_order_qty", "label": "最少接几人起",
                 "kind": "integer",
                 "question": "团建、包场最少几人起接？",
                 "hint": "「一个人也接」就填 1", "required": False},
                {"path": "limits.min_order_value_cny", "label": "最低消费", "kind": "number",
                 "question": "包间或包场有最低消费吗？没有就说没有。",
                 "hint": "没有最低消费也是有效答案，直说「没有」",
                 "required": False, "zero_is_answer": True},
                {"path": "limits.advance_booking_days", "label": "包场需提前几天订",
                 "kind": "integer",
                 "question": "包场一般要提前几天订？",
                 "hint": "例：7", "required": False},
                {"path": "limits.current_load_pct", "label": "当前场地使用率",
                 "kind": "percent",
                 "question": "现在场地大概几成在使用？",
                 "hint": "例：七八成。满了的场次客户 Agent 会自动往后退",
                 "required": False},
                {"path": "limits.rush_available", "label": "能否临时加场", "kind": "bool",
                 "question": "临时加场加位能安排吗？", "hint": "能 / 不能", "required": False},
            ],
        },
        {
            "title": "证照与卫生",
            "fields": [
                {"path": "quality.certifications", "label": "经营证照", "kind": "cert_list",
                 "cert_group": "entertainment",
                 "question": "有哪些经营证照？娱乐经营许可证肯定有的吧，"
                             "消防验收合格证、卫生许可证这些有吗？",
                 "hint": "有几个说几个。★ 消防与卫生许可是团体客户下单前必查项",
                 "required": False},
                {"path": "quality.hygiene_grade", "label": "公共场所卫生等级",
                 "kind": "enum",
                 "enum": ["A", "B", "C", "未评定"],
                 "question": "卫生监督的量化分级是 A 还是 B？不清楚就说未评定。",
                 "hint": "洗浴、游泳、健身类适用", "required": False},
            ],
        },
        {
            "title": "接待服务",
            "fields": [
                {"path": "service.booking_channels", "label": "预订渠道", "kind": "list",
                 "question": "客户怎么预订或咨询？电话、微信、还是美团点评？",
                 "hint": "例：电话、微信、美团", "required": True},
                {"path": "service.invoice_available", "label": "能否开票", "kind": "bool",
                 "question": "能开票吗？",
                 "hint": "企业团建、团购单报销的硬条件", "required": True},
                {"path": "service.deposit_required", "label": "是否收定金", "kind": "bool",
                 "question": "包场要收定金吗？", "hint": "收 / 不收", "required": False},
                {"path": "service.cancellation_policy", "label": "取消政策", "kind": "text",
                 "question": "临时取消怎么算？定金退不退？",
                 "hint": "照原话说。团体客户一定会问这个", "required": False},
            ],
        },
    ],

    # ------------------------------------------------------------ 批发和零售业（含个体商户）
    "F": [
        {
            "title": "经营品类",
            "fields": [
                {"path": "capability.retail_categories", "label": "经营品类",
                 "kind": "cap_multi", "cap_from": "retail-service:零售品类",
                 "question": "主要卖哪些类目的货？五金、建材、食品、还是别的？可以多说几个。",
                 "hint": "可多选。品类是采购方 Agent 的第一道筛选条件",
                 "required": True},
                {"path": "capability.business_modes", "label": "经营方式", "kind": "cap_multi",
                 "cap_from": "retail-service:经营方式",
                 "question": "是零售、批发，还是线上线下都做？",
                 "hint": "可多选。能不能批发直接决定大单找不找你",
                 "required": True},
                {"path": "capability.store_features", "label": "门店条件", "kind": "cap_multi",
                 "cap_from": "retail-service:门店设施",
                 "question": "门店有什么条件？比如可停车、可自提、支持验货、有仓储？",
                 "hint": "可多选。没有就跳过", "required": False},
            ],
        },
        {
            "title": "交易硬边界",
            "fields": [
                # 起订量、现货、发货天数、配送半径都在档案里必填（都是采购方硬筛条件），
                # 这里只补「接单量与负载」这类**承接弹性**问题，避免同一件事问两遍。
                {"path": "limits.min_order_value_cny", "label": "最低订单金额",
                 "kind": "number",
                 "question": "有最低消费或最低订单金额吗？没有就说没有。",
                 "hint": "「没有门槛」也是有效答案，直说「没有」",
                 "required": False, "zero_is_answer": True},
                {"path": "limits.max_daily_orders", "label": "日最大接单量",
                 "kind": "integer",
                 "question": "一天最多能接多少单？",
                 "hint": "例：50。超过就会排到第二天，采购方要提前知道",
                 "required": False},
                {"path": "limits.current_load_pct", "label": "当前备货负荷",
                 "kind": "percent",
                 "question": "现在库存和出货大概几成负荷？",
                 "hint": "例：七八成", "required": False},
                {"path": "limits.rush_available", "label": "能否加急", "kind": "bool",
                 "question": "急单能插吗？", "hint": "能 / 不能", "required": False},
            ],
        },
        {
            "title": "资质与授权",
            "fields": [
                {"path": "quality.certifications", "label": "经营证照", "kind": "cert_list",
                 "cert_group": "retail",
                 "question": "有哪些经营证照？食品经营许可证、烟草专卖零售许可证，"
                             "或者别的？没有就直说没有。",
                 "hint": "有几个说几个。★ 卖烟卖药卖医疗器械，没对应许可是不能供的",
                 "required": False},
                {"path": "quality.brand_authorized", "label": "是否品牌授权", "kind": "bool",
                 "question": "卖的品牌有正规授权吗？", "hint": "有 / 没有", "required": False},
                {"path": "quality.source_note", "label": "货源说明", "kind": "text",
                 "question": "货是从哪来的？厂家直供还是渠道拿货？",
                 "hint": "照原话说。采购方最关心「是不是正经来路」", "required": False},
            ],
        },
        {
            "title": "交易服务",
            "fields": [
                {"path": "capability.delivery_modes", "label": "交付方式",
                 "kind": "cap_multi", "cap_from": "retail-service:交付方式",
                 "question": "货怎么交？自提、同城送货、快递、还是物流发货？",
                 "hint": "可多选", "required": False},
                {"path": "service.quote_inputs_required", "label": "报价所需材料",
                 "kind": "list",
                 "question": "客户询价一般要提供什么？",
                 "hint": "例：品名规格、数量、收货地址", "required": True},
                {"path": "service.quote_response_hours", "label": "报价响应", "kind": "hours",
                 "question": "多久能回价？", "hint": "例：一天内", "required": True},
                {"path": "service.wholesale_available", "label": "是否支持批发",
                 "kind": "bool",
                 "question": "接受批发或批量采购吗？", "hint": "接受 / 不接受",
                 "required": False},
                {"path": "service.invoice_available", "label": "能否开票", "kind": "bool",
                 "question": "能开票吗？",
                 "hint": "企业采购报销的硬条件", "required": False},
                {"path": "service.return_policy", "label": "退换货政策", "kind": "text",
                 "question": "退换货怎么规定？",
                 "hint": "例：非质量问题不退。照原话说", "required": False},
            ],
        },
    ],

    # ------------------------------------------------------------ 居民服务、修理和其他服务业
    "O": [
        {
            "title": "服务项目",
            "fields": [
                {"path": "capability.service_items", "label": "服务项目",
                 "kind": "cap_multi", "cap_from": "retail-service:服务项目",
                 "question": "主要做什么服务？家电维修、水电安装、管道疏通、家政保洁，"
                             "这些里哪几项？可以多说几个。",
                 "hint": "可多选。服务项目是客户报修时第一个要匹配的",
                 "required": True},
                {"path": "capability.business_modes", "label": "经营方式", "kind": "cap_multi",
                 "cap_from": "retail-service:经营方式",
                 "question": "是上门服务、到店服务，还是都做？",
                 "hint": "可多选。决定客户要不要把东西送过去",
                 "required": True},
                {"path": "capability.store_features", "label": "门店条件",
                 "kind": "cap_multi", "cap_from": "retail-service:门店设施",
                 "question": "门店有什么条件？比如可停车、可寄存、有配件库存、夜间可服务？",
                 "hint": "可多选。没有就跳过", "required": False},
            ],
        },
        {
            "title": "上门硬边界",
            "fields": [
                # 服务半径、起步价、上门响应时长都在档案里必填（客户的第一道筛选），
                # 这里只补「起接门槛与承接量」，不重复问同一件事。
                {"path": "limits.min_order_qty", "label": "最少起接", "kind": "integer",
                 "question": "最少接多少的单？比如只修一件做不做？",
                 "hint": "例：1。「一件也接」就填 1", "required": False},
                {"path": "limits.advance_booking_days", "label": "需提前几天预约",
                 "kind": "integer",
                 "question": "一般要提前几天预约？",
                 "hint": "急修通常当天。说不清就填 0", "required": False},
                {"path": "limits.current_load_pct", "label": "当前排期负荷",
                 "kind": "percent",
                 "question": "现在师傅的排期大概几成满？",
                 "hint": "例：七八成", "required": False},
                {"path": "limits.rush_available", "label": "能否加急上门", "kind": "bool",
                 "question": "急修能插单吗？", "hint": "能 / 不能", "required": False},
            ],
        },
        {
            "title": "资质与人员",
            "fields": [
                {"path": "quality.certifications", "label": "经营备案与许可",
                 "kind": "cert_list", "cert_group": "service",
                 "question": "有哪些备案或经营许可？比如机动车维修经营备案、"
                             "特种设备安装改造维修许可证？没有就直说没有。",
                 "hint": "有几个说几个。★ 修机动车、电梯这类必须有对应许可",
                 "required": False},
                {"path": "quality.technician_certified", "label": "师傅是否持证",
                 "kind": "bool",
                 "question": "师傅有职业资格证或上岗证吗？",
                 "hint": "家政、月嫂、维修的信任锚点", "required": False},
                {"path": "quality.hygiene_grade", "label": "公共场所卫生等级",
                 "kind": "enum",
                 "enum": ["A", "B", "C", "未评定"],
                 "question": "卫生监督的量化分级是 A 还是 B？不清楚就说未评定。",
                 "hint": "理发美容、洗浴类适用", "required": False},
            ],
        },
        {
            "title": "接单服务",
            "fields": [
                {"path": "capability.delivery_modes", "label": "交付方式",
                 "kind": "cap_multi", "cap_from": "retail-service:交付方式",
                 "question": "服务怎么交付？上门、到店、还是寄修？",
                 "hint": "可多选", "required": False},
                {"path": "service.booking_channels", "label": "接单渠道", "kind": "list",
                 "question": "客户怎么找你们？电话、微信、还是 58 同城这类平台？",
                 "hint": "例：电话、微信、58同城", "required": True},
                {"path": "service.quote_inputs_required", "label": "报价所需材料",
                 "kind": "list",
                 "question": "报个价一般要知道什么？",
                 "hint": "例：照片、品牌型号、地址。写清楚能少跑很多空趟", "required": True},
                {"path": "service.quote_response_hours", "label": "报价响应",
                 "kind": "hours",
                 "question": "多久能给报价？", "hint": "例：1 小时内", "required": False},
                {"path": "service.price_list_available", "label": "是否有标准价目表",
                 "kind": "bool",
                 "question": "有标准价目表吗？",
                 "hint": "有价目表的客户更好比价，也更容易被选中", "required": False},
                {"path": "service.invoice_available", "label": "能否开票", "kind": "bool",
                 "question": "能开票吗？", "hint": "能 / 不能", "required": False},
            ],
        },
    ],

    # ------------------------------------------------------------ 信息传输、软件和信息技术服务业
    "I": [
        {
            "title": "技术方向",
            "fields": [
                {"path": "capability.tech_directions", "label": "技术方向",
                 "kind": "cap_multi", "cap_from": "tech-service:信息技术方向",
                 "question": "主要做哪些方向？软件开发、移动应用、大数据、网络安全，"
                             "这些里哪几块？可以多说几个。",
                 "hint": "可多选。技术方向是甲方立项时的第一道筛选",
                 "required": True},
                {"path": "capability.team_roles", "label": "团队构成",
                 "kind": "cap_multi", "cap_from": "tech-service:团队构成",
                 "question": "团队里有哪些角色？有没有架构师、测试、产品？还是主要出人手？",
                 "hint": "可多选。有没有架构师，决定能接复杂项目还是只能出人力",
                 "required": False},
            ],
        },
        {
            "title": "承接硬边界",
            "fields": [
                # 最小接单人天、需求响应时长、技术栈、交付成果、代表案例都在档案里，
                # 这里补「排期与报价门槛」。
                {"path": "limits.delivery_days", "label": "常规交付周期", "kind": "days",
                 "question": "一个常规规模的活，从启动到能验收大概多少天？",
                 "hint": "例：45。这是标准规模的口径，不是最大最小", "required": False},
                {"path": "limits.max_concurrent_projects", "label": "同时在制项目上限",
                 "kind": "integer",
                 "question": "团队同时最多能开几个项目？",
                 "hint": "例：6。超过就是排期，不是能力", "required": False},
                {"path": "limits.min_order_value_cny", "label": "最低项目金额",
                 "kind": "number",
                 "question": "有最低项目金额吗？太小不接就说个数。",
                 "hint": "「没有门槛」也是有效答案，直说「没有」",
                 "required": False, "zero_is_answer": True},
                {"path": "limits.current_load_pct", "label": "当前团队负荷",
                 "kind": "percent",
                 "question": "现在团队大概几成在忙？",
                 "hint": "例：七八成", "required": False},
                {"path": "limits.rush_available", "label": "能否加急投入", "kind": "bool",
                 "question": "能加急投入资源吗？", "hint": "能 / 不能", "required": False},
            ],
        },
        {
            "title": "资质与成果",
            "fields": [
                {"path": "quality.certifications", "label": "资质认证",
                 "kind": "cert_list", "cert_group": "it",
                 "question": "有哪些资质？ISO27001、CMMI、高新技术企业这些有吗？"
                             "没有就直说没有。",
                 "hint": "有几个说几个。★ 涉密、涉金融的项目常要求 CMMI 或等保",
                 "required": False},
                {"path": "capability.open_source", "label": "是否有公开开源作品",
                 "kind": "bool",
                 "question": "有公开的开源项目吗？GitHub 上有东西就行。",
                 "hint": "技术能力最硬的可查证据 —— 有就一定写", "required": False},
                {"path": "quality.software_copyright_count", "label": "软件著作权数量",
                 "kind": "integer",
                 "question": "有多少个软件著作权？没有就说没有。",
                 "hint": "「没有」是有效答案，会记成 0",
                 "required": False, "zero_is_answer": True},
            ],
        },
        {
            "title": "合作服务",
            "fields": [
                {"path": "capability.service_modes", "label": "服务模式",
                 "kind": "cap_multi", "cap_from": "tech-service:服务模式",
                 "question": "怎么合作？项目制、驻场、人力外包、还是年框？",
                 "hint": "可多选", "required": False},
                {"path": "service.quote_inputs_required", "label": "报价所需材料",
                 "kind": "list",
                 "question": "客户询价一般要提供什么？",
                 "hint": "例：需求文档、原型、接口文档、预算区间", "required": True},
                {"path": "service.quote_response_hours", "label": "报价响应",
                 "kind": "hours",
                 "question": "多久能给出方案或报价？", "hint": "例：48", "required": True},
                {"path": "service.invoice_available", "label": "能否开票", "kind": "bool",
                 "question": "能开增值税发票吗？",
                 "hint": "甲方走采购流程的硬条件", "required": True},
                {"path": "service.payment_terms", "label": "付款方式", "kind": "list",
                 "question": "付款方式是什么？",
                 "hint": "例：3-3-3-1 分期、验收后付款", "required": False},
            ],
        },
    ],

    # ------------------------------------------------------------ 科学研究和技术服务业
    "M": [
        {
            "title": "技术方向",
            "fields": [
                {"path": "capability.tech_directions", "label": "技术方向",
                 "kind": "cap_multi", "cap_from": "tech-service:科研技术方向",
                 "question": "主要做哪些方向？检验检测、计量校准、环境监测、"
                             "工程勘察设计，这些里哪几块？可以多说几个。",
                 "hint": "可多选。甲方委托时的第一道筛选",
                 "required": True},
                {"path": "capability.team_roles", "label": "团队构成",
                 "kind": "cap_multi", "cap_from": "tech-service:团队构成",
                 "question": "团队里有哪些角色？有没有研究员、检测工程师、注册工程师？",
                 "hint": "可多选。有没有研究员，决定能接研发还是只能接常规检测",
                 "required": False},
            ],
        },
        {
            "title": "委托硬边界",
            "fields": [
                # 出报告天数、最低委托金额、响应时效、CMA/CNAS、关键仪器、代表案例
                # 都在档案里（都是甲方一票否决项），这里补承接量与排期。
                {"path": "limits.min_order_qty", "label": "最少起做量", "kind": "integer",
                 "question": "最少接多少样品或多少件起？",
                 "hint": "例：1。「一件也接」就填 1", "required": False},
                {"path": "limits.max_concurrent_projects", "label": "同时在制项目上限",
                 "kind": "integer",
                 "question": "实验室或团队同时最多能开几个项目？",
                 "hint": "例：20", "required": False},
                {"path": "limits.current_load_pct", "label": "当前排期负荷",
                 "kind": "percent",
                 "question": "现在排期大概几成满？",
                 "hint": "例：七八成", "required": False},
                {"path": "limits.rush_available", "label": "能否加急出报告",
                 "kind": "bool",
                 "question": "能加急出报告吗？", "hint": "能 / 不能", "required": False},
            ],
        },
        {
            "title": "资质与认可",
            "fields": [
                # CMA / CNAS 的「有没有」在档案里单独问（那两问是全门类最硬的字段），
                # 这里问**还有什么别的资质**，避免同一件事问两遍。
                {"path": "quality.certifications", "label": "其他资质证书",
                 "kind": "cert_list", "cert_group": "lab",
                 "question": "除了刚才说的，还有别的资质证书吗？"
                             "比如 ISO17025、工程勘察设计资质、高新技术企业？",
                 "hint": "有几个说几个。没有就直说没有", "required": False},
            ],
        },
        {
            "title": "委托服务",
            "fields": [
                {"path": "capability.service_modes", "label": "服务模式",
                 "kind": "cap_multi", "cap_from": "tech-service:服务模式",
                 "question": "怎么合作？委托检测、驻场服务、联合研发，还是技术转移？",
                 "hint": "可多选", "required": False},
                {"path": "service.quote_inputs_required", "label": "报价所需材料",
                 "kind": "list",
                 "question": "客户委托一般要提供什么？",
                 "hint": "例：样品、检测项目、执行标准、报告用途", "required": True},
                {"path": "service.quote_response_hours", "label": "报价响应",
                 "kind": "hours",
                 "question": "多久能给报价或方案？", "hint": "例：24", "required": True},
                {"path": "service.invoice_available", "label": "能否开票", "kind": "bool",
                 "question": "能开票吗？", "hint": "能 / 不能", "required": True},
                {"path": "service.payment_terms", "label": "付款方式", "kind": "list",
                 "question": "付款方式是什么？",
                 "hint": "例：委托时预付、出报告前结清", "required": False},
            ],
        },
    ],
}


STEPS_TAIL: list[dict] = [
    {
        "title": "询价设置",
        "fields": [
            {"path": "rfq.endpoint", "label": "询价接收地址", "kind": "email_or_text",
             "question": "询价发到哪个邮箱？",
             "hint": "例：sales@example.com", "required": True,
             "label_by_gate": {"H": "预订接收渠道", "R": "预订接收渠道",
                               "F": "接单渠道", "O": "接单渠道", "A": "洽购渠道",
                               "I": "对接渠道", "M": "委托渠道"},
             "question_by_gate": {
                 "H": "客户想订位或者问宴席，留个联系方式吧 —— 电话、微信、邮箱都行。",
                 "R": "客户想咨询或预订，留个联系方式吧。",
                 "F": "客户想找你们，留个联系方式吧。",
                 "O": "客户想找你们，留个联系方式吧。",
                 "I": "客户想找你们谈项目，留个联系方式吧。",
                 "M": "客户想委托你们，留个联系方式吧。",
                 "A": "收购买家联系你们，留个联系方式吧。"},
             "hint_by_gate": {
                 "H": "例：138xxxxxxxx 或微信同号",
                 "A": "例：138xxxxxxxx"}},
            # `required=True` 是刻意的：主动声明边界能帮客户 Agent 秒淘汰不匹配的询单。
            # 但「没有不接的活」是这个字段**最常见的合法答案**，必须以 `[]` 记下来，
            # 不能当成"没提供"丢掉 —— 丢了它，confirm 的必填门禁永远过不去，
            # 整个登记流程会卡死在一个用户已经回答过的问题上（2026-09-11 真机实测）。
            {"path": "exclusions", "label": "不接的活", "kind": "list",
             "question": "最后问一个：有什么活是你们明确不接的？",
             "hint": "★ 主动写边界能让客户 Agent 秒淘汰不匹配的询单，反而提高成交率；"
                     "确实没有不接的活，直说「没有」就行",
             "required": True, "empty_is_answer": True,
             "label_by_gate": {"H": "不接的单", "R": "不接的单", "F": "不接的活",
                               "O": "不接的活", "A": "不做的品类",
                               "I": "不接的项目", "M": "不接的委托"},
             "question_by_gate": {
                 "H": "最后问一个：有什么单子是你们明确不接的？比如不接婚宴、不做团餐、"
                      "不做辣到爆的菜？",
                 "R": "最后问一个：有什么业务是你们明确不接的？",
                 "F": "最后问一个：有什么生意是你们明确不做的？比如不赊账、不接零卖？",
                 "O": "最后问一个：有什么活是你们明确不接的？比如不修某些品牌、不接远单？",
                 "I": "最后问一个：有什么项目是你们明确不接的？比如不接驻场、不接维护类？",
                 "M": "最后问一个：有什么委托是你们明确不接的？比如不接个人送检、不接加急？",
                 "A": "最后问一个：有什么品类是你们明确不做的？"},
             "hint_by_gate": {
                 "H": "★ 写清楚边界，客户 Agent 就不会拿不合适的单来烦你；"
                      "确实没有不接的，直说「没有」就行"}},
        ],
    },
]


# 门类主体里插档案字段的位置：门类主体的第 2 步之后（制造业是「硬边界」之后）。
# 改造前是 COMMON_STEPS 的 index 3，分层后同一位置，顺序不变。
PROFILE_INSERT_AFTER = 2


def steps_for_gate(gate: str) -> list[dict]:
    """按门类组装问句（不含档案字段）。未知门类退回制造业，并留下显式提示。"""
    gate = (gate or "C").upper()
    body = STEPS_BY_GATE.get(gate)
    if body is None:
        body = STEPS_BY_GATE["C"]
    return [dict(s) for s in STEPS_HEAD] + [dict(s) for s in body] + [dict(s) for s in STEPS_TAIL]


def _apply_gate_wording(field: dict, gate: str) -> dict:
    """把 `*_by_gate` 里该门类的措辞覆盖到 label/question/hint 上。

    只对**当前门类**生效，且不改动原字典 —— 深拷贝一份再覆盖，避免归档后污染别的门类
    （同一份 STEPS 定义会被不同门类的会话反复读取）。
    """
    out = dict(field)
    for key, src in (("label", "label_by_gate"),
                     ("question", "question_by_gate"),
                     ("hint", "hint_by_gate")):
        table = field.get(src)
        if isinstance(table, dict) and gate in table:
            out[key] = table[gate]
    return out


# 兼容别名：制造业的完整问句集。改造前后逐字段一致，供既有调用方与回归测试使用。
COMMON_STEPS: list[dict] = steps_for_gate("C")


# ---------------------------------------------------------------- 补充归一化器


def norm_processes(text: str) -> tuple[Optional[list], str]:
    """工艺口述 → [{code,name,level}]。'CNC车和铣都做，氧化外发' → 车削/铣削 primary + 氧化 outsourced"""
    codes = json.loads(CODES_FILE.read_text(encoding="utf-8"))
    alias = codes["aliases"]
    meta = codes["codes"]

    # 先判断是否外发
    outsourced_part = ""
    main_part = text
    m = re.search(r"([^，,。;；]{0,20}?)\s*(?:是)?\s*(?:外[发协]|外包|给别人?做|发外面)", text)
    if m:
        outsourced_part = m.group(1)
        main_part = text.replace(m.group(0), "，")

    found: dict[str, str] = {}  # code -> level
    for word, code in sorted(alias.items(), key=lambda x: -len(x[0])):
        if _alias_hit(main_part, word):
            found.setdefault(code, "primary")
    for word, code in sorted(alias.items(), key=lambda x: -len(x[0])):
        if _alias_hit(outsourced_part, word):
            found[code] = "outsourced"

    if not found:
        return None, "未识别到工艺，请从工艺码表中选择"
    out = [{"code": c, "name": meta.get(c, {}).get("name", c), "level": lv}
           for c, lv in found.items()]
    note = "、".join(f"{i['name']}({i['level']})" for i in out)
    return out, note


def _cert_hit(text: str, word: str) -> bool:
    """证照名是否**肯定地**出现在文本里。

    出现在否定语境里的那次出现不算命中：「没有 ISO9001」「未取得 CNAS」
    「不具备 CMA」都是明确没有，不能记成有。
    一次出现被否定、另一次没被否定时，以肯定那次为准（「没有 ISO9001，但有 ISO14001」）。
    """
    low, w = text.lower(), word.lower()
    if not w:
        return False
    start = 0
    while True:
        i = low.find(w, start)
        if i < 0:
            return False
        if not any(n in text[max(0, i - 4): i] for n in _NEG_CERT):
            return True
        start = i + len(w)


def norm_cert_list(text: str, field: Optional[dict] = None) -> tuple[Optional[list], str]:
    """证照口述 → [{name, evidence}]。清单按 `cert_group` 取（默认制造业）。"""
    group = (field or {}).get("cert_group") or "mfg"
    pool = CERT_GROUPS.get(group, CERT_GROUPS["mfg"])
    hits = [c for c in pool if _cert_hit(text, c)]
    if not hits:
        # 「未取得 / 不具备 / 没办」和「没有」一样是有信息的回答，记 [] 而不是 None——
        # 下游要能分清「问过、答的没有」和「压根没问」。
        if re.search(r"没有|无|没做|没认证|没办|未取得|未办理|不具备|不是|不含", text):
            return [], "明确无认证"
        return None, "未匹配到已知认证（可后续在表单补填）"
    # 「ISO9001，2027 年到期」→ 抽有效期年份
    m = re.search(r"(20\d{2})\s*年", text)
    year = m.group(1) if m else None
    out = [{"name": h, "evidence": "self_declared"} for h in hits]
    if year:
        out[0]["valid_until"] = year
    note = "识别到 " + "、".join(hits)
    if year:
        note += f"（有效期至 {year} 年）"
    return out, note


def norm_cap_multi(text: str, field: dict) -> tuple[Optional[list], str]:
    """口述 → 能力码列表。字段用 `cap_from: "域:组名"` 声明取自哪个码表组。

    与 `norm_processes`（制造业工艺）是同一件事的通用版：那边码表写死、
    还要处理「氧化是外发给别人做的」；这边按域取码，餐饮、娱乐、技术服务各自复用。

    企业明确回答「没有 / 不做」时记 `[]` —— 「不做外卖」是有信息的答案，
    记成 None（未提供）会让下游分不清「没问」和「不做」。
    """
    spec = field.get("cap_from") or ""
    if ":" not in spec:
        return None, "字段未声明 cap_from（形如 restaurant:菜系），无法归一化"
    domain, group = spec.split(":", 1)
    t = (text or "").strip()
    hits = cap_codes.resolve(domain, t, group=group)
    if not hits:
        if is_empty_answer(t):
            return [], f"企业明确回答「{t}」—— 记为「暂无」，不是「未提供」"
        return None, f"未识别到{group}，请从码表中选择"
    names = "、".join(cap_codes.code_name(domain, c) for c in hits)
    return hits, f"{group}：{names}"


def norm_percent(text: str) -> tuple[Optional[int], str]:
    """'七八成'→70, '70%'→70, '0.7'→70

    「七八成」这类**区间说法取下限**：老板说的是 70~80%，记成 80% 会高估产能余量，
    客户 Agent 据此判断"这家还有余量"就会出错。宁可保守，并在 note 里写明是区间。

    顺带修掉一个静默丢字：cn2int('七八') 会把「七」覆盖掉只返回 8，
    于是「七八成」被记成 8 成。这里直接从「成」字前面的数字串取首位。
    """
    t = text.strip()
    if "满产" in t or "满负荷" in t:
        return 100, "满产 → 100%"

    m = re.search(r"([0-9零一壹二贰两三叁四肆五伍六陆七柒八捌九玖十拾]{1,3})\s*成", t)
    if m:
        tok = m.group(1)
        first = tok[0]
        v = int(first) if first.isdigit() else _CN_DIGITS.get(first)
        if v is not None:
            lower = v * 10
            if len(tok) > 1:
                return lower, f"{tok}成 是区间说法，按下限 {lower}% 保守取值"
            return lower, f"{tok}成 → {lower}%"

    n, _n = normalize("number", t)
    if n is None:
        return None, "未识别到负荷"
    if "成" in t:
        # 「七八成」cn2int 得 78，取首位数字 7 → 70%
        if n >= 10:
            n = float(str(int(n))[0])
        return int(n * 10), f"约 {int(n)} 成 → {int(n*10)}%"
    if n <= 1:
        return int(n * 100), f"{n} → {int(n*100)}%"
    return int(n), f"{int(n)}%"


def norm_email_or_text(text: str) -> tuple[Optional[str], str]:
    m = re.search(r"[\w.\-]+@[\w\-]+\.[\w.\-]+", text)
    if m:
        return m.group(0), f"提取邮箱 {m.group(0)}"
    t = text.strip()
    return (t or None), "作为联系方式原样保留"


EXTRA = {
    "processes": norm_processes,
    "percent": norm_percent,
    "email_or_text": norm_email_or_text,
}


def normalize_field(field: dict, text: str) -> tuple[Any, str]:
    """
    按字段定义归一化。返回 (value, note)。

    归一化器名称兼容两种键名：STEPS 用 "kind"，profiles/*.json 用 "normalize"。

    三个「空答案是有效答案」的特例，都在进 normalize() 之前截获 —— 它们存在的理由
    都一样：把「企业明确说没有」和「没问到」分开记，下游才敢信这份数据。

      `empty_is_answer`  集合类字段（不接的活、外卖平台）→ `[]`
      `zero_is_answer`   数值类字段（包间数、最低消费）→ `0`
                         餐饮没有包间是业务事实，记 null 会被下游当成「没问到」，
                         而客户 Agent 恰好拿包间数做硬筛选。
    """
    kind = field.get("kind") or field.get("normalize") or "text"
    t = (text or "").strip()
    # bool 必须在最前面截获。`normalize()` 见到「没有 / 无」一律返回 None（=未提供），
    # 而 bool 字段的「没有」是**明确的否** —— 企业答了等于没答，等于把「明确没有」
    # 记成「没问到」。注意只认明确的否：「不确定 / 不知道」仍留在 None（那是真没答案）。
    if kind == "bool" and is_bool_no(t):
        return False, f"企业明确回答「{t}」—— 记为「否」，不是「未提供」"
    # 空答案拦截**只对集合类字段生效**。以前不判 kind，于是给 bool 字段配
    # `empty_is_answer` 会返回 `[]` —— 往布尔字段里写列表，schema 直接拒卡。
    if kind in ("list", "cap_multi") and field.get("empty_is_answer") and is_empty_answer(t):
        return [], f"企业明确回答「{t}」—— 记为「暂无」，不是「未提供」"
    if field.get("zero_is_answer") and is_empty_answer(t):
        return 0, f"企业明确回答「{t}」—— 记为 0，不是「未提供」"
    if kind == "cert_list":
        return norm_cert_list(text, field)
    if kind == "cap_multi":
        return norm_cap_multi(text, field)
    if kind in EXTRA:
        return EXTRA[kind](text)
    return normalize(kind, text, field.get("enum"))


# ---------------------------------------------------------------- 路径读写


def _set_path(obj: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _get_path(obj: dict, path: str) -> Any:
    cur = obj
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


# ---------------------------------------------------------------- 门类

# 制造业历史前缀。名录已发 23823 条 CN-MFG-，不迁移。
_LEGACY_MFG_PREFIX = "MFG"


def _normalize_gate(gate: str) -> str:
    """门类字母规整。空值退回 C（制造业），与改造前的隐含行为一致。"""
    g = str(gate or "").strip().upper()
    return g or "C"


def _gate_from_id(supplier_id: str) -> str:
    """从 supplier_id 前缀回判门类。CN-MFG-xxx 是制造业的历史前缀，不是门类字母。"""
    return cap_codes.gate_of_id(supplier_id)


# ---------------------------------------------------------------- 会话


class CollectSession:
    """一次采集会话：按步骤逐字段提问，把回答归一化成结构化数据。

    `gate` 决定问什么（GB/T 4754 门类字母，默认 C 制造业）。
    制造业保持旧行为逐字段不变；H 住宿餐饮走另一套问句，不再被
    公差/最大工件尺寸这类必填门禁卡死。
    """

    def __init__(self, supplier_id: str, company: str, category: str,
                 profile: str = None, gate: str = None):
        self.supplier_id = supplier_id
        self.company = company
        self.category = category
        # gate 显式传入优先；否则按 supplier_id 前缀回判（CN-H-xxx → H，
        # CN-MFG-xxx → C）；都判不出才退回 C。默认值**不能**写成 "C" ——
        # 那样会短路掉前缀推导，餐饮企业会被当成制造业问「最薄能压多薄的壁」。
        self.gate = _normalize_gate(gate or _gate_from_id(supplier_id) or "C")
        self.profile = profile or self._guess_profile(category)
        self.data: dict = {}
        self.notes: dict[str, str] = {}
        self.raw: dict[str, str] = {}
        self.steps: list[dict] = self._build_steps()
        self.i = 0  # 当前字段在所有字段中的序号

    # -- profile ------------------------------------------------------
    def _guess_profile(self, category: str) -> str:
        """按门类注册表取默认档案。制造业沿用旧映射（经 gates/C.json 的
        profile_by_category），未知品类落 custom —— 与改造前行为一致。"""
        try:
            return cap_codes.gate_default_profile(self.gate, category)
        except Exception:
            return "custom"

    def load_profile(self) -> dict:
        f = PROFILE_DIR / f"{self.profile}.json"
        if not f.exists():
            return {"profile": self.profile, "fields": [], "conflict_rules": []}
        return json.loads(f.read_text(encoding="utf-8"))

    def _build_steps(self) -> list[dict]:
        """按门类组装：通用头 + 门类主体 [+ 品类档案] + 通用尾。

        档案字段插在门类主体的第 2 步之后（制造业即「硬边界」之后）——
        与改造前 COMMON_STEPS 的插入位置完全相同，顺序不回归。
        """
        steps = steps_for_gate(self.gate)
        for st in steps:
            st["fields"] = [_apply_gate_wording(f, self.gate) for f in st["fields"]]
        prof = self.load_profile()
        pfields = [_apply_gate_wording(f, self.gate) for f in prof.get("fields", [])]
        if pfields:
            title = prof.get("category") or self.profile
            cat_step = {
                "title": f"品类能力 · {title}",
                "fields": pfields,
                "from_profile": True,
            }
            steps.insert(len(STEPS_HEAD) + PROFILE_INSERT_AFTER, cat_step)
        return steps

    # -- 遍历 ---------------------------------------------------------
    @property
    def flat(self) -> list[tuple[str, dict]]:
        """(步骤标题, 字段定义) 的扁平列表。"""
        out = []
        for s in self.steps:
            for f in s["fields"]:
                out.append((s["title"], f))
        return out

    def current(self) -> Optional[tuple[str, dict]]:
        if self.i >= len(self.flat):
            return None
        return self.flat[self.i]

    def next_question(self) -> Optional[dict]:
        cur = self.current()
        if cur is None:
            return None
        title, f = cur
        return {
            "step": title,
            "index": self.i,
            "total": len(self.flat),
            "path": f["path"],
            "label": f["label"],
            "question": f["question"],
            "hint": f.get("hint", ""),
            "required": f.get("required", False),
            "enum": f.get("enum"),
            "value": _get_path(self.data, f["path"]),
        }

    def answer(self, text: str) -> dict:
        """提交当前字段的回答，归一化并存储。返回结果描述。"""
        cur = self.current()
        if cur is None:
            return {"ok": False, "error": "采集已结束"}
        _, f = cur
        path = f["path"]
        value, note = normalize_field(f, text)
        self.raw[path] = text
        self.notes[path] = note
        if value is not None:
            _set_path(self.data, path, value)
        self.i += 1
        return {
            "ok": True,
            "path": path,
            "label": f["label"],
            "raw": text,
            "value": value,
            "note": note,
            "parsed": value is not None,
        }

    def skip(self) -> None:
        self.i += 1

    def back(self) -> bool:
        if self.i > 0:
            self.i -= 1
            return True
        return False

    # -- 统计 ---------------------------------------------------------
    def completeness(self) -> dict:
        req = [f for _, f in self.flat if f.get("required")]
        req_done = [f for f in req if _get_path(self.data, f["path"]) is not None]
        allf = [f for _, f in self.flat]
        done = [f for f in allf if _get_path(self.data, f["path"]) is not None]
        return {
            "filled": len(done),
            "total": len(allf),
            "required_filled": len(req_done),
            "required_total": len(req),
            "score": round(len(done) / len(allf) * 100) if allf else 0,
            "required_complete": len(req) == len(req_done),
        }

    def _passed_paths(self) -> set:
        """已经问过（游标走过）的字段路径。"""
        return {f["path"] for _, f in self.flat[: self.i]}

    def conflicts(self) -> list[dict]:
        """跑冲突规则：Profile 规则 + 内置规则。"""
        out: list[dict] = []
        for r in self.load_profile().get("conflict_rules", []):
            # ── 存在性规则（absent）：字段**缺失**时才报
            #
            # 为什么不能用 `when` 表达：when 的语义是「这些字段都有值才校验」，
            # 值为 None 时直接 continue —— 于是 `when: [某个可能不填的字段]`
            # 这种规则**永远不会触发**。surface-treatment 的 permit_present
            # （"未填写排污许可证号"）就是这么死掉的：它想报的正是「缺失」，
            # 而缺失恰好是 when 跳过它的条件。结构性矛盾，靠改数据修不好。
            #
            # 判据用「已经问过」而不是「现在是空」：会话进行中尚未问到该字段时
            # 不该报警，否则前几轮就顶着一条「未填写」的红字。
            absent = r.get("absent") or []
            if absent:
                asked = self._passed_paths()
                miss = [p for p in absent
                        if p in asked and _get_path(self.data, p) in (None, "", [], {})]
                if not miss:
                    continue
                out.append({"id": r["id"], "level": r.get("level", "warn"),
                            "message": r["message"],
                            "values": {p: _get_path(self.data, p) for p in absent}})
                continue

            vals = {k: _get_path(self.data, k) for k in r.get("when", [])}
            if any(v is None for v in vals.values()):
                continue
            bad = self._check(r["check"], vals)
            if bad:
                out.append({"id": r["id"], "level": r.get("level", "warn"),
                            "message": r["message"], "values": vals})
        # 内置：最大工件 > 铣削行程
        size = _get_path(self.data, "limits.max_part_size_mm")
        stroke = _get_path(self.data, "capability.milling_stroke_mm")
        a, b = self._max2(size), self._max2(stroke)
        if a is not None and b is not None and a > b:
            out.append({
                "id": "size_exceeds_stroke", "level": "error",
                "message": f"最大工件 {size} 超过铣削行程 {stroke}，请确认是外协还是填错",
                "values": {"max_part_size_mm": size, "milling_stroke_mm": stroke},
            })
        # 内置：公差过紧
        tol = _get_path(self.data, "limits.tolerance_mm")
        try:
            tol_bad = tol is not None and 0 < float(tol) < 0.005
        except (TypeError, ValueError):
            tol_bad = False
        if tol_bad:
            out.append({
                "id": "tolerance_too_tight", "level": "warn",
                "message": f"公差 {tol}mm 属于精密磨削范畴，CNC 常规达不到，请确认",
                "values": {"tolerance_mm": tol},
            })
        return out

    @staticmethod
    def _max2(v) -> Optional[float]:
        """取序列前两项的最大值；无法解析时返回 None（避免未归一化数据导致崩溃）。"""
        if not isinstance(v, (list, tuple)) or not v:
            return None
        try:
            return max(float(x) for x in v[:2])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _num(v) -> Optional[float]:
        """标量或区间 → 数值。区间取**上界**（校验用上界更保守：报的是"最极端的说法也说不通"）。"""
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, (list, tuple)) and v:
            nums = [x for x in v if isinstance(x, (int, float)) and not isinstance(x, bool)]
            return float(max(nums)) if nums else None
        return None

    @staticmethod
    def _check(kind: str, vals: dict) -> bool:
        if kind == "max_part_size_le_stroke":
            a = CollectSession._max2(vals.get("limits.max_part_size_mm"))
            b = CollectSession._max2(vals.get("capability.milling_stroke_mm"))
            return bool(a is not None and b is not None and a > b)
        if kind == "tolerance_sane":
            t = vals.get("limits.tolerance_mm")
            try:
                return bool(t is not None and 0 < float(t) < 0.005)
            except (TypeError, ValueError):
                return False
        if kind == "thickness_laser_sane":
            th = vals.get("capability.sheet_thickness_mm")
            p = vals.get("capability.laser_power_w")
            try:
                if not th or not p:
                    return False
                return float(th[-1]) > float(p) / 150.0 * 1.6
            except (TypeError, ValueError, IndexError):
                return False
        if kind == "shot_force_sane":
            sw = vals.get("capability.shot_weight_g")
            cf = vals.get("capability.clamping_force_t")
            try:
                if not sw or not cf:
                    return False
                return float(sw[-1]) > float(cf[-1]) * 6
            except (TypeError, ValueError, IndexError):
                return False

        # ── 以下规则原先**声明了但没有实现**，一律落到 return False，
        #    等于这些校验根本不存在（配置里写着"会检查"，实际从不触发）。
        #    2026-09-11 补齐：要么真的判，要么就从 profile 里删掉，不留死配置。
        if kind == "wall_weight_sane":
            # 压铸：壁厚 ≤1mm 却做出 2kg 以上的件，物理上不成立（多半是单位填错）
            wall = CollectSession._num(vals.get("capability.min_wall_thickness_mm"))
            wt = CollectSession._num(vals.get("capability.part_weight_g"))
            return bool(wall is not None and wt is not None and wall <= 1.0 and wt > 2000)
        if kind == "moq_capacity_sane":
            # 月产能还不到一个最小起订量 —— 单位或量级必然有一个填错了
            moq = CollectSession._num(vals.get("limits.min_order_qty"))
            cap = CollectSession._num(vals.get("limits.monthly_capacity.value"))
            return bool(moq is not None and cap is not None and cap < moq)
        if kind == "banquet_tables_le_seats":
            # 一桌按 10 人算，桌数上限超过总座位数 → 要么含临时加桌，要么填错
            seats = CollectSession._num(vals.get("capability.seats"))
            tables = CollectSession._num(vals.get("capability.max_banquet_tables"))
            return bool(seats is not None and tables is not None and tables * 10 > seats)
        if kind == "private_rooms_sane":
            # 每个包间不到 4 个座位说不通（包间一般是 8~20 人）
            seats = CollectSession._num(vals.get("capability.seats"))
            rooms = CollectSession._num(vals.get("capability.private_rooms"))
            return bool(seats and rooms and rooms >= 1 and seats / rooms < 4)
        if kind == "capacity_vs_private_rooms":
            # 娱乐场所：平均每个包间 >40 人说不通（包间是按房间算的，不是按大厅）。
            # ⚠ 判据方向别写反 —— 写成 `rooms * 40 > cap` 等于「平均不到 40 人时报警」，
            # 而那才是**正常**情况：一把包间很多、每个装十几人的场所会被天天误报，
            # 真正该报的「capacity 填的是大厅总数」反而静默通过。
            cap = CollectSession._num(vals.get("capability.capacity"))
            rooms = CollectSession._num(vals.get("capability.private_rooms"))
            return bool(cap and rooms and rooms >= 1 and cap > rooms * 40)
        if kind == "sku_vs_min_order":
            # 起订量比在售品类数还多 —— 多半是单位混了（"件"填成了"箱"）
            sku = CollectSession._num(vals.get("capability.sku_count"))
            moq = CollectSession._num(vals.get("limits.min_order_qty"))
            return bool(sku and moq and moq > sku)
        if kind == "technician_vs_daily_orders":
            # 一个师傅一天顶多 8 单，超出说明日接单量填的是"月"或算错了
            tech = CollectSession._num(vals.get("capability.technician_count"))
            orders = CollectSession._num(vals.get("limits.max_daily_orders"))
            return bool(tech and orders and orders > tech * 8)
        if kind == "team_vs_concurrent":
            # 一个在制项目至少占 3 个人，同时在制数超过团队规模/3 说明排期不实
            team = CollectSession._num(vals.get("capability.team_size"))
            proj = CollectSession._num(vals.get("limits.max_concurrent_projects"))
            return bool(team and proj and proj * 3 > team)
        if kind == "min_days_vs_delivery":
            # 最小接单量（人天）已经超过常规交付周期天数，算术上不可能
            mod = CollectSession._num(vals.get("limits.min_order_days"))
            dd = CollectSession._num(vals.get("limits.delivery_days"))
            return bool(mod is not None and dd is not None and mod > dd)
        if kind == "researcher_vs_team":
            # 研发人员数超过团队总人数
            rc = CollectSession._num(vals.get("capability.researcher_count"))
            team = CollectSession._num(vals.get("capability.team_size"))
            return bool(rc is not None and team is not None and rc > team)
        # 未实现的检查名一律返回 False。**新加规则必须同时实现 _check 分支**，
        # 否则就是又一条死配置。test_gate_expansion 的 T20 会逐个断言每个
        # profile 里声明的 check 名都有实现，防止再次漏掉。
        # 以下规则目前只做提示，不判失败
        return False
