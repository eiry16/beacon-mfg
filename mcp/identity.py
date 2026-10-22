#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 匿名身份与配额 —— 骨架

设计目标（一句话）：
    **装完就能用，一次注册都不要。** 身份在本地自动生成，服务端只做背书。

三层机制，逐层可选：
    L1 设备身份   ~/.beacon-mfg/client_id        （已有，首次调用生成 uuid）
    L2 服务端背书  POST /v1/anon/bootstrap        （静默换匿名令牌，防伪造 cid）
    L3 配额主体    cid 计账，按「可触达记录数」而非「请求次数」

为什么不内置 API key：
    key 一旦写进 npm 包，全世界共用一把 → 限流等于没有，泄露也无法定位。
    「每装一份生成一份」才是唯一可行的形态，而这恰好就是 client_id。

本文件是**离线优先**的：任何网络失败都不抛异常、不阻塞调用，
只把 bootstrap 挂在 pending 队列里，下次联网补登。
用户永远不会因为「注册服务挂了」而用不了 L0。

契约（服务端只需实现这一个端点）：
    POST /v1/anon/bootstrap
      req : {"cid": "<16hex>", "ver": "1.x.x", "os": "win32", "nonce": "<uuid>"}
      resp: {"token": "<jwt>", "tier": "anon", "expires_at": 1792..., 
             "quota": {"day": 200, "used": 0, "offset_max": 200}}
    鉴权头（所有业务请求）：
      Authorization: Bearer <token>
      X-Beacon-Cid: <16hex>          ← 令牌缺失时的降级身份
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

HOME = Path(os.path.expanduser("~")) / ".beacon-mfg"
CID_PATH = HOME / "client_id"
ID_PATH = HOME / "identity.json"
PENDING_PATH = HOME / "bootstrap_pending.json"

_CID_SALT = b"beacon-mfg-v1"          # 公开盐：让 cid 不可逆，不是防攻击
_TOKEN_REFRESH_MARGIN = 86400          # 提前 1 天续签，避免临界失败
BOOTSTRAP_TIMEOUT = 3.0                # 秒。宁可放弃，不可阻塞用户

# ⚠ 必须显式带 UA。Cloudflare 会在「无 UA / 类库默认 UA」的请求上直接回
#   403 error code 1010（browser_signature_banned）。而 urllib 的默认 UA 是
#   `Python-urllib/3.x` —— 正好命中。曾实测：不带 UA 时 bootstrap 与
#   /v1/identity/me 全部 1010，而 failure 是**静默降级**的，所以这个坑
#   在日志里看起来像「网络不好」，实际是身份链路从来没通过。
_UA = "BeaconMFG-MCP/1.8 (+https://beacon-mfg.pages.dev/)"

# 进程内缓存。**必须有**：一次 `city=上海` 的检索要发 178 个 HTTP 请求，
# 每个请求都去读 client_id / identity.json 就是 178 次磁盘 IO，
# 而这在整体延迟里完全是无谓开销。
_CID_CACHE: Dict[str, str] = {}
_ID_CACHE: Dict[str, Any] = {"d": None, "at": 0.0}
_ID_TTL = 30.0                          # 秒；令牌是分钟级有效期，30s 足够新


# --------------------------------------------------------------------------
# L1 设备身份（复用 server.py 的既有逻辑，不引入第二套）
# --------------------------------------------------------------------------
def client_id() -> str:
    """稳定的本机 id。首次调用生成，之后持久化。

    刻意不采集任何硬件指纹（MAC / 主板 / 磁盘序列号）——
    那属于「设备识别」而非「匿名身份」，在个人信息保护口径下风险高得多，
    而我们只需要「同一台机器算同一个配额单元」。
    """
    if _CID_CACHE.get("v"):
        return _CID_CACHE["v"]
    try:
        if CID_PATH.exists():
            v = CID_PATH.read_text(encoding="utf-8").strip()
            if v:
                _CID_CACHE["v"] = v
                return v
        HOME.mkdir(parents=True, exist_ok=True)
        v = str(uuid.uuid4())
        CID_PATH.write_text(v, encoding="utf-8")
        _CID_CACHE["v"] = v
        return v
    except Exception:
        # 只读文件系统 / 容器 → 退化为进程内临时身份，不崩
        _CID_CACHE.setdefault("v", _FALLBACK_CID)
        return _CID_CACHE["v"]


