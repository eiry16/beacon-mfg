# BeaconMFG 数据格式规范（SPEC）

> 本文档描述**对外发布的数据格式契约** —— 分片布局、manifest 结构、字段语义。
> 客户 Agent 与第三方消费者按它取数与校验。
>
> 取数入口是 MCP 服务或薄框架客户端（见 `SKILL.md` §1），**不是本地脚本**：
> 抓取 / 派生重建 / 发布流水线属本地工厂资产，2026-09-22 起不在本仓库。

---

## 1. 存储布局

```
data/
  manifest.json               分片清单（唯一取数入口）
  gb/{门类}/{大类}/{小类}.json    中文完整档案（zh）        ← 284 片
  en/gb/{门类}/{大类}/{小类}.json 英文镜像（en，与中文同构） ← 281 片
  phone-index.jsonl           号码索引（phone）             ← 1 片
  gb-index.json               行业层级树 + 逐级家数
  gb-alias.json               采购词 → 国标码别名表
  gb-alias-curated.json       人工校订过的别名表（优先级更高）
  gb4754-full.json            国标行业名全表
  industry-index.json         国标小类 → 企业 id 列表
  region-index.json           城市 → 企业 id 列表
  endpoint.json               App 数据源发现指针
  DATA_STATS.md               统计事实（家数以它为准）
skills/registry/
  fingerprint/gb/{门类}/{大类}/{小类}.jsonl   L0 指纹（fp）  ← 273 片
  index/meta.json · index/city.json · index/terms/b####.json   倒排索引
  capability/{id}.json        L1 能力卡（**只在云端**，不进本仓库）
skills/vendors/{id}/SKILL.md  L2 厂商自述（**只在云端**，不进本仓库）
```

**门类**取值 7 个：`C` 制造业 · `F` 批发和零售业 · `H` 住宿和餐饮业 ·
`I` 信息技术服务业 · `M` 科研技术服务业 · `O` 居民服务与修理业 · `R` 文化体育娱乐业。

⚠️ **一个国标码可能拆成多个续片**（`3484.json` + `3484-p2.json`）。按国标码取数必须
**扫全部续片**，只取第一片会静默漏数据。

---

## 2. `manifest.json`

```jsonc
{
  "metadata": {
    "schema": "1.0",
    "total_shards": 839,
    "total_bytes": 365723391,
    "generated_at": "YYYY-MM-DD",
    "by_type": {
      "fp":    { "shards": 273, "bytes": 29796856,  "records": 139318 },
      "zh":    { "shards": 284, "bytes": 196852032, "records": 139318 },
      "en":    { "shards": 281, "bytes": 136143522, "records": 135072 },
      "phone": { "shards": 1,   "bytes": 2930981,   "records": 103137 }
    }
  },
  "shards": [ { "p": "...", "b": "...", "c": "...", "n": "...", "t": "fp", "k": 0, "z": 0, "h": "...", "u": "..." } ]
}
```

分片条目字段：

| 字段 | 含义 |
|---|---|
| `p` | 分片路径（相对仓库根，同时也是相对 CDN 根的 URL 路径） |
| `b` | 归档桶键 |
| `c` | 国标码 |
| `n` | 国标名称 |
| `t` | 类型：`fp` 指纹 / `zh` 中文 / `en` 英文 / `phone` 号码索引 |
| `k` | 记录条数 |
| `z` | 字节数 |
| `h` | 内容 SHA1 —— **用于下载后校验完整性** |
| `u` | 最后更新 |

### 增量更新的正确做法

- **用 HTTP `ETag` 做增量，不要用 `h`。** `h` 是内容摘要，不是版本号；
  把它当 ETag 用会在内容相同的不同版本间误判。
- 流程：拉 `manifest.json` → 筛需要的分片 → 缓存响应头 `ETag` →
  下次带 `If-None-Match`，命中返回 `304`（零传输）。
- `h` 的用途只有一个：下载完分片后本地算 SHA1 与它对拍，确认**没被截断/篡改**。

⚠️ **所有请求必须带 User-Agent**：Cloudflare 对无 UA 请求返回 `403`（error 1010），
现象是「源不通」，常被误判为服务故障。

---

## 3. 分层契约

| 层 | 内容 | 供给方 | 是否进本仓库 |
|---|---|---|---|
| **L0 指纹** | 企业名 / 城市 / 国标码 / 工艺 / 材料 / 认证 / 是否含电话 | `skills/registry/fingerprint/**` | 是 |
| **L1 能力卡** | 工艺位、设备、产能、认证、起订量等硬指标 | `skills/registry/capability/{id}.json` | **否**（只在云端 R2） |
| **L2 自述** | 厂商自述（一厂一 Skill） | `skills/vendors/{id}/SKILL.md` | **否**（只在云端 R2） |

**L0 刻意不存电话号码** —— 号码只由 `phone` 分片提供。
这样指纹层可以任意全量扫描而不触碰联系方式。

---

## 4. 记录字段

