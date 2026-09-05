# BeaconMFG 技术方案：认证 · 排名 · API

> 版本：v0.1 · 日期：2026-09-05 · 状态：草案
> 关联：改进方案 v0.1（BeaconMFG改进方案.md）

---

## 一、微信认证方案（认主验证）

### 1.1 为什么选微信/公众号

中国中小制造商极少有独立域名官网，大量只有：
- 微信公众号（服务号/订阅号）
- 阿里旺铺
- 1688 店铺

微信公众号（服务号）是最适合作为身份锚的渠道，原因：
- 有企业主体认证（工商注册信息绑定）
- 每个企业主体可绑定**唯一**服务号
- 支持模板消息推送（验证通知）
- 用户扫二维码即可完成，无需填表单

### 1.2 认证流程（三步完成）

```
┌─────────────────────────────────────────────────────────────┐
│                      供应商认主流程                           │
│                                                             │
│  ① 供应商访问平台认主页（网页）                                │
│     输入企业名称 + 关键词（品类/城市，辅助定位记录）              │
│                                                             │
│  ② 平台返回「用服务号扫码验证」                                  │
│     → 显示微信公众号二维码                                      │
│     → 供应商用企业服务号扫码（或关注公众号后发送口令）             │
│                                                             │
│  ③ 服务号推送验证消息                                          │
│     模板消息含 6 位数字验证码（有效期 10 分钟）                   │
│     供应商在平台网页输入验证码                                   │
│                                                             │
│  ④ 平台验证通过                                               │
│     → 更新该条记录的 claim 状态为 claimed/verified             │
│     → 绑定企业的微信 OpenID（加密存储）                          │
│     → 供应商可登录管理后台，上传 Skill 链接等                     │
└─────────────────────────────────────────────────────────────┘
```

**关键设计：为什么用验证码而非直接扫码绑定？**

- 直接扫码 = 任何人都能用服务号扫码认主（只要知道企业名）
- 验证码 = 只有服务号运营者才能看到模板消息并转发，起到「知情授权」作用
- 验证码有效期 10 分钟内必须填写，防止验证码被截获后暴力猜测

### 1.3 认证状态机

```
unclaimed ──[扫码+验证码]──► claimed ──[平台人工审核]──► verified
                                  │
                                  └── 可选：直接 verified（白名单豁免）
```

- `unclaimed`：默认状态，未经任何认证
- `claimed`：企业主体微信扫码验证通过（证明企业拥有该服务号）
- `verified`：平台人工审核或更高等级验证（如营业执照对比），可开放全部 Agent 对接能力

**claimed 状态已具备的商业价值：**
- 企业微信身份绑定，不可否认
- 可填写/更新自己的联系方式（平台审核后生效）
- 可提交 Skill URL 链接（平台审核后生效）
- 自动进入「认主供应商」列表，对采购 Agent 可见度更高

### 1.4 微信服务号配置要求

平台需注册**微信服务号**（非订阅号），具备：
- 模板消息权限（需单独申请行业模板，制造业相关）
- 网页授权获取用户信息（OAuth2）
- 企业微信主体认证（服务号默认已完成）

配置项：
```python
WECHAT_APP_ID = "wx_xxxxxxxxxxxxx"       # 服务号 AppID
WECHAT_APP_SECRET = "xxxxxxxxxxxxxxxx"   # 服务号 AppSecret
WECHAT_TOKEN = "xxxxxxxx"                # 验证 Token
WECHAT_TEMPLATE_ID = "TMxxxxxxx"         # 验证码模板消息 ID
```

### 1.5 微信认证 API（后端）

```
POST /api/v1/auth/wechat/verify-start
  Body: { "company_name": "xxx", "category": "精密机械加工" }
  → 返回: { "qrcode_url": "https://mp.weixin.qq.com/...", "session_id": "xxx" }
  说明：生成扫码会话，返回服务号二维码 URL

POST /api/v1/auth/wechat/send-code
  Body: { "session_id": "xxx" }
  → 后端：查询服务号粉丝中匹配企业名的用户，推送模板消息（含验证码）
  → 返回: { "sent": true }

POST /api/v1/auth/wechat/verify-code
  Body: { "session_id": "xxx", "code": "123456" }
  → 返回: { "success": true, "supplier_id": "CN-MFG-xxxx", "claim_token": "xxx" }
  说明：验证通过后颁发短期 claim_token，用于后续认主操作

POST /api/v1/auth/wechat/claim
  Headers: Authorization: Bearer <claim_token>
  Body: { "supplier_id": "CN-MFG-xxxx" }
  → 返回: { "status": "claimed", "can_add_skill": true }
```

