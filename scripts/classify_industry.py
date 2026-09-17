# -*- coding: utf-8 -*-
"""按 GB/T 4754-2017 国民经济行业分类给供应商重新归类

设计原则（与主库红线一致）：
1. **只信公司名**：公司名是最强证据；keywords 只用于补位；
   两者都没有才回退到原 category，并标 confidence="low"。
2. **不猜**：连兜底都拿不到信号时 industry 记为 null，绝不硬贴标签。
3. **制造商 vs 批发商分开**：贸易/批发类进 F51 批发业，采购方一眼能排除。

输出字段（写入 data/suppliers/*.json 的每条记录）：
    industry       : {"code","name","path","confidence","source"} 或 null
    is_manufacturer: true/false（F51 批发业为 false）

用法:
    python scripts/classify_industry.py --stats        # 只看分布，不写盘
    python scripts/classify_industry.py --apply         # 写回中文主库
    python scripts/classify_industry.py --audit 30      # 抽样人工核对
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from industry_taxonomy import (  # noqa: E402
    CODES, path_of, name_of, level_of, match_longest, is_manufacturer,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gb_store  # noqa: E402

# ---------------------------------------------------------------- 规则表
# 顺序 = 优先级，越靠前越具体。每条：(关键词元组, 代码, 置信度)
# 关键词全部按「公司名子串」匹配；刻意不用单字（如"轴""门""铁"）以免误伤。
NAME_RULES = [
    # --- 医疗器械：C35 专用设备制造业里的真制造行业（3584）。
    #     放在最前是因为下面的 EXTENDED_INDUSTRY_RULES 有「药品/药业」，
    #     「XX医疗科技有限公司」这类名字不能被当成药厂。
    (("医疗器械", "医疗设备", "医用器材", "医疗仪器"), "3584", "high"),

    # --- 表面处理：独立行业，采购方会单独找，优先于"压铸电镀"这类复合名
    (("电镀", "阳极氧化", "阳极", "电泳", "镀锌", "镀镍", "镀铬", "镀锡", "镀银", "镀金",
      "磷化", "发黑", "达克罗", "钝化", "喷粉", "喷塑", "喷涂", "喷漆", "烤漆",
      "表面处理", "热处理", "渗碳", "氮化", "氧化加工", "抛光加工"), "3360", "high"),

    # --- 电子 / 电气
    (("线路板", "电路板", "PCB", "PCBA", "印制电路", "电子电路"), "3982", "high"),
    (("集成电路", "半导体", "晶圆", "芯片"), "3973", "high"),
    (("显示屏", "液晶", "LCD", "LED显示", "触控屏"), "3974", "high"),
    (("LED", "发光二极管", "半导体照明"), "3975", "medium"),
    (("传感器", "敏感元件", "变送器"), "3983", "high"),
    (("电容器", "电阻器", "电感器", "电容"), "3981", "high"),
    (("连接器", "接插件", "端子", "线束", "排针"), "3989", "medium"),
    (("锂电池", "锂离子", "蓄电池", "电池"), "3841", "medium"),
    (("变压器", "整流器", "电感"), "3821", "medium"),
    (("配电箱", "配电柜", "开关柜", "控制柜", "断路器"), "3823", "medium"),
    (("电线", "电缆"), "3831", "high"),
    (("电机", "马达", "伺服电机", "步进电机"), "3812", "medium"),
    (("灯具", "灯饰", "照明", "光源"), "3872", "medium"),
    (("电子元件", "电子元器件", "电子配件", "电子制品"), "3989", "medium"),

    # --- 模具（3525 模具制造，含注塑模/冲压模/压铸模）
    (("模具",), "3525", "high"),

    # --- 铸造 / 锻造
    (("压铸",), "3392", "high"),
    (("粉末冶金", "锻造", "锻压", "锻件"), "3393", "high"),
    (("铸造", "铸铁", "铸钢", "翻砂", "精密铸造"), "3391", "high"),

    # --- 塑料：先分原料商 vs 制品厂（这是原「塑料」检索噪音的主因）
    (("塑料原料", "塑胶原料", "塑料粒子", "塑料颗粒", "改性塑料", "工程塑料", "塑料树脂",
      "合成树脂", "色母", "塑化", "母粒", "塑料助剂"), "2651", "high"),
    (("塑料薄膜", "保鲜膜", "缠绕膜", "拉伸膜", "薄膜"), "2921", "high"),
    (("塑料包装", "塑料瓶", "塑料容器", "吸塑", "吹塑", "注塑包装"), "2926", "high"),
    (("塑料板材", "亚克力板", "有机玻璃板", "塑料管", "塑胶管", "塑钢", "塑料型材"), "2922", "medium"),
    (("注塑", "塑料制品", "塑胶制品", "塑料配件", "塑料零件", "塑胶配件", "塑胶零件",
      "塑料件", "塑胶件", "注塑加工", "注塑成型", "塑胶加工",
      "亚克力制品", "亚克力加工", "有机玻璃制品", "有机玻璃"), "2929", "high"),
    (("橡胶", "硅胶", "油封", "密封圈", "O型圈", "密封条", "胶带"), "2913", "medium"),

    # --- 通用零部件
    (("紧固件", "螺丝", "螺栓", "螺母", "螺柱", "螺钉", "铆钉", "垫圈", "标准件"), "3482", "high"),
    (("弹簧",), "3483", "high"),
    (("轴承",), "3451", "high"),
    (("齿轮", "齿条", "减速机", "变速箱", "链轮"), "3453", "high"),
    (("密封件", "机械密封", "密封环"), "3481", "medium"),

    # --- 钣金 / 结构件
    (("钣金", "机箱", "机柜", "金属结构", "钢结构", "货架", "护栏", "栏杆",
      "风管", "通风管", "排烟管"), "3311", "high"),
    (("门窗", "门业", "防盗门", "卷闸门", "铝合金窗", "断桥铝"), "3312", "medium"),
    (("冲压", "冲件", "五金冲压"), "3311", "medium"),

    # --- 机加工
    (("机加工", "机械加工", "数控", "CNC", "车床", "铣床", "加工中心", "车铣",
      "精密机械", "机械零部件"), "3484", "high"),

    # --- 汽车
    (("汽车配件", "汽车零部件", "汽配", "汽车零件", "车用"), "3660", "high"),

    # --- 自动化 / 环保
    (("工业机器人", "机器人", "机械手"), "3491", "high"),
    (("自动化设备", "自动化", "非标设备", "流水线"), "4011", "medium"),
    (("环保设备", "水处理", "净化设备", "除尘", "废气"), "3591", "medium"),

    # --- 玻璃 / 陶瓷
    (("钢化玻璃", "玻璃制品", "玻璃加工", "中空玻璃", "夹胶玻璃"), "3059", "medium"),
    (("特种陶瓷", "工业陶瓷", "陶瓷制品"), "3072", "medium"),

    # --- 金属材料：区分"加工"与"卖材料"
    (("铝型材", "铝材", "铝业", "铝合金型材", "铝板", "铝箔", "铝带", "铝卷"), "3252", "medium"),
    (("铜材", "铜业", "铜管", "铜带", "铜排"), "3251", "medium"),
    (("钢材", "钢铁", "不锈钢材料", "不锈钢板", "不锈钢管", "不锈钢卷", "特钢",
      "钢板", "钢管", "带钢", "卷板", "钢带"), "3130", "medium"),

    # --- 设备制造（放在后面，避免吃掉更具体的规则）
    (("风机", "风扇"), "3462", "medium"),
    (("制冷", "空调", "冷库"), "3464", "medium"),
    (("电动工具", "风动工具", "气动工具"), "3465", "medium"),
    (("包装机械", "包装设备", "灌装机"), "3467", "medium"),
    (("阀门", "旋塞"), "3443", "high"),
    (("液压", "油缸", "气缸", "气动元件"), "3444", "medium"),
    (("泵",), "3441", "medium"),
    (("刀具", "刃具", "刀模", "铣刀", "钻头"), "3321", "high"),

    # --- 兜底：五金制品
    (("五金制品", "金属制品", "五金配件", "五金加工", "五金"), "3399", "medium"),
    (("不锈钢制品", "不锈钢加工", "金属加工", "铝合金加工", "铝加工"), "3399", "medium"),
]

# 非制造信号：只在**没有任何制造类信号**时才生效
WHOLESALE_RULES = [
    (("建材批发", "钢材批发", "金属材料批发", "板材批发"), "5165", "medium"),
    (("五金批发", "工具批发", "水暖批发", "五金工具"), "5174", "medium"),
    (("贸易", "商贸", "批发", "经销", "供应链", "物资"), "5179", "low"),
]

# 现有品类兜底（confidence=low）
# 「原材料」按材料种类分流：金属 → 5164，化工/塑料 → 5169，其余 → 5165。
# 不能一律 5165（建材），实测 1953 条原材料里绝大多数是金属与塑料粒子。
CATEGORY_FALLBACK = {
    "精密机械加工": "3484",
    "钣金冲压": "3311",
    "注塑成型": "2929",
    "压铸": "3392",
    "表面处理": "3360",
    "电子元器件": "3989",
    "标准件": "3482",
    "原材料": "5165",
}
MATERIAL_FALLBACK = [
    (("不锈钢", "铝合金", "铝材", "铜材", "钢材", "铜", "铝", "特钢", "钛"), "5164"),
    (("工程塑料", "塑料粒子", "亚克力", "塑料板材", "塑料", "塑胶"), "5169"),
]

# keywords 补位规则（公司名没命中时才用，置信度最高 medium）
# 注意：刻意不含「不锈钢/铝合金/铜材」这类材料词——它们是原「原材料」品类的关键词，
# 拿去猜行业会把卖材料的商户误判成压延厂（实测「佛山市铝翼新型建材」→ 钢压延加工）。
KEYWORD_RULES = [
    (("轴承",), "3451"),
    (("紧固件",), "3482"),
    (("弹簧",), "3483"),
    (("阳极氧化", "电镀", "喷涂"), "3360"),
    (("激光切割", "钣金加工", "冲压"), "3311"),
    (("数控加工", "CNC加工", "精密机械"), "3484"),
    (("注塑加工", "注塑成型", "塑料制品", "塑胶制品"), "2929"),
]

# ---------------------------------------------------------------- 服务业规则
# 2026-09-14 加。此前「餐饮/美容/酒店/理发」全在 NOISE_PATTERNS 里被当噪音丢掉 ——
# 那是纯制造时代的设计（原注释：真实存在但不属于制造业，硬塞国标制造业代码就是造假）。
# 现在门类已扩到 7 个（C/F/H/I/M/O/R），餐饮、美容、软件开发都是**在册行业**，
# 必须归类而不是丢弃。不修这条，抓再多餐饮数据也只会落进 _unclassified。
#
# 位置很关键：**排在制造业 NAME_RULES 之后**。
# 「XX模具制造」这类制造业信号更强，先让制造业规则吃掉，避免服务业抢命中。
#
# 误伤防护见 SERVICE_SUPPLIER_HINT —— 卖设备给酒店的不是酒店。
SERVICE_RULES = [
    # --- H 住宿和餐饮业
    (("民宿", "客栈", "青年旅舍", "农家院"), "6130", "high"),
    (("火锅", "川菜", "粤菜", "湘菜", "东北菜", "私房菜", "家常菜", "中餐馆", "中餐",
      "酒楼", "菜馆", "饭馆", "餐馆", "餐厅", "海鲜楼", "海鲜酒家", "烧烤店",
      "串串", "小龙虾", "面馆", "粥铺", "食堂", "农家菜"), "6210", "high"),
    (("快餐", "炸鸡", "汉堡", "便当", "简餐", "小吃店", "麻辣烫", "米线",
      "盖饭", "黄焖鸡", "螺蛳粉"), "6220", "high"),
    (("咖啡",), "6232", "high"),
    (("奶茶", "茶饮", "茶室", "茶馆", "果汁", "刨冰"), "6231", "high"),
    (("酒吧", "清吧", "餐吧", "精酿"), "6233", "medium"),
    (("烘焙", "面包", "蛋糕", "西点", "甜品", "糕点"), "6291", "high"),
    (("外卖", "餐饮配送"), "6242", "medium"),
    (("经济型酒店", "连锁酒店", "快捷酒店", "商务酒店"), "6121", "medium"),
    (("酒店", "宾馆", "旅馆", "度假村", "度假酒店", "住宿"), "6110", "medium"),
    # 2026-09-14 补：原先「餐饮/美食/小吃/饭店」落在 SERVICE_HINTS 里被判「证据不足」
    # 直接置空 —— 那是把本地生活主体当成了无法归类的噪音。餐饮是本库在册门类（H），
    # 必须归类。放 medium：光一个「餐饮」定位不到正餐/快餐，但门类不会错。
    (("餐饮", "美食", "饭馆", "饭店"), "6210", "medium"),
    (("小吃", "风味", "美食城", "美食广场"), "6291", "medium"),

    # --- I 信息传输、软件和信息技术服务业
    (("软件开发", "软件科技", "软件技术", "软件服务", "软件"), "6513", "high"),
    (("系统集成", "信息系统集成"), "6531", "high"),
    (("网络安全", "信息安全"), "6440", "high"),
    (("物联网",), "6532", "high"),
    (("大数据", "数据服务", "数据中心"), "6450", "medium"),
    (("云计算", "云服务", "云科技"), "6550", "medium"),
    (("网络科技", "网络技术", "互联网", "信息技术"), "6490", "medium"),

    # --- M 科学研究和技术服务业
    (("第三方检测", "检测技术", "检测服务", "检验检测"), "7452", "high"),
    (("计量", "校准"), "7453", "high"),
    (("认证服务", "认证认可"), "7455", "high"),
    (("环境检测", "环境监测"), "7461", "high"),
    (("测绘", "地理信息"), "7449", "high"),
    (("工业设计",), "7491", "high"),
    (("工程监理",), "7482", "high"),
    (("工程设计", "勘察设计", "设计院", "建筑设计"), "7484", "medium"),
    # 2026-09-14 从「未登记门类标记」挪出来：研究院/设计院是 M 科研技术服务业（已登记），
    # 不是设计面之外的东西。此前被当成不可归类丢弃，属于误伤。
    (("研究院", "研究所"), "7320", "medium"),

    # --- O 居民服务、修理和其他服务业
    (("家政", "保姆", "月嫂"), "8010", "high"),
    (("干洗", "洗衣", "洗染"), "8030", "high"),
    (("美容美发", "美发", "理发", "美容", "造型"), "8040", "high"),
    (("洗浴", "澡堂", "温泉", "水疗", "汗蒸"), "8051", "high"),
    (("足浴", "足疗", "修脚"), "8052", "high"),
    (("按摩", "推拿", "养生馆", "保健按摩", "SPA"), "8053", "medium"),
    (("婚纱摄影", "摄影", "写真"), "8060", "high"),
    (("婚庆", "婚介", "婚礼"), "8070", "high"),
    (("汽车维修", "汽车修理", "汽修", "修车", "汽车服务"), "8111", "high"),
    (("电脑维修", "计算机维修", "笔记本维修"), "8121", "high"),
    (("手机维修", "通讯设备维修"), "8122", "medium"),
    (("家电维修", "空调维修", "电器维修", "冰箱维修", "洗衣机维修"), "8132", "high"),
    (("宠物医院",), "8222", "high"),
    (("宠物美容",), "8223", "high"),
    (("宠物",), "8229", "medium"),
    (("保洁", "清洁服务", "家政服务"), "8219", "medium"),
    (("开锁", "疏通", "搬家"), "8290", "medium"),

    # --- R 文化、体育和娱乐业
    (("健身", "健身房"), "8930", "high"),
    (("游泳馆", "球馆", "体育馆", "羽毛球", "乒乓球", "网球馆"), "8929", "high"),
    (("KTV", "歌舞厅", "量贩式"), "9011", "high"),
    (("网吧", "网咖", "电竞馆"), "9013", "high"),
    (("游乐园", "游乐场", "水上乐园"), "9020", "high"),
    (("电影院", "影城", "影院", "电影放映"), "8760", "high"),
    (("密室", "剧本杀", "桌游", "棋牌", "台球"), "9019", "medium"),
    (("儿童乐园", "亲子乐园"), "9019", "medium"),
    (("娱乐",), "9019", "medium"),
    (("休闲", "度假区", "农家乐"), "9030", "medium"),

    # --- F 零售业（实体门店，与制造业互补）
    (("便利店",), "5213", "high"),
    (("超市", "购物广场", "百货"), "5212", "medium"),
    (("药店", "药房"), "5251", "high"),
    (("书店",), "5243", "medium"),
    # 2026-09-14 补：制造/零售同名行业（服装/鞋/家具/食品）的**门店**形态。
    # 工厂形态已被前面的 EXTENDED_INDUSTRY_RULES 吃掉，走到这里的都是店面。
    # 没有这几条，「XX服装店」会因为前置规则让位而落空。
    (("服装", "服饰", "女装", "男装", "童装"), "5232", "medium"),
    (("鞋", "鞋店", "鞋帽"), "5233", "medium"),
    (("家具", "家居", "家私"), "5283", "medium"),
    (("食品", "零食", "副食", "粮油", "生鲜", "水果"), "5229", "medium"),
]

# 服务业的「供应商」不是服务业本身：
#   「XX酒店设备有限公司」是制造商，「XX厨房设备」是供应商。
# 命中这些词时不套服务业规则 —— 宁可留空（走后面的 gb_token / 兜底），也不标错。
#
# 刻意**不含**「有限」「管理」：
#   - 「有限公司」中文公司名几乎人人都有，放进来会把整张服务业规则表废掉；
#   - 「XX餐饮管理有限公司」在中国通常就是餐厅经营主体本身，不是管理咨询。
SERVICE_SUPPLIER_HINT = (
    "设备", "用品", "机械", "制造", "加工", "批发", "供应", "贸易", "材料",
    "工程", "安装", "物资",
)

# 出现这些词说明是服务业，但连门类都定位不到 → 留空，不猜
# （红线：弱证据不产强结论；猜一个具体小类比留空危害大）
#
# 2026-09-14 收缩到只剩最泛的两个词：原先的「餐饮/饭店/美食/小吃/住宿/休闲/娱乐」
# 都已能在 H/R 门类中定位（见 SERVICE_RULES），把它们留在置空表里等于
# 把本地生活主体判成「无法归类」——那正是主人要移除的负向边界。
SERVICE_HINTS = (
    "服务店", "门店",
)

# POI 噪音：这些名字根本不是企业，给任何行业标签都是污染（实测「泰昌建设项目部」
# 「池田开发区」被关键词规则判成电子元件制造）。命中则 industry=null。
#
# 两个坑：
#   - 不能放「施工」——「体育**设施工**程」会误伤；
#   - 「花园/公馆」这类住宅小区名常出现在括号地址里（「XX厂(嘉德花园店)」），
#     所以噪音判定用**去掉括号内容**后的名字，规则匹配仍用全名。
#
# 2026-09-14：把「餐饮/美容/理发/酒店/宾馆/饭店/超市/便利店」**移出**噪音表，
# 交给上面的 SERVICE_RULES 正式归类 —— 它们是已登记门类的在册行业，不是噪音。
NON_ENTERPRISE_NOISE = (
    "项目部", "工地", "开发区", "产业园", "宿舍", "停车场", "公厕", "厕所",
    "村委会", "居委会", "派出所", "收费站", "服务区", "加油站", "变电站",
    "菜市场", "菜场", "学校", "医院", "银行", "公园", "公馆", "花园",
)
# 兼容旧名：外部脚本可能 import NOISE_PATTERNS
NOISE_PATTERNS = NON_ENTERPRISE_NOISE

# 尚未登记 schema 的门类（E 建筑 / G 运输 / J 金融 / K 房地产 / L 商务服务 /
# P 教育 / Q 卫生）。注意：这些是**真实企业，不是噪音**。它们落在设计面
# （已登记的 7 个门类 C/F/H/I/M/O/R）之外，硬贴已登记门类的代码就是造假，
# 所以 industry 留空。这不是「拒绝收录」——等哪天登记了对应门类 schema，
# 把词从这张表挪进正式规则即可拿到国标码。
#
# 2026-09-14：从 NON_ENTERPRISE_NOISE 里拆出来。原先混在一起，语义上等于把
# 广告公司、物流公司跟「公共厕所」「工地项目部」归为同类，且改动时极易误删。
# 「设计院 / 研究院」已挪出本表 —— 它们是 M 科研技术服务业（已登记），
# 现在由 SERVICE_RULES 正式归类。
UNREGISTERED_GATE_MARKERS = (
    "广告", "传媒", "装饰", "装潢", "工程局", "隧道局", "中铁", "中建",
    "安装工程", "物流", "货运", "快递", "仓储", "物业", "咨询", "律师",
    "会计师事务所",
)

# ---------------------------------------------------------------- 扩展行业 → 真实国标码
# 2026-09-14 重写。这里原先叫 OUT_OF_SCOPE，命中就 `return None` 把记录丢进
# _unclassified。但表里这些（粮食 C13、食品 C14、纺织 C17、服装 C18、皮革制鞋 C19、
# 木材 C20、家具 C21、造纸 C22、印刷 C23、化妆品 C26、医药 C27、建材 C30…）
# **全是 C 制造业里真实存在的小类**，属于已登记门类。因为「设计面窄」而丢弃，
# 等于让客户 Agent 搜不到这些企业 —— 那是设计缺陷，不是数据缺陷。
# 主人的口径：**缺数据是抓取的问题，设计面不能减少。**
#
# 所以改成**映射到真实国标码**：既保住了原先「前置拦截」的防误判作用，又不再丢弃。
# 为什么必须前置（放在 NAME_RULES 之前）：实测关键词「加工中心」抓回来的
#   杨舍镇东莱村粮食烘干加工中心 / 景瑞农产品加工物流中心 /
#   昆山市定点屠宰加工中心 / 从化区供销社粮食加工中心
# 名字里都带「加工中心」，不前置就会被 NAME_RULES 判成 3484 机械零部件加工。
#
# 两道让位保护（宁可交给后面的规则，也别把服务/零售主体判成工厂）：
#   STORE_FORM              —— 门店形态（XX服装店 / XX食品店）让位给零售、服务规则
#   EXTENDED_SUPPLIER_GUARD —— 卖给该行业的设备商（XX食品机械）让位给制造业规则
EXTENDED_INDUSTRY_RULES = [
    # --- C13 农副食品加工业
    (("屠宰", "肉类加工", "肉制品"), "1351", "high"),
    (("饲料",), "1329", "high"),
    (("水产", "海产品", "渔港"), "1369", "medium"),
    (("粮食", "农产品", "粮油", "农副产品", "谷物"), "1399", "high"),
    (("蔬菜", "果品", "食用菌", "净菜"), "1371", "medium"),
    # --- C14 食品制造业
    (("调味品", "酱料", "酱油", "食醋"), "1469", "high"),
    (("糖果", "巧克力", "蜜饯"), "1421", "high"),
    (("乳制品", "牛奶", "奶粉", "奶酪"), "1449", "high"),
    (("罐头",), "1459", "high"),
    (("食品",), "1499", "medium"),
    # --- C15 酒、饮料和精制茶制造业
    (("啤酒",), "1513", "high"),
    (("葡萄酒", "果酒"), "1515", "high"),
    (("白酒", "酿酒", "酒业", "酒厂", "酒类"), "1512", "high"),
    (("饮料",), "1529", "medium"),
    (("茶叶", "茶业", "茶厂", "茶行"), "1530", "high"),
    # --- C16 烟草制品业
    (("烟草", "卷烟", "香烟"), "1620", "high"),
    # --- C17 纺织业 / C18 纺织服装 / C19 皮革毛皮制鞋
    (("印染", "染色"), "1713", "high"),
    (("纺织", "纺纱", "织造", "针织", "布业"), "1711", "medium"),
    (("服饰",), "1830", "high"),
    (("服装", "制衣", "西服", "羽绒服", "童装"), "1819", "high"),
    (("制鞋", "鞋业", "鞋材", "鞋底"), "1959", "high"),
    (("毛皮", "皮草"), "1931", "high"),
    # 刻意不含「箱包」——「木箱包装」「纸箱包装」会被切成箱包判成皮革制品（实测误伤）
    (("皮革", "皮具", "皮草制品"), "1929", "medium"),
    # --- C20 木材 / C21 家具
    (("人造板", "胶合板", "密度板", "刨花板"), "2029", "high"),
    (("木材", "木业", "木制品", "木器"), "2019", "high"),
    (("家具",), "2190", "medium"),
    # --- C22 造纸 / C23 印刷
    (("造纸", "纸浆", "纸厂"), "2221", "high"),
    (("纸制品", "纸箱", "纸板", "纸品", "纸盒"), "2239", "high"),
    (("印刷", "印务", "彩印", "包装装潢"), "2319", "high"),
    # --- C26 日用化学（化妆品）/ C27 医药
    (("化妆品", "护肤品", "日化"), "2682", "medium"),
    (("原料药", "制药", "生物制药", "中药饮片"), "2710", "high"),
    (("药品", "药业", "药剂"), "2720", "high"),
    # --- C30 非金属矿物制品
    # 「水泥制品/混凝土」必须先于「水泥」：否则「XX水泥制品有限公司」会被判成
    # 3011 水泥制造（熟料烧制），而它实际是 3021 制品加工。
    (("混凝土", "水泥制品", "预制构件", "商砼"), "3021", "high"),
    (("水泥",), "3011", "high"),
    (("石材", "石业", "石料", "大理石", "花岗岩"), "3032", "high"),
    (("砖瓦", "砌块", "陶粒"), "3031", "high"),
]
# 门店形态：命中 EXTENDED_INDUSTRY_RULES 时让位给后面的零售/服务规则。
# 刻意**不含**「中心」——「粮食烘干加工中心」正要靠前置拦截才不会被判成 3484。
STORE_FORM = (
    "店", "馆", "吧", "坊", "厅", "苑", "阁", "轩", "超市", "便利店",
    "专卖", "门市", "连锁", "分店", "服务中心", "服务部", "会所",
    "经营部", "商行", "经销",
)
# 卖给这些行业的设备/模具商（「XX食品机械」「XX印刷设备」）不是该行业本身
EXTENDED_SUPPLIER_GUARD = (
    "机械", "设备", "器械", "仪器", "模具", "自动化", "刀具", "配件",
)

# ---------------------------------------------------------------- 后缀降权
# 2026-09-08 实测（杭州 3525 模具）发现的问题：
# NAME_RULES 里 `(("模具",), "3525", "high")` 一个词就给 high，结果 3525 全库 621 家
# 置信度**全是 high**。但其中「留儿模具配件」「模具五金机电」是卖配件、卖五金的门店，
# 不是能接活的模具制造厂。
#
# 公司名确实是强证据，但**后缀已经说明了经营形态**：叫「经营部」「五金机电」的
# 不做制造。这违反红线 3（弱证据不产强结论）——名字给了行业，后缀就该把它拉下来。
#
# 两档处理：
#   RETAIL_SUFFIX 门店型 → 转批发业代码，is_manufacturer 自动 false
#   PARTS_SUFFIX  配件型 → 保留代码，置信度 high 降 medium（可能是小加工，不判死）
#
# 误伤防护：名字里带制造信号（制造/加工/厂/有限公司/股份）时**不降权**——
# 「杭州XX五金机电制造有限公司」是真制造商，不能因为含「五金机电」就打成门店。
RETAIL_SUFFIX = (
    "经营部", "门市部", "商行", "经销部", "经销处", "供应站", "代销",
    "五金机电", "机电经营", "物资供应", "劳保用品", "模具配件", "配件商店",
)
PARTS_SUFFIX = ("配件", "零配件", "易损件", "耗材", "模配")
# 国标小类名里本身就含「配件」的，不能因为名字带「配件」就降级
# （3660 = 汽车零部件**及配件**制造，是真制造行业）
PARTS_EXEMPT = {"3660"}
# 制造信号：命中说明是生产主体，后缀降权要让路
MAKE_SIGNAL = ("制造", "加工", "厂", "有限公司", "股份", "实业")

_WHOLESALE_CODE = "5179"   # 其他批发业
_HARDWARE_WS = "5174"      # 五金批发

_CONF_ORDER = {"high": 2, "medium": 1, "low": 0}


def _match(rules, text):
    for pats, code, conf in rules:
        for p in pats:
            if p in text:
                return code, conf, p
    return None


def classify(rec, expected_code=None):
    """返回 (code, confidence, source, evidence) 或 (None, None, None, None)。

    expected_code：抓取时用的目标国标代码（这次是按 2913 橡胶去搜的）。
    只在公司名/关键词都拿不到信号时兜底用，置信度 low，source="search_keyword"
    —— 搜索词是模糊匹配的产物，只能补位不能当证据（红线 3）。
    """
    name = rec.get("company") or ""
    kws = " ".join(rec.get("keywords") or [])

    # 0) POI 噪音名：不是企业，不给行业标签（括号内容通常是地址，先剥掉再判）
    bare = re.sub(r"[（(][^）)]*[）)]", "", name)
    for noise in NON_ENTERPRISE_NOISE:
        if noise in bare:
            return None, None, None, None

    # 0.4) 扩展行业 → 真实国标码（2026-09-14：原 OUT_OF_SCOPE 命中即丢弃，已改为归类）
    #      排在「未登记门类」之前：像「农产品加工**物流**中心」「包装**装潢**印刷」
    #      这种名字，主业是已登记的 C 门类，不能被后半截的 E/G 门类词带走。
    if not any(w in name for w in STORE_FORM) and \
            not any(w in name for w in EXTENDED_SUPPLIER_GUARD):
        hit = _match(EXTENDED_INDUSTRY_RULES, name)
        if hit:
            code, conf, ev = hit
            return code, conf, "name", ev

    # 0.6) 尚未登记 schema 的门类（E/G/J/K/L/P/Q）：真实企业，只是设计面尚未覆盖。
    #      不硬贴已登记门类的代码，industry 留空。见 UNREGISTERED_GATE_MARKERS 注释。
    for term in UNREGISTERED_GATE_MARKERS:
        if term in bare:
            return None, None, None, None

    # 1) 公司名（最强证据）
    # 例外的例外：带「制品/配件/零件」的工程塑料厂是制品厂(2929)，不是原料商(2651)
    if "工程塑料" in name and any(w in name for w in ("制品", "配件", "零件", "加工")):
        return "2929", "high", "name", "工程塑料+制品"
    hit = _match(NAME_RULES, name)
    if hit:
        code, conf, ev = hit
        # 1.5) 后缀降权（见 RETAIL_SUFFIX 上方的说明）
        if not any(w in name for w in MAKE_SIGNAL):
            store = next((s for s in RETAIL_SUFFIX if s in name), None)
            if store:
                ws = _HARDWARE_WS if "五金" in name else _WHOLESALE_CODE
                return ws, "medium", "name", f"{ev}|门店型:{store}"
        if conf == "high" and code not in PARTS_EXEMPT:
            part = next((s for s in PARTS_SUFFIX if s in name), None)
            if part:
                return code, "medium", "name", f"{ev}|配件型:{part}"
        return code, conf, "name", ev

    # 1.2) 服务业规则（H/I/M/O/R + F 零售）
    #      排在制造业之后：制造业信号更强，先让它吃掉。
    #      「酒店设备」「餐饮管理」这类**卖给**服务业的不是服务业本身，
    #      命中 SERVICE_SUPPLIER_HINT 时不套用 —— 宁可漏也别标错。
    if not any(w in name for w in SERVICE_SUPPLIER_HINT):
        hit = _match(SERVICE_RULES, name)
        if hit:
            code, conf, ev = hit
            return code, conf, "name", ev
    # 看得出是服务业，但没有足够证据定位小类 → 留空，不猜
    for hint in SERVICE_HINTS:
        if hint in bare:
            return None, None, None, None

    # 2a) 国标特征词自动索引（覆盖 1382 个小类的长尾）
    #     只用在公司名上——公司名是强证据，keywords 是弱证据不能拿来做细粒度判定。
    #     命中唯一小类 → class；多个小类同中类 → group；再歧义 → division。
    tok, tcode, tlevel = match_longest(bare)
    if tcode:
        conf = {"class": "high", "group": "medium", "division": "low"}[tlevel]
        return tcode, conf, "gb_token", tok

    # 2) keywords 补位（弱证据，置信度降一级）
    for pats, code in KEYWORD_RULES:
        for p in pats:
            if p in kws:
                conf = "medium" if code in ("3451", "3482", "3483", "3360") else "low"
                return code, conf, "keyword", p

    # 3) 明确是贸易/批发 → 非制造
    hit = _match(WHOLESALE_RULES, name)
    if hit:
        return hit[0], hit[1], "name", hit[2]

    # 4) 抓取目标行业兜底（弱证据，仅当名字毫无信号时）
    if expected_code and expected_code in CODES:
        return expected_code, "low", "search_keyword", rec.get("category") or ""

    # 5) 原品类兜底
    cat = rec.get("category") or ""
    if cat == "原材料":
        for pats, code in MATERIAL_FALLBACK:
            if any(p in kws or p in name for p in pats):
                return code, "low", "category_fallback", cat
    code = CATEGORY_FALLBACK.get(cat)
    if code:
        return code, "low", "category_fallback", cat

    return None, None, None, None


def build_industry(code, confidence, source, evidence):
    """组装 industry 对象。code 可能是 4 位小类 / 3 位中类 / 2 位大类，
    由 level 字段标明判定粒度——**判不到小类就老实停在中类，不硬猜**。
    """
    return {
        "code": code,
        "name": name_of(code),
        "level": level_of(code),
        "path": path_of(code),
        "confidence": confidence,
        "source": source,
        "evidence": evidence,
    }


def backfill(force=False, verbose=True):
    """只给「还没有 industry 字段」的记录补标签，已有的一律不动。

    抓取脚本跑完调这个：新抓的 POI 出生即带行业标签，历史数据不受影响。
    force=True 时全部重算（改了规则后全量重生成用）。

    返回补写条数。
    """
    written = 0
    changed_recs = []
    for r in gb_store.load_all():
        if not force and r.get("industry") is not None:
            continue
        code, conf, src, ev = classify(r)
        if code is None:
            r["industry"] = None
            r["is_manufacturer"] = True
        else:
            r["industry"] = build_industry(code, conf, src, ev)
            # 按门类判定：C 制造业才是制造商，F51/F52 批发零售不是。
            r["is_manufacturer"] = is_manufacturer(code)
        changed_recs.append(r)
        written += 1
    if changed_recs:
        # upsert 会按新的国标码重新落位：补标签可能改变记录所属小类，
        # 跨桶迁移由它自动清理，不会在旧小类留下幽灵数据。
        res = gb_store.upsert(changed_recs)
        if verbose:
            print("   补写 %d 条（新增 %d / 更新 %d / 跨桶迁移 %d / 顺带去重 %d）"
                  % (written, res["added"], res["updated"], res["moved"],
                     res.get("deduped", 0)))
    return written


def apply_suffix_downgrade(verbose=True):
    """只应用「后缀降权」规则，不动其他分类。

    为什么不直接 --apply 全量重算：实测全量重算会改写 1345 条，其中大部分
    （3982→3989 / 3130→5164 / 2651→2929…）跟后缀规则无关，是重算时走了不同的
    推断路径（入库时按抓取目标代码打的标签 vs 现在按公司名推断）。
    那种规模的无差别改写风险太高，所以单独提供这个精准入口。

    返回改动条数。
    """
    changed = 0
    changed_recs = []
    for r in gb_store.load_all():
        name = r.get("company") or ""
        ind = r.get("industry") or {}
        if not ind.get("code"):
            continue
        ev0 = str(ind.get("evidence") or "")
        if "门店型" in ev0 or "配件型" in ev0:
            continue  # 已经降过权，避免重复叠加 evidence
        store = next((s for s in RETAIL_SUFFIX if s in name), None)
        part = next((s for s in PARTS_SUFFIX if s in name), None)
        if store and not any(w in name for w in MAKE_SIGNAL):
            new_code = _HARDWARE_WS if "五金" in name else _WHOLESALE_CODE
            ev = f"{ev0}|门店型:{store}"
        elif (part and ind.get("confidence") == "high"
              and ind["code"] not in PARTS_EXEMPT):
            new_code = ind["code"]
            ev = f"{ev0}|配件型:{part}"
        else:
            continue
        r["industry"] = build_industry(new_code, "medium", "name", ev)
        r["is_manufacturer"] = is_manufacturer(new_code)
        changed_recs.append(r)
        changed += 1
    if changed_recs:
        res = gb_store.upsert(changed_recs)
        if verbose:
            print("   降权 %d 条（跨桶迁移 %d）" % (changed, res["moved"]))
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="全量重算并写回 data/gb/（经 gb_store.upsert 重新落位）")
    ap.add_argument("--downgrade-suffix", action="store_true",
                    help="只应用「门店型/配件型后缀降权」，不动其他分类（推荐）")
    ap.add_argument("--backfill", action="store_true",
                    help="只补缺 industry 的记录（抓取后调用，不覆盖已有标签）")
    ap.add_argument("--stats", action="store_true", help="只打印分布")
    ap.add_argument("--audit", type=int, default=0, help="抽样 N 条人工核对")
    args = ap.parse_args()

    if args.downgrade_suffix:
        print("只应用后缀降权（门店型→转批发，配件型→high 降 medium）:")
        n = apply_suffix_downgrade()
        print("共降权 %d 条" % n)
        print("提示：跑 scripts/gen_industry_index.py 重建行业索引，"
              "再跑 en_sync_industry.py 同步英文镜像（data/en/gb/）")
        return

    if args.backfill:
        print("补写缺失的行业标签（已有标签不动）:")
        n = backfill()
        print("共补写 %d 条" % n)
        print("提示：跑 scripts/gen_industry_index.py 重建行业索引，再跑 en_sync_industry.py 同步英文镜像（data/en/gb/）")
        return

    all_recs = [(gb_store.bucket_of(r), r) for r in gb_store.load_all()]

    code_cnt = Counter()
    conf_cnt = Counter()
    src_cnt = Counter()
    unclassified = 0

    for f, r in all_recs:
        code, conf, src, ev = classify(r)
        if code is None:
            unclassified += 1
            if args.apply:
                r["industry"] = None
                r["is_manufacturer"] = True
            continue
        code_cnt[code] += 1
        conf_cnt[conf] += 1
        src_cnt[src] += 1
        if args.apply:
            r["industry"] = build_industry(code, conf, src, ev)
            r["is_manufacturer"] = is_manufacturer(code)

    total = len(all_recs)
    print("总记录 %d，归到国标 %d 条（%.1f%%），未归类 %d 条"
          % (total, total - unclassified, 100 * (total - unclassified) / total, unclassified))
    print("\n置信度分布:")
    for k in ("high", "medium", "low"):
        print("   %-7s %6d  %.1f%%" % (k, conf_cnt[k], 100 * conf_cnt[k] / total))
    print("\n证据来源分布:")
    for k, v in src_cnt.most_common():
        print("   %-18s %6d  %.1f%%" % (k, v, 100 * v / total))
    print("\n国标小类 TOP40:")
    for code, n in code_cnt.most_common(40):
        print("   %-6s %-24s %6d  %.1f%%" % (code, CODES[code]["name"][:24], n, 100 * n / total))
    print("\n涉及国标小类数: %d" % len(code_cnt))

    if args.audit:
        print("\n=== 抽样核对（前 %d 条 low 置信度）===" % args.audit)
        shown = 0
        for f, r in all_recs:
            ind = r.get("industry")
            if ind and ind.get("confidence") == "low" and shown < args.audit:
                print("   %-46s -> %s %s" % (r.get("company", "")[:46], ind["code"], ind["name"]))
                shown += 1

    if args.apply:
        res = gb_store.upsert([r for _, r in all_recs])
        gb_store.rebuild_index()
        print("\n已写回 data/gb/：新增 %d / 更新 %d / 跨桶迁移 %d"
              % (res["added"], res["updated"], res["moved"]))
    elif not args.stats:
        print("\n[预览模式] 加 --apply 才会写盘。")


if __name__ == "__main__":
    main()
