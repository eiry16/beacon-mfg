# -*- coding: utf-8 -*-
"""GB/T 4754-2017 国民经济行业分类 —— 全量代码表

数据来自国家统计局官方 docx（含 2019 年第 1 号修改单），解析结果落在
`data/gb4754-full.json`：**20 门类 / 97 大类 / 473 中类 / 1382 小类**。
本模块只做加载与查询，不内嵌数据。

代码层级：门类(1位字母) > 大类(2位) > 中类(3位) > 小类(4位)。
末位为 9 的通常是「其他/未列明」收口类。

### 分层落地（重要）

不是每条企业都能判到 4 位小类——公司名证据往往只够到中类甚至大类。
**硬判到 4 位等于编造**，所以分类结果带 `level`：

| level      | 码长 | 含义                       |
|------------|------|----------------------------|
| `class`    | 4    | 证据足够定位到小类         |
| `group`    | 3    | 只能定位到中类（小类有歧义）|
| `division` | 2    | 只能定位到大类             |

检索时按祖先链前缀匹配即可同时命中各级（查 3484 不会漏掉 348 / 34）。

英文小类名在 `data/gb4754-en.json` 人工维护，只覆盖数据里实际出现的代码，
未收录的返回 None —— 不机翻、不编造。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FULL_PATH = ROOT / "data" / "gb4754-full.json"
EN_PATH = ROOT / "data" / "gb4754-en.json"

LEVELS = ("gate", "division", "group", "class")


def _load():
    d = json.load(open(FULL_PATH, encoding="utf-8"))
    return d["gates"], d["divisions"], d["groups"], d["classes"]


GATES, DIVISIONS, GROUPS, CLASSES = _load()

try:
    _EN = json.load(open(EN_PATH, encoding="utf-8"))["en"]
except FileNotFoundError:  # 英文表缺失不影响分类与校验
    _EN = {}


def gate_of(code: str) -> str:
    """任意层级代码 → 门类字母。"""
    code = str(code)
    if code[:1].isalpha():
        return code[:1].upper()
    return DIVISIONS.get(code[:2], {}).get("gate", "")


def is_manufacturer(code: str) -> bool:
    """是否制造业（C 门类）。批发零售 F 明确不是。"""
    return gate_of(code) == "C"


def level_of(code: str) -> str:
    """按码长判断层级。"""
    code = str(code)
    if code[:1].isalpha():
        return "gate"
    return {2: "division", 3: "group", 4: "class"}.get(len(code), "")


def name_of(code: str) -> str:
    code = str(code)
    if code[:1].isalpha():
        return GATES.get(code.upper(), "")
    n = len(code)
    if n == 2:
        return DIVISIONS.get(code, {}).get("name", "")
    if n == 3:
        return GROUPS.get(code, {}).get("name", "")
    if n == 4:
        return CLASSES.get(code, {}).get("name", "")
    return ""


def en_of(code: str):
    """英文小类名；未人工收录返回 None（不机翻）。"""
    return _EN.get(str(code))


def path_of(code: str) -> str:
    """完整可读路径，如 C 制造业 > 通用设备制造业 > 通用零部件制造 > 机械零部件加工。"""
    code = str(code)
    g = gate_of(code)
    parts = ["%s %s" % (g, GATES.get(g, ""))]
    if not code[:1].isalpha():
        if len(code) >= 2:
            parts.append(DIVISIONS.get(code[:2], {}).get("name", ""))
        if len(code) >= 3:
            parts.append(GROUPS.get(code[:3], {}).get("name", ""))
        if len(code) >= 4:
            parts.append(CLASSES.get(code, {}).get("name", ""))
    return " > ".join(p for p in parts if p)


def ancestors(code: str) -> list:
    """从粗到细的祖先链，供检索按前缀命中。如 3484 -> ['C','34','348','3484']。"""
    code = str(code)
    if code[:1].isalpha():
        return [code.upper()]
    chain = []
    g = gate_of(code)
    if g:
        chain.append(g)
    for n in (2, 3, 4):
        if len(code) >= n:
            chain.append(code[:n])
    return chain


# --------------------------------------------------------------------------
# 兼容旧 API：CODES 保持 {code: {name, l3, l2, l1, en}} 形状，但覆盖全部 1382 个
# --------------------------------------------------------------------------
def _build_codes():
    out = {}
    for code, v in CLASSES.items():
        grp = v["group"]
        div = GROUPS.get(grp, {}).get("div", grp[:2])
        gate = DIVISIONS.get(div, {}).get("gate", "")
        out[code] = {
            "name": v["name"],
            "l3": (grp, GROUPS.get(grp, {}).get("name", "")),
            "l2": (div, DIVISIONS.get(div, {}).get("name", "")),
            "l1": (gate, GATES.get(gate, "")),
            "en": _EN.get(code),
        }
    return out


CODES = _build_codes()

# --------------------------------------------------------------------------
# 国标小类 → 存储品类（采购视角）。手工映射优先，其余按大类兜底。
# 品类是采购语言，国标是分类标准，两套并存、不互相取代。
# --------------------------------------------------------------------------
CATEGORY_OF_CODE = {
    # 精密机械加工：机加工 / 通用与专用设备 / 仪表
    "3484": "精密机械加工", "3489": "精密机械加工", "3421": "精密机械加工",
    "3422": "精密机械加工", "3424": "精密机械加工", "3429": "精密机械加工",
    "3435": "精密机械加工", "3441": "精密机械加工", "3443": "精密机械加工",
    "3444": "精密机械加工", "3445": "精密机械加工", "3453": "精密机械加工",
    "3462": "精密机械加工", "3464": "精密机械加工", "3465": "精密机械加工",
    "3467": "精密机械加工", "3491": "精密机械加工", "3499": "精密机械加工",
    "3523": "精密机械加工", "3544": "精密机械加工", "3562": "精密机械加工",
    "3581": "精密机械加工", "3584": "精密机械加工", "3591": "精密机械加工",
    "3599": "精密机械加工", "3660": "精密机械加工", "4011": "精密机械加工",
    "4014": "精密机械加工", "4090": "精密机械加工",
    # 钣金冲压：结构性金属制品
    "3311": "钣金冲压", "3312": "钣金冲压", "3333": "钣金冲压", "3340": "钣金冲压",
    "3351": "钣金冲压", "3352": "钣金冲压", "3389": "钣金冲压", "3399": "钣金冲压",
    # 注塑成型：橡塑
    "2651": "注塑成型", "2652": "注塑成型", "2659": "注塑成型", "2913": "注塑成型",
    "2919": "注塑成型", "2921": "注塑成型", "2922": "注塑成型", "2926": "注塑成型",
    "2927": "注塑成型", "2929": "注塑成型", "3525": "注塑成型",
    # 压铸：铸造与锻压
    "3391": "压铸", "3392": "压铸", "3393": "压铸",
    # 表面处理
    "3360": "表面处理", "4310": "表面处理",
    # 电子元器件
    "3812": "电子元器件", "3821": "电子元器件", "3822": "电子元器件",
    "3823": "电子元器件", "3824": "电子元器件", "3831": "电子元器件",
    "3841": "电子元器件", "3849": "电子元器件", "3872": "电子元器件",
    "3912": "电子元器件", "3913": "电子元器件", "3921": "电子元器件",
    "3973": "电子元器件", "3974": "电子元器件", "3975": "电子元器件",
    "3981": "电子元器件", "3982": "电子元器件", "3983": "电子元器件",
    "3984": "电子元器件", "3985": "电子元器件", "3989": "电子元器件",
    # 标准件
    "3321": "标准件", "3329": "标准件", "3451": "标准件", "3452": "标准件",
    "3459": "标准件", "3481": "标准件", "3482": "标准件", "3483": "标准件",
    # 原材料
    "2641": "原材料", "3051": "原材料", "3059": "原材料", "3072": "原材料",
    "3130": "原材料", "3240": "原材料", "3251": "原材料", "3252": "原材料",
    "3259": "原材料", "5164": "原材料", "5165": "原材料", "5169": "原材料",
    "5174": "原材料", "5179": "原材料",
}

# 大类 → 品类兜底（手工映射未覆盖时用）
_DIVISION_CATEGORY = {
    "33": "钣金冲压", "34": "精密机械加工", "35": "精密机械加工",
    "36": "精密机械加工", "37": "精密机械加工", "38": "电子元器件",
    "39": "电子元器件", "40": "精密机械加工", "29": "注塑成型",
    "26": "原材料", "30": "原材料", "31": "原材料", "32": "原材料",
    "51": "原材料", "52": "原材料",
}
DEFAULT_CATEGORY = "精密机械加工"


def category_of(code: str) -> str:
    """国标代码 → 存储品类文件名（不含 .json）。"""
    code = str(code)
    if code in CATEGORY_OF_CODE:
        return CATEGORY_OF_CODE[code]
    if not code[:1].isalpha() and len(code) >= 2:
        return _DIVISION_CATEGORY.get(code[:2], DEFAULT_CATEGORY)
    return DEFAULT_CATEGORY


def self_check():
    """代码表自检，供 fetch_batch --dry-run 调用。"""
    missing = [c for c in CODES if c not in CATEGORY_OF_CODE]
    bad = [c for c in CODES if not path_of(c)]
    return missing, bad


# --------------------------------------------------------------------------
# 分类用的特征词倒排索引
# --------------------------------------------------------------------------
# 小类名称里的通用后缀，抽特征词时要剥掉，否则「制造」「加工」会命中所有条目
_GENERIC_TAIL = re.compile(
    r"(制造|加工|生产|其他|未列明|及类似|和相关|与|及|和|的|业|品|设备|器材|专用|通用)$"
)
_STOP_TOKENS = {
    "制造", "加工", "生产", "其他", "未列明", "类似", "相关", "设备", "器材",
    "专用", "通用", "制品", "产品", "用品", "材料", "及其", "配件", "零件",
}
# 以这些词收尾的子串没有区分度，却极易误命中：
# 「纸制品加工厂」会被「制品加工」拉到 3552 皮革制品加工专用设备制造。
_BAD_SUFFIX = ("加工", "制品", "设备", "器材", "材料", "产品", "用品", "制造", "生产")


def _feature_tokens(name: str) -> list:
    """从小类名称里抽有区分度的特征词。"""
    toks = []
    for p in re.split(r"[、,，]", name.strip()):
        p = _GENERIC_TAIL.sub("", p).strip()
        if len(p) < 2 or p in _STOP_TOKENS:
            continue
        toks.append(p)
    return toks


def build_token_index(scope_gates=("C", "F")):
    """特征词 → 命中的小类集合（限定制造/批发，其余门类不在本数据集范围）。

    除了完整特征词，还会把长度 ≥2 的**后缀**一并入索引：
    「滚动轴承」「滑动轴承」都能让「XX轴承厂」命中，两者同属中类 345，
    于是自动降级到 group —— 这正是我们要的保守行为。
    """
    idx = {}
    for code, v in CLASSES.items():
        if gate_of(code) not in scope_gates:
            continue
        for tok in _feature_tokens(v["name"]):
            # 只取前缀与后缀（长度 ≥2），不取中间切片。
            # 真实公司名对行业词的截断只有这两种形式：
            #   「XX轴承厂」  ← 滚动轴承 的后缀
            #   「齿轮制造」  ← 齿轮及齿轮减、变速箱 的前缀
            # 中间切片（如「制品加」「金制」）没有语义，只会制造噪音。
            # 前缀/后缀最短 3 字（完整特征词除外，2 字的行业词如「模具」本身就有区分度）。
            # 为什么是 3：「巧克力」切出的 2 字后缀「克力」会命中「亚克力」（有机玻璃），
            # 把一批塑料/广告材料厂判成糖果制造——短片段噪音必须用长度卡掉。
            cand = set()
            for n in range(2, len(tok) + 1):
                if n >= 3 or n == len(tok):
                    cand.add(tok[:n])   # 前缀
                    cand.add(tok[-n:])  # 后缀
            for sub in cand:
                if sub in _STOP_TOKENS:
                    continue
                # 泛词收尾的子串不入索引（完整特征词除外，已被 _GENERIC_TAIL 剥过）
                if sub != tok and sub.endswith(_BAD_SUFFIX):
                    continue
                idx.setdefault(sub, set()).add(code)
    return idx


TOKEN_INDEX = build_token_index()
# 长词优先：匹配时从长到短试，避免「轴承」抢在「滚动轴承」前面
_TOKENS_BY_LEN = sorted(TOKEN_INDEX, key=lambda t: (-len(t), t))


def resolve_token(token: str):
    """特征词 → (code, level)。唯一命中给 class；有歧义降级 group / division。"""
    codes = TOKEN_INDEX.get(token)
    if not codes:
        return None, None
    if len(codes) == 1:
        return next(iter(codes)), "class"
    groups = {c[:3] for c in codes}
    if len(groups) == 1:
        return next(iter(groups)), "group"
    divs = {g[:2] for g in groups}
    if len(divs) == 1:
        return next(iter(divs)), "division"
    return None, None


def match_longest(text: str):
    """在文本里找最长的能命中的特征词，返回 (token, code, level)。

    长词优先，避免短词抢命中导致过度降级。
    """
    if not text:
        return None, None, None
    for tok in _TOKENS_BY_LEN:
        if tok in text:
            code, level = resolve_token(tok)
            if code:
                return tok, code, level
    return None, None, None


if __name__ == "__main__":
    print("门类 %d / 大类 %d / 中类 %d / 小类 %d"
          % (len(GATES), len(DIVISIONS), len(GROUPS), len(CLASSES)))
    print("特征词索引 %d 条（限定 C/F 门类）" % len(TOKEN_INDEX))
    m, b = self_check()
    print("无手工品类映射的小类 %d（走大类兜底）/ 路径渲染失败 %d" % (len(m), len(b)))
    for c in ("3484", "3525", "3391", "3360", "5164"):
        print("  %s %-10s -> %s | %s" % (c, name_of(c), category_of(c), path_of(c)))
    for t in ("模具", "轴承", "紧固件", "电线", "输送"):
        print("  token %-4s -> %s" % (t, resolve_token(t)))