_FALLBACK_CID = str(uuid.uuid4())


def cid() -> str:
    """上报用的不可逆标识（16 hex）。原始 uuid 不出本机。"""
    return hmac.new(_CID_SALT, client_id().encode("utf-8"), hashlib.sha256).hexdigest()[:16]


# --------------------------------------------------------------------------
# L2 令牌缓存
# --------------------------------------------------------------------------
def _load() -> Dict[str, Any]:
    now = time.time()
    if _ID_CACHE["d"] is not None and now - _ID_CACHE["at"] < _ID_TTL:
        return _ID_CACHE["d"]
    d: Dict[str, Any] = {}
    try:
        if ID_PATH.exists():
            v = json.loads(ID_PATH.read_text(encoding="utf-8"))
            if isinstance(v, dict):
                d = v
    except Exception:
        d = {}
    _ID_CACHE["d"] = d
    _ID_CACHE["at"] = now
    return d


def _save(d: Dict[str, Any]) -> None:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        tmp = ID_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(ID_PATH)      # 原子替换，避免半截文件
        _ID_CACHE["d"] = d        # 写穿缓存：否则刚签的令牌 30s 内读不到
        _ID_CACHE["at"] = time.time()
    except Exception:
        pass


def token_state() -> Dict[str, Any]:
    """只读查询，供 tools/list 或诊断工具展示。"""
    d = _load()
    exp = int(d.get("expires_at") or 0)
    fresh = _is_fresh(d)
    # tier 必须由「令牌是否真的可用」推导，不能直接读文件里写的档位 ——
    # 否则令牌过期后仍报 anon，调用方会以为还有配额，直到被服务端 401。
    return {
        "tier": (d.get("tier") or "anon") if fresh else "anon_local",
        "cid": cid(),
        "has_token": bool(d.get("token")),
        "expires_in": max(0, exp - int(time.time())),
        "quota": (d.get("quota") or {}) if fresh else {},
    }


def _is_fresh(d: Dict[str, Any]) -> bool:
    exp = int(d.get("expires_at") or 0)
    return bool(d.get("token")) and exp - _TOKEN_REFRESH_MARGIN > time.time()


