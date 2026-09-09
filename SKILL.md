---
name: beacon-mfg
description: 面向 Agent 的制造业供应商检索。当用户需要寻找制造业供应商（如 CNC 加工、钣金、注塑、压铸、电子元器件、标准件、模具、铸造、橡胶、齿轮等），或需要按 GB/T 4754 国标行业、产品关键词/地区/资质筛选上游制造商时使用。数据来自公开渠道，只提供联系方式，不参与交易。
---

# BeaconMFG · 供应商灯塔检索 Skill

## 能力概述

本 Skill 帮助 Agent 在 BeaconMFG 结构化供应商名录中检索中国制造业供应商。
核心价值：把用户的"产品需求"翻译成"**国标行业 → 归档文件**"，返回**结构化、可溯源**的供应商信息。

**边界：** 只提供公开联系方式与基本信息，**不参与**询价、下单、交易；**不**对供应商做推荐评级。

## 数据在哪

名录按 **GB/T 4754-2017 国标四级归档**（20 门类 / 97 大类 / 473 中类 / 1382 小类）：

- 中文数据：`data/gb/{门类}/{大类}/{小类}.json`（如 `data/gb/C/35/3525.json` = 模具制造）
  - 只到中类/大类精度的记录在同级 `{中类3位}.json` / `_partial.json`；解析不出码的在 `_unclassified.json`
- 英文数据：`data/en/gb/`（与中文同构镜像，字段为 `company_en` / `address_en` 等）
- **归档主索引**：`data/gb-index.json`（四级层级树 + 各级计数，**浏览"有什么行业有货"用它**）
- **行业索引**：`data/industry-index.json`（国标小类 → 企业 ID 列表，**知道行业时先用这个**）
- **地区索引**：`data/region-index.json`（城市 → 供应商 ID 列表，**知道城市时先用这个**）
- **采购词别名表**：`data/gb-alias.json`（数据推导层）+ `data/gb-alias-curated.json`（人工策展层）。
  两层**必须一起读**（`scripts/gb_store.py` 的 `load_alias()` 已合并好，直接用它）。
  只查 `gb-alias.json` 会漏掉「输送线/流水线/传送带」这类采购词——它们不在任何公司名里，
  数据推导层永远推不出来。**别写死只认其中一个文件。**
- **分片清单（推荐入口）**：`data/manifest.json`（列出全部分片的路径/条数/字节/SHA1）
- **L0 能力指纹**：`skills/registry/fingerprint/gb/{门类}/{大类}/{小类}.jsonl`（235 B/条，100% 覆盖）
- 字段结构：`schema/supplier.schema.json`

数据就是普通文件，**无需任何脚本、无需网络、无需 API Key**——Agent 直接读取文件即可检索。

### 只想查一两个小类？用 manifest 按需拉取，别 clone 全库

| 方式 | 传输量 |
|---|---|
| `git clone` 全库 | 4 MB（千万级约 2 GB） |
| 拉 manifest + 命中的小类分片 | **约 0.3 MB** |

```python
import json, urllib.request
base = "https://raw.githubusercontent.com/eiry16/beacon-mfg/main"
man = json.loads(urllib.request.urlopen(f"{base}/data/manifest.json").read())
hit = [s for s in man["shards"] if s["t"] == "fp" and s["c"] == "3525"]
rows = [json.loads(l) for l in
        urllib.request.urlopen(f"{base}/{hit[0]['p']}").read().decode().splitlines() if l.strip()]
# 带 If-None-Match: 分片["h"] 可走 304 缓存，二次查询近乎零流量
```

参考实现见 `scripts/client_search.py`。

## 使用流程

### 第 0 步（优先）：把需求翻译成国标代码

**如果用户说的是一个行业/工艺（模具、压铸、电镀、橡胶件、齿轮…），先按行业定位，别用关键词猜。**
关键词会漏（"注塑"在高德上几乎搜不到厂，厂都叫"塑料制品"），国标代码不会。

三条路拿到国标码（按顺序尝试）：