### 1.6 小型供应商无服务号的降级方案

如果企业没有服务号，提供备选通道：

**方案 A：企业邮箱验证（最轻量）**
1. 供应商输入企业邮箱（如 `contact@company.com`）
2. 平台发送验证链接到该邮箱
3. 点击链接完成验证（证明企业可管理该域名邮箱）

**方案 B：人工审核兜底**
1. 供应商填写表单 + 上传营业执照（图片）
2. 平台运营者人工审核（1-3 个工作日）
3. 审核通过后人工标记为 claimed

两种降级方案均不推荐作为主流程（体验差/人工成本高），但需作为兜底保留。

---

## 二、排名算法

### 2.1 排序维度与权重

按「品类匹配度 > 认证状态 > 信息完善度 > 距离」顺序，设置如下：

| 维度 | 权重 | 计算方式 |
|---|---|---|
| **品类匹配度** | 40% | `关键词命中得分 / 最高可能命中数` |
| **认证状态** | 30% | 见下表 |
| **信息完善度** | 20% | `非空核心字段数 / 8` |
| **地理距离** | 10% | `1 - (距离_km / MAX_DISTANCE_KM)`，MAX=200km |

### 2.2 认证状态得分

| claim.status | 得分 | 说明 |
|---|---|---|
| `verified` | 1.0 | 平台人工审核通过，信任度最高 |
| `claimed` | 0.6 | 微信服务号验证，身份可信 |
| `unclaimed` | 0.0 | 默认状态 |

### 2.3 品类匹配度得分计算

```python
def category_match_score(supplier_keywords: list, query_keywords: list) -> float:
    """
    关键词命中得分 / 最大可能得分
    - 精确匹配（完整包含）：得 1.0 分
    - 模糊匹配（包含关联系）：得 0.5 分
    - 每条关键词只计一次
    """
    score = 0.0
    for q in query_keywords:
        q_lower = q.lower()
        matched = False
        for s in supplier_keywords:
            s_lower = s.lower()
            if q_lower == s_lower:
                score += 1.0   # 精确匹配
                matched = True
                break
            elif q_lower in s_lower or s_lower in q_lower:
                score += 0.5   # 模糊匹配
                matched = True
                break
        if not matched:
            # 语义相似度兜底（后续可引入 embedding）
            score += 0.0

    max_possible = len(query_keywords)  # 最多得满分
    return min(score / max_possible, 1.0) if max_possible > 0 else 0.0
```

### 2.4 信息完善度得分计算

核心字段（共 8 项）：
```
phone（contact_phone 非空且非"待核实"）
email（contact_email 非空）
address（address 非空）
website（website 非空）
certifications（certifications 非空且非空数组）
note（note 非空）
location（lat + lng 均非空）
keywords（keywords 数量 >= 3）
```

```python
def completeness_score(s: dict) -> float:
    fields = [
        bool(s.get("contact_phone") and s["contact_phone"] != "待核实"),
        bool(s.get("contact_email")),
        bool(s.get("address")),
        bool(s.get("website")),
        bool(s.get("certifications") and len(s["certifications"]) > 0),
        bool(s.get("note")),
        bool(s.get("lat") is not None and s.get("lng") is not None),
        bool(s.get("keywords") and len(s["keywords"]) >= 3),
    ]
    return sum(fields) / len(fields)  # 0.0 ~ 1.0
```

### 2.5 地理距离得分

```python
import math

def haversine(lat1, lng1, lat2, lng2):
    R = 6371  # km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def distance_score(supplier_lat, supplier_lng, query_lat, query_lng):
    if supplier_lat is None or query_lat is None:
        return 0.5  # 无坐标时给中间分，不加分也不减分
    dist = haversine(query_lat, query_lng, supplier_lat, supplier_lng)
    MAX_D = 200  # km，超过按 0 分计
    return max(0.0, 1.0 - dist / MAX_D)
```

