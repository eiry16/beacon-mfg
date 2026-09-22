---
name: beacon-mfg
description: 面向 Agent 的中国企业与商户检索名录，按 GB/T 4754-2017 国标门类归档，覆盖 7 大门类 —— C 制造业、F 批发和零售业、H 住宿和餐饮业、I 信息技术服务业、M 科研技术服务业、O 居民服务与修理业、R 文化体育娱乐业。用于找供应商、工厂、厂家、代工厂、OEM/ODM（CNC加工、钣金、注塑、压铸、模具、标准件、电子元器件），找批发商、经销商、贸易商，找餐饮住宿门店（餐厅、火锅、快餐、咖啡、奶茶、烘焙、酒吧、酒店、民宿），找本地生活服务商（美容美发、洗衣干洗、汽车维修、家电维修、健身、KTV、网吧、影院、游乐园、宠物服务），找技术服务商（软件开发、系统集成、网络安全、大数据、第三方检测、计量校准、认证、工业设计、环境监测），或按国标行业代码、产品关键词、城市地区筛选。英文场景 sourcing / find supplier / manufacturer / factory / OEM / restaurant / local service 同样适用。**检索入口是 MCP 服务（beacon-mfg-mcp）或薄框架客户端，不是本地脚本。** 仅提供公开 POI 名录与联系方式，不参与交易。
---

# BeaconMFG · 供应商灯塔检索 Skill

> ## ⚠️ 2026-09-22 起：本仓库只承载 MCP 与只读数据面
>
> 抓取 / 翻译 / 派生重建 / 发布（Cloudflare Pages、R2、push 云端）属于**本地工厂**，
> 不在本仓库里。**所以本文档不再描述「整库检出后跑本地脚本」那条路径** ——
> 那条路径上的 `scripts/*.py` 已随工厂移出，照敲只会得到「文件不存在」。
>
> 对外检索入口只有两条，见下节。

---

## 1. 两条检索入口

### 入口 A：MCP（推荐；托管、免维护、无密钥）

```bash
npx -y beacon-mfg-mcp          # 或按 mcp/README.md 的配置片段接进任意 MCP 客户端
```

不需要 API Key、不需要 clone 本仓库。提供 6 个工具：

| 工具 | 用途 | 主要参数 |
|---|---|---|
| `search_vendors` | 名录检索（关键词 / 城市 / 国标码） | `query`（企业名·工艺·材料·认证子串）、`city`（地级市与区县/县级市都认）、`gb`（国标码；给了就只扫对应分片）、`limit`（1–200）、`offset` |
| `get_vendor` | 按 id 取完整中文档案 | `id`、`gb`（不填会自动从指纹分片反查，稍慢） |
| `get_capability_card` | 按 id 取 L1 能力卡（工艺位/设备/产能/认证/起订量） | `id`；无卡时返回 `has_card: false` 及原因 |
| `start_sourcing` | **找厂/代工/采购/询价意图自动触发**：品类识别 → 指纹宽召回 → 需求归一 → 生成 1~2 轮澄清问题 | `demand_text`、`audience_id`（`domestic_downstream` / `intl_buyer`） |
| `answer_sourcing` | 续接澄清轮次，或返回按需求匹配度初选的供应商 | `session_id`、`answers` |
| `refine_sourcing` | 推荐轮交互：`details` / `more` / `best` | `session_id`、`action`、`value` |

**多轮采购对话请用 `start_sourcing` 起手** —— 它内部走 `skills/rfq-kernel/` 的协议内核
（品类识别 + 澄清问题生成 + 需求归一），不是关键词匹配的薄包装。

### 入口 B：薄框架（不装 MCP 时）

只装两个文件、**不 clone 数据**：

- `agent-skill/SKILL.md` —— 协议与流程
- `agent-skill/client_search.py` —— 按需拉取封装，自带 UA 与 ETag 缓存

```bash
python client_search.py --industry 3525 --city 宁波 --limit 5
```

数据全部**按需从 CDN 拉命中的那一个分片**，平均每次查询传输 < 1 MB。
两条入口检索结果同源、口径一致。

### 关于 GitHub 上的 `data/`

