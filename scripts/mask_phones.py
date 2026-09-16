#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机号脱敏过滤器（git clean filter + 独立文件模式）。

设计目标（方案 A：只脱敏 git，Pages/App 数据源保留全号，旧 App 无感）：
- 作为 git clean filter 挂到 data/gb/**、data/en/**、data/phone-index.jsonl，
  `git add` 时自动把入库内容里的手机号脱敏，但**工作树（磁盘）文件保持全号**
  → Pages 部署读的是工作树（全号），App 照常拿到全号+拨号；GitHub 仓库入库的是脱敏版。
- 规则（主人拍板 2026-09-16）：
    * 11 位中国大陆手机（独立出现的 1[3-9]\\d{9}）→ 前 3 + **** + 后 4，例如 13812340000 → 138****0000
    * 支持「一个字段内多个号码」与「座机; 手机」组合：
        '13826925826; 18924331149' -> '138****5826; 189****1149'
        '0512-36691404; 18662658332' -> '0512-36691404; 186****8332'
    * 座机（含 -）、400/800、'待核实' 等 → 原样不脱敏
    * 已脱敏（含 *）→ 幂等保留
- 处理三种文件格式：
    * JSON 数组（[...]）
    * JSONL（每行一个 {...}）
    * CSV（id,phone，仅 phone-index.jsonl）

用法：
    git 调用（在 .gitattributes + git config filter.maskphone.clean 配好后自动）：
        python scripts/mask_phones.py --filter        # stdin -> stdout
    独立模式：
        python scripts/mask_phones.py IN.jsonl OUT.jsonl
        python scripts/mask_phones.py --in-place IN.jsonl
"""
from __future__ import annotations

import json
import re
import sys

# 匹配「独立的 11 位手机」——前后都不是数字，避免误伤更长的数字串；
# 座机/400 含 '-' 不会命中，已脱敏的 138****0000 也不会二次命中（幂等）。
MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def _mask_one(m: "re.Match") -> str:
    s = m.group(0)
    return s[:3] + "****" + s[-4:]


def mask_mobile_in_text(value):
    """把字符串里每一个独立的 11 位手机脱敏；座机/400（含 -）原样；已脱敏幂等。

    支持一个字段里多个号码或「座机; 手机」的组合，例如
    '13826925826; 18924331149' -> '138****5826; 189****1149'，
    '0512-36691404; 18662658332' -> '0512-36691404; 186****8332'。
    """
    if not isinstance(value, str):
        return value
    return MOBILE_RE.sub(_mask_one, value)


def mask_phone(value) -> str:
    """对单个手机号值脱敏；非 11 位手机原样返回（座机/待核实/已脱敏都保持）。
    内部委托 mask_mobile_in_text，自动支持多号码/组合串。"""
    return mask_mobile_in_text(value)


def is_claimed(rec) -> bool:
    """认主门控：claim.status 为 claimed/verified 时视为已认领，仓库展示全号。

    复用 supplier.schema.json 已有的 claim.status 字段（unclaimed/claimed/verified），
    不另起炉灶。未认领（含字段缺失/待核实）一律脱敏。
    """
    claim = rec.get("claim") if isinstance(rec, dict) else None
    if isinstance(claim, dict):
        return claim.get("status") in ("claimed", "verified")
    return False


PHONE_FIELDS = ("contact_phone", "address", "address_en")


def mask_record(rec) -> None:
    """就地脱敏 dict 里的手机号字段（contact_phone / address / address_en）。

    认领记录（claim.status=claimed/verified）保留全号；其余手机脱敏、座机原样。
    一个字段内可含多个号码或「座机; 手机」组合，全部独立脱敏。
    """
    if isinstance(rec, dict):
        if is_claimed(rec):
            return  # 已认领：仓库也展示全号
        for f in PHONE_FIELDS:
            if f in rec:
                rec[f] = mask_mobile_in_text(rec.get(f))


def _keep_trailing_nl(text: str) -> bool:
    return text.endswith("\n")


def process_jsonl(text: str) -> str:
    out = []
    for line in text.splitlines():
        if not line.strip():
            out.append(line)
            continue
        rec = json.loads(line)
        mask_record(rec)
        out.append(json.dumps(rec, ensure_ascii=False))
    return "\n".join(out) + ("\n" if _keep_trailing_nl(text) else "")


def process_json_array(text: str) -> str:
    arr = json.loads(text)
    if isinstance(arr, list):
        for r in arr:
            mask_record(r)
    return json.dumps(arr, ensure_ascii=False, indent=2)


def process_csv(text: str) -> str:
    out = []
    for line in text.splitlines():
        if not line.strip():
            out.append(line)
            continue
        if "," in line:
            rid, sep, phone = line.rpartition(",")
            out.append(rid + sep + mask_phone(phone))
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if _keep_trailing_nl(text) else "")


def looks_like_csv(text: str) -> bool:
    """全部非空行都是 `id,phone` 且无 JSON 行（不以 { 开头）则判为 CSV。

    仅 phone-index.jsonl 是这种格式；jsonl 行以 { 开头，不会误判。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    if any(ln.lstrip().startswith("{") or ln.lstrip().startswith("[") for ln in lines[:50]):
        return False
    return all(re.match(r"^[^,]+,\S+$", ln) for ln in lines[:50])


def process(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith("["):
        return process_json_array(text)
    if looks_like_csv(text):
        return process_csv(text)
    return process_jsonl(text)


def main() -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    is_filter = "--filter" in sys.argv

    if is_filter:
        data = sys.stdin.read()
        sys.stdout.write(process(data))
        return 0

    if "--in-place" in sys.argv and args:
        p = args[0]
        t = open(p, encoding="utf-8").read()
        open(p, "w", encoding="utf-8").write(process(t))
        return 0
    if len(args) >= 2:
        t = open(args[0], encoding="utf-8").read()
        open(args[1], "w", encoding="utf-8").write(process(t))
        return 0

    sys.stderr.write(
        "usage:\n"
        "  mask_phones.py --filter            # git clean filter: stdin -> stdout\n"
        "  mask_phones.py IN OUT             # 文件 -> 文件\n"
        "  mask_phones.py --in-place IN      # 就地脱敏\n"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
