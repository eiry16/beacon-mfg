# -*- coding: utf-8 -*-
"""
BeaconMFG 口语归一化引擎
========================

把制造业供应商的口语回答转成结构化数值。这是采集引擎的核心难点：
供应商说"一般一丝吧"，要落成 0.01mm；说"八百乘六百，高度四百"，要落成 [800,600,400]。

设计原则：
1. 宁可返回 None 也不要猜错 —— 猜错的数值会直接进入客户 Agent 的淘汰判断
2. 每次转换都返回 (value, note)，note 记录归一化过程，供人工复核与面板展示
3. 单位换算必须显式，禁止隐式假设

用法：
    from normalize import normalize
    v, note = normalize("tolerance_mm", "一般一丝吧，精细的能到半丝")
"""
from __future__ import annotations

import re
from typing import Any, Optional

# ---------------------------------------------------------------- 中文数字

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
    "陆": 6, "柒": 7, "捌": 8, "玖": 9,
}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_BIG = {"万": 10000, "亿": 100000000}
_CN_NUM_CHARS = "".join(_CN_DIGITS) + "".join(_CN_UNITS) + "".join(_CN_BIG)


def cn2int(s: str) -> Optional[int]:
    """中文数字字符串 → 整数。'八百'→800, '十五'→15, '三千'→3000。非数字返回 None。"""
    s = s.strip()
    if not s or any(c not in _CN_NUM_CHARS for c in s):
        return None
    total = section = number = 0
    for ch in s:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            section += (number if number else 1) * unit
            number = 0
        elif ch in _CN_BIG:
            unit = _CN_BIG[ch]
            section = (section + number) * unit
            total += section
            section = number = 0
    return total + section + number


def _to_num(tok: str) -> Optional[float]:
    """单个 token → 数字，兼容阿拉伯与中文。"""
    tok = tok.strip()
    if not tok:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", tok):
        return float(tok)
    v = cn2int(tok)
    return float(v) if v is not None else None


# ---------------------------------------------------------------- 单位

# 长度 → mm 的换算系数
_LENGTH_UNIT = {
    "mm": 1.0, "毫米": 1.0, "公厘": 1.0,
    "cm": 10.0, "厘米": 10.0, "公分": 10.0,
    "m": 1000.0, "米": 1000.0, "公尺": 1000.0,
    "um": 0.001, "μm": 0.001, "微米": 0.001, "缪": 0.001,
    "丝": 0.01, "道": 0.01,          # 制造业行话：1 丝 = 0.01mm
    "英寸": 25.4, "inch": 25.4, "寸": 25.4, '"': 25.4,
}

_TIME_DAY = {
    "天": 1, "日": 1, "个工作日": 1, "周": 7, "星期": 7, "礼拜": 7,
    "个月": 30, "月": 30, "半月": 15, "半个月": 15,
}

# ---------------------------------------------------------------- 基础抽取


# 单位 token 表（匹配时按长度降序，避免「米」抢先于「毫米」）
_UNIT_TOKENS = [
    "毫米", "微米", "公厘", "厘米", "公分", "英寸", "千瓦", "千克", "公斤",
    "平米", "平方", "平",
    "mm", "cm", "um", "μm", "kw", "W", "pcs",
    "丝", "道", "米", "寸", "吨", "台", "条", "个", "天", "日",
    "周", "星期", "月", "年", "人", "件", "只", "克", "瓦", "万", "亿", "成",
]
# 中文数字后面必须是这些字符（或已到串尾），否则视为噪声（如「一般」的「一」）
_CN_OK_SUFFIX = "乘xX×*,，、。；;:： "


def _unit_after(rest: str) -> str:
    for u in sorted(set(_UNIT_TOKENS), key=len, reverse=True):
        if rest.startswith(u):
            return u
    return ""


def _first_number(text: str) -> tuple[Optional[float], str]:
    """
    从文本中抽第一个有效数字，返回 (数值, 紧随其后的单位串)。

    中文数字要求后接单位或分隔符，避免把「一般」的「一」误当数字。
    """
    m = re.search(r"\d+(?:\.\d+)?", text)
    if m:
        return float(m.group(0)), _unit_after(text[m.end():])

    for m in re.finditer(r"([" + _CN_NUM_CHARS + r"]{1,6})", text):
        v = cn2int(m.group(1))
        if v is None:
            continue
        rest = text[m.end():]
        unit = _unit_after(rest)
        # 注意：不能用 startswith("")，那会让「一般」的「一」也被当成数字
        if unit or rest == "" or rest[0] in _CN_OK_SUFFIX:
            return float(v), unit
    return None, ""


