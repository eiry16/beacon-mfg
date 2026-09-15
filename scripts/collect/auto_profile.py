"""
auto_profile.py — 平台自动整理引擎（冷启动弹药）

从薄名录记录（company / category / keywords / address / phone）自动推断
供应商能力卡。用于生成「平台自动整理 · 未认证」卡片，作为供应商付费认主的钩子。

设计红线（不可违反）：
  1. 只推断「名义能力」——做什么工艺、用什么材料。
     绝不推断「数值能力」——公差、MOQ、交期、产能、价格一律留空。
     猜数值 = 编造数据，一旦被客户发现，整个库的信誉归零。
  2. 一切推断结果必须通过 provenance 字段显式标注，
     并在 SKILL.md 顶部渲染警告横幅。
  3. POI 噪声（农场、果园、窗帘店、楼栋号）必须过滤，
     宁可少生成，不可生成垃圾卡片。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gb_store  # noqa: E402
import cap_codes  # noqa: E402  能力码表域（restaurant / retail-service / tech-service / entertainment）
import gate_schema  # noqa: E402 门类 schema 装配 / 校验 / caps_index 派生
PROCESS_CODES = json.loads(
    (REPO_ROOT / "skills" / "schema" / "process-codes.json").read_text(encoding="utf-8")
)
VALID_CODES = set(PROCESS_CODES["codes"])


def _code_name(code: str) -> str:
    """process-codes.json 里 codes 的值可能是字符串，也可能是 {name, category}。"""
    v = PROCESS_CODES["codes"].get(code, code)
    return v.get("name") if isinstance(v, dict) else str(v)

CATEGORY_PROFILE = {
    "精密机械加工": "precision-machining",
    "钣金冲压": "sheet-metal",
    "注塑成型": "injection-molding",
    "压铸": "die-casting",
    "电子元器件": "electronics",
    "表面处理": "surface-treatment",
    "标准件": "standard-parts",
    "原材料": "raw-materials",
    "成套设备制造": "equipment_assembly",
    "信息技术服务": "it-service",
    "餐饮": "restaurant",
    "居民服务": "resident-service",
    "娱乐": "entertainment",
    "科研与技术服务": "tech-research",
    "零售": "retail-shop",
    "其他": "custom",
    # 非 C 门类的**完整门类名**：category_of() 对非 C 门类返回的是门类名
    # （「住宿和餐饮业」等），不是上面那些简称。缺这几条时 get(cat, "custom")
    # 一律兜底成 custom，客户 Agent 拿到的卡片模板就是错的。
    "住宿和餐饮业": "restaurant",
    "信息传输、软件和信息技术服务业": "it-service",
    "科学研究和技术服务业": "tech-research",
    "居民服务、修理和其他服务业": "resident-service",
    "文化、体育和娱乐业": "entertainment",
    "批发和零售业": "retail-shop",
}

# ------------------------------------------------------------------ 门类守卫
# 自动工艺推断**只对 C 制造业成立**。
#
# 2026-09-14 实测（名录扩到 7 大门类后）：
#   嘉兴市老百姓大药房（F 零售 5251）→ 品类「原材料」→ 兜底 metal_material
#   嘉兴经开区美宜佳便利店（F 零售 5213）→ 同上
# 药房、便利店、火锅店、软件公司根本没有「CNC / 注塑 / 钣金」可言，
# infer 却会拿制造业词典从字面硬凑一个出来 —— 这是**编造**，触碰项目红线
# （数据宁可留空也不编造）。故按门类守卫：非 C 门类一律不推断工艺。
#
# 这不是「设计面收缩」：名录抓取、能力卡渲染、供应商自填三条链路都覆盖全门类，
# 只有「从公司名自动推断工艺」这一条能力以制造业为界。餐饮的"工艺"
# 无法从店名推断，硬推就是造假。
try:  # industry_taxonomy 在 scripts/ 下，已加入 sys.path
    import industry_taxonomy as _it

    def gate_of_code(code: str) -> str:
        try:
            return _it.gate_of(str(code)) or ""
        except Exception:  # noqa: BLE001
            return ""

    # 门类名（中文）→ 门类字母：无国标码时按名录粗分类回判门类
    _GATE_OF_NAME = {v: k for k, v in _it.GATES.items()}

    def gate_of_category_name(cat_name: str) -> str:
        return _GATE_OF_NAME.get((cat_name or "").strip(), "")
except Exception:  # noqa: BLE001
    def gate_of_code(code: str) -> str:  # type: ignore[misc]
        c = str(code or "")
        return c[:1] if c[:1].isalpha() else ""

    def gate_of_category_name(cat_name: str) -> str:  # type: ignore[misc]
        return ""


def is_manufacturing(rec: dict) -> bool:
    """只有 C 制造业才做自动工艺推断。

    无行业码的旧数据按 is_manufacturer 标志兜底（老名录全是制造业）。
    """
    code = (rec.get("industry") or {}).get("code") or ""
    if not code:
        return bool(rec.get("is_manufacturer", True))
    return gate_of_code(code) == "C"

# ------------------------------------------------------------------ 名称清洗

# POI 里常把楼栋、宿舍、停车场当成独立条目抓进来
_TAIL_JUNK = re.compile(
    r"(\d+\s*(号楼|号厂房|号车间|号房|幢|栋|座|号)|"
    r"[A-Za-z]\d+\s*(厂区|厂房|车间)|"
    r"(宿舍楼|食堂|停车场|公厕|配电房|门卫)|"
    r"第[一二三四五六七八九十\d]+(车间|期|幢))+\s*$"
)


def clean_name(raw: str) -> str:
    """清洗 POI 名称，去掉楼栋/附属设施后缀，返回公司本体名称。"""
    n = (raw or "").strip()
    for _ in range(3):  # 可能有多层后缀
        n2 = _TAIL_JUNK.sub("", n).strip()
        if n2 == n:
            break
        n = n2
    return n.strip(" ·-—")


# ------------------------------------------------------------------ 噪声过滤

# 明确不是「供应商主体」的 POI / 非经营主体（地图误抓）。
#
# 2026-09-14 重新梳理：下列词**只保留真正非经营的误抓对象**。
# 凡属于「已登记门类」的主体一律放行 —— 餐饮(H)/住宿(H)/零售(F)/居民服务(O)/
# 信息技术(I)/科研技术(M)/娱乐(R) 都是合法供应商，它们的店名/业态词
# （餐饮/饭店/酒店/美容/理发/超市/便利店/门窗/窗帘/服装/批发…）**不是噪音**。
# 旧版把它们当噪音，等于把整片新门类挡在自动整理门外，与「全门类自动补卡」目标冲突。
#
# 仍保留为噪音的：
#   - A 农林牧渔（农场/果园/养殖…，未登记门类，且非供应商主体）
#   - 公共设施 / 机构 / 物流场站 / 生活配套（非经营主体）
#   - 注塑机/挤出机/吹塑机/造粒机（极具体的塑料设备名，几乎只出现在设备商/维修商名中，
#     留在 C 门类的工艺误判防护里；几乎不会出现在服务业名中，对 F/O/I/M/R 无副作用）
# 注意：批发/经销/自动化科技等已从噪音移除 —— 「XX奶茶原料批发」是 F 门类的正常主体，
# 「XX自动化科技」是 I 门类的正常主体，旧版把它们误杀。
NOISE_PATTERNS = [
    # 农业 / 养殖 / 生活（A 门类未登记，且非经营主体）
    r"农场|果园|花卉|种植|苗木|水产|养殖|牧场|菜地|桃园|葡萄|垂钓|农家乐",
    # 公共设施 / 非经营主体（POI 误抓）
    r"公厕|垃圾|变电|配电|泵站|公墓|陵园|寺庙|教堂|景区|旅游|公园|广场",
    # 机构（非经营主体）
    r"幼儿园|小学|中学|大学|学院|学校|医院|诊所|银行|保险|律师|会计师|"
    r"税务|工商|村委会|居委会|派出所",
    # 纯物流 / 仓储（非经营主体）
    r"停车场|加油站|充电站|服务区",
    # 非经营主体：销售点 / 管理机构 / 生活配套
    r"营销中心|营销部|办事处|展示中心|体验店|旗舰店|总部|"
    r"职工公寓|公寓|宿舍|食堂|生活区",
    # 设备名词（保护 C 不被「注塑机」类设备商误判为加工厂；极具体，对服务业无副作用）
    r"注塑机|挤出机|吹塑机|造粒机",
]
_NOISE_RE = re.compile("|".join(NOISE_PATTERNS))

# 降权但不排除：贸易公司、营销点、培训机构
SOFT_NOISE = [
    r"培训中心|培训班|教育",
    r"物流|快递|运输|货运",
    r"贸易|商贸|进出口",
    r"营销中心|办事处|售后服务|维修点",
]
_SOFT_RE = re.compile("|".join(SOFT_NOISE))


def noise_kind(name: str) -> str | None:
    """返回 'hard'（排除）/ 'soft'（降权）/ None（正常）。"""
    if _NOISE_RE.search(name):
        return "hard"
    if _SOFT_RE.search(name):
        return "soft"
    return None


# ------------------------------------------------------------------ 能力推断

# 强特征词 → 明确的工艺代码（高置信）
STRONG_RULES: list[tuple[str, list[str], str]] = [
    # (正则, [工艺代码], 说明)
    (r"五轴|五面体", ["cnc_5axis"], "五轴"),
    (r"线切割|慢走丝|快走丝|中走丝", ["wire_cutting"], "线切割"),
    (r"电火花|火花机|放电", ["edm"], "电火花"),
    (r"磨床|磨削|研磨|平面磨|外圆磨", ["grinding"], "磨削"),
    (r"激光切割|激光下料|镭射", ["laser_cutting"], "激光切割"),
    (r"折弯|折床", ["bending"], "折弯"),
    (r"冲压|冲床|五金冲压", ["stamping"], "冲压"),
    (r"焊接|焊机|氩弧焊|点焊", ["welding"], "焊接"),
    (r"压铸", ["die_casting"], "压铸"),
    (r"注塑|射出成型|啤机", ["injection_molding"], "注塑"),
    (r"双色|双物料", ["two_shot"], "双色注塑"),
    (r"模具|模架|制模", ["mold_making"], "模具"),
    (r"电镀|镀锌|镀镍|镀铬|滚镀|挂镀", ["electroplating"], "电镀"),
    (r"喷涂|喷漆|喷油|涂装", ["spray_painting"], "喷涂"),
    (r"粉末|喷塑|粉体", ["powder_coating"], "粉末喷涂"),
    (r"阳极|氧化处理|硬质氧化", ["anodizing"], "阳极氧化"),
    (r"热处理|淬火|退火|渗碳|氮化", ["heat_treatment"], "热处理"),
    (r"发黑|发蓝|磷化", ["blackening"], "发黑"),
    (r"紧固件|螺丝|螺栓|螺母|螺钉|螺柱|铆钉", ["fastener"], "紧固件"),
    (r"轴承|轴瓦|轴套", ["bearing"], "轴承"),
    (r"弹簧|弹片|碟簧", ["spring"], "弹簧"),
    (r"齿轮|齿条|蜗轮|链轮", ["gear"], "齿轮"),
    (r"连接器|接插件|端子|排针|排母", ["connector"], "连接器"),
    (r"传感器|变送器|探头", ["sensor"], "传感器"),
    (r"继电器|接触器|开关电器", ["relay"], "继电器"),
    (r"线束|排线|端子线|汽车线", ["wire_harness"], "线束"),
    (r"贴片|SMT|SMD", ["smt", "pcb_assembly"], "SMT贴片"),
    (r"PCBA|电路板组装|电子组装", ["pcb_assembly"], "PCBA"),
    (r"PCB|线路板|印制板|覆铜板", ["pcb_fab"], "PCB制板"),
    (r"零切|剪板|分条|开平|激光切管", ["cutting_service"], "零切服务"),
]

# 中特征词 → 通用能力（中置信）
MEDIUM_RULES: list[tuple[str, list[str], str]] = [
    (r"精密机械|精密加工|精密零部件|机械加工|数控加工|CNC加工",
     ["cnc_milling", "cnc_turning"], "CNC加工"),
    (r"钣金|机箱|机柜|外壳|金属结构",
     ["laser_cutting", "bending", "stamping"], "钣金加工"),
    (r"车削|车床|数控车", ["cnc_turning"], "车削"),
    (r"铣削|铣床|加工中心|钻攻", ["cnc_milling"], "铣削"),
    (r"五金|金属制品|金属加工|钢结构", ["metal_stamping"], "五金加工"),
    (r"表面处理|金属处理", ["electroplating", "spray_painting"], "表面处理"),
    # 注意：不要用「科技」「制造」这种万能词——几乎所有公司名都带，
    # 会把「精密钢材科技」判成电子元件。实测坑过一次。
    (r"电子|电器|电气|电控|仪表|仪器", ["electronic_component"], "电子元件"),
    # 注：「塑料」不在这里——它是材料不是工艺，单独在下方按是否含加工动词处理
    (r"机械|机电|装备|机床", ["cnc_milling"], "机械制造"),
]

# 「塑料」是材料不是工艺。若名称含加工动词（塑料加工厂 / 塑料制品 / 塑料成型），
# 它做的是塑料件加工，应走品类兜底拿到加工工艺；只有纯材料型名称
# （「海亚泡沫塑料」）才是卖塑料原料。
# 否则实测会出现：「水果塑料加工厂」被判成 plastic_material（原材料），
# 客户搜 cnc_milling 永远找不到它 —— 抓到了也白抓。
PLASTIC_PAT = re.compile(r"塑料|塑胶|橡塑|树脂|泡沫", re.I)

# 品类兜底：名称里什么都没匹配到时，用品类关键词兜底（低置信）
CATEGORY_FALLBACK = {
    "精密机械加工": (["cnc_milling", "cnc_turning"], "按品类推断"),
    "钣金冲压": (["laser_cutting", "bending", "stamping"], "按品类推断"),
    "注塑成型": (["injection_molding"], "按品类推断"),
    "压铸": (["die_casting"], "按品类推断"),
    "电子元器件": (["electronic_component"], "按品类推断"),
    "表面处理": (["electroplating"], "按品类推断"),
    "标准件": (["fastener"], "按品类推断"),
    "原材料": (["metal_material"], "按品类推断"),
}

# 材料推断
MATERIAL_RULES: list[tuple[str, str]] = [
    (r"不锈钢|不锈铁", "不锈钢"),
    (r"铝材|铝业|铝合金|铝型材|铝板|铝箔", "铝合金"),
    (r"铜材|铜业|紫铜|黄铜|青铜|铜合金", "铜材"),
    (r"钛材|钛合金|钛业", "钛合金"),
    (r"锌合金|锌业", "锌合金"),
    (r"镁合金", "镁合金"),
    (r"钢材|钢铁|钢板|钢管|带钢|特钢|不锈钢管", "碳钢/不锈钢"),
    (r"塑料|塑胶|工程塑料|ABS|PP|PE|PC|尼龙|POM", "工程塑料"),
    (r"橡胶|硅胶|橡塑", "橡胶/硅胶"),
    (r"亚克力|有机玻璃|PMMA", "亚克力"),
]

# ---------------------------------------------------------------------------
# 抓取关键词 → 材料线索（2026-09-08 新增）
#
# 设计要点：**关键词只用于推断材料，绝不用于推断工艺。**
# 工艺类关键词（CNC加工 / 注塑 / 钣金）与 category 高度重合，拿它推断工艺等于
# 把分类结果当证据自我循环（实测会把「海亚泡沫塑料」判成连接器）。
# 但材料是另一个维度：搜「塑料加工」抓到的企业，说它做工程塑料是成立的，
# 且不与任何 category 冲突。材料覆盖率低是实测最大短板，这里补上。
# ---------------------------------------------------------------------------
KEYWORD_MATERIAL_RULES: list[tuple[str, str]] = [
    (r"塑料|塑胶|橡塑", "工程塑料"),
    (r"尼龙|聚甲醛|POM", "工程塑料"),
    (r"亚克力|有机玻璃", "亚克力"),
    (r"铝合金|铝材|铝板|铝型材", "铝合金"),
    (r"不锈钢", "不锈钢"),
    (r"铜材|紫铜|黄铜", "铜材"),
    (r"钛合金", "钛合金"),
    (r"锌合金", "锌合金"),
    (r"镁合金", "镁合金"),
]

# 高德 POI 的 type 三级分类（如「公司企业;工厂;工厂」）→ 质量信号。
# 抓取已改为全召回（不限 types），噪声判断下沉到这里。
AMAP_TYPE_POSITIVE = re.compile(r"工厂|制造|工业")
AMAP_TYPE_NEGATIVE = re.compile(r"商务住宅|住宅区|公寓|别墅|写字楼|写字楼|科教文化")

# 名称里出现这些词，说明主营是别的材料/行业，不能按抓取关键词给它贴材料标签
OTHER_MATERIAL_PAT = re.compile(
    r"木|竹|藤|布|篷|纸|皮|革|玻璃|陶瓷|石材|大理石|水泥|混凝土|羽绒|棉花")


def _kw_material_conflict(name: str, mat: str) -> bool:
    """
    判断「用抓取关键词推出材料」是否与公司名冲突。

    必须做这层校验：高德关键词搜索是模糊匹配，搜「塑料加工」会返回
    「上海顺雨篷布批发」「振鸿木门木楼梯加工厂」这类完全不相关的主体。
    直接拿搜索词反推企业能力，会把垃圾写进能力卡（实测已发生）。
    """
    if not name:
        return False
    # 名称自身已含材料线索 → 以名称为准，与关键词材料不同即冲突
    for pat, m in MATERIAL_RULES:
        if re.search(pat, name, re.I):
            return m != mat
    # 名称无材料线索，但含明显相斥的材料/行业词 → 关键词材料不可信
    return bool(OTHER_MATERIAL_PAT.search(name))


def infer_materials_from_keywords(keywords: list[str] | None,
                                  name: str = "") -> list[str]:
    """从抓取关键词里抽材料线索（需通过名称冲突校验）。"""
    out: list[str] = []
    for kw in keywords or []:
        for pat, m in KEYWORD_MATERIAL_RULES:
            if re.search(pat, str(kw), re.I) and m not in out:
                if _kw_material_conflict(name, m):
                    continue
                out.append(m)
    return out

# 规模线索（弱信号，仅用于打分，不写入数据）
SCALE_HINT = re.compile(r"股份|集团|控股|实业|科技股份|有限")


# 品类 ↔ 合理工艺集合。用于检测「名录分类 vs 名称推断」冲突。
CATEGORY_PROCESSES = {
    "精密机械加工": {"cnc_turning", "cnc_milling", "cnc_5axis", "wire_cutting",
                 "edm", "grinding"},
    "钣金冲压": {"laser_cutting", "bending", "stamping", "welding", "sheet_assembly"},
    "注塑成型": {"injection_molding", "mold_making", "two_shot"},
    "压铸": {"die_casting", "cnc_post_machine", "mold_making"},
    "电子元器件": {"pcb_assembly", "smt", "wire_harness", "pcb_fab",
               "connector", "sensor", "relay", "electronic_component"},
    "表面处理": {"anodizing", "electroplating", "spray_painting", "powder_coating",
              "heat_treatment", "blackening"},
    "标准件": {"fastener", "bearing", "spring", "gear", "metal_stamping"},
    "原材料": {"metal_material", "plastic_material", "cutting_service"},
}


def resolve_category(procs: list[str], declared: str) -> tuple[str, bool]:
    """
    名称推断的工艺与名录品类冲突时，**以名称为准重新归类**。

    为什么必须改：名录品类来自抓取时的关键词（搜「塑料模具」抓到的全归注塑成型），
    属弱证据；公司名才是硬证据。实测「嘉兴世博特五金机械有限公司」落在注塑成型
    品类里、工艺却推成 metal_stamping —— 客户 Agent 查「嘉兴注塑厂」时它会冒出来，
    是纯粹的错配。

    返回 (最终品类, 是否被改判)。
    """
    allowed = CATEGORY_PROCESSES.get(declared)
    if not allowed or not procs:
        return declared, False
    if set(procs) & allowed:
        return declared, False
    best, best_n = None, 0
    for cat, ps in CATEGORY_PROCESSES.items():
        n = len(set(procs) & ps)
        if n > best_n:
            best, best_n = cat, n
    return (best or declared), True


def infer(name: str, category: str = "", keywords: list[str] | None = None,
          extra_text: str = "") -> dict:
    """
    推断能力。两条证据链分开对待：

    **工艺 → 只信公司名（+ amap 的 alias/tag 这类企业自述文本）。**
      不用 keywords：名录里的 keywords 就是品类标签本身（如「连接器」），
      拿它推断工艺等于把分类结果当成能力证据——实测会把「海亚泡沫塑料」
      判成连接器、「佳能塑料厂」判成连接器。名称才是硬证据。

    **材料 → 公司名 + 抓取关键词。**
      材料与 category 是不同维度，不会自我循环；且材料覆盖率只有 13%，
      是实测最大短板，必须用上关键词这条线索。
    """
    text = name or ""
    if extra_text:
        text = f"{text} {extra_text}"
    codes: list[str] = []
    matched: list[str] = []
    confidence = "low"
    fallback_used = False

    for pat, cs, label in STRONG_RULES:
        if re.search(pat, text, re.I):
            for c in cs:
                if c not in codes:
                    codes.append(c)
            matched.append(label)
            confidence = "high"

    if not codes:
        for pat, cs, label in MEDIUM_RULES:
            if re.search(pat, text, re.I):
                for c in cs:
                    if c not in codes:
                        codes.append(c)
                matched.append(label)
                confidence = "medium"

    if not codes:
        fb = CATEGORY_FALLBACK.get(category)
        if fb:
            codes = list(fb[0])
            matched.append(fb[1])
            fallback_used = True
            confidence = "low"
            # 名称只说了「塑料」、没说做什么时，交给品类兜底，而不是直接判成原料。
            # 旧规则「含塑料 + 无加工动词 → plastic_material」抢在兜底之前，
            # 实测把嘉兴 26 家注塑厂（洲泉三盛塑料厂、海盐县石泉滔滔塑胶电器配件厂、
            # 桐乡远阳塑胶包装）全误判成原料贸易商 —— 查「嘉兴注塑厂」时它们全消失。
            # 正确语义：注塑成型品类下的塑料厂 → 注塑；原材料品类下的 → 塑料原料。
            if PLASTIC_PAT.search(text):
                if "metal_material" in codes:
                    codes = ["plastic_material"]
                    matched.append("塑料原料")

    # 过滤非法 code（防止词表与 schema 不同步）
    codes = [c for c in codes if c in VALID_CODES]

    mats: list[str] = []
    for pat, m in MATERIAL_RULES:
        if re.search(pat, text, re.I) and m not in mats:
            mats.append(m)
    # 补材料：抓取关键词（如搜「塑料加工」「尼龙加工」抓到的企业）
    kw_mats: list[str] = []
    # 只在名称推断不出工艺（走品类兜底）时，才用抓取关键词补材料。
    # 名称已有工艺线索时说明名称信息足够，而搜索词是模糊匹配产物，不可靠——
    # 实测「上海精密机械制造」会被「塑料加工」这个词搜到，若不看名称直接贴标签，
    # 一家金属 CNC 厂就成了塑料厂。弱证据不能覆盖强证据。
    if confidence == "low" or fallback_used:
        # 传原始 name 做冲突校验（不能用含 extra_text 的 text，alias/tag 不是主营证据）
        for m in infer_materials_from_keywords(keywords, name or ""):
            if m not in mats:
                mats.append(m)
                kw_mats.append(m)

    # 品类冲突：名称推断的工艺完全不属于名录给的品类
    allowed = CATEGORY_PROCESSES.get(category)
    conflict = False
    if allowed and codes and not (set(codes) & allowed):
        conflict = True

    return {
        "processes": codes,
        "materials": mats,
        "materials_from_keyword": kw_mats,  # 来自抓取关键词的材料（弱证据，但聊胜于无）
        "confidence": confidence,
        "matched": matched,
        "fallback_used": fallback_used,
        "category_conflict": conflict,
    }


def amap_extra_text(rec: dict) -> str:
    """从高德扩展字段里取可用于工艺推断的自述类文本（alias / tag / keytag）。

    注意不含 type：「公司企业;工厂;工厂」这种分类不是工艺，拿它推断会出错。
    """
    am = rec.get("amap") or {}
    bits = [str(am.get(k) or "") for k in ("alias", "tag", "keytag")]
    return " ".join(b for b in bits if b).strip()


# ------------------------------------------------------------------ 非 C 门类能力推断
#
# 2026-09-14 改造：自动整理从「仅制造业」扩展到全 7 大门类。
# 非 C 门类**不推断「工艺」**（餐饮没有 CNC、理发没有注塑），而是用各门类的
# 能力码表域（restaurant / retail-service / tech-service / entertainment）从公司名
# 推断相应的业务能力码（菜系 / 零售品类 / 技术方向 / 场所类型 …）。
# 这是与制造业「名义能力」同类的推断，同样**不编造数值**（座位/桌数/价格/工时一律留空）。
#
# 推断文本 = 清洗后公司名 + 抓取关键词 + 高德 alias/tag/keytag 自述。
# 各域 resolve() 按「别名(长优先) → 码名」匹配，命中即记码，绝不兜底成编造值。

NON_MFG_CAPABILITY: dict[str, dict] = {
    "H": {  # 住宿和餐饮业 → restaurant 域
        "domain": "restaurant",
        "groups": {
            "cuisines": "菜系",
            "service_modes": "服务形态",
            "venue_features": "场地",
            "time_slots": "时段",
        },
    },
    "F": {  # 批发和零售业 → retail-service 域（取「零售品类」组）
        "domain": "retail-service",
        "groups": {
            "retail_categories": "零售品类",
            "business_modes": "经营方式",
            "delivery_modes": "交付方式",
            "store_features": "门店设施",
        },
    },
    "O": {  # 居民服务、修理和其他服务业 → retail-service 域（取「服务项目」组）
        "domain": "retail-service",
        "groups": {
            "service_items": "服务项目",
            "business_modes": "经营方式",
            "delivery_modes": "交付方式",
            "store_features": "门店设施",
        },
    },
    "I": {  # 信息传输、软件和信息技术服务业 → tech-service 域（取「信息技术方向」）
        "domain": "tech-service",
        "groups": {
            "tech_directions": "信息技术方向",
            "service_modes": "服务模式",
            "deliverables": "交付成果",
            "tech_stacks": "技术栈",
            "team_roles": "团队构成",
        },
    },
    "M": {  # 科学研究和技术服务业 → tech-service 域（取「科研技术方向」）
        "domain": "tech-service",
        "groups": {
            "tech_directions": "科研技术方向",
            "service_modes": "服务模式",
            "deliverables": "交付成果",
            "team_roles": "团队构成",
        },
    },
    "R": {  # 文化、体育和娱乐业 → entertainment 域
        "domain": "entertainment",
        "groups": {
            "venue_types": "场所类型",
            "service_modes": "服务形态",
            "venue_features": "场地设施",
            "time_slots": "时段",
        },
    },
}


def _infer_text(rec: dict, cleaned: str) -> str:
    """非 C 门类的能力推断文本：公司名（硬证据）+ 抓取关键词 + 高德自述。"""
    parts = [cleaned or ""]
    kw = rec.get("keywords") or []
    if kw:
        parts.append(" ".join(str(k) for k in kw))
    extra = amap_extra_text(rec)
    if extra:
        parts.append(extra)
    return " ".join(p for p in parts if p)


def infer_non_mfg_capability(gate: str, text: str) -> dict:
    """按门类把自由文本解析成各能力字段的码列表（分组互不串用）。"""
    spec = NON_MFG_CAPABILITY.get(gate)
    if not spec:
        return {}
    domain = spec["domain"]
    out: dict[str, list[str]] = {}
    for field, group in spec["groups"].items():
        out[field] = cap_codes.resolve(domain, text, group=group)
    return out


def _non_mfg_confidence(inf: dict) -> str:
    """非 C 门类：按推断命中条数给置信度（与制造业 high/medium/low 同档）。"""
    n = sum(len(v) for v in inf.values() if isinstance(v, list))
    if n >= 3:
        return "high"
    if n >= 1:
        return "medium"
    return "low"


# 国标小类/中类码 → 门类展示子枚举（category）。
# 数据记录里 category 仍是完整门类名（如「住宿和餐饮业」），而 schema 要求的是
# 该门类内部的业态子枚举（如 H 的 正餐/快餐/住宿…），故按国标码映射。
# 未登记到具体码的码回落到该门类的「其他X」兜底（仍是合法枚举值，绝不编造）。
GB_CODE_CATEGORY: dict[str, dict[str, str]] = {
    "H": {
        "6110": "住宿", "6121": "住宿", "6130": "住宿",
        "6210": "正餐", "6220": "快餐", "6231": "饮品烘焙", "6232": "饮品烘焙",
        "6233": "其他餐饮", "6242": "其他餐饮", "6291": "其他餐饮",
    },
    "F": {
        "5131": "批发商行", "5138": "批发商行", "5146": "批发商行", "5147": "批发商行",
        "5164": "批发商行", "5165": "批发商行", "5169": "批发商行", "5171": "批发商行",
        "5174": "批发商行", "5179": "批发商行", "5183": "其他零售", "5193": "网店电商",
        "5212": "综合超市", "5213": "零售门店", "5229": "零售门店", "5232": "专营专卖",
        "5251": "专营专卖", "526": "专营专卖", "5274": "专营专卖", "5281": "零售门店",
        "5283": "专营专卖", "5287": "专营专卖",
    },
    "O": {
        "8010": "家政服务", "8030": "洗衣洗染", "8040": "美容美发",
        "8051": "洗浴养生", "8052": "洗浴养生", "8053": "洗浴养生",
        "8060": "摄影婚庆", "8090": "其他服务", "8111": "汽车维修",
        "8121": "家电数码维修", "8122": "家电数码维修", "8211": "清洁搬家",
        "8290": "其他服务",
    },
    "I": {
        "6440": "网络安全", "6450": "互联网平台", "6490": "互联网平台",
        "651": "软件开发", "6513": "软件开发", "6531": "系统集成与物联网",
        "6540": "运维服务", "6550": "云计算", "6572": "游戏数字内容",
        "6591": "其他信息服务",
    },
    "M": {
        "7320": "技术研发", "7330": "技术研发", "7350": "技术研发",
        "7452": "检验检测", "7453": "计量校准", "7455": "认证认可",
        "7461": "环境监测", "7484": "工程勘察设计", "7485": "工程勘察设计",
        "749": "工业设计", "7491": "工业设计", "7492": "工业设计",
        "7516": "节能环保", "7520": "知识产权服务",
    },
    "R": {
        "8760": "影院", "8850": "其他娱乐", "8870": "其他娱乐",
        "8921": "球馆泳池", "8929": "球馆泳池", "8930": "健身运动",
        "8991": "其他娱乐", "9011": "KTV", "9013": "网吧网咖",
        "9019": "其他娱乐", "9030": "其他娱乐", "9051": "其他娱乐",
    },
}

DEFAULT_SUBENUM = {
    "C": "其他", "H": "其他餐饮", "F": "其他零售", "O": "其他服务",
    "I": "其他信息服务", "M": "其他技术服务", "R": "其他娱乐",
}


def category_subenum_of(gate: str, code: str) -> str:
    """国标码 → 该门类的展示子枚举。找不到回落到「其他X」兜底（仍是合法枚举）。"""
    return GB_CODE_CATEGORY.get(gate, {}).get(str(code)) or DEFAULT_SUBENUM.get(gate, "")


# C 门类 category 合法枚举：用于 _build_cap_c 防御性兜底
try:
    _C_CATEGORY_ENUM = set(
        gate_schema.build_schema("C")["properties"]["category"]["enum"]
    )
except Exception:  # noqa: BLE001
    _C_CATEGORY_ENUM = set()


# 各门类 limits 子字段（全可为 null）—— 缓存一次，避免每张卡重算 schema。
_LIMITS_CACHE: dict[str, dict] = {}


def _null_limits(gate: str) -> dict:
    """按门类 schema 的 limits 子字段生成「全 null」硬边界块（数值一律留空=红线）。"""
    if gate not in _LIMITS_CACHE:
        props = gate_schema.build_schema(gate)["properties"]["limits"]["properties"]
        _LIMITS_CACHE[gate] = {k: None for k in props}
    return dict(_LIMITS_CACHE[gate])


# ------------------------------------------------------------------ 打分

def quality_score(rec: dict, cleaned: str, gate: str = "C",
                  inf: dict | None = None) -> tuple[int, list[str]]:
    """
    「信息比较全」打分。信息量 = 能推断出多少可信的能力信息。
    返回 (分数, 理由列表)

    2026-09-14 扩展：gate≠C 时按各门类能力码表推断业务能力并据此给分；
    C 仍是「工艺 + 材料」口径。通用加分项（电话/地址/国标置信度/可联系性）全门类共用。
    """
    score = 0
    why: list[str] = []

    if gate == "C":
        cat = rec.get("category") or ""
        kws = rec.get("keywords") or []
        cinf = inf or infer(cleaned, cat, kws, amap_extra_text(rec))
        if cinf["confidence"] == "high":
            score += 5
            why.append(f"强工艺线索：{'、'.join(cinf['matched'][:3])}")
        elif cinf["confidence"] == "medium":
            score += 3
            why.append(f"中工艺线索：{'、'.join(cinf['matched'][:3])}")
        else:
            score -= 2
            why.append("仅品类兜底，名称无工艺线索")
        if cinf["materials"]:
            score += 2
            why.append(f"材料线索：{'、'.join(cinf['materials'][:3])}")
    else:
        ninf = inf or infer_non_mfg_capability(gate, _infer_text(rec, cleaned))
        conf = _non_mfg_confidence(ninf)
        n = sum(len(v) for v in ninf.values() if isinstance(v, list))
        if conf == "high":
            score += 5
            why.append(f"明确业务特征：名下 {n} 项能力码")
        elif conf == "medium":
            score += 3
            why.append(f"名称含部分业务特征（{n} 项能力码）")
        else:
            score -= 2
            why.append("名称无明确业务特征，能力推断很少")

    # ---- 通用加分项（C 与非 C 共用）----
    if SCALE_HINT.search(cleaned):
        score += 1
        why.append("名称含股份/集团/实业（规模线索）")

    amtype = str((rec.get("amap") or {}).get("type") or "")
    if amtype and AMAP_TYPE_POSITIVE.search(amtype):
        score += 1
        why.append("高德分类含工厂/制造（真实生产主体）")
    elif amtype and AMAP_TYPE_NEGATIVE.search(amtype):
        score -= 3
        why.append(f"高德分类为「{amtype.split(';')[0]}」（非生产主体）")

    addr = str(rec.get("address") or "")
    if re.search(r"镇|街道|工业区|园区|开发区|路\d+号|大道", addr):
        score += 1
        why.append("地址具体到镇/街道/园区")

    if rec.get("status") == "verified":
        score += 1
        why.append("名录状态 verified")

    # ------------------------------------------------------------------
    # 可联系性 + 行业标签置信度（与 C 同为硬信息量）
    phone = str(rec.get("contact_phone") or "")
    if phone and phone not in ("待核实", "None", "null"):
        score += 2
        why.append("联系电话可用")
    if rec.get("website"):
        score += 1
        why.append("有官网")
    ind = rec.get("industry") or {}
    if ind.get("confidence") == "high":
        score += 1
        why.append("国标行业标签置信度高")
    elif ind.get("code") is None:
        score -= 1
        why.append("未归入国标行业，行业口径存疑")

    if gate == "C" and re.search(r"厂$|工厂$|制造|实业", cleaned):
        score += 1
        why.append("名称指向生产主体")

    nk = noise_kind(cleaned)
    if nk == "soft":
        score -= 3
        why.append("疑似贸易/培训/营销点，降权")

    return score, why


# ------------------------------------------------------------------ 生成 capability

def build_capability(rec: dict, cleaned: str, today: str) -> tuple[dict | None, dict]:
    """
    生成自动整理的 capability.json。
    数值能力一律留空 —— 这是红线，不是偷懒。

    2026-09-14 扩展：所有已登记门类都走这里，按 gate 分派到各自的构造器。
    卡片统一带显式 `gate` 字段与 `caps_index`（派生），并通过对应门类 schema 校验。
    """
    ind = rec.get("industry") or {}
    code = ind.get("code") or ""
    gate = gate_of_code(code)
    if not gate:
        # 无国标码：名录粗分类若是某门类名（如「住宿和餐饮业」），据此回判；
        # 否则兜底制造业（老数据惯例）。避免把门类名当 C 子类用而校验崩溃。
        gate = gate_of_category_name(rec.get("category")) or "C"
    if gate == "C":
        return _build_cap_c(rec, cleaned, today, code)
    return _build_cap_nonc(rec, cleaned, today, gate, code)


def _build_cap_c(rec: dict, cleaned: str, today: str, code: str) -> tuple[dict | None, dict]:
    """制造业（C）能力卡：processes + materials + 公差全空。"""
    cat = rec.get("category") or ""
    # 防御：rec["category"] 可能是上游误标的门类名（如「住宿和餐饮业」），
    # 不在 C 子枚举内时回落「其他」，避免校验崩溃。
    if cat not in _C_CATEGORY_ENUM:
        cat = "其他"
    kws = rec.get("keywords") or []
    inf = infer(cleaned, cat, kws, amap_extra_text(rec))

    if not inf["processes"]:
        return None, {"skip": "no_process", "reason": "推断不出任何工艺"}

    # 名称（硬证据）与名录品类（弱证据）冲突时，按名称重新归类
    declared_cat = cat
    cat, reclassified = resolve_category(inf["processes"], cat)
    if reclassified:
        inf["reclassified_from"] = declared_cat

    region = rec.get("region") or {}
    procs = [
        {"code": c, "name": _code_name(c), "level": "primary"}
        for c in inf["processes"]
    ]

    note = ("平台从公开名录自动整理；工艺与材料由企业名称推断，"
            "未获企业确认，硬指标全部空缺。企业认领后可更正。")
    if inf.get("materials_from_keyword"):
        note += (f" 材料「{'、'.join(inf['materials_from_keyword'])}」依据抓取关键词推断，"
                 "可信度低于名称推断。")
    if inf.get("category_conflict"):
        if reclassified:
            note += (f" 注意：企业名称指向的工艺与名录分类「{declared_cat}」不一致，"
                     f"已按名称改判为「{cat}」。")
        else:
            note += (f" 注意：企业名称指向的工艺与名录分类「{cat}」不一致，"
                     "分类可能需更正。")

    cap = {
        "beacon_version": "1.0",
        "supplier_id": rec["id"],
        "gate": "C",
        "company": cleaned or rec.get("company", ""),
        "category": cat,
        "profile": CATEGORY_PROFILE.get(cat, "custom"),
        "updated_at": today,
        "claim": {
            "status": "unclaimed",
            "verified_by": None,
            "verified_at": None,
            "badge": "L0",
        },
        "identity": {
            "province": region.get("province"),
            "city": region.get("city"),
            "address": rec.get("address"),
            "lat": rec.get("lat"),
            "lng": rec.get("lng"),
        },
        "contact": {
            "phone": rec.get("contact_phone"),
            "address": rec.get("address"),
        },
        "processes": procs,
        "materials": inf["materials"],
        # 数值能力全部留空 —— 平台不替供应商编造数字
        "limits": {
            "tolerance_mm": None,
            "max_part_size_mm": None,
            "min_order_qty": None,
            "lead_time_days": None,
            "current_load_pct": None,
            "rush_available": None,
        },
        "safety": {
            "content_is_data_only": True,
            "no_agent_instructions": True,
        },
        "provenance": {
            "mode": "auto",
            "source": "public_directory",
            "confidence": inf["confidence"],
            "inferred_fields": ["processes", "materials"],
            # 改判留痕：客户 Agent 与企业都能看到「原本被归到哪、为什么改了」
            **({"reclassified_from": declared_cat} if reclassified else {}),
            "note": note,
        },
        "evidence": {
            "self_declared": [],
            "platform_verified": [],
            "field_audited": [],
        },
    }
    # L0 指纹与检索层唯一读取的能力键：只能由派生函数产出
    cap["caps_index"] = gate_schema.derive_caps_index(cap, "C")
    # 校验兜底（理论上 C 卡早已验证过，这里再兜一层）
    ok, errs = gate_schema.validate_card(cap)
    if not ok:
        return None, {"skip": "validate_failed",
                      "reason": "; ".join(errs[:5]), "card": cap}
    return cap, inf


def _build_cap_nonc(rec, cleaned, today, gate, code) -> tuple[dict | None, dict]:
    """非 C 门类能力卡：按各门类能力码表从公司名推断业务能力码。"""
    inf = infer_non_mfg_capability(gate, _infer_text(rec, cleaned))
    total = sum(len(v) for v in inf.values() if isinstance(v, list))
    if total == 0:
        return None, {"skip": "no_capability", "reason": "推断不出任何业务能力"}

    subenum = category_subenum_of(gate, code)
    profile = gate_schema.default_profile(gate, subenum)
    spec = NON_MFG_CAPABILITY[gate]

    capability = {}
    for field, group in spec["groups"].items():
        vals = inf.get(field) or []
        if vals:
            capability[field] = vals

    region = rec.get("region") or {}
    conf = _non_mfg_confidence(inf)
    inferred_fields = [f"capability.{f}" for f in capability]
    note = ("平台从公开名录自动整理；业务能力（菜系/服务项目/技术方向等）由企业名称与"
            "经营范围推断，未获企业确认，硬指标全部空缺。企业认领后可更正。")

    cap = {
        "beacon_version": "1.0",
        "supplier_id": rec["id"],
        "gate": gate,
        "company": cleaned or rec.get("company", ""),
        "category": subenum,
        "profile": profile,
        "updated_at": today,
        "claim": {
            "status": "unclaimed",
            "verified_by": None,
            "verified_at": None,
            "badge": "L0",
        },
        "identity": {
            "province": region.get("province"),
            "city": region.get("city"),
            "address": rec.get("address"),
            "lat": rec.get("lat"),
            "lng": rec.get("lng"),
        },
        "contact": {
            "phone": rec.get("contact_phone"),
            "address": rec.get("address"),
        },
        "capability": capability,
        "limits": _null_limits(gate),
        "safety": {
            "content_is_data_only": True,
            "no_agent_instructions": True,
        },
        "provenance": {
            "mode": "auto",
            "source": "public_directory",
            "confidence": conf,
            "inferred_fields": inferred_fields,
            "note": note,
        },
        "evidence": {
            "self_declared": [],
            "platform_verified": [],
            "field_audited": [],
            "public_record": [],
        },
    }
    # L0 指纹与检索层唯一读取的能力键：只能由派生函数产出
    cap["caps_index"] = gate_schema.derive_caps_index(cap, gate)
    # 校验兜底：任一字段越界（码表漂移 / 字段名错）都在此拦下，不污染 R2
    ok, errs = gate_schema.validate_card(cap)
    if not ok:
        return None, {"skip": "validate_failed",
                      "reason": "; ".join(errs[:5]), "card": cap}
    return cap, inf


# ------------------------------------------------------------------ 候选筛选

def load_candidates(city: str = "嘉兴") -> list[dict]:
    """载入指定城市的全部薄记录。"""
    out = []
    for r in gb_store.load_all():
        if (r.get("region") or {}).get("city") == city:
            out.append(r)
    return out


def screen(city: str = "嘉兴") -> tuple[list[dict], dict]:
    """
    筛选候选。返回 (通过筛选的记录列表, 统计信息)
    """
    recs = load_candidates(city)
    kept, stats = [], {
        "total": len(recs),
        "noise_hard": 0,
        "no_process": 0,           # 制造业：推断不出工艺；非 C：推断不出业务能力
        "non_manufacturing": 0,    # 历史兼容统计（现已不再据此跳过任何门类）
        "kept": 0,
        "by_category": {},
        "by_confidence": {},
        "by_gate": {},             # 新增：按门类字母看全门类覆盖
        "noise_samples": [],
    }
    stats["by_category"]["__raw__"] = {}
    for r in recs:
        stats["by_category"]["__raw__"][r.get("category")] = (
            stats["by_category"]["__raw__"].get(r.get("category"), 0) + 1)

    for r in recs:
        cleaned = clean_name(r.get("company", ""))
        if not cleaned:
            stats["noise_hard"] += 1
            continue
        nk = noise_kind(cleaned)
        if nk == "hard":
            stats["noise_hard"] += 1
            if len(stats["noise_samples"]) < 25:
                stats["noise_samples"].append(f"{r.get('company')} [{r.get('category')}]")
            continue

        # 2026-09-14：去掉「门类守卫」。所有已登记门类（C/F/H/I/M/O/R）都进入推断，
        # 非 C 走各门类能力码表（infer_non_mfg_capability），不拿制造业词典硬凑，
        # 但推断不出任何业务能力的仍按「无能力」剔除——红线：数据宁可留空也不编造。
        gate = gate_of_code((r.get("industry") or {}).get("code") or "") or "C"

        if gate == "C":
            inf = infer(cleaned, r.get("category") or "", r.get("keywords"),
                        amap_extra_text(r))
            if not inf["processes"]:
                stats["no_process"] += 1
                continue
            conf = inf["confidence"]
        else:
            inf = infer_non_mfg_capability(gate, _infer_text(r, cleaned))
            n = sum(len(v) for v in inf.values() if isinstance(v, list))
            if n == 0:
                stats["no_process"] += 1
                continue
            conf = _non_mfg_confidence(inf)

        stats["by_gate"][gate] = stats["by_gate"].get(gate, 0) + 1
        score, why = quality_score(r, cleaned, gate, inf)
        kept.append({
            "record": r, "cleaned": cleaned, "score": score,
            "why": why, "infer": inf, "gate": gate,
        })
        stats["by_category"][r.get("category")] = (
            stats["by_category"].get(r.get("category"), 0) + 1)
        stats["by_confidence"][conf] = stats["by_confidence"].get(conf, 0) + 1

    stats["kept"] = len(kept)
    kept.sort(key=lambda x: (-x["score"], x["record"]["id"]))
    return kept, stats
