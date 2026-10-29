#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""供应商 id 的对外 token 化（D，2026-10-28）。

问题
====
内部 id 是 `CN-MFG-0000163` —— **顺序编号**。这意味着「存在性可枚举」：
实测 `/skills/registry/capability/CN-MFG-0000009.json` → 200、
`.../0000010.json` → 404，于是从 1 遍历到 222875 就能把全库重建一遍。
A（manifest 需要身份）+ B（R2 私有）+ C（服务端签发令牌）把「拖库」从 5 分钟
抬到几十小时，但**枚举本身仍然成立**：只要 id 可预测，攻击者不需要任何
目录信息，盲扫 URL 就够了。D 要终结的正是这一点。

做法
====
**内部 id 不变，对外只出现 token。** token = 密钥化置换后的 40 bit，编码成
8 字符 Crockford base32。

为什么是「置换」而不是「哈希」
------------------------------
哈希（SHA/HMAC）不可逆 → 服务端收到 token 后无法还原内部 id，只能建一张
21 万行的映射表并每次查库 —— 那是给每个请求加一次 D1 往返。
置换（双射）可逆，还原是纯计算、零 IO。这在这里是决定性的：
  · capability / vendors 路径要还原后才知道去 R2 取哪个对象
  · 记账（beacon_card_view.vid）要还原成内部 id 才能和存量数据对齐

为什么是 Feistel 而不是「XOR 一个常数」
--------------------------------------
XOR 是线性的：攻击者只要拿到**一个** (内部编号, token) 对就能解出常数、
进而解开全部。而攻击者能自己认领一家企业（他知道自己的编号），所以这个
前提一定成立。Feistel 网络 4 轮 + 密钥派生自 SHA-256，已知明文对不会泄露
其余映射（唯一攻击是穷举 256 bit 密钥）。

轮函数用 murmur3 的 finalizer（**不是** HMAC）：Feistel 的安全性来自
轮密钥保密，轮函数本身不需要是密码学原语。这样整个置换是**纯同步整数运算**，
在 worker 里每个请求只需算几次，不必 await 加密 API。

编码
====
Crockford base32（去掉 I/L/O/U，避免和 1/0 混淆），8 字符 = 40 bit。
只出现 `0-9A-Z`，可直接放进 URL 路径，不需要转义。

用法
====
    python scripts/vid_token.py selftest
    python scripts/vid_token.py enc CN-MFG-0000163
    python scripts/vid_token.py dec 7K3M9QXA
    python scripts/vid_token.py bench          # 21 万条全量往返校验
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

# ── 参数（改这里必须同时改 pages/_vid.js 的 VID_* 常量）──────────────────────
# Feistel 内部的半宽（20 bit），与"编号占几位"无关：整域固定 40 bit，
# 左右各 20 bit。改这里等于改变换本身，会让存量 token 全部失效。
MASK20 = (1 << 20) - 1
HALF = 1 << 20

# 40 bit 域怎么切：低 NUM_BITS 位放编号，高位放类型。
#   NUM_BITS=32 → 单类型上限 4,294,967,295（42.9 亿），类型槽 2^8=256 个。
#
# 为什么不是 24 位（用户提的"24 位才够 10 亿"）：
#   2^24 = 16,777,216，只有 1677 万，离 10 亿差 60 倍。
#   10 亿 = 10^9 < 2^30 = 1,073,741,824 → 真正够用的下界是 **30 位**。
#   30 位上限定在 10.7 亿，只剩 7.4% 余量；32 位直接给到 42.9 亿（4 倍余量），
#   而 40 - 32 = 8 bit 留类型仍然够用（当前只用了 2 个类型）。
#   所以这里取 32，不是 24，也不是 30。
#
# 超限的行为：`encode()` 原样返回内部 id（等于在 token 化的数据里混进明文 id），
# 因此 `parse_batch`/`bench` 必须看守上限，见 MAX_NUM 断言。
TOKEN_BITS = 40
NUM_BITS = 32
TYPE_BITS = TOKEN_BITS - NUM_BITS
TYPE_SHIFT = NUM_BITS          # 兼容旧调用点的别名
MAX_NUM = (1 << NUM_BITS) - 1
TYPE_MASK = (1 << TYPE_BITS) - 1
TYPE_BY_PREFIX = {"CN-MFG": 1, "CN-I": 2}
PREFIX_BY_TYPE = {v: k for k, v in TYPE_BY_PREFIX.items()}
TOKEN_LEN = 8
ROUNDS = 4

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE_MAP = {c: i for i, c in enumerate(_CROCKFORD)}
# 常见误读归一（Crockford 的标准做法）：小写、I/L→1、O→0
for _c in "IL":
    _DECODE_MAP.setdefault(_c.lower(), 1)
    _DECODE_MAP.setdefault(_c, 1)