def _all_numbers(text: str) -> list[float]:
    """抽出文本中所有数字（阿拉伯优先，其次中文数字+量词）。"""
    out: list[float] = []
    for m in re.finditer(r"\d+(?:\.\d+)?", text):
        out.append(float(m.group(0)))
    if out:
        return out
    for m in re.finditer(r"([" + _CN_NUM_CHARS + r"]{1,6})\s*(?=[乘xX×*,，、\s]|$)", text):
        v = cn2int(m.group(1))
        if v is not None:
            out.append(float(v))
    return out


# ---------------------------------------------------------------- 各字段归一化


def _first_number_before(text: str, units: tuple) -> tuple[Optional[float], str]:
    """
    找**紧邻指定单位**的第一个数字。

    必需：口语里常在目标数字前带别的量词，
    如问「做一百件呢」答「一百件十二天左右」——直接取第一个数字会得到 100（件），
    而正确答案是 12（天）。
    """
    alt = "|".join(re.escape(u) for u in sorted(units, key=len, reverse=True))
    pat = r"(\d+(?:\.\d+)?|[" + _CN_NUM_CHARS + r"]{1,6})\s*(" + alt + r")"
    for m in re.finditer(pat, text):
        n = _to_num(m.group(1))
        if n is not None:
            return float(n), m.group(2)
    return None, ""


def norm_length_mm(text: str) -> tuple[Optional[float], str]:
    """长度 → mm。'3米'→3000, '500'→500, '一丝'→0.01"""
    n, unit = _first_number(text)
    if n is None:
        return None, "未识别到数字"
    factor = None
    for u, f in _LENGTH_UNIT.items():
        if u and u in unit:
            factor = f
            break
    if factor is None:
        if "米" in text and "毫" not in text and "厘" not in text:
            factor = 1000.0
        else:
            factor = 1.0  # 默认 mm（制造业口语缺省单位）
    v = round(n * factor, 6)
    note = f"{n}{unit or ''} → {v}mm"
    if factor != 1.0:
        note += f"（按 1{unit or '单位'}={factor}mm 换算）"
    return v, note


def norm_tolerance(text: str) -> tuple[Optional[float], str]:
    """
    公差 → mm。制造业行话密集区：
    '一丝'→0.01, '半丝'→0.005, '3个丝'→0.03, '±0.01'→0.01

    关键：一句话里可能同时出现「常规」与「精细」两档（"一般一丝，精细的能到半丝"），
    必须取**最先提到的**那一档，因为问题问的是常规能力。
    """
    t = text.strip()
    cands: list[tuple[int, float, str]] = []

    for m in re.finditer(r"半丝|半个丝|0\.5\s*丝", t):
        cands.append((m.start(), 0.005, "半丝 → 0.005mm"))

    for m in re.finditer(
        r"(\d+(?:\.\d+)?|[" + _CN_NUM_CHARS + r"]{1,3})\s*(?:个)?\s*丝", t
    ):
        raw = m.group(1)
        n = _to_num(raw)
        if n is None:
            continue
        v = round(n * 0.01, 6)
        cands.append((m.start(), v, f"{raw}丝 → {v}mm（1丝=0.01mm）"))

    for m in re.finditer(
        r"(\d+(?:\.\d+)?|[" + _CN_NUM_CHARS + r"]{1,3})\s*(?:个)?\s*道", t
    ):
        raw = m.group(1)
        n = _to_num(raw)
        if n is None:
            continue
        v = round(n * 0.01, 6)
        cands.append((m.start(), v, f"{raw}道 → {v}mm（1道=0.01mm）"))

    for m in re.finditer(r"[±]?\s*(\d+(?:\.\d+)?)", t):
        n = float(m.group(1))
        if n > 1:  # 无单位且 >1，按「丝」处理（公差不可能 >1mm）
            v = round(n * 0.01, 6)
            cands.append((m.start(), v, f"无单位且 >1，按丝处理：{n}丝 → {v}mm"))
        else:
            cands.append((m.start(), n, f"{n}mm"))

    if not cands:
        return None, "未识别到公差数值"
    cands.sort(key=lambda x: x[0])
    return cands[0][1], cands[0][2]


