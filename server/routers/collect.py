"""对话式采集 API（PROPOSAL_V2 §4.3）

这是全案价值最高的一招：**制造业老板不会写 JSON，也不会填 40 个字段的表单，
但他会回答问题。** 把填表成本从 2 小时压到 15 分钟对话，冷启动才有可能。

采集引擎（scripts/collect）早就跑通了，但只接了命令行——供应商没法用，
平台 Agent 也没法驱动。本路由把它接到 HTTP 上，让一次对话能真正填出硬指标。

三条红线（沿用能力卡纪律，不因"采集"这个场景放松）：

1. **不编造数字。** 归一化不出口述里没有的数值；识别不出就是 null，留待补填。
2. **自动预填 ≠ 供应商自述。** 由公司名/名录推断出来的字段，在
   `provenance.inferred_fields` 里逐条列出，**不进 `evidence.self_declared`**。
   只有会话中真正回答过的字段才算自述。混淆二者违反"弱证据不覆盖强证据"。
3. **没有必填项和未决冲突，不许上架。** `confirm` 前必须必填齐、无 error 级冲突，
   否则明确报缺什么，不生成一个看起来完整的空壳卡。

会话状态落盘在 server/.data/collect/{supplier_id}.json，
一次对话可以跨请求、跨天继续（老板聊到一半去车间了，回来接着答）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from auth.api_key import bearer_scheme
from config import BASE_DIR
from loaders import supplier_loader
# 会话存储：D1（云端真源）为主、本地磁盘降级。见 services/session_store.py
from services.session_store import store

# 采集引擎在 scripts/ 下，以包的形式导入（session.py 内部用相对导入）
SCRIPTS_DIR = BASE_DIR.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from collect.render import (  # noqa: E402
    append_fingerprint, render_capability, render_fingerprint,
    render_skill_md, write_vendor_skill,
)
from collect.session import CollectSession, normalize_field  # noqa: E402

router = APIRouter(prefix="/v1/collect", tags=["collect"])

CST = timezone(timedelta(hours=8))

COLLECT_DIR = BASE_DIR / ".data" / "collect"
COLLECT_DIR.mkdir(parents=True, exist_ok=True)

# 认证建档目录（server/routers/certification.py 的 CERT_DIR，同一处）。
# 企业主动注册时名录里本来就没有它，资料先落在这里 —— 采集必须认得这类主体，
# 否则"注册"只是生成了一个谁也用不上的号，等于没注册。
CERT_DIR = BASE_DIR / ".data" / "certification"

CAPABILITY_DIR = BASE_DIR.parent / "skills" / "registry" / "capability"
VENDOR_DIR = BASE_DIR.parent / "skills" / "vendors"


def _err(code: str, message: str, details: dict | None = None, status_code: int = 400):
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": details}},
    )


async def require_operator(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> str:
    """采集会话的操作方指纹。

    生产环境应绑定认主凭证（claim_token），只允许供应商本人或平台采集 Agent 操作。
    认主流程尚未实现，现阶段只做可追溯：存证里留指纹，不存明文密钥。
    """
    import hashlib

    if credentials is None:
        _err("UNAUTHORIZED", "采集会话要求 Bearer 凭证（写入能力数据必须可追溯）",
             None, status.HTTP_401_UNAUTHORIZED)
    return hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()[:12]


# ─── 会话状态持久化 ──────────────────────────────────────────────────────────

def _state_path(sid: str) -> Path:
    return COLLECT_DIR / f"{sid}.json"


def _atomic_write(path: Path, obj: dict) -> None:
    import tempfile

    data = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _persist_or_500(sid: str, st: dict) -> None:
    try:
        # D1 优先（云端真源）；D1 不可用时 store 内部降级到本地并打 WARNING
        store.save(sid, st)
    except Exception as exc:
        _err("PERSIST_FAILED", "采集会话未能写入，本次回答不予确认",
             {"supplier_id": sid, "type": type(exc).__name__},
             status.HTTP_500_INTERNAL_SERVER_ERROR)


def _rebuild(st: dict) -> CollectSession:
    """从落盘状态还原会话对象（引擎本身是无状态遍历器）。"""
    # gate 优先用落盘状态里的（会话创建时就定死），没有再按 supplier_id 前缀回判。
    # 不这样做的话，一个落盘时是 H 门类的会话，只要 supplier_id 前缀看着像制造业，
    # 还原后就会拿制造业问句继续问 —— 问到一半换了题库，现场完全看不出原因。
    ses = CollectSession(st["supplier_id"], st["company"], st["category"],
                         st.get("profile"), gate=st.get("gate"))
    ses.data = st.get("data") or {}
    ses.notes = st.get("notes") or {}
    ses.raw = st.get("raw") or {}
    ses.i = st.get("i", 0)
    return ses


def _name_key(s: str) -> str:
    """公司名归一化。**复用认证侧的口径**（certification.normalize_name）。

    两处各写一份"名字怎么算一样"，迟早分叉：一边认为「上海耐特斯传输设备有限公司」
    和「耐特斯传输设备（上海）有限公司」是同一家、另一边不认，于是同一家企业在
    "会话归属"和"主体核验"上得到相反结论。懒加载 import 是为了避开循环依赖
    （certification 反过来要用本模块的 confirmed_capability）。
    """
    try:
        from routers.certification import normalize_name
    except Exception:  # 极少数导入失败场景：退化成保守口径，宁可判为"不同"
        import re as _re

        return _re.sub(r"[\s（）()]", "", s or "").lower()
    return normalize_name(s)


def _progress(ses: CollectSession) -> dict:
    cur = ses.current()
    return {
        "index": ses.i,
        "total": len(ses.flat),
        "step": cur[0] if cur else None,
        "finished": cur is None,
    }


def _state_payload(sid: str, ses: CollectSession, st: dict) -> dict:
    comp = ses.completeness()
    conflicts = ses.conflicts()
    missing = [f["path"] for _, f in ses.flat
               if f.get("required") and _get(ses.data, f["path"]) is None]
    prefilled = set(st.get("prefilled") or [])
    confirmed = set(st.get("confirmed") or [])
    return {
        "supplier_id": sid,
        "company": ses.company,
        "category": ses.category,
        "profile": ses.profile,
        "progress": _progress(ses),
        # 带上当前问题，调用方不必为了知道"现在问到哪"而发一次消耗性的请求
        "next_question": ses.next_question(),
        "completeness": comp,
        "conflicts": conflicts,
        "missing_required": missing,
        "prefilled_unconfirmed": sorted(prefilled - confirmed),
        "confirmed_fields": sorted(confirmed),
        "ready_to_confirm": not missing
        and not any(c["level"] == "error" for c in conflicts),
        "updated_at": st.get("updated_at"),
        # 会话实际存在哪：d1 = 云端真源，local = 降级到本地磁盘。
        # **必须回给调用方**：不写这一条，D1 挂了也没人知道会话其实没上云。
        "storage": store.backend,
    }


def _get(data: dict, path: str) -> Any:
    cur = data
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _load_state(sid: str) -> dict:
    st = store.get(sid)
    if st is None:
        _err("SESSION_NOT_FOUND",
             f"{sid} 没有进行中的采集会话，请先 POST /v1/collect/session",
             {"supplier_id": sid}, status.HTTP_404_NOT_FOUND)
    return st


def _pending_registration(sid: str) -> dict | None:
    """名录里没有这家企业时，去认证建档目录找它。

    为什么需要这一步：企业主动注册（`POST /v1/certify/apply`）时，名录里**本来就没有它**，
    平台会新分配一个 supplier_id。这类主体必须能立刻开始采集资料，
    否则「注册」就只是生成了一个谁也用不上的号。

    返回 None = 既不在名录、也没有建档记录 —— 那才是真的查无此主体，该报 404。
    """
    if not CERT_DIR.exists():
        return None
    for f in CERT_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("supplier_id") == sid:
            return {
                "company": d.get("company") or "",
                "category": d.get("category") or "",
                "gate": d.get("gate") or "",
                "source": "certification",
                "app_id": d.get("app_id"),
            }
    return None


# ─── 请求/响应模型 ───────────────────────────────────────────────────────────

class CollectSessionCreate(BaseModel):
    supplier_id: str
    profile: Optional[str] = None
    reset: bool = Field(default=False, description="丢弃已存在的会话重新开始")


class CollectTurn(BaseModel):
    text: Optional[str] = Field(default=None, description="供应商的口述回答")
    skip: bool = False
    back: bool = False


class CollectConfirmBody(BaseModel):
    overwrite_existing: bool = Field(
        default=False,
        description="已存在 vendor_claimed / fixture 卡时，需显式确认才覆盖")


# ─── POST /v1/collect/session ────────────────────────────────────────────────

@router.post("/session")
async def create_session(body: CollectSessionCreate,
                         operator: str = Depends(require_operator)):
    """创建（或继续）一次采集会话。"""
    sid = body.supplier_id
    rec = supplier_loader.get_supplier(sid)
    # 名录里没有 ≠ 这家企业不存在。企业主动注册时名录里本来就没有它，
    # 资料先在认证建档目录里 —— 这类主体同样要能采集资料。
    registered = None if rec is not None else _pending_registration(sid)
    if rec is None and registered is None:
        _err("SUPPLIER_NOT_FOUND", f"供应商 '{sid}' 不存在",
             {"supplier_id": sid}, status.HTTP_404_NOT_FOUND)

    # 会话态在 D1（云端真源），本地磁盘只是降级副本 —— 见 services/session_store.py
    src = rec if rec is not None else (registered or {})
    company = src.get("company") or ""
    category = src.get("category") or ""

    st = store.get(sid)
    stale_from = None
    if st is not None and not body.reset:
        # supplier_id 会被回收复用（新注册时按名录最大号 +1 分配）。一家测试/退场企业的
        # **采集会话**如果没被一起清掉，新企业拿到同一个号就会续上一个别人的会话：
        # 现场看起来"题都答完了"，实际答的是另一家公司的内容，而**这些答案会原样
        # 进它的能力卡**（2026-09-11 真机实测：ZZTEST 的会话被赤兔续上，
        # company 字段还写着 ZZTEST）。
        #
        # 判据：两边公司名都能取到、且归一化后不一致。
        # 只在不一致时重建——同名主体的"跨天续答"是既有功能，不能误伤；
        # 任一侧取不到名字时也保持原行为（fail-safe：宁可续，不要凭一个字段就丢数据）。
        old_company = (st.get("company") or "").strip()
        if company and old_company and _name_key(company) != _name_key(old_company):
            stale_from = old_company
            print(f"[collect] {sid} 已有会话属于「{old_company}」，与当前主体「{company}」"
                  f"不一致（编号复用导致串档），重建会话", flush=True)
        else:
            ses = _rebuild(st)
            return {
                "supplier_id": sid,
                "resumed": True,
                "next_question": ses.next_question(),
                "state": _state_payload(sid, ses, st),
            }

    # 门类来源优先级：名录/注册资料里的显式 gate > supplier_id 前缀回判。
    # 让名录能纠正"前缀看不出门类"的情况（例如制造业名录里混进的贸易/服务主体）。
    ses = CollectSession(sid, company, category, body.profile,
                         gate=src.get("gate"))
    st = {
        "supplier_id": sid,
        "gate": ses.gate,
        "company": company,
        "category": category,
        "profile": ses.profile,
        "data": {},
        "notes": {},
        "raw": {},
        "i": 0,
        "prefilled": [],
        "confirmed": [],
        "operator": operator,
        "created_at": datetime.now(CST).isoformat(),
        "updated_at": datetime.now(CST).isoformat(),
    }
    _persist_or_500(sid, st)
    return {
        "supplier_id": sid,
        "resumed": False,
        # 串档被识别并重建时，把原公司名如实回给调用方 —— 调用方（App）要据此
        # 告诉用户"之前那份会话不是你的，已重新开始"，而不是假装什么都没发生。
        "stale_session_reset": stale_from,
        "next_question": ses.next_question(),
        "state": _state_payload(sid, ses, st),
    }


# ─── POST /v1/collect/{sid}/autofill ─────────────────────────────────────────

@router.post("/{supplier_id}/autofill")
async def autofill(supplier_id: str, operator: str = Depends(require_operator)):
    """通道 A：自动预填。

    只填两类**有据可查**的东西，其余一律留空：

    - `processes` ← 公司名（强证据：厂名里写了做什么，如"精密钣金"）
    - `identity.city/province/address/website` ← 名录 POI 字段

    **预填不等于供应商确认过。** 这些路径会进 `provenance.inferred_fields`，
    在 confirm 之前不算 `self_declared`。会话仍会逐题走一遍，让老板确认或改口。
    """
    sid = supplier_id
    st = _load_state(sid)
    ses = _rebuild(st)

    rec = supplier_loader.get_supplier(sid)
    if rec is None:
        # 新注册主体：名录里还没有它，但建档记录里有公司名 ——
        # 「公司名 → 主营工艺」这条预填靠的就是公司名，不能因为没进名录就白丢。
        # 只补 company/category：名录 POI（地址、经纬度、官网）确实没有，那就是没有，
        # 不许拿别的东西顶替。
        rec = _pending_registration(sid) or {}
    filled: list[dict] = []

    # 1) 公司名 → 主营工艺（只在 processes 字段存在时填）
    proc_field = next((f for _, f in ses.flat if f["path"] == "processes"), None)
    if proc_field and _get(ses.data, "processes") is None:
        name = rec.get("company") or ""
        if name:
            value, note = normalize_field(proc_field, name)
            if value:
                ses.data["processes"] = value
                ses.notes["processes"] = note
                filled.append({"path": "processes", "value": value,
                               "source": "company_name", "note": note,
                               "confidence": "medium"})
                st.setdefault("prefilled", [])
                if "processes" not in st["prefilled"]:
                    st["prefilled"].append("processes")

    # 2) 名录字段 → 身份信息（地址/城市/官网来自 POI，不是老板说的）
    region = rec.get("region") or {}
    mapping = {
        "identity.city": region.get("city"),
        "identity.province": region.get("province"),
        "identity.address": rec.get("address"),
        "identity.website": rec.get("website"),
    }
    for path, value in mapping.items():
        if not value or _get(ses.data, path) is not None:
            continue
        cur = ses.data
        parts = path.split(".")
        for pp in parts[:-1]:
            cur = cur.setdefault(pp, {})
        cur[parts[-1]] = value
        filled.append({"path": path, "value": value,
                       "source": "directory_record", "note": "来自名录 POI 字段",
                       "confidence": "medium"})
        st.setdefault("prefilled", [])
        if path not in st["prefilled"]:
            st["prefilled"].append(path)

    st["updated_at"] = datetime.now(CST).isoformat()
    _persist_or_500(sid, st)

    return {
        "supplier_id": sid,
        "filled": filled,
        "count": len(filled),
        "note": ("以上字段均为平台推断/名录来源，未经供应商确认；"
                 "confirm 时会计入 provenance.inferred_fields，不算自述。"),
        "next_question": ses.next_question(),
        "state": _state_payload(sid, ses, st),
    }


# ─── POST /v1/collect/{sid}/turn ─────────────────────────────────────────────

@router.post("/{supplier_id}/turn")
async def turn(supplier_id: str, body: CollectTurn,
               operator: str = Depends(require_operator)):
    """提交一轮对话。

    `text` 是老板的原话，交给归一化引擎（"一丝"→0.01mm、"八百乘六百"→[800,600,400]）。
    归一化失败不阻塞：值记为 null，字段留待补填，不会为了凑数编一个。
    """
    sid = supplier_id
    st = _load_state(sid)
    ses = _rebuild(st)

    if body.back:
        if not ses.back():
            _err("AT_FIRST_QUESTION", "已经是第一题了", {"index": ses.i})
        st["i"] = ses.i
        st["updated_at"] = datetime.now(CST).isoformat()
        _persist_or_500(sid, st)
        return {"supplier_id": sid, "action": "back", "extracted": None,
                "next_question": ses.next_question(),
                "state": _state_payload(sid, ses, st)}

    if ses.current() is None:
        _err("COLLECT_FINISHED", "所有问题已答完，请调用 confirm 生成能力卡",
             {"supplier_id": sid})

    if body.skip or not (body.text or "").strip():
        _, cur_field = ses.current()
        cur_path = cur_field["path"]
        # 必填字段**不许静默跳过**。跳掉它并不会让流程往前走一步 —— 它只是把定稿
        # 卡死在 confirm 的必填门禁上，而且那时候现场已经看不出是哪一题缺了
        # （i 已走到末尾，"当前题"变成 None）。这是 2026-09-11 真机卡死的第二入口：
        # 用户在「不接的活」上答"没有"被丢弃，换个人用 skip 也一样卡。
        #
        # 拒绝要给出**可执行的补救说法**，不能只说"不能跳过"：
        # 对声明了 empty_is_answer 的字段（"确实没有"本身是合法答案），
        # 明确告诉对方"说没有就行"。
        if cur_field.get("required"):
            hint = (
                "如果确实没有，请明确回一句「没有」——会记为「暂无」，不是「未提供」。"
                if cur_field.get("empty_is_answer")
                else "这一项不能留空，请让对方给一个答复（答不上来也要说清是哪一种答不上来）。"
            )
            _err(
                "REQUIRED_NOT_SKIPPABLE",
                f"「{cur_field['label']}」是必填项，不能跳过——跳过会让定稿永远被拒。{hint}",
                {"path": cur_path, "label": cur_field["label"],
                 "question": cur_field["question"],
                 "empty_is_answer": bool(cur_field.get("empty_is_answer"))},
            )
        ses.skip()
        st["i"] = ses.i
        st["updated_at"] = datetime.now(CST).isoformat()
        _persist_or_500(sid, st)
        return {"supplier_id": sid, "action": "skipped",
                "extracted": {"path": cur_path, "parsed": False,
                              "note": "已跳过，该字段留空"},
                "next_question": ses.next_question(),
                "state": _state_payload(sid, ses, st)}

    result = ses.answer(body.text.strip())
    # 真正在会话里回答过 → 才算供应商自述（预填的不算）
    st.setdefault("confirmed", [])
    if result["parsed"] and result["path"] not in st["confirmed"]:
        st["confirmed"].append(result["path"])

    st["data"] = ses.data
    st["notes"] = ses.notes
    st["raw"] = ses.raw
    st["i"] = ses.i
    st["updated_at"] = datetime.now(CST).isoformat()
    _persist_or_500(sid, st)

    return {
        "supplier_id": sid,
        "action": "answered",
        "extracted": result,
        "next_question": ses.next_question(),
        "state": _state_payload(sid, ses, st),
    }


# ─── POST /v1/collect/{sid}/fix-missing ──────────────────────────────────────

@router.post("/{supplier_id}/fix-missing")
async def fix_missing(supplier_id: str, operator: str = Depends(require_operator)):
    """把游标**退回第一个必填空缺的题**上，让企业补答。

    为什么必须有这个端点：`confirm` 的必填门禁一旦不过，调用方**无路可走**。
    会话已经走到末尾，`turn` 一律回 COLLECT_FINISHED；`back` 一次只退一步，
    而缺的字段可能在任意位置（历史上被 skip 掉的题都不在末尾）。
    结果是：用户回答过了、平台却没记下、而且没有任何手段补救 —— 只能删会话重来。

    幂等：没有缺失就原样返回，`moved: false`，不产生任何写入副作用。
    """
    sid = supplier_id
    st = _load_state(sid)
    ses = _rebuild(st)
    state = _state_payload(sid, ses, st)

    if not state["missing_required"]:
        return {
            "supplier_id": sid,
            "moved": False,
            "missing_required": [],
            "next_question": ses.next_question(),
            "state": state,
        }

    # 缺失字段里取在 flat 中**序号最小**的那个：让补答按原来的提问顺序走，
    # 而不是按"哪个缺得最晚"。顺序稳定，重跑结果一致。
    indexes = {f["path"]: i for i, (_, f) in enumerate(ses.flat)}
    target_path = min(state["missing_required"], key=lambda p: indexes.get(p, 1 << 30))
    ses.i = indexes.get(target_path, ses.i)
    st["i"] = ses.i
    st["updated_at"] = datetime.now(CST).isoformat()
    _persist_or_500(sid, st)

    cur = ses.current()
    print(f"[collect] {sid} 必填缺失 {state['missing_required']} → 退回到第 {ses.i} 题 "
          f"{target_path}", flush=True)
    return {
        "supplier_id": sid,
        "moved": True,
        "missing_required": state["missing_required"],
        "next_question": ses.next_question(),
        "state": _state_payload(sid, ses, st),
    }


# ─── GET /v1/collect/{sid}/state ─────────────────────────────────────────────

@router.get("/{supplier_id}/state")
async def get_state(supplier_id: str, operator: str = Depends(require_operator)):
    """当前完整度、缺失必填、冲突项、预填未确认的字段。"""
    sid = supplier_id
    st = _load_state(sid)
    ses = _rebuild(st)
    return _state_payload(sid, ses, st)


def confirmed_capability(supplier_id: str) -> Optional[dict]:
    """已**定稿**的能力卡（认证档案要拿它做完成度判定与 L3 佐证判定）。

    只认定稿产物：采集会话里的草稿不算——半成品参与灯牌判定，等于让材料
    自己给自己打分。
    """
    f = CAPABILITY_DIR / f"{supplier_id}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cert_app(supplier_id: str) -> Optional[dict]:
    """这家企业的认证档案（取最近更新的一份）。没有就返回 None。

    口径与 `certification.find_app_by_supplier` 完全一致——**故意共用同一个函数**：
    两处各写一份"怎么找档案"的逻辑，迟早会分叉（一边取最新、一边取第一个），
    然后同一家企业在两个界面上显示不同的灯牌。
    """
    try:
        from routers.certification import find_app_by_supplier
    except Exception:
        return None
    return find_app_by_supplier(supplier_id)


def _declared_contact(supplier_id: str) -> dict:
    """企业自己在认证流程里申报的联系方式。

    名录里没有这家（新注册）时，这是**唯一**的联系方式来源。企业既然自己走完了
    认证流程，这份自述就该进能力卡——留空反而是把已有信息丢掉。
    来源会由 render_capability 标进 evidence.self_declared，不冒充公开记录。
    """
    app = _cert_app(supplier_id)
    if not app:
        return {}
    ident = app.get("identity") or {}
    return {
        "phone": ident.get("contact_phone"),
        "email": ident.get("contact_email"),
        "address": ident.get("claimed_address"),
        "name": ident.get("contact_name"),
        "app_id": app.get("app_id"),
    }


# ─── POST /v1/collect/{sid}/confirm ──────────────────────────────────────────

@router.post("/{supplier_id}/confirm")
async def confirm(supplier_id: str, body: CollectConfirmBody,
                  operator: str = Depends(require_operator)):
    """确认生成能力卡 + SKILL.md，并同步进检索链路。

    门禁（不通过就不生成，而不是生成一个看起来完整的空壳）：
      1. 必填字段必须齐
      2. 无 error 级冲突
      3. 已存在人工/认主卡时必须显式 overwrite_existing
    """
    sid = supplier_id
    st = _load_state(sid)
    ses = _rebuild(st)
    state = _state_payload(sid, ses, st)

    if state["missing_required"]:
        # 只说"必填未齐"等于没说：调用方（App/模型）既不知道缺哪些、也不知道怎么补救，
        # 只能反复重试同一个必然失败的调用。missing_required 是**路径**，
        # 这里补上人能读的 label/question，外加一条可执行的补救路径。
        by_path = {f["path"]: f for _, f in ses.flat}
        _err(
            "MISSING_REQUIRED",
            "必填字段未齐，不能生成完整能力卡",
            {
                "missing_required": state["missing_required"],
                "missing_labels": [by_path[p]["label"] for p in state["missing_required"]
                                   if p in by_path],
                "missing_questions": [by_path[p]["question"] for p in state["missing_required"]
                                      if p in by_path],
                "how_to_fix": (
                    "调用 POST /v1/collect/{sid}/fix-missing 把流程退回到第一个缺的题上，"
                    "补答完再 confirm。注意：字段确实为空（如「不接的活」企业说没有）"
                    "时要**明确回答「没有」**，那会被记为「暂无」而不是「未提供」。"
                ),
            },
        )

    errors = [c for c in state["conflicts"] if c["level"] == "error"]
    if errors:
        _err("UNRESOLVED_CONFLICTS",
             "存在未解决的冲突，请先修正再确认",
             {"conflicts": errors})

    # 自动卡可以被采集结果覆盖（这是升级）；人工/认主/样板卡必须显式确认
    existing = None
    cap_file = CAPABILITY_DIR / f"{sid}.json"
    if cap_file.exists():
        try:
            existing = json.loads(cap_file.read_text(encoding="utf-8"))
        except Exception:
            existing = None
    prev_mode = ((existing or {}).get("provenance") or {}).get("mode")
    if prev_mode in ("vendor_claimed", "fixture", "manual_reviewed") and not body.overwrite_existing:
        _err("EXISTING_CARD_PROTECTED",
             f"该供应商已有一张 {prev_mode} 卡，覆盖需显式传 overwrite_existing=true",
             {"supplier_id": sid, "existing_mode": prev_mode})

    base = supplier_loader.get_supplier(sid) or {}
    # 灯牌只能从材料算出来。以前 render_capability 硬编码 L1 —— 只注册、连手机号
    # 都没验过的企业，出去的卡也顶着「已认领」。现在从认证档案实算；
    # 没有档案就如实 L0（未认领），而不是给一个好看的默认值。
    cert = _cert_app(sid)
    claim = None
    if cert is not None:
        try:
            from routers.certification import claim_block

            claim = claim_block(cert)
        except Exception as exc:
            print(f"[collect] 灯牌计算失败，按未认领处理：{exc}")
    cap = render_capability(ses, base, declared=_declared_contact(sid), claim=claim)

    # ── 红线：预填未确认的字段不算供应商自述 ──
    prefilled = set(st.get("prefilled") or [])
    confirmed = set(st.get("confirmed") or [])
    inferred = sorted(prefilled - confirmed)
    cap["provenance"] = {
        "mode": "agent_collected",
        "source": "vendor_interview",
        "confidence": "high" if not inferred else "medium",
        "inferred_fields": inferred,
        "note": (f"采集会话产出；{len(confirmed)} 项为供应商口述确认"
                 + (f"，{len(inferred)} 项为平台预填待确认" if inferred else "")),
    }
    ev = cap.get("evidence") or {}
    # session.raw 里的路径都是真正回答过的；预填路径不在这里。
    # **并集而不是覆盖**：render_capability 已经把「企业自己申报的联系方式」
    # （contact.phone / contact.address 等，来自认证档案）写进 self_declared 了，
    # 直接赋值会把那几条静默抹掉——卡上就再也看不出电话是企业自报的。
    ev["self_declared"] = sorted(set(ev.get("self_declared") or [])
                                 | {p for p in ses.raw if p not in prefilled})
    cap["evidence"] = ev

    skill_md = render_skill_md(cap)

    # ── 校验：按**卡片自己的门类**校验 ──
    #
    # 这里原来读的是 `skills/schema/vendor-skill.schema.json` —— 门类扩展**之前**的
    # 单份制造业 schema。它的顶层是 `additionalProperties: false`，不认识 gate /
    # caps_index，而 render_capability 现在两个都会写。于是每一张定稿卡都被判
    # `Additional properties are not allowed ('gate', 'caps_index' were unexpected)`
    # → confirm 全部 422。**一条真实的企业采集链路被整条掐断**，而且报错说的是
    # 「capability.json 未通过 Schema 校验」，现场完全看不出是校验器用错了版本
    # （2026-09-11 由 test_api_collect 场景 D 暴露；此前该测试因指纹路径退役而
    #  FileNotFoundError，压根跑不到这一步，所以问题被藏了很久）。
    #
    # 门类扩展的整个意义就是「一份信封 + 每门类一份私有 schema」，校验必须跟着走，
    # 不能再拿制造业那份硬套。schema 的单一来源是 gate_schema.build_schema。
    validated: Optional[bool]
    validation_error: Optional[str] = None
    try:
        import gate_schema as _gs
    except ImportError:
        validated = None
        validation_error = "gate_schema 不可用，未做 Schema 校验"
    else:
        _ok, _errs = _gs.validate_card(cap)
        if not _ok:
            _payload = []
            for e in _errs[:5]:
                loc, _, msg = e.partition(": ")
                _payload.append({
                    "path": [] if loc in ("", "(root)") else loc.split("/"),
                    "message": msg or e,
                })
            _err("CAPABILITY_INVALID",
                 "生成的 capability.json 未通过 Schema 校验",
                 {"errors": _payload, "gate": cap.get("gate")},
                 status.HTTP_422_UNPROCESSABLE_ENTITY)
        validated = True

    paths = write_vendor_skill(cap, skill_md)
    score = state["completeness"]["score"]
    fp = render_fingerprint(cap, score)
    fp_path = append_fingerprint(cap, score)

    # 同步进检索链路：L1 capability / L0 指纹 / 名录 agent 字段
    sync_note = None
    try:
        from sync_vendor_skills import rebuild_registry_index, sync_one
        ok, detail = sync_one(sid)
        sync_note = detail if ok else f"同步失败：{detail}"
        if ok:
            rebuild_registry_index()
    except Exception as exc:
        sync_note = f"同步异常（能力卡已写入，需手动跑 sync_vendor_skills.py）：{exc}"

    # 发布到检索云：刷新 manifest SHA1 + 提交 + 推送（L1 卡另走 Pages）。
    # 这一步解决「灯牌亮了但手机搜不到」——指纹已落盘，但 manifest 的 h 还是旧哈希，
    # 手机比对后判定无更新会跳过下载。刷新 h 并提交推送后，手机联网更新即可搜到。
    # 失败也不影响 confirm 本身（能力卡已落盘），仅记入 publish_note 提示人工补跑。
    publish_note = None
    try:
        from publish_registry import publish
        pres = publish(apply=True, push=True)
        publish_note = pres["message"]
    except Exception as exc:
        publish_note = f"发布异常（指纹已落盘，需手动跑 scripts/publish_registry.py）：{exc}"

    st["confirmed_at"] = datetime.now(CST).isoformat()
    st["confirmed_by"] = operator
    st["updated_at"] = datetime.now(CST).isoformat()
    _persist_or_500(sid, st)

    return {
        "supplier_id": sid,
        "company": cap.get("company"),
        "status": "generated",
        "provenance": cap["provenance"],
        "completeness": state["completeness"],
        "inferred_fields": inferred,
        "self_declared_count": len(ev["self_declared"]),
        "validated": validated,
        "validation_error": validation_error,
        "written": {
            "capability": str(paths.get("capability")),
            "skill": str(paths.get("skill")),
            "fingerprint": str(fp_path),
        },
        "fingerprint": fp,
        "sync": sync_note,
        "publish": publish_note,
    }
