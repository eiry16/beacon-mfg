# -*- coding: utf-8 -*-
"""按项目真实数据结构生成「企业信息收集模板.xlsx」。

数据来源：
  - skills/profiles/*.json       16 个品类档案 -> 每个 sheet 的专属字段（label/unit/required）
  - capability.json schema       通用身份/联系/凭证字段
  - data/gb/{门类字母}/           GB/T 4754 行业门类
表头随 profile 动态生成，schema 变了重跑本脚本即可同步。
"""
import json, glob, os
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES_DIR = os.path.join(ROOT, "skills", "profiles")
OUT = os.path.join(ROOT, "企业信息收集模板.xlsx")

# 品类 -> GB/T 4754 门类
GB_MAP = {
    "precision-machining": "C 制造业",
    "sheet-metal": "C 制造业",
    "injection-molding": "C 制造业",
    "die-casting": "C 制造业",
    "electronics": "C 制造业",
    "surface-treatment": "C 制造业",
    "standard-parts": "C 制造业",
    "raw-materials": "C 制造业",
    "equipment_assembly": "C 制造业",
    "custom": "C 制造业",
    "retail-shop": "F 批发和零售业",
    "restaurant": "H 住宿和餐饮业",
    "it-service": "I 信息传输、软件和信息技术服务业",
    "tech-research": "M 科学研究和技术服务业",
    "resident-service": "O 居民服务、修理和其他服务业",
    "entertainment": "R 文化、体育和娱乐业",
}

# 通用字段（前 15 列），对应 capability.json 的 JSON path
COMMON = [
    ("企业ID", "supplier_id", "形如 CN-MFG-0000005，平台唯一ID"),
    ("企业名称", "company", "工商全称"),
    ("行业门类", "GB/T 4754", "C制造业/F批零/H餐饮/I信息技术/M科研/O居民服务/R文体"),
    ("品类档案", "profile", "对应 skills/profiles/ 的品类，决定专属字段"),
    ("省份", "identity.province", ""),
    ("城市", "identity.city", ""),
    ("详细地址", "identity.address", "门牌号，用于定位与导航"),
    ("经度", "identity.lng", "高德坐标系 GCJ-02"),
    ("纬度", "identity.lat", "高德坐标系 GCJ-02"),
    ("联系电话", "contact.phone", "多个用分号分隔；本模板填真实全号"),
    ("凭证等级", "claim.badge", "L0公开名录 / L1企业自述 / L2平台核验 / L3实地验厂"),
    ("认领状态", "claim.status", "unclaimed / claimed / verified / audited"),
    ("数据来源", "provenance.source", "public_directory 等"),
    ("更新日期", "updated_at", "YYYY-MM-DD"),
    ("备注", "note", "补充说明、不确定项"),
]