def norm_size3(text: str) -> tuple[Optional[list], str]:
    """三元尺寸 → [L,W,H] mm。'八百乘六百，高度四百'→[800,600,400]"""
    t = text.strip()
    # 长/宽/高/厚 前缀转分隔符，便于切分
    t = re.sub(r"[长宽高厚深]\s*(?:度)?\s*(?=[\d" + _CN_NUM_CHARS + r"])", "|", t)
    # 统一分隔符
    t = re.sub(r"[乘xX×*]|，|,|、|/|\s+", "|", t)
    segs = [s for s in t.split("|") if s.strip()]
    vals: list[float] = []
    for seg in segs:
        n, unit = _first_number(seg)
        if n is None:
            continue
        factor = 1.0
        for u, f in _LENGTH_UNIT.items():
            if u and u in (unit or ""):
                factor = f
                break
        else:
            if "米" in seg and "毫" not in seg and "厘" not in seg:
                factor = 1000.0
        vals.append(round(n * factor, 4))
        if len(vals) == 3:
            break
    if not vals:
        return None, "未识别到尺寸数值"
    note = " × ".join(str(v) for v in vals) + " mm"
    if len(vals) < 3:
        note += f"（仅识别到 {len(vals)} 个维度）"
    return vals, note


def norm_range(text: str) -> tuple[Optional[list], str]:
    """区间 → [min,max]。'0.5到20'→[0.5,20], '10-5000'→[10,5000]"""
    nums = _all_numbers(text)
    if len(nums) >= 2:
        lo, hi = nums[0], nums[1]
        if lo > hi:
            lo, hi = hi, lo
        return [lo, hi], f"{lo} ~ {hi}"
    if len(nums) == 1:
        return [nums[0], nums[0]], f"仅识别到一个值 {nums[0]}，区间上下限相同"
    return None, "未识别到区间数值"


def norm_days(text: str) -> tuple[Optional[int], str]:
    """周期 → 天。'五天'→5, '一周'→7, '半个月'→15, '一个月'→30"""
    t = text.strip()
    if "半个月" in t or "半月" in t:
        return 15, "半个月 → 15 天"
    if "一天" in t or "当天" in t or "次日" in t:
        return 1, "当天/次日 → 1 天"
    # 优先取紧邻时间单位的数字，避开「一百件十二天」里的「一百」
    n, unit = _first_number_before(t, ("个工作日", "天", "日", "周", "星期", "个月", "月"))
    if n is not None:
        for u, f in _TIME_DAY.items():
            if u in unit:
                v = int(round(n * f))
                return v, f"{n}{unit} → {v} 天"
        return int(n), f"{int(n)} 天"
    n, unit = _first_number(t)
    if n is None:
        return None, "未识别到周期"
    for u, f in _TIME_DAY.items():
        if u in (unit or "") or u in t:
            v = int(round(n * f))
            return v, f"{n}{u} → {v} 天"
    return int(n), f"{int(n)} 天（未识别单位，按天计）"


def norm_hours(text: str) -> tuple[Optional[int], str]:
    """响应时长 → 小时。'一天内'→24, '2小时'→2, '当天'→8"""
    t = text.strip()
    m = re.search(r"(\d+(?:\.\d+)?|[" + _CN_NUM_CHARS + r"]{1,4})\s*(小?时|h|H)", t)
    if m:
        n = _to_num(m.group(1))
        if n is not None:
            return int(n), f"{int(n)} 小时"
    if "半小时" in t:
        return 1, "半小时 → 1 小时"
    if "当天" in t or "当日" in t:
        return 8, "当天 → 8 小时（按工作日计）"
    n, unit = _first_number_before(t, ("天", "日", "周", "星期", "个月", "月"))
    if n is None:
        n, unit = _first_number(t)
    if n is None:
        return None, "未识别到时长"
    if "周" in (unit or "") or "星期" in (unit or "") or "周" in t or "星期" in t:
        return int(n * 7 * 24), f"{n} 周 → {int(n*7*24)} 小时"
    if "月" in (unit or ""):
        return int(n * 30 * 24), f"{n} 月 → {int(n*30*24)} 小时"
    v = int(n * 24)
    return v, f"{int(n)} 天 → {v} 小时"