本仓库里的 `data/**` 是**已脱敏的公开快照**（手机号形如 `138****0000`），
供「只读已提交内容」的离线检索模式（MCP 的 `BEACON_REPO` 模式）使用；
完整联系方式由云端按需提供。两条入口都不需要你 clone 它。

---

## 2. 数据源

| 项 | 值 |
|---|---|
| 主源（CDN） | `https://beacon-mfg.pages.dev` |
| 分片清单 | `data/manifest.json` |
| 数据基准日 | 见 `data/DATA_STATS.md` |

取数流程（两条入口内部都这么做）：

1. 拉 `data/manifest.json`（约 143 KB，可缓存）；
2. 按国标码筛出要的**那几片**；
3. 缓存响应头里的 `ETag`，下次带 `If-None-Match` —— 命中返回 304，零传输；
4. 命中后按 `id` 取详情（或 L1/L2）。

⚠️ **必须带 User-Agent**：Cloudflare 对无 UA 请求返回 403（error 1010），
现象是「源不通」，很容易被误判成服务挂了。

---

## 3. 覆盖范围：7 个国标门类

| 门类 | 名称 | 典型需求 |
|---|---|---|
| C | 制造业 | CNC加工、钣金、注塑、压铸、模具、标准件、电子元器件 |
| F | 批发和零售业 | 批发商、经销商、贸易商、五金/建材批发、便利店、药店 |
| H | 住宿和餐饮业 | 餐厅、火锅、快餐、咖啡、奶茶、烘焙、酒吧、酒店、民宿 |
| R | 文化体育娱乐业 | 健身、KTV、网吧、影院、游乐园、球馆 |
| O | 居民服务与修理业 | 美容美发、洗衣干洗、汽车维修、家电维修、宠物服务 |
| I | 信息技术服务业 | 软件开发、系统集成、网络安全、大数据、运维 |
| M | 科研技术服务业 | 第三方检测、计量校准、认证、工业设计、环境监测 |

> **逐门类家数每日变化，一律以 `data/DATA_STATS.md` 为准**，不要引用本文档里的旧数字。
>
> **某个门类查不到时怎么办**：先看 `data/DATA_STATS.md` 的小类明细确认该门类条目数，
> 再如实告诉用户当前规模，**不要**从别的门类凑近似结果糊弄过去。
> 另有少量记录 `industry` 为 null（尚未判定国标行业）——**不硬贴标签**，
> 它们仍在名录里，靠关键词检索可以命中。

---

## 4. 分片契约（`data/manifest.json`）

`metadata.total_shards = 839`，`by_type`：

| 类型 `t` | 片数 | 记录数 | 内容 | 路径形如 |
|---|---|---|---|---|
| `fp` | 273 | 139,318 | L0 指纹（检索召回面，最轻） | `skills/registry/fingerprint/gb/{门类}/{大类}/{小类}.jsonl` |
| `zh` | 284 | 139,318 | 中文完整档案 | `data/gb/{门类}/{大类}/{小类}.json` |
| `en` | 281 | 135,072 | 英文镜像 | `data/en/gb/{门类}/{大类}/{小类}.json` |
| `phone` | 1 | 103,137 | 号码索引 | `data/phone-index.jsonl` |

分片条目字段：`p` 路径 · `b` 归档桶键 · `c` 国标码 · `n` 国标名称 · `t` 类型 ·
`k` 记录条数 · `z` 字节数 · `h` 内容 SHA1（用于**下载后校验完整性**）· `u` 最后更新。

⚠️ **做增量更新请用响应头里的 `ETag`，不要用 `h`** —— `h` 是内容摘要，不是版本号。

⚠️ **一个国标码可能拆成多个续片**（如 `3484.json` + `3484-p2.json`），
按国标码取数时必须**扫全部续片**，只取第一片会漏。

数字为 2026-09-22 快照，实时值见 `data/manifest.json` 的 `metadata.by_type`。

---

## 5. 分层：L0 / L1 / L2

| 层 | 内容 | 在哪 |
|---|---|---|
| **L0 指纹** | 企业名 / 城市 / 国标码 / 工艺 / 材料 / 认证 / 是否含电话（精简） | `skills/registry/fingerprint/**`，进仓库 |
| **L1 能力卡** | 工艺位、设备、产能、认证、起订量等硬指标 | `skills/registry/capability/{id}.json`，**只在云端**（`get_capability_card`） |
| **L2 自述** | 厂商自述（一厂一 Skill） | `skills/vendors/{id}/SKILL.md`，**只在云端** |

