"""地区/城市索引加载器"""
import json
from pathlib import Path

from config import REGION_INDEX_JSON

# 内存缓存
_region_data: dict | None = None


def load() -> dict:
    """加载地区索引到内存"""
    global _region_data
    if _region_data is None:
        with REGION_INDEX_JSON.open(encoding="utf-8") as f:
            _region_data = json.load(f)
    return _region_data


def get_all_cities() -> list[str]:
    """返回所有城市 key 列表（'省-市'）"""
    data = load()
    return list(data.get("index", {}).keys())


def get_city_info(city_key: str) -> dict | None:
    """返回指定城市的信息，含 ids 列表"""
    data = load()
    return data.get("index", {}).get(city_key)


def get_metadata() -> dict:
    """返回 region-index.json 的 metadata"""
    data = load()
    return data.get("metadata", {})