def norm_moq(text: str) -> tuple[Optional[int], str]:
    """最小起订量 → 件。'一件也做'→1, '最少100件'→100, '500起订'→500"""
    t = text.strip()
    if re.search(r"一件也?(做|行|接)|单件|1\s*件|不设(起订|门槛)|没有?起订", t):
        return 1, "一件也做 → MOQ=1"
    nums = _all_numbers(t)
    if nums:
        return int(nums[0]), f"MOQ = {int(nums[0])} 件"
    v = cn2int(t)
    if v is not None:
        return v, f"MOQ = {v} 件"
    return None, "未识别到起订量"


def norm_bool(text: str) -> tuple[Optional[bool], str]:
    """是否判断。'有'/'能做'/'能出'→True；'外发'/'不做'/'不能'→False

    否定词表覆盖生活服务业的说法（2026-09-11 门类扩展时补）：
    原先只认「能 / 有 / 可以」，餐饮问句里最常见的答案「收 / 不收」「要 / 不要」
    一律返回「无法判断是/否」—— 企业答了等于没答，字段留空，下游还以为是没问到。
    制造业里同样有受影响的问题（`service.nda_accepted` 提示企业答「接受」）。

    判定顺序**必须否定在前**：'不要' 含 '要'、'不收' 含 '收'，反过来就先命中肯定词了。
    同理「不收费」不该被判成 True（'收' 命中）—— 所以否定的正则要吃掉整个短语。
    """
    t = text.strip()
    # 否定先判，避免「没有」被后面的「有」命中
    if re.search(r"外[发协]|外包|不能|没法|不行|不是|不(做|接|提供|太|收|要|需|用|含|支持|接受)|"
                 r"没有|无|不确定|否|拒绝|谢绝|免[费收]|不需要|不收取", t):
        return False, f"「{t}」→ False"
    if re.search(r"能|可以|支持|有|自有|会|行|ok|OK|收|要|需要|含|接受|是|提供", t):
        return True, f"「{t}」→ True"
    return None, "无法判断是/否"


def norm_axis(text: str) -> tuple[Optional[int], str]:
    """轴数。'五轴'→5, '3+2'→5, '四轴'→4"""
    t = text.strip()
    if "3+2" in t or "3加2" in t:
        return 5, "3+2 轴 → 记 5 轴"
    n, _ = _first_number(t)
    if n is not None and n in (3, 4, 5):
        return int(n), f"{int(n)} 轴"
    if "五轴" in t or "5轴" in t:
        return 5, "五轴"
    if "四轴" in t or "4轴" in t:
        return 4, "四轴"
    if "三轴" in t or "3轴" in t:
        return 3, "三轴"
    return None, "未识别轴数"


def norm_power_w(text: str) -> tuple[Optional[float], str]:
    """激光功率 → W。'3000W'→3000, '3千瓦'→3000, '六千瓦'→6000"""
    t = text.strip()
    n, unit = _first_number(t)
    if n is None:
        return None, "未识别功率"
    if "千瓦" in t or "kw" in t.lower() or "KW" in t:
        return float(n * 1000), f"{n} 千瓦 → {int(n*1000)}W"
    return float(n), f"{int(n)}W"


def norm_tonnage(text: str) -> tuple[Optional[float], str]:
    """吨位。'160吨'→160, '800T'→800"""
    n, _ = _first_number(text)
    if n is None:
        return None, "未识别吨位"
    return float(n), f"{int(n)} 吨"


def norm_number_list(text: str) -> tuple[Optional[list], str]:
    """数字列表。'160、280、500、800'→[160,280,500,800]"""
    nums = _all_numbers(text)
    if not nums:
        return None, "未识别到数字列表"
    return [int(x) for x in nums], "、".join(str(int(x)) for x in nums)


def norm_equipment_list(text: str) -> tuple[Optional[list], str]:
    """
    设备清单。'加工中心8台、数控车床5台、五轴2台（德国DMG）'
    → [{'name':'加工中心','qty':8}, {'name':'数控车床','qty':5},
       {'name':'五轴','qty':2,'spec':'德国DMG'}]
    """
    parts = re.split(r"[、,，;；]|\s{2,}", text)
    out: list[dict] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        spec = None
        m = re.search(r"[（(]([^）)]+)[）)]", p)
        if m:
            spec = m.group(1).strip()
            p = re.sub(r"[（(][^）)]*[）)]", "", p)
        qty = None
        m = re.search(r"(\d+|[" + _CN_NUM_CHARS + r"]{1,4})\s*(台|部|条|套|个)", p)
        if m:
            qty = _to_num(m.group(1))
            p = p[: m.start()] + p[m.end():]
        name = re.sub(r"[的和共有大概约左右。.\s]", "", p).strip()
        if not name:
            continue
        item: dict[str, Any] = {"name": name}
        if qty is not None:
            item["qty"] = int(qty)
        if spec:
            item["spec"] = spec
        out.append(item)
    if not out:
        return None, "未识别到设备条目"
    note = f"识别 {len(out)} 项：" + "、".join(
        f"{i['name']}×{i.get('qty','?')}" for i in out
    )
    return out, note