# ---------- 样例数据：每个品类 3 行 ----------
# cap 的 key 用「字段 label」（与 profile.fields[].label 一致）
SAMPLES = {
    "precision-machining": [
        dict(company="东莞市精锐精密机械有限公司", province="广东", city="东莞",
             address="长安镇振安中路18号A栋", lng=113.812, lat=22.815, phone="0769-85331288",
             cap={"主要加工设备": "CNC加工中心×12、数控车床×8、线切割×3", "最高轴数": "五轴",
                  "最大车削直径": 320, "铣削行程": "800×600×500", "最小壁厚": 0.8,
                  "螺纹标准": "公制/英制", "热处理是否自有": "否（外协）", "编程软件": "Mastercam、UG"}),
        dict(company="苏州恒昌精密机械有限公司", province="江苏", city="苏州",
             address="吴中区胥口镇子胥路566号", lng=120.581, lat=31.256, phone="0512-66218866",
             cap={"主要加工设备": "立式加工中心×6、车削中心×4", "最高轴数": "四轴",
                  "最大车削直径": 250, "铣削行程": "650×500×450", "最小壁厚": 1.0,
                  "螺纹标准": "公制", "热处理是否自有": "否", "编程软件": "UG"}),
        dict(company="宁波北仑精工机械厂", province="浙江", city="宁波",
             address="北仑区江南路128号", lng=121.833, lat=29.868, phone="0574-86776655",
             cap={"主要加工设备": "数控车床×20、加工中心×5", "最高轴数": "三轴",
                  "最大车削直径": 400, "铣削行程": "1000×600×500", "最小壁厚": 1.2,
                  "螺纹标准": "公制", "热处理是否自有": "否", "编程软件": "Mastercam"}),
    ],
    "sheet-metal": [
        dict(company="铭辉激光切割", province="浙江", city="嘉兴",
             address="魏塘街道成功路36号嘉善全森精机有限公司", lng=120.958, lat=30.866,
             phone="13918174353; 18905835516",
             cap={"激光功率": 3000, "切割幅面": "3000×1500×20", "冲压吨位": 160,
                  "最大折弯长度": 3200, "可加工板厚": "0.5-20", "焊接工艺": "氩弧焊、气保焊",
                  "表面处理是否配套": "否（外协）", "排版软件": "SigmaNest"}),
        dict(company="佛山市鑫泰金属制品有限公司", province="广东", city="佛山",
             address="南海区狮山镇兴业北路9号", lng=113.032, lat=23.128, phone="0757-86663388",
             cap={"激光功率": 6000, "切割幅面": "6000×2000×25", "冲压吨位": 400,
                  "最大折弯长度": 4000, "可加工板厚": "0.8-25", "焊接工艺": "氩弧焊、点焊",
                  "表面处理是否配套": "是（喷粉线）", "排版软件": "AutoNest"}),
        dict(company="青岛海诚钣金有限公司", province="山东", city="青岛",
             address="城阳区流亭街道正阳中路77号", lng=120.392, lat=36.298, phone="0532-87712299",
             cap={"激光功率": 2000, "切割幅面": "3000×1500×16", "冲压吨位": 110,
                  "最大折弯长度": 2500, "可加工板厚": "0.5-12", "焊接工艺": "气保焊",
                  "表面处理是否配套": "否", "排版软件": "SigmaNest"}),
    ],
    "injection-molding": [
        dict(company="深圳市金达注塑有限公司", province="广东", city="深圳",
             address="宝安区沙井街道新和大道66号", lng=113.818, lat=22.729, phone="0755-29881122",
             cap={"锁模力范围": "80-650", "射出量范围": "50-3200", "最多模穴数": 32,
                  "常用原料": "ABS、PP、PC、尼龙", "模具能力": "自开模（含试模）", "开模周期": 25,
                  "模具寿命": 500000, "包胶/镶件": "支持双色包胶"}),
        dict(company="台州黄岩兴发塑模有限公司", province="浙江", city="台州",
             address="黄岩区东城街道二环东路188号", lng=121.262, lat=28.648, phone="0576-84223377",
             cap={"锁模力范围": "60-400", "射出量范围": "30-1500", "最多模穴数": 16,
                  "常用原料": "PP、PE、ABS", "模具能力": "自开模", "开模周期": 20,
                  "模具寿命": 300000, "包胶/镶件": "不支持"}),
        dict(company="苏州市嘉明精密注塑厂", province="江苏", city="苏州",
             address="相城区黄埭镇春兴路22号", lng=120.583, lat=31.398, phone="0512-65489966",
             cap={"锁模力范围": "50-260", "射出量范围": "20-800", "最多模穴数": 8,
                  "常用原料": "POM、PC、ABS", "模具能力": "代管模", "开模周期": 30,
                  "模具寿命": 200000, "包胶/镶件": "支持镶件"}),
    ],
    "die-casting": [
        dict(company="广东鸿图精密压铸有限公司", province="广东", city="肇庆",
             address="高要区金渡镇世纪大道1号", lng=112.465, lat=23.032, phone="0758-8512333",
             cap={"压铸机吨位": "160-2500", "合金牌号": "ADC12、A380、YL113", "件重范围": "20-8000",
                  "最小壁厚": 1.5, "气孔等级": "ASTM E505 二级", "后加工能力": "CNC、钻孔攻牙、喷粉",
                  "模具能力": "自开模"}),
        dict(company="宁波旭升压铸有限公司", province="浙江", city="宁波",
             address="北仑区大碶街道沿山河南路68号", lng=121.842, lat=29.912, phone="0574-86119900",
             cap={"压铸机吨位": "200-1600", "合金牌号": "ADC12、A356", "件重范围": "50-5000",
                  "最小壁厚": 2.0, "气孔等级": "二级", "后加工能力": "CNC、抛丸",
                  "模具能力": "自开模"}),
        dict(company="重庆渝江压铸有限公司", province="重庆", city="重庆",
             address="渝北区空港工业园区翔宇路8号", lng=106.638, lat=29.716, phone="023-67182266",
             cap={"压铸机吨位": "125-1250", "合金牌号": "ADC12", "件重范围": "30-3000",
                  "最小壁厚": 2.0, "气孔等级": "三级", "后加工能力": "钻孔攻牙、CNC",
                  "模具能力": "外协开模"}),
    ],
    "electronics": [
        dict(company="深圳市创信电子科技有限公司", province="广东", city="深圳",
             address="龙华区观澜街道桂花路199号", lng=114.075, lat=22.703, phone="0755-28016688",
             cap={"产品线": "PCBA代工、SMT贴片、整机组装", "SMT 产线数": 6, "日贴片点数": 8000000,
                  "最小封装": "0201", "BGA 最小间距": 0.35, "测试能力": "ICT、FCT、老化测试",
                  "三防漆": "有（选择性涂覆）", "产品认证": "ISO9001、UL"}),
        dict(company="苏州市新锐电子有限公司", province="江苏", city="苏州",
             address="工业园区星龙街298号", lng=120.718, lat=31.322, phone="0512-62589977",
             cap={"产品线": "SMT贴片、PCBA", "SMT 产线数": 3, "日贴片点数": 3000000,
                  "最小封装": "0402", "BGA 最小间距": 0.4, "测试能力": "ICT、FCT",
                  "三防漆": "有（整板涂覆）", "产品认证": "ISO9001"}),
        dict(company="东莞市立德电路板有限公司", province="广东", city="东莞",
             address="虎门镇沙角工业区工业路12号", lng=113.668, lat=22.812, phone="0769-85526611",
             cap={"产品线": "PCB制板、PCBA", "SMT 产线数": 2, "日贴片点数": 1200000,
                  "最小封装": "0603", "BGA 最小间距": 0.5, "测试能力": "飞针测试、AOI",
                  "三防漆": "无", "产品认证": "ISO9001、RoHS"}),
    ],
    "surface-treatment": [
        dict(company="东莞市长安宏发五金电镀厂", province="广东", city="东莞",
             address="长安镇厦岗社区工业区兴业路5号", lng=113.796, lat=22.802, phone="0769-85439922",
             cap={"处理工艺": "镀锌、镀镍、阳极氧化", "适用基材": "钢、铁、铝、铜", "膜厚范围": "5-25",
                  "可做颜色": "银白、黑镍、彩色", "盐雾测试": 96, "最大工件尺寸": "1200×600×800",
                  "排污许可证号": "粤环证字S2021-08871", "挂镀/滚镀": "两者均可"}),
        dict(company="苏州市吴江金辉电泳涂装有限公司", province="江苏", city="苏州",
             address="吴江区汾湖高新区东联路33号", lng=120.905, lat=31.058, phone="0512-63251199",
             cap={"处理工艺": "电泳、喷粉、阳极氧化", "适用基材": "铝型材、钢板", "膜厚范围": "15-80",
                  "可做颜色": "黑、灰、香槟", "盐雾测试": 500, "最大工件尺寸": "3000×800×600",
                  "排污许可证号": "苏环证字E2020-11234", "挂镀/滚镀": "挂镀"}),
        dict(company="佛山市顺德区华彩喷涂有限公司", province="广东", city="佛山",
             address="顺德区容桂街道华口工业区兴业路8号", lng=113.278, lat=22.756, phone="0757-28387766",
             cap={"处理工艺": "喷粉、喷漆", "适用基材": "钢、铝", "膜厚范围": "40-120",
                  "可做颜色": "按色卡定制", "盐雾测试": 240, "最大工件尺寸": "2500×1000×800",
                  "排污许可证号": "粤环证字E2019-04512", "挂镀/滚镀": "不适用"}),
    ],
    "standard-parts": [
        dict(company="嘉兴晋亿标准件有限公司", province="浙江", city="嘉兴",
             address="嘉善县惠民街道晋亿大道1号", lng=120.951, lat=30.842, phone="0573-84185666",
             cap={"产品线": "螺栓、螺母、垫圈、铆钉", "标准体系": "GB、DIN、ISO、ANSI", "规格区间": "M3-M36",
                  "备货模式": "现货库存", "常备库存": "约8000吨", "当日发货": "支持", "非标定制": "支持"}),
        dict(company="深圳市永宏紧固件有限公司", province="广东", city="深圳",
             address="宝安区松岗街道潭头工业区12栋", lng=113.842, lat=22.778, phone="0755-27098822",
             cap={"产品线": "螺丝、螺母、车削件", "标准体系": "GB、ISO", "规格区间": "M2-M20",
                  "备货模式": "现货+订货", "常备库存": "约1500吨", "当日发货": "支持", "非标定制": "支持"}),
        dict(company="苏州市华东标准件厂", province="江苏", city="苏州",
             address="吴中区临湖镇石舍村工业南区6号", lng=120.612, lat=31.198, phone="0512-66298833",
             cap={"产品线": "螺栓、垫圈、销", "标准体系": "GB、DIN", "规格区间": "M4-M24",
                  "备货模式": "订货为主", "常备库存": "约300吨", "当日发货": "不支持", "非标定制": "支持"}),
    ],
    "raw-materials": [
        dict(company="上海宝钢钢材贸易有限公司", province="上海", city="上海",
             address="宝山区牡丹江路1588号", lng=121.489, lat=31.402, phone="021-56781188",
             cap={"主营牌号": "Q235、45#、304、6061", "材料形态": "板材、棒材、管材", "规格区间": "板厚0.5-200；棒Φ6-300",
                  "常备库存": 12000, "加工服务": "开平、分条、切割", "提供材质单": "是",
                  "最小切割长度": 100, "公差标准": "GB/T 709"}),
        dict(company="佛山市南海宏发铝业有限公司", province="广东", city="佛山",
             address="南海区大沥镇广佛路铝材市场A区18号", lng=113.106, lat=23.106, phone="0757-85556622",
             cap={"主营牌号": "6061、6063、7075", "材料形态": "型材、棒材、管材", "规格区间": "型材截面≤400；棒Φ8-350",
                  "常备库存": 2600, "加工服务": "锯切、氧化", "提供材质单": "是",
                  "最小切割长度": 50, "公差标准": "GB/T 5237"}),
        dict(company="宁波金田铜业集团", province="浙江", city="宁波",
             address="江北区慈城镇城西西路1号", lng=121.512, lat=29.982, phone="0574-87592288",
             cap={"主营牌号": "T2、H62、H65", "材料形态": "棒材、管材、带材", "规格区间": "棒Φ5-200；带厚0.1-3",
                  "常备库存": 5200, "加工服务": "分条、切割", "提供材质单": "是",
                  "最小切割长度": 200, "公差标准": "GB/T 5231"}),
    ],
    "equipment_assembly": [
        dict(company="苏州汇川自动化设备有限公司", province="江苏", city="苏州",
             address="吴中区胥口镇孙武路88号", lng=120.566, lat=31.243, phone="0512-66889900",
             cap={"交付形态": "整机交付、产线集成", "产能计量单位": "台/月", "电控系统集成": "自有电气团队（PLC、伺服）",
                  "可承接产线长度": 60, "是否含现场安装调试": "含", "主要设备": "装配线×2、调试工位×6"}),
        dict(company="东莞市台群智能装备有限公司", province="广东", city="东莞",
             address="松山湖工业北路8号", lng=113.885, lat=22.938, phone="0769-22897766",
             cap={"交付形态": "整机交付", "产能计量单位": "台/月", "电控系统集成": "自有（PLC、机器人）",
                  "可承接产线长度": 30, "是否含现场安装调试": "含", "主要设备": "装配工位×12、老化台×4"}),
        dict(company="青岛华瑞包装机械有限公司", province="山东", city="青岛",
             address="城阳区棘洪滩街道锦宏西路66号", lng=120.402, lat=36.312, phone="0532-87903355",
             cap={"交付形态": "整机交付、改造升级", "产能计量单位": "套/月", "电控系统集成": "外协（可对接）",
                  "可承接产线长度": 20, "是否含现场安装调试": "含（限国内）", "主要设备": "装配工位×6"}),
    ],
    "custom": [
        dict(company="深圳市三丰检测服务有限公司", province="广东", city="深圳",
             address="南山区西丽街道留仙大道3370号", lng=113.952, lat=22.588, phone="0755-86532211",
             cap={"服务项目": "尺寸检测、材质分析、失效分析", "能力概述": "三坐标+光谱+金相，具备CNAS认可",
                  "典型客户行业": "消费电子、汽车零部件", "核心资源": "蔡司三坐标×2、直读光谱仪×1"}),
        dict(company="苏州智联工业设计有限公司", province="江苏", city="苏州",
             address="工业园区星湖街328号创意产业园6栋", lng=120.722, lat=31.308, phone="0512-62897700",
             cap={"服务项目": "结构设计、外观设计、手板制作", "能力概述": "15人设计团队，可承接整机结构开发",
                  "典型客户行业": "家电、医疗器械", "核心资源": "设计工程师×15、3D打印设备×3"}),
        dict(company="东莞市中泰装配服务有限公司", province="广东", city="东莞",
             address="塘厦镇林村社区新阳路18号", lng=114.072, lat=22.802, phone="0769-87925566",
             cap={"服务项目": "整机组装、包装代工、返修", "能力概述": "200人装配车间，日组装5000台",
                  "典型客户行业": "小家电、数码配件", "核心资源": "装配线×6、包装线×3"}),
    ],
    "retail-shop": [
        dict(company="贵阳南明老字号五金机电经营部", province="贵州", city="贵阳",
             address="南明区解放路五金机电市场B区12号", lng=106.712, lat=26.567, phone="0851-85811322",
             cap={"在售品类数": 2600, "最少起订量": 1, "起订单位": "个", "是否有现货": "有",
                  "营业时间": "08:30-18:30", "发货/到货天数": 1, "主营品牌": "世达、史丹利、博世",
                  "配送半径": 30}),
        dict(company="苏州工业园区华亿劳保用品店", province="江苏", city="苏州",
             address="工业园区娄葑镇东环路168号", lng=120.688, lat=31.302, phone="0512-67482255",
             cap={"在售品类数": 850, "最少起订量": 10, "起订单位": "件", "是否有现货": "有",
                  "营业时间": "09:00-18:00", "发货/到货天数": 2, "主营品牌": "3M、霍尼韦尔",
                  "配送半径": 20}),
        dict(company="佛山顺德陈村花卉世界绿植批发部", province="广东", city="佛山",
             address="顺德区陈村镇花卉世界芳华大道9号", lng=113.238, lat=22.972, phone="0757-23336688",
             cap={"在售品类数": 320, "最少起订量": 50, "起订单位": "盆", "是否有现货": "有",
                  "营业时间": "07:00-19:00", "发货/到货天数": 3, "主营品牌": "本地自繁",
                  "配送半径": 80}),
    ],
    "restaurant": [
        dict(company="贵阳市胡子哥餐厅", province="贵州", city="贵阳",
             address="云岩区北京路66号", lng=106.706, lat=26.596, phone="0851-85829410",
             cap={"总座位数": 120, "包间数": 6, "人均消费": 88, "营业时间": "10:00-21:30",
                  "营业时段": "午市、晚市", "最大宴席桌数": 12, "场地条件": "可停大巴、有投影"}),
        dict(company="苏州市吴门人家饭店", province="江苏", city="苏州",
             address="姑苏区临顿路225号", lng=120.632, lat=31.312, phone="0512-67770088",
             cap={"总座位数": 200, "包间数": 10, "人均消费": 150, "营业时间": "11:00-22:00",
                  "营业时段": "午市、晚市", "最大宴席桌数": 20, "场地条件": "独立宴会厅、可办婚宴"}),
        dict(company="东莞长安客家庄酒楼", province="广东", city="东莞",
             address="长安镇长青南路88号", lng=113.806, lat=22.818, phone="0769-85532299",
             cap={"总座位数": 300, "包间数": 15, "人均消费": 120, "营业时间": "09:30-21:00",
                  "营业时段": "早茶、午市、晚市", "最大宴席桌数": 30, "场地条件": "宴会厅、免费停车"}),
    ],
    "it-service": [
        dict(company="深圳市云智软件技术服务有限公司", province="广东", city="深圳",
             address="南山区科技园科苑路15号科兴科学园B栋", lng=113.948, lat=22.542, phone="0755-86639900",
             cap={"技术团队人数": 45, "最小接单人天": 20, "需求响应时效": 4, "技术栈": "Java、Spring Cloud、Vue、K8s",
                  "交付成果": "源码、部署文档、运维手册", "代表案例": "某制造企业MES系统",
                  "免费维保月数": 3, "是否接受 NDA": "接受", "技术支持时段": "工作日 09:00-18:00"}),
        dict(company="苏州工业园区数联信息技术有限公司", province="江苏", city="苏州",
             address="工业园区星湖街218号生物纳米园C栋", lng=120.729, lat=31.318, phone="0512-62997711",
             cap={"技术团队人数": 18, "最小接单人天": 5, "需求响应时效": 8, "技术栈": "Python、React、PostgreSQL",
                  "交付成果": "源码、接口文档", "代表案例": "工业数据采集看板",
                  "免费维保月数": 1, "是否接受 NDA": "接受", "技术支持时段": "工作日 09:00-21:00"}),
        dict(company="贵阳市黔云网络科技有限公司", province="贵州", city="贵阳",
             address="观山湖区林城西路88号金融城B座", lng=106.632, lat=26.638, phone="0851-86881122",
             cap={"技术团队人数": 12, "最小接单人天": 3, "需求响应时效": 12, "技术栈": "PHP、Uni-app、MySQL",
                  "交付成果": "源码、部署", "代表案例": "本地生活小程序",
                  "免费维保月数": 6, "是否接受 NDA": "接受", "技术支持时段": "7×12 小时"}),
    ],
    "tech-research": [
        dict(company="深圳市华测检测技术研究院", province="广东", city="深圳",
             address="宝安区新安街道留仙三路4号", lng=113.902, lat=22.652, phone="0755-29987766",
             cap={"是否有 CMA 资质": "是", "是否有 CNAS 认可": "是", "关键仪器设备": "ICP-MS、GC-MS、万能材料试验机",
                  "团队人数": 220, "研发/技术人员数": 150, "出报告天数": 7, "最低委托金额": 800,
                  "响应时效": 4, "样品是否退还": "可退还（付费）", "是否接受保密协议": "接受",
                  "代表案例": "RoHS/REACH 检测、材料失效分析"}),
        dict(company="苏州中科材料检测有限公司", province="江苏", city="苏州",
             address="工业园区若水路398号", lng=120.752, lat=31.278, phone="0512-62809922",
             cap={"是否有 CMA 资质": "是", "是否有 CNAS 认可": "否", "关键仪器设备": "金相显微镜、硬度计、盐雾箱",
                  "团队人数": 45, "研发/技术人员数": 32, "出报告天数": 5, "最低委托金额": 500,
                  "响应时效": 8, "样品是否退还": "不退", "是否接受保密协议": "接受",
                  "代表案例": "金属件盐雾与金相检测"}),
        dict(company="贵阳市黔力计量校准服务有限公司", province="贵州", city="贵阳",
             address="白云区白云南路188号", lng=106.652, lat=26.682, phone="0851-84602299",
             cap={"是否有 CMA 资质": "否", "是否有 CNAS 认可": "否", "关键仪器设备": "标准件、量块、压力校验仪",
                  "团队人数": 16, "研发/技术人员数": 10, "出报告天数": 3, "最低委托金额": 300,
                  "响应时效": 12, "样品是否退还": "退还", "是否接受保密协议": "接受",
                  "代表案例": "卡尺/压力表年度校准"}),
    ],
    "resident-service": [
        dict(company="贵阳市南明区顺发水电维修服务部", province="贵州", city="贵阳",
             address="南明区花果园大街1号", lng=106.702, lat=26.572, phone="0851-85993311",
             cap={"师傅人数": 12, "服务半径": 25, "起步价": 80, "上门响应时长": 2,
                  "接单时间": "07:30-20:00", "质保天数": 90, "服务保障": "持证上岗、明码标价", "日最大接单量": 40}),
        dict(company="苏州市姑苏区万家家政服务部", province="江苏", city="苏州",
             address="姑苏区干将东路558号", lng=120.638, lat=31.302, phone="0512-65187722",
             cap={"师傅人数": 35, "服务半径": 15, "起步价": 120, "上门响应时长": 3,
                  "接单时间": "08:00-19:00", "质保天数": 30, "服务保障": "员工体检、可换人", "日最大接单量": 60}),
        dict(company="东莞市虎门快洁家电维修中心", province="广东", city="东莞",
             address="虎门镇运河北路28号", lng=113.672, lat=22.812, phone="0769-85198833",
             cap={"师傅人数": 8, "服务半径": 20, "起步价": 60, "上门响应时长": 1,
                  "接单时间": "08:00-21:00", "质保天数": 180, "服务保障": "原厂配件、保修凭证", "日最大接单量": 25}),
    ],
    "entertainment": [
        dict(company="贵阳市云岩区星光量贩KTV", province="贵州", city="贵阳",
             address="云岩区中华中路168号", lng=106.708, lat=26.588, phone="0851-85857788",
             cap={"同时可容纳人数": 300, "包间数": 38, "人均消费": 120, "营业时间": "12:00-次日02:00",
                  "营业时段": "下午场、晚场、夜场", "单次最短时长": 2, "招牌项目": "量贩自助、生日布置"}),
        dict(company="苏州工业园区极地网咖", province="江苏", city="苏州",
             address="工业园区星海街200号", lng=120.692, lat=31.322, phone="0512-62557799",
             cap={"同时可容纳人数": 160, "包间数": 4, "人均消费": 45, "营业时间": "24小时",
                  "营业时段": "全天", "单次最短时长": 1, "招牌项目": "高配电竞区、包夜套餐"}),
        dict(company="东莞长安飞跃运动馆", province="广东", city="东莞",
             address="长安镇锦厦社区体育路6号", lng=113.812, lat=22.822, phone="0769-85667711",
             cap={"同时可容纳人数": 120, "包间数": 0, "人均消费": 60, "营业时间": "09:00-22:00",
                  "营业时段": "日场、晚场", "单次最短时长": 1, "招牌项目": "羽毛球×12片、篮球全场"}),
    ],
}

