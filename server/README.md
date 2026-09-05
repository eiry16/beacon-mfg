# BeaconMFG API 服务

制造业供应商检索与认主 API，基于 FastAPI + Uvicorn。

## 数据文件说明

> **重要：** 所有数据文件位于项目根目录的 `../data/`（即 `C:\DATA\QClaw\Workspace\beacon-mfg\data\`），服务**只读不写**，不会修改原始 JSON 文件。

数据源：
- `data/index.json` — 品类索引（含关键词词典）
- `data/suppliers/*.json` — 按品类存储的供应商详情（8 个品类文件）
- `data/region-index.json` — 城市→供应商ID 映射索引

## 快速启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动服务（开发模式，支持热重载）
cd server
python -m uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# 3. 访问文档
open http://localhost:8080/docs
```

## 依赖

```
fastapi>=0.100.0
uvicorn[standard]>=0.23.0
pydantic>=2.0.0
httpx>=0.25.0
python-multipart>=0.0.6
```

## API 概览

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/` | — | 服务信息 |
| GET | `/health` | — | 健康检查 |
| GET | `/stats` | — | 全局统计 |
| POST | `/reload` | X-Reload-Secret | 热更新数据 |
| GET | `/suppliers` | — | 供应商列表（支持搜索/过滤/排序） |
| GET | `/suppliers/{id}` | — | 供应商详情 |
| GET | `/suppliers/{id}/skill` | — | 供应商 Skill 信息 |
| GET | `/categories` | — | 品类列表 |
| GET | `/regions` | — | 城市列表 |
| GET | `/regions/{city}` | — | 城市供应商 ID 列表 |
| POST | `/claim/verify-start` | — | 认主流程开始（stub） |
| POST | `/claim/send-code` | — | 发送验证码（stub） |
| POST | `/claim/verify-code` | — | 验证验证码（stub） |
| POST | `/claim/confirm` | — | 确认认主（stub） |
| GET | `/me` | Bearer API Key | 当前供应商信息 |
| PATCH | `/me` | Bearer API Key | 更新联系信息 |
| POST | `/me/skill` | Bearer API Key | 提交 Skill（白名单过滤） |
| DELETE | `/me/skill` | Bearer API Key | 下架 Skill |

## 供应商检索

### GET /suppliers

**查询参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `keyword` | string | 关键词（逗号分隔），匹配公司名/品类/地址 |
| `category` | string | 品类名称（如 `精密机械加工`） |
| `city` | string | 城市名（如 `东莞`） |
| `province` | string | 省份名（如 `广东`） |
| `lat` / `lng` | float | 搜索中心坐标 |
| `radius` | float | 搜索半径（km） |
| `claimed` | bool | 已认主 |
| `verified` | bool | 已认证 |
| `has_skill` | bool | 有 Skill |
| `page` | int | 页码（默认 1） |
| `per_page` | int | 每页数量（默认 20，最大 100） |
| `sort` | string | `relevance`（默认）/ `distance` / `completeness` |
| `ranking_profile` | string | `default` / `nearest` / `verified` / `complete` |

**请求头：**

| 头部 | 说明 |
|------|------|
| `X-Include-Ranking-Debug: true` | 在返回结果中包含 `_rank_score` 和 `_rank_debug` |
| `X-Ranking-Profile` | 覆盖 ranking_profile 参数 |

**示例：**

```bash
# 按品类搜索（上海附近的 CNC 加工供应商）
curl "http://localhost:8080/suppliers?category=精密机械加工&keyword=CNC&city=上海&lat=31.2&lng=121.5&radius=50&sort=relevance&ranking_profile=nearest"
```

## 排名算法

综合评分公式（`ranking_profile=default`）：

```
品类匹配度 = (精确命中数 + 0.5×模糊命中数) / 查询关键词总数  （上限 1.0）
认证状态   = verified:1.0  claimed:0.6  unclaimed:0.0
完善度     = 核心字段非空数 / 8
距离       = max(0, 1 - 距离km/200)，无坐标给 0.5
综合分     = 0.4×品类 + 0.3×认证 + 0.2×完善度 + 0.1×距离
```

Profile 权重切换：

| Profile | 品类 | 认证 | 完善度 | 距离 |
|---------|------|------|--------|------|
| `default` | 0.4 | 0.3 | 0.2 | 0.1 |
| `nearest` | 0.2 | 0.2 | 0.2 | 0.4 |
| `verified` | 0.2 | 0.4 | 0.2 | 0.2 |
| `complete` | 0.2 | 0.2 | 0.4 | 0.2 |

## 速率限制

| 操作类型 | 限制 | Key |
|----------|------|-----|
| GET 读取 | 200 次/分钟 | 每 IP |
| POST/PATCH 写入 | 60 次/分钟 | 每 API Key |

超限返回 HTTP 429，含 `Retry-After` 头。

## 错误响应格式

所有端点统一使用以下格式：

```json
{
  "error": {
    "code": "INVALID_PARAMS",
    "message": "参数校验失败",
    "details": {"field": "category", "reason": "不在索引中"}
  }
}
```

## API Key 认证（Stub）

Bearer Token 格式：任意有效 UUID（如 `a1b2c3d4-e5f6-7890-abcd-ef1234567890`）均可通过验证。

**示例：**

```bash
curl -H "Authorization: Bearer a1b2c3d4-e5f6-7890-abcd-ef1234567890" \
     http://localhost:8080/me
```

> ⚠️ 当前为 Phase 0 Stub 实现，生产环境需接入真实微信登录和密钥管理。

## Skill 白名单过滤

`POST /me/skill` 会自动对 Skill JSON 进行安全审查：

**允许的工具类型：** `catalog`, `rfq`, `quote`, `live_chat`, `search`, `query`

**禁止的工具类型：** `shell`, `exec`, `system`, `code_interpreter`, `download`, `upload`, `database`, `delete`

禁止的工具会被自动移除，并在响应中返回警告。

## 项目结构

```
server/
├── requirements.txt
├── main.py              # FastAPI 应用入口
├── config.py            # 配置（端口/路径/权重）
├── loaders/
│   ├── supplier_loader.py   # 供应商数据加载与索引
│   └── region_loader.py     # 城市索引加载
├── models/
│   ├── supplier.py          # Pydantic 模型
│   └── responses.py        # API 响应模型
├── routers/
│   ├── suppliers.py         # GET /suppliers, /suppliers/{id}
│   ├── categories.py        # GET /categories, /regions
│   ├── claim.py             # POST /claim/*
│   ├── me.py                # GET/PATCH /me, /me/skill
│   └── stats.py             # GET /stats, /health
├── services/
│   ├── ranker.py            # 排名算法
│   └── skill_filter.py      # Skill 安全过滤
└── auth/
    └── api_key.py           # API Key 验证依赖
```
