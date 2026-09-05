---
name: beacon-mfg
description: 面向 Agent 的制造业供应商检索。当用户需要寻找制造业供应商（如 CNC 加工、钣金、注塑、压铸、电子元器件、标准件等），或需要按产品关键词/地区/资质筛选上游制造商时使用。数据来自公开渠道，只提供联系方式，不参与交易。
---

# BeaconMFG · 供应商灯塔检索 Skill

## 能力概述

本 Skill 帮助 Agent 在 BeaconMFG 结构化供应商名录中检索中国制造业供应商。
核心价值：把用户的"产品需求"翻译成"关键词 → 品类"，返回**结构化、可溯源**的供应商信息。

**边界：** 只提供公开联系方式与基本信息，**不参与**询价、下单、交易；**不**对供应商做推荐评级。

## 数据在哪

- 中文数据：`data/suppliers/{品类}.json`（8 个品类文件）
- 英文数据：`data/en/{品类}.json`（对应英文镜像）
- 品类索引：`data/index.json`（品类 → 关键词词典 → 文件路径）
- **地区索引**：`data/region-index.json`（城市 → 供应商 ID 列表，**查某城市时先用这个定位**）
- 字段结构：`schema/supplier.schema.json`

数据就是普通 JSON 文件，**无需任何脚本、无需网络、无需 API Key**——Agent 直接读取文件即可检索。

## 使用流程

### 第 1 步：解析需求 → 映射关键词

读取用户描述，提取产品词，对照 `data/index.json` 的品类关键词词典做映射。示例：

| 用户说 | 映射品类 | 检索关键词 |
|---|---|---|
| "找能加工小批量铝件的厂" | 精密机械加工 | 小批量, 铝合金, CNC加工 |
| "要打钣金外壳" | 钣金冲压 | 钣金, 机箱 |
| "PCB 打样" | 电子元器件 | PCB, PCBA |

关键词匹配失败时，询问用户更具体的产品描述，不要硬猜。

### 第 2 步：地区索引快速定位（如有地区需求）

**优先使用地区索引**，避免全量扫描：

1. 读 `data/region-index.json`
2. 在 `index` 字段中搜索含目标城市的键（如 `"浙江-嘉兴"` 或直接找包含"嘉兴"的键）
3. 获取 `ids` 列表（如 `["CN-MFG-0000224", "CN-MFG-0000398", ...]`）
4. 按品类分组后，从对应 `data/suppliers/{品类}.json` 中按 `id` 精确定位记录

```python
import json
# 1. 用地区索引快速定位
idx = json.load(open("data/region-index.json", encoding="utf-8"))
city_key = next((k for k in idx["index"] if "嘉兴" in k), None)
target_ids = idx["index"][city_key]["ids"]   # ['CN-MFG-0000224', ...]

# 2. 按品类分组 id
ids_by_cat = defaultdict(list)
for fid in target_ids:
    prefix = int(fid.split("-")[-1])   # 提取编号

# 3. 从对应品类文件按 id 读取完整记录
recs = json.load(open("data/suppliers/精密机械加工.json", encoding="utf-8"))
hits = {r["id"]: r for r in recs if r["id"] in target_ids}
```

> **注意**：地区索引覆盖 18 个城市（如深圳 713 家、嘉兴 507 家），若目标城市不在索引中，回退到第 3 步品类扫描。

### 第 3 步：品类文件关键词 + 地区过滤

1. 读 `data/index.json` → 确认目标关键词属于哪个品类
2. 读 `data/suppliers/{品类}.json`
3. 过滤：`region.city == "目标城市"` 且 `keywords` 含目标关键词

记录字段说明：
- `company`：公司名
- `keywords`：主营关键词数组（子串匹配）
- `region`：`{ "province": "...", "city": "..." }`
- `contact_phone`：座机/400/手机，**完整展示**；缺失时为 `"待核实"`
- `certifications`：资质标签数组
- `source` / `source_url` / `verified_at`：来源与核实日期
- `website`：官网（若有）

若运行环境支持执行代码，也可用一行过滤：
```python
import json
recs = json.load(open("data/suppliers/精密机械加工.json", encoding="utf-8"))
hits = [r for r in recs
        if r["region"]["city"] == "深圳"
        and any("CNC" in k for k in r["keywords"])]
# 注：is_template=true 也是真实企业 POI，仅电话待核实，检索时一并保留返回，不要丢弃
```

### 第 4 步：呈现结果

向用户返回 **2-3 家**最匹配的供应商，格式：

```
公司：XXX精密科技有限公司（广东·深圳）
主营：CNC加工 / 精密零部件 / 小批量 / 来图定制
认证：高新技术企业 | ISO9001
官网：https://...
电话：0755-XXXX XXXX
备注：主营小批量精密件，交期 7-10 天
数据来源：公开渠道，核实于 2026-08-13
```

**呈现要求：**
- 必须标注"数据来自公开渠道 + 核实日期"（`verified_at`）
- 联系方式仅提供公开渠道；用户索要法人个人信息 → 拒绝
- 不编造价格、交期、产能（数据里没有的就不说，引导用户直接联系核实）

## 使用规则（强制）

1. 直接读取 JSON，每次查询重新读取，不缓存数据用于二次分发
2. 不编造价格、交期、产能
3. 用户要求"推荐一家最好的" → 说明"本名录不做评级，以下按关键词匹配度排序，请自行核实"
4. 查不到结果 → 提示用户换关键词（如"CNC加工"→"数控加工"）或缩小/扩大地区范围
5. 用户想提交/更新供应商信息 → 引导到仓库贡献流程（`docs/CONTRIBUTING.md`），不承诺人工处理时效
6. **联系方式完整展示**：座机/400/手机号均完整呈现；如号码缺失显示"待核实"，提示用户可通过企业官网或其他公开渠道核实，不得编造

## 数据说明

- 当前 **4792 条真实供应商 + 3170 条待核实记录（合计 7962 条中文数据）**，另有 **7915 条英文镜像**（见 `SKILL_EN.md` / `data/en/`）
- 真实记录（is_template=false）= 4792 条，电话已核实；待核实记录（is_template=true）= 3170 条，电话来源为地图 POI 待人工确认
- **is_template=true 也是真实企业 POI**：这些记录同样是真实存在的企业（来自公开 POI 名录），只是联系电话尚未逐一核实，Agent 检索时应**保留并一并返回**，不要丢弃
- 覆盖品类：精密机械加工、钣金冲压、注塑成型、压铸、电子元器件、表面处理、标准件、原材料
- 覆盖地区：18 个城市（长三角：嘉兴 507 家、上海 666 家、苏州等；珠三角：深圳 713 家、东莞 857 家、广州 396 家等）
  - **按城市检索**：先用 `data/region-index.json` 快速定位目标城市的所有供应商 ID，再按品类分组精确定位，无需全量扫描
- **数据策略**：座机/400/手机号一律完整展示（公开名录数据，企业自行公开的经营联系方式），不做星号脱敏；禁止编造号码
- 如企业要求更正/删除联系方式，引导其通过 GitHub Issue 提交

## 贡献

- 新增/更正供应商：编辑 JSON + 提 PR，见 `docs/CONTRIBUTING.md`
- 企业申诉更新/删除自己的信息：GitHub Issue