1. **别名表**：采购口语先查别名表（默认 307 条 = 数据推导 62 + 人工策展 245，
   用 `scripts/gb_store.py` 的 `load_alias()` 读合并结果），
   例如 "CNC加工"→3484、"PCB"→3989、"输送线"→3434。

   - **数据推导层**（`gb-alias.json`）：统计关键词实际出现在哪些小类下、≥3 次才收录，
     带真实命中次数。覆盖「公司名里会写」的词。
   - **人工策展层**（`gb-alias-curated.json`）：覆盖「客户会搜、但公司名里没有」的词
     （输送线/流水线/传送带/密封圈/吸塑/齿轮…）。**这些条目没有命中次数**——
     命中数只能由真实数据统计得出，手写就是编数字。

   ⚠️ **结果分三档，强度不同，别一视同仁**：
   别名扩展是"整类扩展"而非"同义词扩展"——命中某个国标码 = 把这个码下所有企业都算进来。
   搜「齿轮」会把 3484 机械零部件加工的 3415 家一起带回来（放大 3421 倍）。
   所以结果按证据强度排序并标注：

   | 档位 | 含义 | 怎么用 |
   |---|---|---|
   | `字面关键词` | 企业自己的关键词里就有这个词 | 最强，直接用 |
   | `别名首位码` | 语义最贴近采购词的小类 | 强，可直接用 |
   | `行业推断` | 只是被归在这个行业，企业没说过自己能做 | **弱，需二次确认，别直接写进结论** |

   `query.py` 输出里弱档会打 `[行业推断·未确认]` 标记，并在开头给出三档的条数构成。

   **补位码有供给上限**（默认 `max_supply=500`）：目标小类家数超过 500 的补位码
   会被剔除，避免「齿轮」(3453，6 家) 被 3484 机械零部件加工 (3415 家) 淹没。
   实测削减 70% 结果量。**首位码永不淘汰**——否则「钣金→3311」这类正确但宽的结果
   会被砍成 0。个别词可在 `gb-alias-curated.json` 里用 `max_supply` 逐条覆盖。

   收敛后若结果为 0，`query.py` 会提示放宽可得多少家（如「齿轮·上海」→
   "放宽可得到 216 家，但全部是行业推断"），**不会静默放宽**。
   确实要看放宽结果就加 `--alias-broad`。
2. **行业速查表**（下方）。
3. **归档树浏览**：读 `data/gb-index.json` 的 `tree`，逐级看门类 → 大类 → 中类 → 小类及各家数。

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

### 第 1 步：行业索引锁定 ID 列表

```python
import json
idx = json.load(open("data/industry-index.json", encoding="utf-8"))
m = idx["metadata"]          # 覆盖家数、小类数、生成日期
code = "3525"                # 模具制造
item = idx["index"][code]
print(item["name"], item["count"], item["confidence"])   # 模具制造 1091 {'high':..,'medium':..,'low':..}
target_ids = set(item["ids"])                            # 该行业全部企业 ID
```

> **注意**：`industry-index.json` 是快照，抓取新数据后必须重建（维护脚本
> `scripts/gen_industry_index.py`，属内部维护脚本、不随仓库分发），
> 否则新企业按行业永远搜不到——这是一个不会报错的静默缺陷。
> 若你检索时发现某小类条数明显偏少，优先怀疑索引没重建，而不是数据缺失。

代码前缀可当层级用：`29`=橡胶和塑料制品业（大类）、`339`=铸造及其他金属制品制造（中类）、`C`=制造业（门类）。

### 第 2 步：地区索引取交集（如有地区需求）

```python
city_ids = set(json.load(open("data/region-index.json", encoding="utf-8"))["index"]["浙江-宁波"]["ids"])
hit_ids = target_ids & city_ids
```

> 地区索引覆盖 18 个城市（苏州 1812、中山 1725、宁波 1520、杭州 1489、东莞 1443…）。
> 目标城市不在索引中时，退化为只按行业取 ID，再逐记录核对 `region`。

### 第 3 步：按 ID 从归档文件读详情

ID 编号与归档文件没有映射关系，两种取法：

1. **全量读再过滤**（2 万条约 20MB，环境允许时最简单）：

```python
import glob, json
ids = hit_ids
hits = []
for f in glob.glob("data/gb/*/*/*.json"):
    hits += [r for r in json.load(open(f, encoding="utf-8")) if r["id"] in ids]
```

2. **按 `gb-index.json` 只读目标小类的桶文件**：树里每个小类的键就是文件路径
   （`data/gb/{门类}/{大类}/{小类}.json`），命中 ID 落在哪个小类，去哪个文件取。

记录字段说明：
- `company`：公司名
- `keywords`：主营关键词数组（子串匹配）；`工艺:`/`材料:` 前缀的是结构化元信息
- `region`：`{ "province": "...", "city": "..." }`
- `contact_phone`：座机/400/手机，**完整展示**；缺失时为 `"待核实"`
- `certifications`：资质标签数组
- `source` / `source_url` / `verified_at`：来源与核实日期（`source="certification"` 为平台认证回流，带 `cl` 灯牌）
- `website`：官网（若有）
- `industry`：国标行业，`{code, name, path, confidence, source}`；`null` = 未归类（拿不到行业信号，**不要替它猜**）
- `is_manufacturer`：`false` = 批发/贸易商，不是生产企业
- `cl` / `certification`：凭证等级（L0-L3）与认证档案（仅认证回流记录有）
- `amap`：地图 POI 扩展字段（typecode / website / email 等），仅供交叉核对