**L0 刻意不存电话号码**，号码由 `phone` 分片提供。
`is_template=true` 的记录**不是占位模板**，而是电话待核实的真实企业 POI，检索时应保留。

---

## 6. 字段

**权威定义是 `schema/supplier.schema.json`**，下面是摘要。⚠️ 注意三套口径不同：

| 层 | 字段形如 | 用途 |
|---|---|---|
| 中文档案 `zh` | `id` · `company` · `category` · `keywords` · `region{province,city}` · `address` · `lat/lng` · `industry{code,name,level,path,confidence}` · `is_manufacturer` · `contact_phone` · `source`/`source_url` · `status` · `is_template` · `amap{poi_id,adcode,…}` · `note` | 完整档案 |
| L0 指纹 `fp` | `id` · `co` · `city` · `dist` · `gb` · `mf` · `proc` · `mat` · `cert` · `cl` · `pv` · `sc` · `tel` | 召回面（短键省体积） |
| `search_vendors` 返回 | `id` · `company` · `city` · `district` · `gb` · `badge` · `score` · `has_phone` · `process` · `material` · `cert` | 摘要（指纹行归一后的可读名） |

几条容易踩的：

- **`name` / `gb` / `proc` 不在中文档案里** —— 中文档案用 `company` 与 `industry.code`；
  短键只在指纹行。别按直觉写字段名。
- **区县不在中文档案的 `region` 里**（只有省 + 市）。县级市归在地级市名下
  （昆山 → 苏州），按县级市检索要靠指纹行的 `dist`。
- `category`（品类，如「注塑成型」）与 `industry`（国标）是**两套口径**，不要互相替代。
- `industry` 为 null = 尚未判定国标行业，**不硬贴标签**，这类记录仍在名录里、靠关键词可命中。
- `is_template=true` **不是占位模板**，而是电话待核实的真实企业 POI，检索时应保留。
- 英文镜像字段是 `company_en` / `address_en` / `keywords_en` / `industry_en`，且**必带 `industry`**、
  同一 `id` 不在多个分片里重复出现。

---

## 7. 边界与红线（强制）

- **只提供公开联系方式与基本信息**，不参与询价 / 下单 / 交易。
- **不做推荐评级**。用户要「最好的一家」→ 说明「按契合度排序，请自行核实」。
- **不编造**价格、交期、产能 —— 数据里没有的就直说没有。
- 弱证据不覆盖强证据：工艺 / 材料只信公司名与能力卡，搜索关键词只能补位。
- 「没填 ≠ 不做」：未填字段降权保留，不要据此淘汰企业。
- 误打标签要修正：不能因为标签问题让真实企业搜不到。
- 优先用`search_vendors`/`start_sourcing`，**别自己猜国标码**；不确定就先检索。

---

## 8. 许可与申诉

- **代码** MIT；**数据** CC BY 4.0（见 `DATA_LICENSE.md`）。
- 企业可通过 GitHub Issue 申诉更正或删除自己的信息。

---

## 9. 本仓库结构

```
mcp/                        只读 MCP 服务（npm 包 beacon-mfg-mcp）
agent-skill/                薄框架：SKILL.md + client_search.py
data/
  manifest.json             分片清单（取数入口）
  gb/**  en/**              中文 / 英文名录分片
  phone-index.jsonl         号码索引
  gb-index.json  industry-index.json  region-index.json
  gb-alias.json  gb-alias-curated.json  gb4754-full.json
  endpoint.json             App 数据源发现指针
  DATA_STATS.md             数据统计（数字以它为准）
skills/
  registry/fingerprint/**   L0 指纹分片
  registry/index/**         倒排索引（meta / city / terms）
  rfq-kernel/**             多轮采购对话的协议内核（sourcing 工具用它）
README.md  README_EN.md     数据现状与覆盖
SPEC.md                     数据格式规范
```

---

*抓取 / 翻译 / 派生重建 / 发布流水线属本地工厂资产，不在本仓库。
本仓库只承载「让客户的 Agent 找到制造业供应商」的公开产物。*
