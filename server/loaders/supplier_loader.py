"""供应商数据加载与内存索引构建"""
import json
import logging
from pathlib import Path
from typing import Any

from config import INDEX_JSON, SUPPLIERS_DIR, REGION_INDEX_JSON

logger = logging.getLogger(__name__)

# ─── 内存索引（全局单例） ───────────────────────────────────────────────────

# category_name → list[supplier_dict]
_category_map: dict[str, list[dict]] = {}

# supplier_id → supplier_dict
_supplier_index: dict[str, dict] = {}

# city_key "省-市" → list[supplier_id]
_city_index: dict[str, list[str]] = {}

# 预计算每条记录的 completeness_score（避免每次查询重算）
_completeness_cache: dict[str, float] = {}

_LOADED = False
_LOAD_VERSION = 0


# ─── 核心字段（用于计算信息完善度） ──────────────────────────────────────────
_COMPLETENESS_FIELDS = [
    "contact_phone",
    "email",
    "address",
    "website",
    "certifications",
    "note",
    "location",
    "keywords",
]

# keywords 子字段阈值
_KEYWORDS_THRESHOLD = 3


def _compute_completeness(record: dict[str, Any]) -> float:
    """计算信息完善度得分（0.0 ~ 1.0）"""
    filled = 0
    for field in _COMPLETENESS_FIELDS:
        val = record.get(field)
        if val is None:
            continue
        if isinstance(val, str) and val.strip():
            filled += 1
        elif isinstance(val, (list, dict)):
            if field == "keywords" and isinstance(val, list):
                filled += 1 if len(val) >= _KEYWORDS_THRESHOLD else 0
            else:
                filled += 1
    return filled / len(_COMPLETENESS_FIELDS)


def _load_all() -> None:
    """加载所有数据文件并建立内存索引"""
    global _category_map, _supplier_index, _city_index, _completeness_cache, _LOADED, _LOAD_VERSION

    _category_map.clear()
    _supplier_index.clear()
    _city_index.clear()
    _completeness_cache.clear()

    # 1. 读取 index.json 获取品类→文件映射
    with INDEX_JSON.open(encoding="utf-8") as f:
        index_data = json.load(f)

    for cat in index_data.get("categories", []):
        cat_name = cat["name"]
        cat_file = SUPPLIERS_DIR / cat["file"]
        if not cat_file.exists():
            logger.warning("品类文件不存在: %s", cat_file)
            continue

        with cat_file.open(encoding="utf-8") as f:
            suppliers = json.load(f)

        _category_map[cat_name] = suppliers

        for sup in suppliers:
            sid = sup.get("id")
            if not sid:
                continue
            _supplier_index[sid] = sup
            _completeness_cache[sid] = _compute_completeness(sup)

    # 2. 读取 region-index.json 建立城市→id 列表索引
    with REGION_INDEX_JSON.open(encoding="utf-8") as f:
        region_data = json.load(f)

    for city_key, info in region_data.get("index", {}).items():
        _city_index[city_key] = info.get("ids", [])

    _LOADED = True
    _LOAD_VERSION += 1
    logger.info(
        "数据加载完成: 供应商 %d 条，品类 %d 个，城市 %d 个 (v%d)",
        len(_supplier_index),
        len(_category_map),
        len(_city_index),
        _LOAD_VERSION,
    )


def load() -> None:
    """启动时调用一次"""
    _load_all()


def reload() -> None:
    """热更新数据（不重启服务）"""
    _load_all()


def is_loaded() -> bool:
    return _LOADED


def get_supplier(supplier_id: str) -> dict[str, Any] | None:
    return _supplier_index.get(supplier_id)


def get_suppliers_by_category(category: str) -> list[dict[str, Any]]:
    return _category_map.get(category, [])


def get_suppliers_by_city(city_key: str) -> list[dict[str, Any]]:
    """city_key 格式：'省-市'，如 '广东-东莞'"""
    ids = _city_index.get(city_key, [])
    return [_supplier_index[sid] for sid in ids if sid in _supplier_index]


def get_suppliers_by_ids(ids: list[str]) -> list[dict[str, Any]]:
    return [_supplier_index[sid] for sid in ids if sid in _supplier_index]


def get_all_suppliers() -> list[dict[str, Any]]:
    return list(_supplier_index.values())


def get_categories() -> list[dict[str, Any]]:
    """返回 index.json 中的品类列表（不含详细数据）"""
    with INDEX_JSON.open(encoding="utf-8") as f:
        index_data = json.load(f)
    return index_data.get("categories", [])


def get_cities() -> list[str]:
    """返回所有城市 key 列表"""
    return list(_city_index.keys())


def get_load_version() -> int:
    return _LOAD_VERSION


def completeness_score(supplier_id: str) -> float:
    return _completeness_cache.get(supplier_id, 0.0)


def supplier_count() -> int:
    return len(_supplier_index)