# 口述列表里剥掉开头的引导词。
# ⚠ 原实现用正则 `主要?是?` —— 它匹配的是「主」+ 可选的「要」，**不是「主要」**。
# 于是「主题密室」被剥成「题密室」、「主营」不受影响、还得靠别的分支兜。
# 这类 bug 不报错、不返回 None，只是把数据悄悄改错，是最难被发现的一种。
# 修正三条：(1) 「主要/主营」写成完整词；(2) 长词排前面（正则逐个尝试，先命中者赢）；
# (3) 单字引导词碰到构词不许剥 —— 「有机食品」不能说成「机食品」。
_LEAD_WORDS = ("主要是", "主要做", "主营", "主要", "包括有", "包括", "也做点", "做点",
               "另外", "还有", "以及", "也上", "也做", "有", "做")
_LEAD_GUARD = ("有机", "有色金属", "有声", "有限", "做法")


def _strip_lead(p: str) -> str:
    if any(p.startswith(g) for g in _LEAD_GUARD):
        return p
    for w in _LEAD_WORDS:
        if p.startswith(w):
            return p[len(w):].strip()
    return p


# 拆条时「和」不能盲拆：「和面机」「和牛」「和田玉」会被拆成「面机」「牛」「田玉」。
# 拆错是往卡里写**不存在的条目**（编造），漏拆只是少一条（但仍是真话）——
# 两害相权，宁可漏拆。所以先把这些构词保护起来，拆完再还原。
_HE_COMPOUND = ("和面", "和牛", "和田", "和平", "和睦", "和谐", "和美",
                "和风", "和声", "和服", "和尚", "和硕", "和顺")


def _split_items(text: str) -> list[str]:
    saved: list[str] = []

    def _protect(m):
        saved.append(m.group(0))
        return "\x00%d\x00" % (len(saved) - 1)

    for g in _HE_COMPOUND:
        text = re.sub(re.escape(g), _protect, text)
    parts = re.split(r"[、,，;；/]|以及|还有|和|\s{2,}", text)
    out = []
    for p in parts:
        p = re.sub(r"\x00(\d+)\x00", lambda m: saved[int(m.group(1))], p).strip()
        if p:
            out.append(p)
    return out


def norm_list(text: str) -> tuple[Optional[list], str]:
    """普通字符串列表。'304、316L、6061'→['304','316L','6061']"""
    out = [_strip_lead(p) for p in _split_items(text)]
    out = [p for p in out if p]
    if not out:
        return None, "未识别到列表项"
    return out, "、".join(out)


def norm_multi_enum(text: str, enum: list) -> tuple[Optional[list], str]:
    """多选枚举匹配。'氩弧焊和点焊' + enum → ['氩弧焊','点焊']"""
    if not enum:
        return norm_list(text)
    hits = [e for e in enum if e in text]
    if hits:
        return hits, "匹配到 " + "、".join(hits)
    return None, f"未匹配到枚举项，可选值：{'、'.join(enum)}"


def _loose(s: str) -> str:
    """去掉分隔符与空白，用于容忍 '50到100人' vs 枚举 '50-100人' 这类写法差异。"""
    return re.sub(r"[\s\-–—~到至_]", "", str(s))


def norm_enum(text: str, enum: list) -> tuple[Optional[Any], str]:
    """单选枚举匹配。先精确后宽松（忽略分隔符差异）。"""
    if not enum:
        return norm_text(text)
    # 先找完整匹配（优先长词，避免 "自制" 命中 "自制+外协" 的子串问题）
    for e in sorted(enum, key=lambda x: -len(str(x))):
        if str(e) in text:
            return e, f"匹配到「{e}」"
    t = _loose(text)
    for e in sorted(enum, key=lambda x: -len(str(x))):
        if _loose(e) in t:
            return e, f"匹配到「{e}」（忽略分隔符差异）"
    return None, f"未匹配到枚举项，可选值：{'、'.join(str(e) for e in enum)}"