若运行环境支持执行代码，也可用检索脚本一步到位：
```bash
python scripts/query.py --industry 3525 --city 宁波 --limit 5   # 模具制造
python scripts/query.py --industry 29 --city 东莞                 # 橡胶和塑料制品业（大类）
python scripts/query.py --industry 3360 --manufacturer-only       # 排除批发贸易商
python scripts/query.py --list-gb                                 # 浏览国标归档树
python scripts/query.py --keyword "CNC加工" --city 深圳 --limit 5     # 采购词（自动查别名表展开）
python scripts/query.py --keyword "小批量铝件" --no-alias            # 关闭别名展开，纯关键词匹配
```

> `is_template=true` / `status="unverified_poi"` 的记录也是真实企业 POI（仅电话待核实），检索时一并保留返回，不要丢弃。

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
用 `skills/registry/fingerprint/gb/{门类}/{大类}/{小类}.jsonl`——每家一行能力指纹，235 B，**覆盖全部 20265 家**。

**先粗筛后精读，不要一上来就全量读供应商自述**（2 万家全读会撑爆上下文）：

| 阶段 | 读什么 | 规模 |
|---|---|---|
| 1 粗筛 | 该小类的 `fingerprint/gb/.../{小类}.jsonl`，数值规则过滤 | 单小类 → 10-30 家 |
| 2 比对 | `capability/{id}.json` | 30 家 → 5 家 |
| 3 精读 | `vendors/{id}/SKILL.md` | 5 家 → 3 家 |

**只查某个小类时只读那一个分片**（如 3525 模具制造 = 0.23 MB），不要全量扫描 4.5 MB 的指纹库。

指纹行字段（刻意用短键，省钱）：

```
id / co(公司) / city / gb(国标码) / mf(是否制造商) / proc(工艺码) / mat(材料)
cert / cl(凭证等级) / pv(来源: auto 能力卡 / derived 国标推导) / sc(画像分) / tel(有电话)
tol / size / moq / lt / rt —— 仅能力卡厂商才有，无值不占位
```

`pv=derived` 的厂商是按国标码推导的工艺（无能力卡），**sc=0 不是评分低，是尚未画像**。

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
4. 查不到结果 → 先查 `gb-alias.json` 换采购词（如"CNC加工"→"数控加工"），再试同级国标代码，最后缩小/扩大地区范围
5. 用户想提交/更新供应商信息 → 引导到仓库贡献流程（`docs/CONTRIBUTING.md`），不承诺人工处理时效
5.5 **行业口径**：`industry.confidence=low` 表示公司名里没有行业信号、由关键词或品类兜底推断，呈现时说明"行业为推断值，采购前需复核"；`industry=null` 就是没归类，不要替它猜一个；`is_manufacturer=false` 要提示"批发/贸易商，非生产企业"
6. **联系方式完整展示**：座机/400/手机号均完整呈现；如号码缺失显示"待核实"，提示用户可通过企业官网或其他公开渠道核实，不得编造

## 数据说明

- 当前 **12693 条电话已核实 + 7571 条待核实 + 1 条示例（合计 20265 条中文数据）**，另有英文镜像 20265 条（`data/en/gb/`，见 `SKILL_EN.md`）
- 已核实记录（`status="verified"`）联系电话可用；待核实记录（`status="unverified_poi"`）是真实企业 POI，电话待人工确认
- **待核实记录也是真实企业**：检索时**保留并一并返回**，不要丢弃（只有 `status="template"` 才是示例占位，当前 1 条）
- **国标归档覆盖**：20265 条按 GB/T 4754 四级归档，19559 条已归入 **102 个小类**（706 条未归类）；
  制造业（C）17911 家、批发业（F）1648 家；前三大类：通用设备（34）5941、金属制品（33）5640、橡塑（29）2280
- 覆盖地区：18 个城市（长三角：苏州、宁波、上海、无锡、杭州、嘉兴等；珠三角：东莞、深圳、佛山、广州等），明细见 `data/region-index.json`
  - 2026-09-08 补采：杭州 480→1489、绍兴 181→1251、惠州 174→1386、中山 155→1725、珠海 21→799
  - **按城市检索**：先用 `data/region-index.json` 快速定位目标城市的所有供应商 ID，再按 ID 读详情，无需全量扫描
  - **按行业检索**：先用 `data/industry-index.json` 取目标行业的 ID 列表，再与城市 ID 取交集
- **数据策略**：座机/400/手机号一律完整展示（公开名录数据，企业自行公开的经营联系方式），不做星号脱敏；禁止编造号码
- 如企业要求更正/删除联系方式，引导其通过 GitHub Issue 提交

## 贡献

- 新增/更正供应商：编辑 `data/gb/` 对应小类文件 + 提 PR，见 `docs/CONTRIBUTING.md`
- 企业申诉更新/删除自己的信息：GitHub Issue