# ---------- 样式 ----------
HDR_FILL = PatternFill("solid", fgColor="1F4E79")
HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
SUB_FILL = PatternFill("solid", fgColor="DDEBF7")
REQ_FILL = PatternFill("solid", fgColor="FFF2CC")
SAMPLE_FONT = Font(color="808080", size=10)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def style_header(ws, ncols, row=1):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER


def build_sheet(wb, profile, meta, idx=0):
    cat = meta.get("category", profile)
    fields = meta.get("fields", [])
    gb = GB_MAP.get(profile, "")
    title = f"{cat}-{profile}"
    ws = wb.create_sheet(title[:31])

    # 表头：通用 + 专属
    headers = [(n, d, False) for n, _, d in COMMON]
    for f in fields:
        label = f.get("label")
        unit = f.get("unit")
        headers.append((f"{label}({unit})" if unit else label, f.get("hint", ""), bool(f.get("required"))))

    for i, (name, _hint, _req) in enumerate(headers, start=1):
        ws.cell(row=1, column=i, value=name)
    style_header(ws, len(headers))
    ws.row_dimensions[1].height = 34

    # 必填列底色标记
    for i, (_n, _h, req) in enumerate(headers, start=1):
        if req:
            ws.cell(row=1, column=i).fill = PatternFill("solid", fgColor="C00000")

    # 3 行样例
    rows = SAMPLES.get(profile, [])
    for r, s in enumerate(rows, start=2):
        vals = [
            f"CN-MFG-{1000000 + idx * 10 + (r - 2):07d}",
            s["company"], gb, f"{cat}（{profile}）",
            s["province"], s["city"], s["address"], s["lng"], s["lat"], s["phone"],
            "L0", "unclaimed", "public_directory", "2026-09-17", "样例数据，可删除",
        ]
        for f in fields:
            vals.append(s["cap"].get(f.get("label"), ""))
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = BORDER
            cell.font = SAMPLE_FONT
            cell.alignment = Alignment(vertical="center", wrap_text=True)

    # 列宽
    for i, (name, _h, _r) in enumerate(headers, start=1):
        w = max(10, min(22, len(str(name)) * 2.0 + 4))
        if i <= 3:
            w = 26 if i == 2 else 16
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C2"
    return ws, len(headers), len(rows)