def norm_integer(text: str) -> tuple[Optional[int], str]:
    n, _ = _first_number(text)
    if n is None:
        return None, "未识别到整数"
    v = int(round(n))
    if "万" in text:
        v = int(n * 10000)
        return v, f"{n}万 → {v}"
    return v, str(v)


def norm_number(text: str) -> tuple[Optional[float], str]:
    n, _ = _first_number(text)
    if n is None:
        return None, "未识别到数值"
    if "万" in text:
        v = float(n * 10000)
        return v, f"{n}万 → {int(v)}"
    return float(n), str(n)


def norm_text(text: str) -> tuple[Optional[str], str]:
    t = text.strip()
    if not t:
        return None, "空输入"
    return t, "原样保留"


def norm_thread_standard(text: str) -> tuple[Optional[list], str]:
    return norm_multi_enum(text, ["公制", "英制", "管螺纹", "美制"])


# ---------------------------------------------------------------- 分发表


# 「没有 / 无 / 不填 / 不确定 …」这类回答：对**绝大多数**字段，它等于"没提供"，
# 所以 normalize() 见到就直接返回 None。这是对的。
#
# 但有个别字段，它恰恰是**有效答案**——最典型的是 `exclusions`（「有什么活是你们
# 明确不接的？」）：一家什么活都接的企业，诚实的回答就是"没有"。这类字段在定义里
# 带 `empty_is_answer: true`，由调用方（session.normalize_field）**先行截获**，
# 记成空列表（= 明确为空），而不是丢掉。
#
# 为什么必须显式区分：丢掉它不只是少一个字段。`exclusions` 是 required，
# 值丢了 → collect_confirm 的必填门禁永远通不过 → 整个登记流程卡死在一个
# **用户已经回答过**的问题上，且现场再也看不到是哪一题（2026-09-11 真机实测）。
#
# 这张表必须与 session.normalize_field 用的是同一份，否则会出现
# 「normalize 拦住、调用方不认」的静默不一致。
EMPTY_ANSWERS: tuple[str, ...] = ("不填", "没有", "无", "不确定", "不知道", "待定")

# 「确实没有」的口语变体。只在 `empty_is_answer` 字段上使用（见 is_empty_answer）。
#
# 为什么需要它：老板不会只说"没有"，更多是「暂时没有」「没有特别不接的」
# 「没什么不接的活」。这些如果不识别，就会被当成**列表项**记下来，
# 于是在 SKILL.md 的「我们不做的（边界）」里出现一条 `- 暂时没有`——
# 那是把"什么都接"写成了一条边界，属于轻微编造。
#
# 必须**只匹配"否定 + 无内容"**：`不做医疗器械`、`不做硬件` 是有内容的边界声明，
# 绝不能落进这里。所以 不做/不接 之后只允许出现 的/活/单/东西/业务/限制/要求。
_EMPTY_PAT = re.compile(
    r"^(?:暂时|目前|现在|基本|一般|通常|都|也)*"
    r"(?:没什么|没啥|没有|没|无)"
    r"(?:特别|其他|其它|别的)*"
    r"(?:不接|不做)?"
    r"(?:的)?"
    r"(?:活|单|东西|业务|限制|要求)?$"
)


def is_empty_answer(text: str) -> bool:
    """这句话是不是「明确说没有」。供 `empty_is_answer` 字段使用。"""
    t = (text or "").strip().rstrip("。.!！")
    return bool(t) and (t in EMPTY_ANSWERS or bool(_EMPTY_PAT.match(t)))


# bool 字段的「明确否」。
#
# 为什么必须单独一张表：`normalize()` 见到「没有 / 无」会直接返回 None（= 未提供），
# 于是问答里最自然的那句回答对企业答了等于没答。M 门类的问句甚至在提示里写着
# 「没有就说没有」，而那时的实现会把这句话丢掉 —— 属于「明确没有」被记成
# 「没问到」，正是本项目红线禁止的那种失真。
#
# 但**不能把 EMPTY_ANSWERS 整张表搬过来**：`不确定 / 不知道 / 待定 / 不填`
# 不是「否」，它们是真的没答案，必须留在 None。把「不知道」记成 False 是编造。
#
# 用全匹配（`$`）而非包含：`不锈钢`、`无线网`、`不动产` 都不该被判成否。
_BOOL_NO = re.compile(
    r"^(?:暂时|目前|现在|基本|一般|通常|都|也|确实|真|并)?"
    r"(?:没有|没|无|否|不是|不能|不行|不可以|不支持|不接受|不需要|不做|不接"
    r"|不提供|不含|不具备|未取得|未办)$"
)