### 4.1 中文完整档案（`data/gb/{门类}/{大类}/{小类}.json`，JSON 数组）

字段的**权威定义**是 `schema/supplier.schema.json`（JSON Schema draft-07）。
下表是可读摘要，**字段名以 Schema 为准**：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | str | 全局唯一，形如 `CN-MFG-0106029` |
| `company` | str | 企业名 |
| `category` | str | 品类（如「注塑成型」）。**与国标 `industry` 是两套口径**，不要互相替代 |
| `keywords` | list[str] | 检索关键词 |
| `region` | obj | `{province, city}` —— 只有省和市，**区县不在这一层**（见 `amap.adcode`，或指纹行的 `dist`） |
| `address` | str | 地址 |
| `lat` / `lng` | num | 经纬度 |
| `industry` | obj \| null | `{code, name, level, path, confidence, source, evidence}`。`code` 是国标码，`path` 是四级路径（`C 制造业 > 农副食品加工业 > 谷物磨制 > 玉米加工`）。**为 null = 尚未判定，不硬贴标签**，这类记录仍在名录里 |
| `is_manufacturer` | bool | 是否制造企业 |
| `contact_phone` | str | 公开联系方式。**空 = 未采集到，不要编造** |
| `source` / `source_url` | str | 来源（`public_directory` / `certification`）与可溯链接 |
| `status` | str | `verified` 已核实 / `unverified_poi` 待核实 |
| `is_template` | bool | ⚠️ 语义易误读：`true` **不是**占位模板，而是**电话待核实**的真实企业 POI（对应 `status: "unverified_poi"`），**检索时应保留** |
| `verified_at` / `imported_at` / `last_verified_at` | date \| null | 核验与导入时间 |
| `amap` | obj | 原始 POI 元数据（`poi_id` / `typecode` / `adcode` / `search_keyword`…），用于溯源 |
| `note` | str | 备注 |

> ⚠️ **不要按直觉写字段名。** `name` / `gb` / `proc` / `city` 这些**都不在 zh 记录里**——
> 中文档案用的是 `company` / `industry.code` / `category`；短键（`co` / `gb` / `proc`）
> 只出现在指纹行。本文档的早期草稿就犯过这个错（按记忆写字段名），
> 所以这里明确：**以 `schema/supplier.schema.json` 为准**。

### 4.2 L0 指纹行（`skills/registry/fingerprint/**`，JSONL，一行一条）

刻意用短键压缩体积（指纹层是召回面，要能全量扫描）：

| 键 | 含义 |
|---|---|
| `id` / `co` | id / 企业名 |
| `city` / `dist` | 城市 / 区县 —— **县级市归在地级市名下**（昆山 → 苏州），按县级市检索要靠 `dist` |
| `gb` | 国标码 |
| `mf` | 是否制造企业（`0` / `1`） |
| `proc` / `mat` / `cert` | 工艺位 / 材料 / 认证 |
| `cl` | 认证等级（如 `L0`） |
| `pv` | 证据来源（如 `derived`） |
| `sc` | 资料完整度得分 |
| `tel` | 是否含电话（`0` / `1`）—— **指纹层不存号码本身**，号码只在 `phone` 分片 |

### 4.3 `search_vendors` 返回的摘要字段

指纹行归一到可读名（`mcp/server.py` 的 `_rec_summary`）：

`id` · `company` · `city` · `district` · `gb` · `badge`(←`cl`) · `score`(←`sc`) ·
`has_phone`(←`tel`) · `process`(←`proc`) · `material`(←`mat`) · `cert`

### 4.4 英文镜像（`data/en/gb/{门类}/{大类}/{小类}.json`）

字段：`id` · `company_en` · `category` / `category_en` · `keywords_en` · `region` ·
`address_en` · `contact_phone` · `note_en` · `industry` · `industry_en` ·
`source` · `verified_at` · `_bucket`

`industry_en` = `{code, name_en, level, name, confidence}`。

**额外约束**：

- 每条记录**必带 `industry`**；
- **同一 `id` 不得跨分片重复出现**（en 分片间重复 id 是踩过的坑）。

---

## 5. 数据红线

1. 只发布**公开**经营信息（公开地图 / 工商 POI）；不批量爬 B2B 平台（1688 等）。
2. `id` 全局唯一。
3. 联系方式公开展示，**不编造**。
4. 弱证据不覆盖强证据：工艺 / 材料只信公司名与能力卡，搜索关键词只能补位。
5. 「没填 ≠ 不做」：未填字段降权保留，不据此淘汰企业。
6. 误打标签要修正 —— 不能因为标签问题让真实企业搜不到。

---

## 6. 许可

- **代码** MIT
- **数据** CC BY 4.0（见 `DATA_LICENSE.md`）
- 企业可经 GitHub Issue 申诉更正 / 删除自身信息

---

*家数与分片数的权威来源是 `data/DATA_STATS.md` 与 `data/manifest.json`；
本文档只固定**格式契约**，不承诺具体数字。*