# --------------------------------------------------------------------------
# bootstrap：静默换取令牌
# --------------------------------------------------------------------------
def _post(url: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import urllib.request

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": _UA},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=BOOTSTRAP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def ensure_token(bases: Optional[list[str]] = None) -> Dict[str, Any]:
    """确保有可用令牌。**绝不抛异常、绝不阻塞超过 3 秒。**

    返回当前身份状态；拿不到令牌时返回 tier=anon_local，调用方照常继续。
    """
    d = _load()
    if _is_fresh(d):
        return token_state()

    bases = bases or _bases()
    payload = {
        "cid": cid(),
        "ver": _ver(),
        "os": os.name,
        "nonce": str(uuid.uuid4()),   # 防重放；服务端可据此去重同一毫秒的并发请求
    }
    for b in bases:
        resp = _post(b.rstrip("/") + "/v1/anon/bootstrap", payload)
        if isinstance(resp, dict) and resp.get("token"):
            d.update({
                "token": resp["token"],
                "tier": resp.get("tier") or "anon",
                "expires_at": int(resp.get("expires_at") or (time.time() + 30 * 86400)),
                "quota": resp.get("quota") or {},
                "cid_at_issue": cid(),
            })
            _save(d)
            _drop_pending()
            return token_state()

    # 全部失败 → 排队，下次补登。用户此刻仍然能查 L0。
    _enqueue_pending(payload)
    return token_state()


def _bases() -> list[str]:
    """端点候选。**内置默认值**，用户不需要配置任何东西。

    这是产品决策：MCP 的用户预期是「装个插件」，不是「接一个后端」。
    要求填 endpoint 会让试用转化掉一个数量级。
    """
    out: list[str] = []
    env = os.environ.get("BEACON_API_BASE")
    if env:
        out.append(env.strip())
    # 内置默认值：身份端点就挂在现有 Pages 站点上（worker 的 /v1/* 路由），
    # 不新增域名、不新增证书、也不给用户留任何「填地址」的空位。
    out.append("https://beacon-mfg.pages.dev")
    return [x for x in out if x]


def _ver() -> str:
    try:
        return (HOME.parent / ".beacon-mfg-ver").read_text().strip()
    except Exception:
        return "0.0.0"


# --------------------------------------------------------------------------
# 离线补登
# --------------------------------------------------------------------------
def _enqueue_pending(payload: Dict[str, Any]) -> None:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        PENDING_PATH.write_text(
            json.dumps({"payload": payload, "at": int(time.time())}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _drop_pending() -> None:
    try:
        if PENDING_PATH.exists():
            PENDING_PATH.unlink()
    except Exception:
        pass


def pending_age() -> int:
    """补登排队了多久（秒）。0 表示没有排队。"""
    try:
        d = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        return max(0, int(time.time()) - int(d.get("at") or 0))
    except Exception:
        return 0


def warmup() -> None:
    """后台静默取一次令牌，**不阻塞**。

    server 启动时调一次即可。放在后台线程里的理由：bootstrap 最坏要等
    3 秒超时，而 MCP 的 `initialize` 如果卡 3 秒，客户端会以为服务挂了。
    拿不到也没关系，`headers()` 会降级成只带 cid。
    """
    if _is_fresh(_load()):
        return
    try:
        import threading
        threading.Thread(target=lambda: ensure_token(), daemon=True).start()
    except Exception:
        pass


# --------------------------------------------------------------------------
# 请求头
# --------------------------------------------------------------------------
def headers() -> Dict[str, str]:
    """所有出站请求共用。与 server.py 的 _beacon_headers() 合并使用。"""
    st = token_state()
    h = {"X-Beacon-Cid": cid()}
    d = _load()
    if d.get("token") and _is_fresh(d):
        h["Authorization"] = "Bearer " + d["token"]
    return h


# --------------------------------------------------------------------------
# L3 手机绑定（verified 档）—— request / confirm / status
# --------------------------------------------------------------------------
# 为什么只绑手机号、不要邮箱/GitHub：
#   ① 用户对「GitHub 授权」的戒心远高于手机号（它会暴露社交身份），而手机号
#      只换来一个「配额从 200 提到 2000」的档位，代价/收益严重不匹配 → 会直接
#      掉转化。② 手机号还能顺带挡住批量刷号（一机一卡），这是邮箱做不到的。
#   本地**不存手机号明文**，只存脱敏显示串（138****8000）用于回显。
#   服务端也只存 HMAC（见 worker `phoneHash`），所以两边都拿不到明文。
API_TIMEOUT = 8.0        # 手机链路宁可多等一会儿，也别让用户以为没发出去


def _post_auth(path: str, payload: Dict[str, Any], bases: Optional[list[str]] = None) -> Dict[str, Any]:
    """带身份头发 POST。返回服务端 JSON，或 `{"error": "<本地错误>"}`。"""
    import urllib.error
    import urllib.request

    h = headers()
    h["Content-Type"] = "application/json"
    h["Accept"] = "application/json"
    h["User-Agent"] = _UA          # 没有它 Cloudflare 直接 1010，见 _UA 注释
    last: Dict[str, Any] = {"error": "no_endpoint"}
    for b in (bases or _bases()):
        url = b.rstrip("/") + path
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 4xx 是**业务结论**（验证码错、频控），必须原样交给用户，
            # 不能当成网络错误吞掉 —— 否则用户只会看到「发送失败」而无从下手。
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                last = {"error": "http_%d" % e.code}
        except Exception as e:      # noqa: BLE001
            last = {"error": "net:" + str(e)[:80]}
    return last


def norm_phone(raw: str) -> str:
    """归一成 +86 11 位。只接受中国大陆手机号 —— 本项目的数据与合规都在境内。"""
    d = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if d.startswith("0086"):
        d = d[4:]
    elif d.startswith("86") and len(d) == 13:
        d = d[2:]
    return d if len(d) == 11 and d[0] == "1" and d[1] in "3456789" else ""


def mask_phone(p: str) -> str:
    return p[:3] + "****" + p[-4:] if len(p) == 11 else ""


def request_code(phone: str, bases: Optional[list[str]] = None) -> Dict[str, Any]:
    """发验证码。成功返回 {ok, ttl, phone(masked)}；失败返回 {error, hint}。"""
    p = norm_phone(phone)
    if not p:
        return {"error": "bad_phone", "hint": "请提供中国大陆 11 位手机号"}
    r = _post_auth("/v1/auth/phone/request", {"phone": p}, bases)
    if r.get("ok"):
        out = {"ok": True, "phone": mask_phone(p), "ttl": r.get("ttl") or 300,
               "phone_e164": p}
        if r.get("dev_code"):
            # 仅本地 dev 通道（服务端 BEACON_OTP_DEV=1 且未配短信服务商）才会出现
            out["dev_code"] = r["dev_code"]
            out["hint"] = "本地 dev 通道：验证码已直接返回，不会真发短信"
        return out
    return r


def confirm_code(phone: str, code: str, bases: Optional[list[str]] = None) -> Dict[str, Any]:
    """提交验证码。成功即把令牌写入本地，档位变 verified（有效期 30 天）。"""
    p = norm_phone(phone)
    c = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not p:
        return {"error": "bad_phone"}
    if len(c) != 6:
        return {"error": "bad_code", "hint": "验证码是 6 位数字"}
    r = _post_auth("/v1/auth/phone/verify", {"phone": p, "code": c, "cid": cid()}, bases)
    if not r.get("token"):
        return r
    d = _load()
    d.update({
        "token": r["token"],
        "tier": r.get("tier") or "verified",
        "expires_at": int(r.get("expires_at") or (time.time() + 30 * 86400)),
        "quota": r.get("quota") or {},
        "cid_at_issue": cid(),
        "phone_masked": mask_phone(p),
        "verified_at": int(time.time()),
    })
    _save(d)
    return {"ok": True, "tier": d["tier"], "phone": d["phone_masked"],
            "expires_in": max(0, d["expires_at"] - int(time.time())),
            "quota": d["quota"]}


def logout() -> Dict[str, Any]:
    """清掉本地令牌，回到匿名档。**不通知服务端** —— 服务端只存 aid→phone 的
    HMAC 绑定，没有会话可撤销；真要解绑得走运营（改绑手机号是人工流程）。"""
    d = _load()
    for k in ("token", "tier", "expires_at", "quota", "phone_masked", "verified_at"):
        d.pop(k, None)
    _save(d)
    return {"ok": True, "tier": "anon_local"}


def status(bases: Optional[list[str]] = None, probe: bool = True) -> Dict[str, Any]:
    """身份与配额总览。本地优先，联网可拿到服务端权威的 used/remaining。"""
    if not _is_fresh(_load()):
        # 没有令牌就先换一张：否则 /v1/identity/me 只能靠 cid 兜底，
        # 显示出来的 tier 是「服务端按 cid 猜的」，而不是真正签发的档位。
        try:
            ensure_token(bases)
        except Exception:
            pass
    st = token_state()
    out: Dict[str, Any] = {
        "tier": st["tier"],
        "cid": st["cid"],
        "phone": _load().get("phone_masked") or None,
        "token_expires_in": st["expires_in"],
        "quota": st["quota"],
    }
    if not probe:
        return out
    r = _post_auth("/v1/identity/me", {}, bases)
    if isinstance(r, dict) and r.get("tier"):
        out.update({
            "tier": r.get("tier"),
            "quota": r.get("quota") or out.get("quota"),
            "used": r.get("used"),
            "remaining": r.get("remaining"),
            "window": r.get("window"),
            "offset_max": r.get("offset_max"),
            "enforce": r.get("enforce"),
            "source": r.get("source"),
        })
        d = _load()
        if d:
            d["quota"] = r.get("quota") or d.get("quota") or {}
            _save(d)
    else:
        out["server"] = "unreachable"
        out["server_error"] = (r or {}).get("error")
    pending = pending_age()
    if pending:
        out["bootstrap_pending_s"] = pending
    return out


def quota_from_response(headers_map: Any) -> Dict[str, Any]:
    """从响应头读剩余配额，本地只做展示（真正计数在服务端）。"""
    def g(k: str) -> Optional[str]:
        try:
            return headers_map.get(k)
        except Exception:
            return None

    day = g("X-Beacon-Quota-Day")
    used = g("X-Beacon-Quota-Used")
    off = g("X-Beacon-Offset-Max")
    if day is None and used is None:
        return {}
    q = {"day": _int(day), "used": _int(used), "offset_max": _int(off)}
    d = _load()
    if d:
        d["quota"] = {k: v for k, v in q.items() if v is not None}
        _save(d)
    return q


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------
def _self_test() -> int:
    import shutil
    import tempfile

    global HOME, CID_PATH, ID_PATH, PENDING_PATH
    tmp = Path(tempfile.mkdtemp(prefix="beacon_identity_"))
    old = (HOME, CID_PATH, ID_PATH, PENDING_PATH)
    HOME = tmp
    CID_PATH, ID_PATH, PENDING_PATH = tmp / "client_id", tmp / "identity.json", tmp / "p.json"
    ok = 0
    total = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok, total
        total += 1
        ok += 1 if cond else 0
        print("  %s %-42s %s" % ("PASS" if cond else "FAIL", name, extra))

    try:
        # 切换 HOME 后必须清缓存，否则测的是上一个临时目录的残留状态
        _CID_CACHE.clear()
        _ID_CACHE["d"], _ID_CACHE["at"] = None, 0.0

        # T1 首次生成 + 复用
        c1 = cid()
        c2 = cid()
        check("首次调用自动生成身份", len(c1) == 16)
        check("二次调用复用同一身份", c1 == c2)
        check("原始 uuid 不出本机", CID_PATH.read_text(encoding="utf-8").strip() != c1)

        # T2 无网络：不抛异常、不返回令牌、仍可继续
        t0 = time.time()
        st = ensure_token(bases=["http://127.0.0.1:1"])
        dt = time.time() - t0
        check("无网络时不抛异常", isinstance(st, dict))
        check("无网络时降级为 anon_local", st["tier"] == "anon_local")
        check("无网络时 3 秒内返回", dt < 3.5, "(%.1fs)" % dt)
        check("无网络时进入补登队列", pending_age() >= 0 and PENDING_PATH.exists())

        # T3 有令牌时带 Authorization
        _save({"token": "tkn_test", "tier": "anon", "expires_at": int(time.time()) + 86400 * 20})
        h = headers()
        check("有令牌时带 Authorization", h.get("Authorization") == "Bearer tkn_test")
        check("始终带 X-Beacon-Cid", "X-Beacon-Cid" in h)

        # T4 过期令牌自动换新（此处无网络 → 降级）
        _save({"token": "old", "tier": "anon", "expires_at": int(time.time()) - 10})
        st = ensure_token(bases=["http://127.0.0.1:1"])
        check("过期令牌不用于鉴权", "Authorization" not in headers())
        check("过期后降级不崩", st["tier"] == "anon_local")

        # T5 令牌文件损坏 → 自愈（先清缓存，否则 30s 内读到的是旧值，测了个寂寞）
        _ID_CACHE["d"], _ID_CACHE["at"] = None, 0.0
        ID_PATH.write_text("{ 这不是 json", encoding="utf-8")
        check("令牌损坏时可自愈", isinstance(token_state(), dict))

        # T6 配额头解析
        q = quota_from_response({"X-Beacon-Quota-Day": "200", "X-Beacon-Quota-Used": "37",
                                 "X-Beacon-Offset-Max": "200"})
        check("配额头解析正确", q.get("used") == 37 and q.get("day") == 200)

        # T7 手机号归一（无网络，纯函数）
        check("号归一 +86 前缀", norm_phone("+86 138 0013 8000") == "13800138000")
        check("号归一 0086 前缀", norm_phone("008613800138000") == "13800138000")
        check("号归一 拒座机", norm_phone("010-12345678") == "")
        check("号归一 拒 12 段", norm_phone("12800138000") == "")
        check("号脱敏", mask_phone("13800138000") == "138****8000")

        # T8 断网时 request/confirm 不抛异常（用户看到的是可读错误，不是堆栈）
        r1 = request_code("13800138000", bases=["http://127.0.0.1:1"])
        check("断网发码返回错误而非异常", isinstance(r1, dict) and "error" in r1)
        r2 = confirm_code("13800138000", "123456", bases=["http://127.0.0.1:1"])
        check("断网验码返回错误而非异常", isinstance(r2, dict) and "error" in r2)
        check("验码位数校验前置", confirm_code("13800138000", "123")["error"] == "bad_code")

        # T9 logout 回到匿名档
        _save({"token": "t", "tier": "verified", "expires_at": int(time.time()) + 86400 * 9,
               "phone_masked": "138****8000"})
        logout()
        check("logout 清空令牌", token_state()["tier"] == "anon_local")
        check("logout 清空手机回显", not _load().get("phone_masked"))
    finally:
        HOME, CID_PATH, ID_PATH, PENDING_PATH = old
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d/%d 通过" % (ok, total))
    return 0 if ok == total else 1


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
