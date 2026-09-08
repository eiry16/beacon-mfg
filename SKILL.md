---
name: beacon-mfg
description: 面向 Agent 的制造业供应商检索。当用户需要寻找制造业供应商（如 CNC 加工、钣金、注塑、压铸、电子元器件、标准件、模具、铸造、橡胶、齿轮等），或需要按 GB/T 4754 国标行业、产品关键词/地区/资质筛选上游制造商时使用。数据来自公开渠道，只提供联系方式，不参与交易。
---

# BeaconMFG · 供应商灯塔检索 Skill

## 能力概述

本 Skill 帮助 Agent 在 BeaconMFG 结构化供应商名录中检索中国制造业供应商。
核心价值：把用户的"产品需求"翻译成"**国标行业 → 关键词 → 品类**"，返回**结构化、可溯源**的供应商信息。

**边界：** 只提供公开联系方式与基本信息，**不参与**询价、下单、交易；**不**对供应商做推荐评级。

## 数据在哪

- 中文数据：`data/suppliers/{品类}.json`（8 个品类文件）
- 英文数据：`data/en/{品类}.json`（对应英文镜像）
- 品类索引：`data/index.json`（品类 → 关键词词典 → 文件路径）
- **行业索引**：`data/industry-index.json`（GB/T 4754 国标小类 → 企业 ID 列表，**知道要找什么行业时先用这个**）
- **地区索引**：`data/region-index.json`（城市 → 供应商 ID 列表，**知道城市时先用这个**）
- 字段结构：`schema/supplier.schema.json`

数据就是普通 JSON 文件，**无需任何脚本、无需网络、无需 API Key**——Agent 直接读取文件即可检索。

## 使用流程

### 第 0 步（优先）：按 GB/T 4754 国标行业锁定

**如果用户说的是一个行业/工艺（模具、压铸、电镀、橡胶件、齿轮…），先按行业定位，别用关键词猜。**
关键词会漏（"注塑"在高德上几乎搜不到厂，厂都叫"塑料制品"），国标代码不会。

每个行业一个 4 位代码，层级是 `门类(C/F) > 大类(2位) > 中类(3位) > 小类(4位)`：

```python
import json
idx = json.load(open("data/industry-index.json", encoding="utf-8"))
m = idx["metadata"]          # 覆盖家数、小类数、生成日期
code = "3525"                # 模具制造
item = idx["index"][code]
print(item["name"], item["count"], item["confidence"])   # 模具制造 621 {'high':..,'medium':..,'low':..}
target_ids = set(item["ids"])                            # 该行业全部企业 ID
```

> **注意**：`industry-index.json` 是快照，抓取新数据后必须重建（`scripts/gen_industry_index.py`），
> 否则新企业按行业永远搜不到——这是一个不会报错的静默缺陷。

常见行业代码速查（完整清单读索引的 `index` 键）：

| 代码 | 行业小类 | 代码 | 行业小类 |
|---|---|---|---|
| 3484 | 机械零部件加工（CNC/数控） | 3391 | 黑色金属铸造 |
| 3525 | 模具制造 | 3392 | 有色金属铸造（压铸） |
| 3311 | 金属结构制造（钣金/冲压） | 3393 | 锻件及粉末冶金制品 |
| 3360 | 金属表面处理及热处理加工 | 2913 | 橡胶零件制造 |
| 2929 | 塑料零件及其他塑料制品制造 | 3451 | 滚动轴承制造 |
| 2921 | 塑料薄膜制造 | 3453 | 齿轮及齿轮减、变速箱 |
| 2926 | 塑料包装箱及容器制造 | 3482 | 紧固件制造 |
| 2651 | 初级形态塑料及合成树脂（原料） | 3483 | 弹簧制造 |
| 3982 | 电子电路制造（PCB） | 3660 | 汽车零部件及配件制造 |
| 3989 | 其他电子元件制造 | 5164 | 金属及金属矿批发（**非制造**） |

**行业筛选后再交叉地区索引**（两者都给 ID 列表，取交集），比扫描品类文件快得多：

```python
city_ids = set(json.load(open("data/region-index.json", encoding="utf-8"))["index"]["浙江-宁波"]["ids"])
hit_ids = target_ids & city_ids
```

代码前缀可当层级用：`29`=橡胶和塑料制品业（大类）、`339`=铸造及其他金属制品制造（中类）、`C`=制造业（门类）。

### 第 1 步：解析需求 → 映射关键词

行业代码无法覆盖的口语化描述（"小批量""来图定制"），再对照 `data/index.json` 的品类关键词词典：

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

> **注意**：地区索引覆盖 18 个城市（如东莞 1108 家、深圳 920 家、苏州 1347 家），若目标城市不在索引中，回退到第 3 步品类扫描。

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
- `industry`：国标行业，`{code, name, path, confidence, source}`；`null` = 未归类（拿不到行业信号，**不要替它猜**）
- `is_manufacturer`：`false` = 批发/贸易商（F51 批发业），不是生产企业
- `amap`：地图 POI 扩展字段（typecode / website / email 等），仅供交叉核对

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
行业：3484 机械零部件加工（置信度高）
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

## 能力层检索（能做 vs 只存在）

上面的流程只回答"**有这家厂**"。如果还要判断"**这家厂能不能做我的活**"，
用 `skills/registry/fingerprint/{品类}.jsonl`——每家一行能力指纹，约 120 字。

**先粗筛后精读，不要一上来就全量读供应商自述**（8000 家全读会撑爆上下文）：

