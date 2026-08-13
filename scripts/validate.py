#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BeaconMFG 数据校验脚本（纯标准库，供 CI 与本地 PR 前使用）

检查项：
1. data/index.json 是合法 JSON，品类文件路径存在
2. 每条记录满足 schema/supplier.schema.json 的核心约束
3. id 全局唯一
4. 真实数据（is_template=false）必须标注 source 且不得使用占位联系方式

用法:
    python scripts/validate.py
    退出码 0 = 通过，1 = 有错误
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "data" / "index.json"
SUPPLIERS_DIR = ROOT / "data" / "suppliers"

ID_RE = re.compile(r"^CN-MFG-\d{4}$")
PLACEHOLDER_MARKERS = ["XXXX", "待核实", "占位", "example", "示例"]


def err(msg):
    print(f"[ERROR] {msg}")


def check_supplier(item, path, seen_ids):
    ok = True
    for field in ("id", "company", "category", "keywords", "region", "contact_phone", "source", "source_url", "verified_at", "is_template"):
        if field not in item:
            err(f"{path}: 缺少必填字段 '{field}'")
            ok = False

    if "id" in item:
        if not ID_RE.match(item["id"]):
            err(f"{path} ({item.get('id')}): id 格式应为 CN-MFG-XXXX")
            ok = False
        if item["id"] in seen_ids:
            err(f"重复 id: {item['id']}")
            ok = False
        seen_ids.add(item["id"])

    if "keywords" in item and not item["keywords"]:
        err(f"{path} ({item.get('id')}): keywords 不能为空")
        ok = False

    if "is_template" in item:
        if item["is_template"] is True:
            pass  # 模板数据放行
        else:
            # 真实数据检查
            if item.get("source") in (None, "template", ""):
                err(f"{path} ({item.get('id')}): 真实数据的 source 不能为空或 template")
                ok = False
            phone = item.get("contact_phone") or ""
            if any(m in phone for m in PLACEHOLDER_MARKERS):
                err(f"{path} ({item.get('id')}): 真实数据联系方式疑似占位符 '{phone}'")
                ok = False
    return ok


def main():
    ok = True

    # 1. index.json
    try:
        with open(INDEX, encoding="utf-8") as f:
            index = json.load(f)
        categories = index.get("categories", [])
        if not categories:
            err("index.json: categories 为空")
            ok = False
    except (json.JSONDecodeError, OSError) as e:
        err(f"index.json 无法解析: {e}")
        sys.exit(1)

    # 2. 品类文件存在 + 记录校验
    seen_ids = set()
    total = 0
    for cat in categories:
        path = SUPPLIERS_DIR / cat["file"]
        if not path.exists():
            err(f"品类 '{cat['name']}' 文件不存在: {cat['file']}")
            ok = False
            continue
        try:
            with open(path, encoding="utf-8") as f:
                items = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            err(f"{cat['file']} 无法解析: {e}")
            ok = False
            continue
        total += len(items)
        for item in items:
            if not check_supplier(item, cat["file"], seen_ids):
                ok = False

    # 3. 分类字段与 index 一致性
    valid_categories = {c["name"] for c in categories}
    for cat in categories:
        path = SUPPLIERS_DIR / cat["file"]
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for item in json.load(f):
                if item.get("category") not in valid_categories:
                    err(f"{cat['file']}: 未知 category '{item.get('category')}'")
                    ok = False

    if ok:
        print(f"校验通过：{len(categories)} 个品类，{total} 条记录")
    else:
        print("校验未通过，请修复后重试。")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