### 2.6 综合得分与排序

```python
def rank_suppliers(suppliers: list, query: dict) -> list:
    """
    query: {
        "keywords": [...],
        "lat": float | None,
        "lng": float | None,
        "city": str | None,
        "category": str | None,
    }
    """
    scored = []
    for s in suppliers:
        w_cat   = 0.40
        w_claim = 0.30
        w_info  = 0.20
        w_dist  = 0.10

        s_cat   = category_match_score(s.get("keywords", []), query["keywords"])
        s_claim = claim_score(s.get("claim", {}).get("status", "unclaimed"))
        s_info  = completeness_score(s)
        s_dist  = distance_score(s.get("lat"), s.get("lng"),
                                 query.get("lat"), query.get("lng"))

        total = w_cat*s_cat + w_claim*s_claim + w_info*s_info + w_dist*s_dist
        scored.append((total, -s_info, s))   # 同分时信息完善度高的排前面

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, _, item in scored]
```

### 2.7 权重可配置性

排名权重通过 `X-Ranking-Profile` 请求头覆盖，支持预设 profiles：

| Profile | 说明 |
|---|---|
| `default` | 40/30/20/10（品类优先） |
| `nearest` | 20/20/20/40（距离优先） |
| `verified` | 30/40/20/10（认证优先） |
| `complete` | 30/20/40/10（信息完整优先） |

---

## 三、API 规范

### 3.1 设计原则

- **面向 Agent 优化**：JSON-only 响应、结构化错误、无 HTML、错误码明确
- **无需 Key 即可读**：GET 操作公开，降低 Agent 集成门槛
- **写入需要认证**：API Key（供应商后台）或 OAuth2（微信）
- **REST 风格但不过度教条**：资源 + 动词，符合 Agent 消费习惯
- **支持 CORS**：允许跨域请求（Agent 可能跨域调用）
- **版本控制**：所有端点带 `/v1/` 前缀

### 3.2 基础信息

```
Base URL: https://api.beacon-mfg.com/v1
Content-Type: application/json
字符编码: UTF-8
错误格式: { "error": { "code": "...", "message": "...", "details": {...} } }
```

### 3.3 认证方式

**读取（公开）：**
- 无需认证
- Rate limit：200 请求/分钟/IP（通过 X-Forwarded-For 识别）

**写入（供应商操作）：**
- API Key：`Authorization: Bearer <api_key>`
- Key 由平台在供应商认主成功后颁发

**Agent 特殊通道：**
- Agent 可申请专用 Key，免速率限制，用于高频检索场景
- 申请地址：`https://beacon-mfg.com/dev`（人工审核）

### 3.4 端点清单

#### 3.4.1 供应商检索（核心）

```
GET /suppliers
```

**查询参数：**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `keyword` | string | 否 | 产品关键词（支持空格分隔多词，如 `CNC 铝合金`） |
| `category` | string | 否 | 品类名称（精确匹配） |
| `city` | string | 否 | 城市名（精确匹配） |
| `province` | string | 否 | 省份名（精确匹配） |
| `lat` | float | 否 | 查询方纬度（触发距离排序） |
| `lng` | float | 否 | 查询方经度（触发距离排序） |
| `radius` | int | 否 | 半径（km），默认 200，与 lat/lng 共用 |
| `claimed` | bool | 否 | 只返回已认主供应商（true/false） |
| `verified` | bool | 否 | 只返回已验证供应商（true/false） |
| `has_skill` | bool | 否 | 只返回已配置 Skill 的供应商 |
| `page` | int | 否 | 页码，从 1 开始，默认 1 |
| `per_page` | int | 否 | 每页数量，默认 20，最大 100 |
| `sort` | string | 否 | 排序方式：`rank`（综合排名，默认）/`distance`/`completeness`/`claimed` |
| `ranking_profile` | string | 否 | 排名配置：`default`/`nearest`/`verified`/`complete` |

**响应示例：**