def is_bool_no(text: str) -> bool:
    """bool 字段：这句话是不是**明确的「否」**（而不是「不知道」）。"""
    t = (text or "").strip().rstrip("。.!！")
    return bool(t) and bool(_BOOL_NO.match(t))


def normalize(kind: str, text: str, enum: Optional[list] = None) -> tuple[Any, str]:
    """
    按归一化类型处理文本。统一入口。

    返回 (value, note)；失败时 value 为 None。
    """
    t = (text or "").strip()
    if not t or t in EMPTY_ANSWERS:
        return None, "未提供"

    table = {
        "length_mm": lambda: norm_length_mm(t),
        "tolerance_mm": lambda: norm_tolerance(t),
        "size3": lambda: norm_size3(t),
        "range": lambda: norm_range(t),
        "days": lambda: norm_days(t),
        "hours": lambda: norm_hours(t),
        "moq": lambda: norm_moq(t),
        "bool": lambda: norm_bool(t),
        "axis": lambda: norm_axis(t),
        "power_w": lambda: norm_power_w(t),
        "tonnage": lambda: norm_tonnage(t),
        "number_list": lambda: norm_number_list(t),
        "equipment_list": lambda: norm_equipment_list(t),
        "list": lambda: norm_list(t),
        "integer": lambda: norm_integer(t),
        "number": lambda: norm_number(t),
        "text": lambda: norm_text(t),
        "thread_standard": lambda: norm_thread_standard(t),
    }
    if kind in ("multi_enum",):
        return norm_multi_enum(t, enum or [])
    if kind in ("enum",):
        return norm_enum(t, enum or [])
    fn = table.get(kind)
    if fn is None:
        return norm_text(t)
    return fn()


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    CASES = [
        ("tolerance_mm", "一般一丝吧，精细的能到半丝", None, 0.01),
        ("tolerance_mm", "半丝", None, 0.005),
        ("tolerance_mm", "±0.05", None, 0.05),
        ("tolerance_mm", "5丝", None, 0.05),
        ("size3", "八百乘六百，高度四百", None, [800, 600, 400]),
        ("size3", "800x600x400", None, [800, 600, 400]),
        ("size3", "长3米宽1.5米高0.02米", None, [3000, 1500, 20]),
        ("range", "0.5到20", None, [0.5, 20]),
        ("range", "10-5000", None, [10, 5000]),
        ("days", "打样五天", None, 5),
        ("days", "一周", None, 7),
        ("days", "半个月", None, 15),
        ("hours", "一天内回", None, 24),
        ("hours", "2小时", None, 2),
        ("moq", "一件也做，就是单价高点", None, 1),
        ("moq", "500起订", None, 500),
        ("equipment_list", "加工中心8台、数控车床5台、五轴2台（德国DMG）", None, None),
        ("axis", "有两台五轴", None, 5),
        ("power_w", "3千瓦", None, 3000.0),
        ("tonnage", "160吨", None, 160.0),
        ("integer", "300万点", None, 3000000),
        ("bool", "外发给别人做的", None, False),
        ("bool", "自己有", None, True),
        ("enum", "自制+外协", ["自制", "外协", "自制+外协"], "自制+外协"),
        ("multi_enum", "氩弧焊和点焊", ["氩弧焊", "激光焊", "点焊"], ["氩弧焊", "点焊"]),
        ("length_mm", "3米", None, 3000.0),
        ("length_mm", "0.5", None, 0.5),
    ]
    ok = fail = 0
    for kind, text, enum, expect in CASES:
        v, note = normalize(kind, text, enum)
        good = (expect is None and v is not None) or (v == expect)
        flag = "OK  " if good else "FAIL"
        if good:
            ok += 1
        else:
            fail += 1
        print(f"{flag} [{kind}] {text!r}\n       -> {v}   ({note})")
        if not good:
            print(f"       期望 {expect!r}")
    print(f"\n通过 {ok} / {ok+fail}")