_DECODE_MAP.setdefault("o", 0)
_DECODE_MAP.setdefault("O", 0)
for _i, _c in enumerate(_CROCKFORD):
    _DECODE_MAP.setdefault(_c.lower(), _i)


def _fmix(h: int) -> int:
    """murmur3 finalizer，32 bit。与 worker 里 `fmix()` 必须逐位一致。"""
    h &= 0xFFFFFFFF
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


_KEYS_CACHE: dict[str, tuple[int, int, int, int]] = {}


def round_keys(secret: str) -> tuple[int, int, int, int]:
    """从 secret 派生 4 个 20 bit 级轮密钥（实际取 32 bit，用低 20 位）。

    用 SHA-256 而不是把 secret 直接切段：secret 可能是人写的短串，
    直接切段会让高位恒为常量，Feistel 的有效密钥空间塌缩。
    """
    k = _KEYS_CACHE.get(secret)
    if k is not None:
        return k
    d = hashlib.sha256(("beacon-vid|" + secret).encode("utf-8")).digest()
    k = (int.from_bytes(d[0:4], "big"), int.from_bytes(d[4:8], "big"),
         int.from_bytes(d[8:12], "big"), int.from_bytes(d[12:16], "big"))
    _KEYS_CACHE[secret] = k
    return k


def _permute(x: int, keys) -> int:
    left = (x >> 20) & MASK20
    right = x & MASK20
    for i in range(ROUNDS):
        f = _fmix(right ^ keys[i]) & MASK20
        left, right = right, (left ^ f) & MASK20
    return ((left << 20) | right) & 0xFFFFFFFFFF


def _unpermute(y: int, keys) -> int:
    left = (y >> 20) & MASK20
    right = y & MASK20
    for i in range(ROUNDS - 1, -1, -1):
        prev_right = left
        prev_left = (right ^ (_fmix(prev_right ^ keys[i]) & MASK20)) & MASK20
        left, right = prev_left, prev_right
    return ((left << 20) | right) & 0xFFFFFFFFFF


def b32enc(x: int) -> str:
    out = []
    for _ in range(TOKEN_LEN):
        out.append(_CROCKFORD[x & 31])
        x >>= 5
    return "".join(reversed(out))


def b32dec(s: str) -> int:
    if len(s) != TOKEN_LEN:
        raise ValueError("bad length")
    x = 0
    for ch in s:
        d = _DECODE_MAP.get(ch)
        if d is None:
            raise ValueError("bad char")
        x = (x << 5) | d
    return x


# ── 对外 API ────────────────────────────────────────────────────────────────
# 编号位数放到 10 位十进制（上限 4,294,967,295 正好是 10 位），
# 所以正则必须允许 1~10 位；仍然只接受 ASCII 数字，不接受前导符号/空格。
_ID_RE = re.compile(r"^(CN-MFG|CN-I)-(\d{1,10})$")


def encode(vid: str, secret: str) -> str:
    """内部 id → token。非法 id 原样返回（调用方据此跳过而不是崩）。"""
    m = _ID_RE.match(vid or "")
    if not m:
        return vid
    t = TYPE_BY_PREFIX[m.group(1)]
    n = int(m.group(2))
    if n > MAX_NUM:
        return vid
    return b32enc(_permute((t << TYPE_SHIFT) | n, round_keys(secret)))


def decode(token: str, secret: str) -> str:
    """token → 内部 id。非法 token 返回空串（**不要**在这里抛异常：
    worker 里它面对的是公网输入，抛异常 = 500）。"""
    if not token or len(token) != TOKEN_LEN:
        return ""
    try:
        x = b32dec(token)
    except ValueError:
        return ""
    x = _unpermute(x, round_keys(secret))
    t = (x >> TYPE_SHIFT) & TYPE_MASK
    n = x & MAX_NUM
    pre = PREFIX_BY_TYPE.get(t)
    # 三重校验：类型合法 + 编号非零 + 不越过上限。任何一条不满足都当伪造。
    if not pre or n == 0 or n > MAX_NUM:
        return ""
    # %07d：7 位是存量数据的写法；越过 9,999,999 后自然变 8/9/10 位，
    # 与生成侧 str(n) 的写法一致，不需要另外分支。
    return "%s-%07d" % (pre, n)