```json
{
  "meta": {
    "total": 247,
    "page": 1,
    "per_page": 20,
    "total_pages": 13,
    "query": {
      "keyword": "CNC 铝合金",
      "category": "精密机械加工",
      "city": "深圳"
    }
  },
  "suppliers": [
    {
      "id": "CN-MFG-0001234",
      "company": "深圳市鑫锐精密科技有限公司",
      "category": "精密机械加工",
      "keywords": ["CNC加工", "铝合金", "精密零部件", "小批量"],
      "region": { "province": "广东", "city": "深圳" },
      "lat": 22.5431,
      "lng": 114.0579,
      "address": "深圳市宝安区福永街道...",
      "contact_phone": "0755-2739XXXX",
      "claim": {
        "status": "claimed",
        "claimed_at": "2026-09-01"
      },
      "agent": {
        "skill_url": "https://xinrui.example.com/.well-known/beaconmfg-skill.json",
        "capabilities": ["catalog", "rfq"],
        "verified": false
      },
      "_rank_score": 0.847,
      "_rank_breakdown": {
        "category_match": 0.85,
        "claim": 0.60,
        "completeness": 0.75,
        "distance": 0.91
      }
    }
  ]
}
```

**说明：**
- `_rank_score` 和 `_rank_breakdown` 为可选字段，通过 `X-Include-Ranking-Debug: true` 请求头开启
- `agent` 字段仅在 `has_skill=true` 时返回
- `contact_phone` 完整展示（无脱敏），未核实时为 `"待核实"`

---

```
GET /suppliers/{id}
```

获取单条供应商详情。

| 参数 | 类型 | 说明 |
|---|---|---|
| `id` | string | 供应商 ID，如 `CN-MFG-0001234` |

**响应：**
- 404：供应商不存在
- 200：完整供应商记录（包含 claim / agent 字段）

---

```
GET /suppliers/{id}/skill
```

获取该供应商的官方 Skill 内容（从 `agent.skill_url` 加载）。

**安全策略：**
- 平台对 Skill 内容做**能力白名单过滤**（禁止任意代码执行工具）
- 返回过滤后的安全 Skill JSON

---

```
POST /suppliers/search  （可选，支持复杂查询）
```

POST Body 为查询对象（支持更复杂的布尔查询）：

```json
{
  "must": { "category": "精密机械加工" },
  "should": [{ "keyword": "铝合金" }, { "keyword": "钛合金" }],
  "filter": {
    "city": "深圳",
    "claimed": true
  },
  "geo": { "lat": 22.5, "lng": 114.0, "radius_km": 50 },
  "sort": "rank",
  "page": 1,
  "per_page": 20
}
```

#### 3.4.2 品类与地区

```
GET /categories
```

返回品类列表（含关键词词典）。

```
GET /regions
```

返回地区索引（城市 → 供应商 ID 列表）。

```
GET /regions/{city}
```

返回指定城市的供应商统计（品类分布）。

#### 3.4.3 认主与更新（需认证）

```
POST /claim
Authorization: Bearer <claim_token>（微信验证码流程获取）
Body: { "supplier_id": "CN-MFG-xxxx" }
```

```
GET /me
Authorization: Bearer <api_key>
```

获取当前供应商的完整档案（认主后管理后台用）。

```
PATCH /me
Authorization: Bearer <api_key>
```

更新当前供应商的联系信息（需平台审核后生效）：

```json
{
  "contact_phone": "新号码",
  "contact_email": "new@example.com",
  "note": "主营：..."
}
```

#### 3.4.4 Skill 管理（认主后）

```
POST /me/skill
Authorization: Bearer <api_key>
Body: {
  "skill_url": "https://company.com/.well-known/beaconmfg-skill.json",
  "protocol": "skill"
}
```

提交 Skill URL，平台审核（内容安全 + 域名验证）后生效。

```
DELETE /me/skill
Authorization: Bearer <api_key>
```

下架 Skill。

#### 3.4.5 统计与健康

```
GET /stats
```

返回全局统计（供 Agent 了解数据规模）：

```json
{
  "total": 7962,
  "real": 4791,
  "template": 3167,
  "claimed": 0,
  "verified": 0,
  "with_skill": 0,
  "categories": [...],
  "cities": 18
}
```

