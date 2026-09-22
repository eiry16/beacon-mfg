"""协议内核 —— 跨行业不变的部分。

新增行业时若需要修改本文件，说明抽象漏了，应回去重拆 industry/ 与 schema/。

本文件同时托管认证三方验证的判定：一条认证「作数」当且仅当
verified == True 且 cert_no 非空（证书号已上传并被核验）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_json(rel_path: str) -> dict:
    with open(ROOT / rel_path, encoding="utf-8") as f:
        return json.load(f)


def load_industry(pack_id: str) -> dict:
    return load_json(f"industry/{pack_id}.json")


def load_audience(profile_id: str) -> dict:
    return load_json(f"audience/{profile_id}.json")


def load_suppliers() -> list[dict]:
    out = []
    for p in sorted((ROOT / "suppliers").glob("*.json")):
        with open(p, encoding="utf-8") as f:
            out.append(json.load(f))
    return out


# ---------------------------------------------------------------- provenance

def stated(note: str | None = None) -> dict:
    return {"source": "stated", "confidence": 1.0, "confirmed": True, "note": note}


def inferred(confidence: float, note: str) -> dict:
    """推断值。confirmed 恒为 False —— 未经回显确认不得进入正式询价。"""
    return {"source": "inferred", "confidence": round(confidence, 2),
            "confirmed": False, "note": note}


def default(note: str) -> dict:
    return {"source": "default", "confidence": 0.5, "confirmed": False, "note": note}


def unknown(note: str = "客户未指定") -> dict:
    return {"source": "unknown", "confidence": 0.0, "confirmed": False, "note": note}


# ------------------------------------------------------------------ resolve

def resolve_term(text: str, pack: dict) -> tuple[str | None, float]:
    """让客户用自己的词问，工厂侧负责翻译。取最长匹配。"""
    vocab = pack.get("vocab", {})
    best: tuple[str, float] | None = None
    for surface, meta in vocab.items():
        if surface.lower() in text.lower():
            if best is None or len(surface) > len(best[0]):
                best = (meta["canonical"], meta["confidence"])
    return best if best else (None, 0.0)


# -------------------------------------------------------------- 动态字段降级

def capacity_state(cap: dict, now: datetime | None = None) -> dict:
    """静态优先，动态降级。

    as_of 超过 ttl_days 即回落为「需向工厂确认」，绝不用陈旧值冒充实时值。
    """
    now = now or datetime.now(timezone.utc)
    level = cap.get("load_level", "light")
    as_of_raw = cap.get("as_of")
    ttl = cap.get("ttl_days", 30)

    if not as_of_raw:
        return {"load_level": level, "fresh": False,
                "reason_code": "CONFIRM.CAPACITY_STALE",
                "message": "未提供采样时间，按静态声明值处理，交期需人工确认"}

    as_of = datetime.fromisoformat(as_of_raw).replace(tzinfo=timezone.utc)
    age_days = (now - as_of).days
    fresh = age_days <= ttl
    return {
        "load_level": level,
        "fresh": fresh,
        "age_days": age_days,
        "reason_code": "OK.EXACT" if fresh else "CONFIRM.CAPACITY_STALE",
        "message": None if fresh else f"产能数据已 {age_days} 天未更新，交期需向工厂确认",
    }


# ------------------------------------------------------- 认证三方验证判定

def cert_satisfied(required_codes: list[str], certs: list) -> tuple[list[str], list[str]]:
    """判定 RFQ 要求的认证是否被满足。

    返回 (missing, unverified)：
      - missing    : 供应商完全未声明该认证码
      - unverified : 声明了但无三方可验证的证书号（verified 且 cert_no 非空才作数）

    设计原则：认证必须是三方可验证的。裸声称（无证书号）等同于没有 ——
    这正是「ISO9001 需要上传证书号才能作数」的落地。
    """
    missing, unverified = [], []
    by_code: dict[str, list[dict]] = {}
    for c in (certs or []):
        if isinstance(c, dict):
            by_code.setdefault(c.get("code"), []).append(c)
        else:  # 兼容旧裸字符串（理论上不该出现，适配器已统一转对象）
            by_code.setdefault(c, []).append(
                {"code": c, "verified": False, "cert_no": None})

    for code in (required_codes or []):
        entries = by_code.get(code, [])
        if not entries:
            missing.append(code)
        elif not any(bool(e.get("verified")) and e.get("cert_no") for e in entries):
            unverified.append(code)
    return missing, unverified


# ------------------------------------------------------------------ envelope

def envelope(status: str, reason_code: str, confidence: float,
             payload=None, evidence=None, as_of: str | None = None,
             ttl_days: int | None = None, human_fallback=None,
             message: str | None = None) -> dict:
    return {
        "status": status,
        "reason_code": reason_code,
        "confidence": round(confidence, 2),
        "as_of": as_of,
        "ttl_days": ttl_days,
        "payload": payload,
        "evidence": evidence or [],
        "human_fallback": human_fallback,
        "message": message,
    }
