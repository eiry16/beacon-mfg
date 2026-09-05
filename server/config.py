"""应用配置"""
from pathlib import Path

# 项目根目录（server/）
BASE_DIR = Path(__file__).parent.resolve()

# 数据文件根目录（server 的上一级）
DATA_DIR = BASE_DIR.parent / "data"

# 数据源路径
INDEX_JSON = DATA_DIR / "index.json"
REGION_INDEX_JSON = DATA_DIR / "region-index.json"
SUPPLIERS_DIR = DATA_DIR / "suppliers"

# 服务端口
HOST = "0.0.0.0"
PORT = 8080

# 速率限制（每 IP/Key 每分钟）
RATE_LIMIT_READ = 200   # GET 请求：200次/分钟/IP
RATE_LIMIT_WRITE = 60   # POST/PATCH 请求：60次/分钟/Key
RATE_WINDOW = 60        # 时间窗口秒

# 排名权重
RANK_WEIGHTS = {
    "category": 0.4,
    "verified": 0.3,
    "completeness": 0.2,
    "distance": 0.1,
}

# 坐标参考（中国中心）
DEFAULT_LNG = 113.0
DEFAULT_LAT = 23.0