```
GET /health
```

健康检查端点（用于监控）。

### 3.5 错误码

| HTTP 状态码 | error.code | 说明 |
|---|---|---|
| 400 | `INVALID_PARAMS` | 参数校验失败 |
| 400 | `INVALID_CATEGORY` | 品类名称不在索引中 |
| 400 | `INVALID_CITY` | 城市不在地区索引中 |
| 401 | `UNAUTHORIZED` | 缺少或无效认证 |
| 403 | `FORBIDDEN` | 无权操作（如非本人认主记录） |
| 404 | `SUPPLIER_NOT_FOUND` | 供应商 ID 不存在 |
| 409 | `ALREADY_CLAIMED` | 该供应商已被其他企业认主 |
| 429 | `RATE_LIMITED` | 请求频率超限 |
| 500 | `INTERNAL_ERROR` | 服务端错误 |

### 3.6 速率限制（Rate Limiting）

| 端点类型 | 限制 | 说明 |
|---|---|---|
| GET /suppliers（公开） | 200次/分钟/IP | 正常 Agent 足够 |
| GET /suppliers（带 Key） | 2000次/分钟/Key | 认证用户放宽 |
| POST /claim | 10次/小时/IP | 防滥用 |
| PATCH /me | 30次/小时/Key | 防频繁修改 |
| 其他写入 | 60次/小时/Key | 标准限制 |

响应头包含：
```
X-RateLimit-Limit: 200
X-RateLimit-Remaining: 187
X-RateLimit-Reset: 1725523200
```

---

## 四、部署架构建议

```
                    ┌─────────────────┐
                    │  Cloudflare/CDN │
                    │  (HTTPS + WAF)  │
                    └────────┬────────┘
                             │
              ┌──────────────▼──────────────┐
              │        Nginx / Caddy        │
              │  (反向代理 + SSL + 静态资源) │
              └──────────────┬──────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                    │
  ┌──────▼──────┐    ┌───────▼──────┐    ┌───────▼──────┐
  │  FastAPI    │    │  微信服务号   │    │  PostgreSQL  │
  │  Web API    │    │  消息回调     │    │  (元数据)    │
  │  (异步)     │    │  Webhook     │    │             │
  └─────────────┘    └──────────────┘    └──────────────┘
         │                                        │
  ┌──────▼────────────────────────────────────────▼──┐
  │              JSON 文件存储（OSS / S3）             │
  │  data/suppliers/*.json（主数据，不可变快照）         │
  │  data/en/*.json（英文镜像）                         │
  └──────────────────────────────────────────────────┘
```

- **JSON 文件**作为主数据（Git 管理，不可变快照），OSS 存储用于 API 服务
- **PostgreSQL**存储认主状态、API Key、Skill 索引等元数据
- **FastAPI**异步处理，支持高并发 Agent 检索
- **微信服务号**作为独立认证渠道，通过 Webhook 与主服务交互

### 数据更新流程

```
Git push（PR 合并）
    ↓
GitHub Actions 触发
    ↓
CI 脚本：JSON Schema 校验 + 数字一致性检查
    ↓
通过 → 上传新 JSON 到 OSS
    ↓
PostgreSQL 更新 supplier_meta（如果有新增 ID）
    ↓
CDN 刷新
```

---

## 五、实施优先级

| 优先级 | 任务 | 原因 |
|---|---|---|
| P0 | 微信服务号注册 + 认证流程 | 无认证无法跑通认主飞轮 |
| P0 | GET /suppliers 实现（内存检索） | 最核心功能，MVP 必须 |
| P1 | 排名算法实现 | 体现品类匹配度优势 |
| P1 | 认主 API（POST /claim + PATCH /me） | 商业闭环 |
| P1 | Skill 提交与白名单过滤 | Agent 对接层 |
| P2 | 微信模板消息回调 | 认证流程完整化 |
| P2 | PostgreSQL 元数据层 | 规模化后必须 |
| P3 | Agent 专用 Key 申请流程 | 开发者生态 |

---

*本文档为技术方案草案，落地前需结合微信服务号申请进度和 MVP 验证结果迭代修订。*