| 阶段 | 读什么 | 规模 |
|---|---|---|
| 1 粗筛 | `fingerprint/{品类}.jsonl`，数值规则过滤 | 全量 → 10-30 家 |
| 2 比对 | `capability/{id}.json` | 30 家 → 5 家 |
| 3 精读 | `vendors/{id}/SKILL.md` | 5 家 → 3 家 |

指纹行字段（刻意用短键，省钱）：

```
id / co(公司) / city / proc(工艺码) / mat(材料) / tol(公差mm)
size([长,宽,高]mm) / moq / lt([打样天,百件天]) / cert / rt(回价小时) / cl(凭证等级) / sc(完整度)
```

按需求做确定性过滤：

```python
import json
need = {"city": "深圳", "proc": "cnc_milling", "tol": 0.05, "moq": 10}
hits = []
for line in open("skills/registry/fingerprint/精密机械加工.jsonl", encoding="utf-8"):
    r = json.loads(line)
    if need["city"] != r["city"]: continue
    if need["proc"] not in r["proc"]: continue
    if r["tol"] is None or r["tol"] > need["tol"]: continue   # 公差达不到
    if r["moq"] is None or r["moq"] > need["moq"]: continue   # 起订量太高
    hits.append(r)
```

也可直接用现成脚本：

```bash
python scripts/search_capabilities.py --city 深圳 --proc cnc_milling \
    --mat 铝合金6061 --tol 0.05 --moq 10 --size 300,200,100
# 按国标行业先圈定范围（--industry 支持小类码/中类/大类/门类/中文名）
python scripts/search_capabilities.py --industry 3525 --city 宁波
```

也可用名录检索脚本按行业查：
```bash
python scripts/query.py --industry 3525 --city 宁波 --limit 5   # 模具制造
python scripts/query.py --industry 29 --city 东莞                 # 橡胶和塑料制品业（大类）
python scripts/query.py --industry 3360 --manufacturer-only       # 排除批发贸易商
python scripts/query.py --list-industries                         # 看看哪些行业有货
```

**规则：**
1. 工艺码取值域见 `skills/schema/process-codes.json`
2. `tol` / `moq` / `size` 为 `null` 表示未填——**无法确定性筛选，不要当作合格**
3. `cl`（凭证等级）：L0 未核验 / L1 企业自述 / L2 平台已认证 / L3 第三方核验。
   呈现给用户时必须原样标注，L1 及以下要说明"未经平台核验"
4. `sc` 是**资料完整度**（不是评级），仅用于排序
5. 没有提交 Skill 的供应商在指纹库里查不到——回退到前面的名录检索即可，不影响使用

## 使用规则（强制）

1. 直接读取 JSON，每次查询重新读取，不缓存数据用于二次分发
2. 不编造价格、交期、产能
3. 用户要求"推荐一家最好的" → 说明"本名录不做评级，以下按关键词匹配度排序，请自行核实"
4. 查不到结果 → 提示用户换关键词（如"CNC加工"→"数控加工"）、换同级国标代码，或缩小/扩大地区范围
5. 用户想提交/更新供应商信息 → 引导到仓库贡献流程（`docs/CONTRIBUTING.md`），不承诺人工处理时效
5.5 **行业口径**：`industry.confidence=low` 表示公司名里没有行业信号、由关键词或品类兜底推断，呈现时说明"行业为推断值，采购前需复核"；`industry=null` 就是没归类，不要替它猜一个；`is_manufacturer=false` 要提示"批发/贸易商，非生产企业"
6. **联系方式完整展示**：座机/400/手机号均完整呈现；如号码缺失显示"待核实"，提示用户可通过企业官网或其他公开渠道核实，不得编造

## 数据说明

- 当前 **12692 条电话已核实 + 7572 条待核实记录（合计 20264 条中文数据）**，另有英文镜像（见 `SKILL_EN.md` / `data/en/`）
- 已核实记录（`status="verified"`）联系电话可用；待核实记录（`status="unverified_poi"`）是真实企业 POI，电话待人工确认
- **待核实记录也是真实企业**：`status="unverified_poi"` 的记录同样是真实存在的企业（来自公开 POI 名录），只是联系电话尚未逐一核实，Agent 检索时应**保留并一并返回**，不要丢弃（只有 `status="template"` 才是示例占位，当前为 0 条）
- 覆盖品类：精密机械加工、钣金冲压、注塑成型、压铸、电子元器件、表面处理、标准件、原材料
- **国标行业覆盖**：19610/20264 条已归入 **48 个 GB/T 4754 小类**（654 条未归类），
  集中在通用设备制造业（6042）、金属制品业（5675）、橡胶和塑料制品业（2198）；
  另有 1414 家落在 F51 批发业（`is_manufacturer=false`，贸易/批发商）
- 覆盖地区：18 个城市（长三角：苏州、宁波、上海、无锡、杭州、嘉兴等；珠三角：东莞、深圳、佛山、广州等），明细见 `data/region-index.json`
  - 2026-09-08 补采：杭州 480→1489、绍兴 181→1251、惠州 174→1386、中山 155→1725、珠海 21→799，五城已铺满全部 8 个品类
  - **按城市检索**：先用 `data/region-index.json` 快速定位目标城市的所有供应商 ID，再按品类分组精确定位，无需全量扫描
  - **按行业检索**：先用 `data/industry-index.json` 取目标行业的 ID 列表，再与城市 ID 取交集
- **数据策略**：座机/400/手机号一律完整展示（公开名录数据，企业自行公开的经营联系方式），不做星号脱敏；禁止编造号码
- 如企业要求更正/删除联系方式，引导其通过 GitHub Issue 提交

## 贡献

- 新增/更正供应商：编辑 JSON + 提 PR，见 `docs/CONTRIBUTING.md`
- 企业申诉更新/删除自己的信息：GitHub Issue