def build_readme(wb, sheet_index):
    ws = wb.create_sheet("填写说明", 0)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 46
    ws.column_dimensions["C"].width = 62

    def sec(row, text):
        c = ws.cell(row=row, column=1, value=text)
        c.font = Font(bold=True, size=12, color="1F4E79")
        return row + 1

    def kv(row, k, v, note=""):
        ws.cell(row=row, column=1, value=k).font = Font(bold=True, size=10)
        ws.cell(row=row, column=2, value=v).alignment = Alignment(wrap_text=True, vertical="center")
        ws.cell(row=row, column=3, value=note).alignment = Alignment(wrap_text=True, vertical="center")
        return row + 1

    r = 1
    c = ws.cell(row=r, column=1, value="BeaconMFG 企业信息收集模板")
    c.font = Font(bold=True, size=16, color="1F4E79")
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    r += 2

    r = sec(r, "一、用途")
    r = kv(r, "用途", "按品类分表采集企业信息，一行一家企业，填完可直接结构化入库（capability.json）。",
           "表头由 skills/profiles/*.json 动态生成，与实际数据结构保持一致。")
    r = kv(r, "行数要求", "每张表已填 3 行样例（灰色斜体），正式采集时删除样例行，从下方追加。", "")
    r += 1

    r = sec(r, "二、数据结构来源（为什么要这样分表）")
    r = kv(r, "行业门类", "data/gb/{C,F,H,I,M,O,R}/ —— 按 GB/T 4754 门类物理分库。",
           "C制造业 / F批发零售 / H住宿餐饮 / I信息技术 / M科研技术 / O居民服务 / R文体娱乐")
    r = kv(r, "品类档案", "skills/profiles/*.json —— 16 个品类，每个品类定义自己的专属字段（label/unit/required/hint）。",
           "这是本模板“一个品类一张 sheet”的依据。")
    r = kv(r, "能力卡 schema", "skills/vendors/{id}/capability.json —— identity/contact/processes/materials/limits/claim/provenance。",
           "前 15 个通用列即来自该 schema。")
    r += 1

    r = sec(r, "三、通用字段（前 15 列，所有品类共用）")
    for name, path, note in COMMON:
        r = kv(r, name, path, note)
    r += 1

    r = sec(r, "四、凭证等级 L0-L3")
    for lv, desc in [("L0", "公开名录收录（公开名录，未核验）"), ("L1", "已认领，信息由企业自述"),
                     ("L2", "平台已核验（营业执照/资质证书）"), ("L3", "第三方实地验厂")]:
        r = kv(r, lv, desc, "")
    r += 1

    r = sec(r, "五、填写规范（红线）")
    for k, v in [
        ("不填就留空", "禁止编造。价格、产能、交期、公差没写就是没写，平台会如实呈现“未提供”。"),
        ("多值用分号", "如电话 13900000001; 0769-85331288，工艺“激光切割; 折弯”。"),
        ("单位必须统一", "列名已标单位（如 激光功率(W)、板厚(mm)），只填数字，不要带单位。"),
        ("红底表头=必填", "来自 profile.fields[].required=true，缺该字段无法通过校验。"),
        ("外协要标注", "非自有产线的工艺要写明“外协”，否则会被误判为自有能力。"),
        ("边界要写清", "能力边界（不做什么）能挡掉不匹配询单，提高成交率。"),
    ]:
        r = kv(r, k, v, "")
    r += 1

    r = sec(r, "六、电话脱敏约定（重要）")
    r = kv(r, "本模板填全号", "采集阶段填真实全号（如 0851-85829410），不要填 138****0000。",
           "入库后 data/gb 工作树必须保持全号。")
    r = kv(r, "提交时才脱敏", "git add 时按 .gitattributes 的 clean filter 自动脱敏为掩码。",
           "★ 禁止对 data/gb、data/en、phone-index 执行 git checkout/restore —— 会把全号无声抹成掩码且 git status 仍干净。")
    r += 1

    r = sec(r, "七、采集后的入库流程")
    r = kv(r, "1. 采集", "python scripts/collect/cli.py --id {企业ID}（对话式，15 分钟）或手工填 capability.json。", "")
    r = kv(r, "2. 生成", "产出 SKILL.md + capability.json + 指纹行。", "")
    r = kv(r, "3. 校验", "python scripts/validate_vendor_skills.py {企业ID}，出现 [PASS] 才可提交。", "")
    r = kv(r, "4. 提交", "提 PR，标题格式：[SKILL] {公司名} / {品类}", "")
    r += 1

    r = sec(r, "八、Sheet 索引")
    ws.cell(row=r, column=1, value="Sheet 名").font = Font(bold=True, size=10)
    ws.cell(row=r, column=2, value="品类（profile）").font = Font(bold=True, size=10)
    ws.cell(row=r, column=3, value="GB/T 4754 门类").font = Font(bold=True, size=10)
    r += 1
    for title, profile, gb in sheet_index:
        ws.cell(row=r, column=1, value=title)
        ws.cell(row=r, column=2, value=profile)
        ws.cell(row=r, column=3, value=gb)
        r += 1
    return ws


def main():
    wb = Workbook()
    wb.remove(wb.active)

    files = sorted(glob.glob(os.path.join(PROFILES_DIR, "*.json")))
    sheet_index = []
    total_rows = 0
    for idx, fp in enumerate(files):
        meta = json.load(open(fp, encoding="utf-8"))
        profile = meta.get("profile") or os.path.basename(fp)[:-5]
        ws, ncols, nrows = build_sheet(wb, profile, meta, idx)
        total_rows += nrows
        sheet_index.append((ws.title, profile, GB_MAP.get(profile, "")))
        print(f"  ✓ {ws.title:<32} 列={ncols:<3} 样例行={nrows}")

    build_readme(wb, sheet_index)
    wb.active = 0
    wb.save(OUT)
    print(f"\n生成完成: {OUT}")
    print(f"sheet 数: {len(wb.sheetnames)}（1 说明 + {len(sheet_index)} 品类），样例行合计 {total_rows}")


if __name__ == "__main__":
    main()