# ── 批量/深层改写（MCP、控制台、发布脚本的对外出口用）────────────────────────
# 内部 id 不止出现在 `id` 字段里，还写在路径（skills/vendors/CN-MFG-…/SKILL.md）、
# markdown 正文、以及 LLM 会照抄给用户的自然语言里。所以对外出口不能只改一个
# key —— 那等于只关了一半门。这里做的是「整棵结果树里的 id 全部换掉」。
_ANY_ID_RE = re.compile(r"(?:CN-MFG|CN-I)-\d{1,10}")
# 入参侧只认「整个字符串就是一个 token」，避免把公司名里恰好出现的 8 位串当成 id。
_TOKEN_RE = re.compile(r"^[0-9A-Za-z]{8}$")


def encode_text(s: str, secret: str) -> str:
    """把一段文本里出现的所有内部 id 换成 token。"""
    if not s or ("CN-MFG-" not in s and "CN-I-" not in s):
        return s
    return _ANY_ID_RE.sub(lambda m: encode(m.group(0), secret), s)


def encode_deep(obj, secret: str, _depth: int = 0):
    """递归改写 dict/list/str 里的内部 id。

    只走这三类容器：数字/布尔原样返回（不做字符串化，否则会把 int 变成 str，
    客户端按类型解析的地方会静默失效）。深度上限 12 是防环，正常结果树远小于此。
    """
    if _depth > 12:
        return obj
    if isinstance(obj, str):
        return encode_text(obj, secret)
    if isinstance(obj, dict):
        return {k: encode_deep(v, secret, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [encode_deep(v, secret, _depth + 1) for v in obj]
    return obj


def decode_arg(v: str, secret: str) -> str:
    """入参里的 id：能解成内部 id 就解，解不动原样返回（可能是别的形态）。"""
    s = v.strip() if isinstance(v, str) else v
    if not isinstance(s, str) or not _TOKEN_RE.match(s):
        return v
    inner = decode(s, secret)
    return inner or v


def vid_secret() -> str:
    """轮密钥的 secret。**必须显式配置**（.env / worker secret）。

    缺省值只是让人本地能跑通自测；线上绝不能落到缺省值 —— 那等于公开密钥。
    """
    v = os.environ.get("BEACON_VID_KEY", "")
    if v:
        return v
    envf = Path(__file__).resolve().parent.parent / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("BEACON_VID_KEY="):
                return line.split("=", 1)[1].strip()
    return "dev-only-insecure-key"


def has_key() -> bool:
    """是否配置了真密钥。

    为什么客户端要先问这个：npm 装的 MCP **没有** .env，`vid_secret()` 会落到
    缺省值。缺省值等于公开密钥 —— 用它算出来的 token 服务端解不开（服务端用的是
    真密钥），客户端拿到的是一串**看起来合法、实际全 404** 的 id。
    所以没密钥时宁可不改写 id（继续走内部形态 + 服务端 legacy 窗口），也不要
    自信地输出错 token。
    """
    return vid_secret() != "dev-only-insecure-key"


def selftest() -> int:
    sec = "test-secret-abc"
    ok = tot = 0

    def ck(name, cond, extra=""):
        nonlocal ok, tot
        tot += 1
        ok += 1 if cond else 0
        print("  %s %-40s %s" % ("PASS" if cond else "FAIL", name, extra))

    v = "CN-MFG-0000163"
    t = encode(v, sec)
    ck("往返可逆", decode(t, sec) == v, t)
    ck("token 长度固定", len(t) == TOKEN_LEN)
    ck("token 只含 URL 安全字符", all(c in _CROCKFORD for c in t))
    ck("CN-I 前缀可区分", decode(encode("CN-I-0000001", sec), sec) == "CN-I-0000001")
    ck("两种前缀不撞", encode("CN-MFG-0000001", sec) != encode("CN-I-0000001", sec))
    ck("非法 id 原样返回", encode("XX-1", sec) == "XX-1")
    ck("非法 token 返回空", decode("BAD", sec) == "")
    ck("非法字符返回空", decode("UUUUUUUU", sec) == "")
    ck("小写可解", decode(t.lower(), sec) == v)

    # ── 容量边界（这一组是本次把编号位从 20 抬到 32 的验收项）─────────────
    ck("上限常量 = 42.9 亿", MAX_NUM == 4294967295, str(MAX_NUM))
    ck("百分比位宽 = 8", TYPE_BITS == 8)
    for probe in (10_000_000, 99_999_999, 1_000_000_000, 4_000_000_000, MAX_NUM):
        pid = "CN-MFG-%07d" % probe
        ck("大编号往返 %d" % probe, decode(encode(pid, sec), sec) == pid,
           encode(pid, sec))
    over = "CN-MFG-4294967296"
    ck("超上限原样返回（不静默降级）", encode(over, sec) == over)
    ck("10 位编号可解析", decode(encode("CN-MFG-1234567890", sec), sec) == "CN-MFG-1234567890")
    ck("11 位编号不接收", encode("CN-MFG-00000000001", sec) == "CN-MFG-00000000001")

    # 顺序不可枚举：相邻编号的 token 不能相邻
    seq = [encode("CN-MFG-%07d" % i, sec) for i in range(1, 200)]
    diffs = sum(1 for i in range(len(seq) - 1) if seq[i] < seq[i + 1])
    ck("相邻编号 token 不保序", 60 <= diffs <= 140, "递增比例 %.0f%%" % (diffs / 199 * 100))
    ck("200 个 token 无碰撞", len(set(seq)) == len(seq))
    ck("不同 secret 出不同 token", encode(v, "other") != t)
    print("\n%d/%d 通过" % (ok, tot))
    return 0 if ok == tot else 1


def bench() -> int:
    """全量往返 + 唯一性校验。这是 D 上线前的最后一道闸。

    两段：
      1) 存量 222,875 条连续编号（真实数据形态）
      2) 大编号随机 + 边界采样（1 亿 ~ 42.9 亿区间，验证抬位后没有溢出/碰撞）
    """
    import random

    sec = vid_secret()
    seen = set()
    bad = 0

    n = 222875
    for i in range(1, n + 1):
        v = "CN-MFG-%07d" % i
        t = encode(v, sec)
        if t in seen or decode(t, sec) != v:
            bad += 1
        seen.add(t)
    print("存量 %d 条：唯一 token %d，往返失败 %d" % (n, len(seen), bad))

    probes = [10**8, 10**9, 2**31 - 1, 2**31, 2**32 - 2, 2**32 - 1]
    rng = random.Random(20261028)
    # 取样下界必须 > n：落入 1..222875 的随机编号本来就等于存量里的一条，
    # 共享 token 是**正确行为**，混进来会被误记成碰撞（上一版就是这么误报 2 条的）。
    probes += [rng.randrange(n + 1, 2**32) for _ in range(50000)]
    big_seen = set()
    big_bad = 0
    for p in probes:
        v = "CN-MFG-%07d" % p
        t = encode(v, sec)
        if t in big_seen or t in seen or decode(t, sec) != v:
            big_bad += 1
        big_seen.add(t)
    print("大编号 %d 条：唯一 token %d，往返/碰撞失败 %d"
          % (len(probes), len(big_seen), big_bad))

    ok = bad == 0 and len(seen) == n and big_bad == 0
    print("结论：%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        sys.exit(selftest())
    if cmd == "bench":
        sys.exit(bench())
    if cmd == "dump":
        # 供 pages/test_vid_parity.mjs 做**跨语言**逐位比对。取样要覆盖边界：
        # 最小 / 最大 / 相邻 / 两个前缀 / 十进制进位处（0079999 → 0080000）。
        import json as _json
        sec = vid_secret()
        # 取样必须覆盖边界：最小 / 7→8→9→10 位十进制进位处 / 24 bit 边界
        # （1677 万，用户原以为够用的档）/ 10 亿 / 32 bit 符号位 / 上限。
        picks = [1, 2, 9, 10, 99, 100, 163, 999, 1000, 79999, 80000,
                 99999, 100000, 222875, 9999999, 10000000,
                 16777215, 16777216, 999999999, 1000000000,
                 2147483647, 2147483648, 4294967295]
        pairs = [["CN-MFG-%07d" % i, encode("CN-MFG-%07d" % i, sec)] for i in picks]
        pairs.append(["CN-I-0000001", encode("CN-I-0000001", sec)])
        print(_json.dumps({"secret": sec, "pairs": pairs}, ensure_ascii=False))
        sys.exit(0)
    if cmd == "enc" and len(sys.argv) > 2:
        print(encode(sys.argv[2], vid_secret()))
    elif cmd == "dec" and len(sys.argv) > 2:
        print(decode(sys.argv[2], vid_secret()))
    else:
        print(__doc__)
        sys.exit(2)
