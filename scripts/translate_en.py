#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 英文翻译 —— 用智谱 GLM-4-Flash（免费模型）把供应商数据翻译成英文镜像

- 读 data/suppliers/*.json → 批量翻译 → 写 data/en/<category_en>.json
- 品类名与常见省市用手工映射（准确），公司名/地址/关键词/备注用 LLM 翻译
- API: https://open.bigmodel.cn/api/paas/v4/chat/completions  model: glm-4-flash
- Key: .env 的 ZHIPU_API_KEY，或 --key 传入；不入 git

用法:
    python scripts/translate_en.py               # 全量翻译（从 .env 读 key）
    python scripts/translate_en.py --key xxx     # 指定 key
    python scripts/translate_en.py --dry-run     # 只打印计划
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "data" / "suppliers"
EN_DIR = ROOT / "data" / "en"
ENV_FILE = ROOT / ".env"

ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-4-flash"
BATCH = 8          # 每批翻译条数
DELAY = 1.0        # 批次间延时（尊重免费模型速率限制）

# 品类中英映射（手工维护，准确优先）
CATEGORY_EN = {
    "精密机械加工": "Precision Machining",
    "钣金冲压": "Sheet Metal & Stamping",
    "注塑成型": "Injection Molding",
    "压铸": "Die Casting",
    "电子元器件": "Electronic Components",
    "表面处理": "Surface Treatment",
    "标准件": "Standard Parts",
    "原材料": "Raw Materials",
}
CATEGORY_FILE_EN = {
    "精密机械加工": "precision-machining",
    "钣金冲压": "sheet-metal",
    "注塑成型": "injection-molding",
    "压铸": "die-casting",
    "电子元器件": "electronic-components",
    "表面处理": "surface-treatment",
    "标准件": "standard-parts",
    "原材料": "raw-materials",
}
REGION_EN = {
    "广东": "Guangdong", "江苏": "Jiangsu", "浙江": "Zhejiang", "福建": "Fujian",
    "山东": "Shandong", "上海": "Shanghai", "北京": "Beijing",
    "深圳": "Shenzhen", "东莞": "Dongguan", "苏州": "Suzhou", "宁波": "Ningbo",
    "佛山": "Foshan", "无锡": "Wuxi", "广州": "Guangzhou", "青岛": "Qingdao",
    "天津": "Tianjin", "重庆": "Chongqing", "成都": "Chengdu",
}

SYSTEM_PROMPT = (
    "You are a professional translator for a B2B manufacturing supplier directory "
    "(BeaconMFG). Translate Chinese supplier data into concise, professional English. "
    "Rules: company = company name (keep Chinese name transliteration if no official "
    "English name); keywords = product/service terms (short list); address = postal "
    "address; note = short business description. Note: 高德 = AMap (a map service "
    "provider), keep it as 'AMap public directory'. Return ONLY a JSON array matching "
    "the input order, each item: {\"company\":\"\",\"keywords\":[\"\"],\"address\":\"\","
    "\"note\":\"\"}. No extra text."
)


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def call_glm(api_key, items):
    """调用 GLM-4-Flash 翻译一批 items（[{company,keywords,address,note}]），返回同序结果"""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
        ],
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        ZHIPU_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    return json.loads(content)


def translate_records(api_key, records, cat):
    """records: 中文记录列表 → 英文镜像列表"""
    out = []
    items = [
        {
            "company": r.get("company", ""),
            "keywords": r.get("keywords", []),
            "address": r.get("address", "") or "",
            "note": r.get("note", "") or "",
        }
        for r in records
    ]
    for i in range(0, len(items), BATCH):
        chunk = items[i : i + BATCH]
        for attempt in range(3):
            try:
                translated = call_glm(api_key, chunk)
                break
            except Exception as e:
                print(f"  批次 {i // BATCH + 1} 失败（{attempt + 1}/3）: {e}")
                if attempt == 2:
                    translated = None
                else:
                    time.sleep(3)
        for j, r in enumerate(records[i : i + BATCH]):
            region = r.get("region", {})
            if translated:
                t = translated[j] if j < len(translated) else {}  # 容错：LLM 偶尔漏项
            else:
                t = {}
            out.append({
                "id": r["id"],
                "company_en": t.get("company") or r.get("company", ""),
                "category": cat,
                "category_en": CATEGORY_EN.get(cat, cat),
                "keywords_en": t.get("keywords") or r.get("keywords", []),
                "region": {
                    "province": REGION_EN.get(region.get("province", ""), region.get("province", "")),
                    "city": REGION_EN.get(region.get("city", ""), region.get("city", "")),
                },
                "address_en": t.get("address") or (r.get("address") or ""),
                "contact_phone": r.get("contact_phone", ""),
                "source": r.get("source", ""),
                "verified_at": r.get("verified_at", ""),
                "note_en": t.get("note") or (r.get("note") or ""),
            })
        print(f"  批次 {i // BATCH + 1}/{(len(items) + BATCH - 1) // BATCH} 完成（{len(out)}/{len(records)}）")
        time.sleep(DELAY)
    return out


def main():
    parser = argparse.ArgumentParser(description="BeaconMFG 英文翻译（GLM-4-Flash 免费模型）")
    parser.add_argument("--key", help="智谱 API Key（默认读 .env 的 ZHIPU_API_KEY）")
    parser.add_argument("--category", help="只翻译指定品类（可选）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    args = parser.parse_args()

    api_key = args.key or load_env().get("ZHIPU_API_KEY", "")
    if not api_key:
        print("缺少智谱 API Key：.env 配置 ZHIPU_API_KEY 或用 --key 传入")
        raise SystemExit(1)

    files = sorted(SRC_DIR.glob("*.json"))
    if args.category:
        files = [f for f in files if json.load(open(f, encoding="utf-8"))[0].get("category") == args.category] if files else []

    if args.dry_run:
        total = 0
        for f in files:
            recs = json.load(open(f, encoding="utf-8"))
            total += len(recs)
            print(f"{f.name}: {len(recs)} 条")
        print(f"共 {total} 条，按每批 {BATCH} 条，约需 {max(1, (total + BATCH - 1) // BATCH)} 次 API 调用")
        return

    EN_DIR.mkdir(parents=True, exist_ok=True)
    total_done = 0
    for f in files:
        cat = json.load(open(f, encoding="utf-8"))[0]["category"] if json.load(open(f, encoding="utf-8")) else None
        if cat not in CATEGORY_EN:
            continue
        records = json.load(open(f, encoding="utf-8"))
        print(f"\n翻译 {f.name}（{cat}，{len(records)} 条）…")
        en = translate_records(api_key, records, cat)
        out = EN_DIR / f"{CATEGORY_FILE_EN[cat]}.json"
        out.write_text(json.dumps(en, ensure_ascii=False, indent=2), encoding="utf-8")
        total_done += len(en)
        print(f"→ 已写 {out.relative_to(ROOT)}（{len(en)} 条）")

    print(f"\n完成：{total_done} 条英文数据 → data/en/")
    print("提示：海外 Agent 可加载 SKILL_EN.md 使用英文数据集；更新中文数据后重跑本脚本同步。")


if __name__ == "__main__":
    main()
