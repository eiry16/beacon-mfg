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
from pathlib import Path
from typing import Any, Optional

from .normalize import _CN_DIGITS, normalize

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = REPO_ROOT / "skills" / "profiles"
CODES_FILE = REPO_ROOT / "skills" / "schema" / "process-codes.json"

KNOWN_CERTS = [
    "ISO9001", "IATF16949", "ISO14001", "ISO13485", "AS9100", "ISO45001",
    "高新技术企业", "专精特新", "RoHS", "REACH", "UL", "CE",
]

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


# ---------------------------------------------------------------- 通用采集流程

COMMON_STEPS: list[dict] = [
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
             "question": "厂房多大面积？", "hint": "例：3000（指 3000 平米）", "required": False},
            {"path": "identity.export_markets", "label": "销售市场", "kind": "list",
             "question": "主要做国内还是也出口？",
             "hint": "例：国内、东南亚、欧美", "required": False},
        ],
    },
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
            {"path": "service.quote_inputs_required", "label": "询价所需材料", "kind": "list",
             "question": "客户询价一般要提供什么东西？",
             "hint": "例：3D 图、材质、数量、表面处理", "required": True},
            {"path": "service.quote_response_hours", "label": "报价响应", "kind": "hours",
             "question": "多久能回价？", "hint": "例：一天内", "required": True},
            {"path": "service.sample_policy", "label": "打样政策", "kind": "text",
             "question": "打样怎么收费？", "hint": "例：收费打样，批量下单返还", "required": False},
            {"path": "service.payment_terms", "label": "付款方式", "kind": "list",
             "question": "付款方式是什么？", "hint": "例：三成预付，发货前付清", "required": False},
        ],
    },
    {
        "title": "询价设置",
        "fields": [
            {"path": "rfq.endpoint", "label": "询价接收地址", "kind": "email_or_text",
             "question": "询价发到哪个邮箱？",
             "hint": "例：sales@example.com", "required": True},
            {"path": "exclusions", "label": "不接的活", "kind": "list",
             "question": "最后问一个：有什么活是你们明确不接的？",
             "hint": "★ 主动写边界能让客户 Agent 秒淘汰不匹配的询单，反而提高成交率",
             "required": True},
        ],
    },
]


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


def norm_cert_list(text: str) -> tuple[Optional[list], str]:
    hits = [c for c in KNOWN_CERTS if c.lower() in text.lower()]
    if not hits:
        if re.search(r"没有|无|没做|没[有]?认证", text):
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
    "cert_list": norm_cert_list,
    "percent": norm_percent,
    "email_or_text": norm_email_or_text,
}


def normalize_field(field: dict, text: str) -> tuple[Any, str]:
    """
    按字段定义归一化。返回 (value, note)。

    归一化器名称兼容两种键名：COMMON_STEPS 用 "kind"，profiles/*.json 用 "normalize"。
    """
    kind = field.get("kind") or field.get("normalize") or "text"
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


# ---------------------------------------------------------------- 会话


class CollectSession:
    """一次采集会话：按步骤逐字段提问，把回答归一化成结构化数据。"""

    def __init__(self, supplier_id: str, company: str, category: str,
                 profile: str = None):
        self.supplier_id = supplier_id
        self.company = company
        self.category = category
        self.profile = profile or self._guess_profile(category)
        self.data: dict = {}
        self.notes: dict[str, str] = {}
        self.raw: dict[str, str] = {}
        self.steps: list[dict] = self._build_steps()
        self.i = 0  # 当前字段在所有字段中的序号

    # -- profile ------------------------------------------------------
    @staticmethod
    def _guess_profile(category: str) -> str:
        mapping = {
            "精密机械加工": "precision-machining",
            "钣金冲压": "sheet-metal",
            "注塑成型": "injection-molding",
            "压铸": "die-casting",
            "电子元器件": "electronics",
            "表面处理": "surface-treatment",
            "标准件": "standard-parts",
            "原材料": "raw-materials",
        }
        return mapping.get(category, "custom")

    def load_profile(self) -> dict:
        f = PROFILE_DIR / f"{self.profile}.json"
        if not f.exists():
            return {"profile": self.profile, "fields": [], "conflict_rules": []}
        return json.loads(f.read_text(encoding="utf-8"))

    def _build_steps(self) -> list[dict]:
        """通用流程 + 品类字段。品类字段插到「硬边界」之后。"""
        steps = [dict(s) for s in COMMON_STEPS]
        prof = self.load_profile()
        pfields = prof.get("fields", [])
        if pfields:
            cat_step = {
                "title": f"品类能力 · {prof.get('category', self.profile)}",
                "fields": pfields,
                "from_profile": True,
            }
            # 插到「硬边界」后面（index 2 之后）
            steps.insert(3, cat_step)
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

    def conflicts(self) -> list[dict]:
        """跑冲突规则：Profile 规则 + 内置规则。"""
        out: list[dict] = []
        for r in self.load_profile().get("conflict_rules", []):
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
        # 以下规则目前只做提示，不判失败
        return False
