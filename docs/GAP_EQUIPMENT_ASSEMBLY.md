# 枚举缺口报告：成套设备制造（equipment_assembly）

- 报告日期：2026-09-10
- 触发案例：CN-MFG-0020317 耐特斯传输设备（上海）有限公司（3434 连续搬运设备制造，L2 已认证）
- 结论一句话：**认证侧早就引入了 `equipment_assembly`，能力卡的 profile / 工艺码 / Schema 三处都没跟上。**

---

## 处置状态（同日主人拍板后已全部执行）

| # | 缺口 | 处置 | 结果 |
|---|---|---|---|
| 1 | profile 无 equipment_assembly | 新建 `skills/profiles/equipment_assembly.json` | ✓ |
| 2 | 工艺码零件加工导向 | 码表加 `gb` 国标维度 + 补 equipment 族 5 码 + 12 条 alias | ✓ 该卡工艺位由 1 个码变 5 个 |
| 3 | Schema 合规欠账 | 补 gb_code 等 3 键、identity 5 键、claim 2 键、evidence.public_record、not_applicable、process_self_report、service.installation | ✓ 4126 张卡从不合规变合规 |
| 4 | 门禁验过期副本 | 校验源改指 `skills/registry/capability/`；registry 现行卡倒灌回 vendors（4137 张） | ✓ 旧快照摒弃 |
| 5 | `agent.verified` | 已认领且审核日期非空 → `true` | ✓ 该卡已 true |

全量复校：**4137 张 0 ERROR、0 孤儿**（剩余均为「未填」类 WARN，属诚实留空）。
下游已补跑：`gen_fingerprint --apply` → `validate --strict`（0 错 0 警）→ `gen_manifest`（429 片）→ `sync_assets`（App 内置 4137 张卡）。

---

## 0. 这次做了什么（标准流程跑通）

`sync_vendor_skills.py` 的三步全部落盘（该脚本注释里写「历史上从未实现」，现已补上）：

| 步 | 产物 | 结果 |
|---|---|---|
| ① L1 能力卡入口 | `skills/registry/capability/CN-MFG-0020317.json` | ✓ |
| ② L0 指纹行 | `skills/registry/fingerprint/gb/C/34/3434.jsonl` | ✓ 按 id 幂等，无幽灵行 |
| ③ 名录 `agent` 字段回写 | `data/gb/C/34/3434.json` | ✓ `catalog/rfq/quote` |

下游连带重建：`gen_fingerprint --apply`（富字段 4136 条未掉）→ `validate --strict`（0 错 0 警）→ `gen_manifest`（429 分片）→ `sync_assets`（App 内置能力卡 4137 张）。

指纹行最终形态：

```json
{"id":"CN-MFG-0020317","co":"耐特斯传输设备（上海）有限公司","city":"上海","gb":"3434",
 "mf":1,"proc":["welding"],"mat":["不锈钢"],"cert":["ISO9001"],"cl":"L2",
 "pv":"vendor_claimed","sc":80,"tel":1,"moq":1,"lt":[30,null],"rt":24}
```

---

## 缺口 1 · profile 枚举里没有「成套设备制造」

`skills/profiles/` 现有 9 个（8 品类 + custom）：

```
precision-machining  sheet-metal  injection-molding  die-casting
surface-treatment    standard-parts  raw-materials  electronics   custom
```

全是**零件加工 / 零件成型**导向，没有一个是「做整条产线、整套设备」。
`validate_vendor_skills.py` 直接报：`未知 profile: equipment_assembly`。

**最有力的证据**：认证档案 `CERT-20260908-0317.json` 里 `capability.completeness.profile` 已经是
`"equipment_assembly"`，`not_applicable` 也按该 profile 的字段逐条给出
（公差 / 最大工件尺寸 / 批量交期三项判为不适用）。**认证侧两月前就有这个概念，
只是能力卡这一层的枚举没有同步。**

建议新增 `skills/profiles/equipment_assembly.json`，字段候选：

| 字段 | 说明 | 本企业实际值 |
|---|---|---|
| `capability.line_length_m` | 可承接产线长度 | 未填 |
| `capability.capacity_unit` | 产能计量单位（米/台/套） | `米`（月 500） |
| `service.installation` | 是否含现场安装调试 | 未填（completeness 已列为 missing） |
| `capability.control_integration` | 电控系统集成能力 | 企业自述有 |
| `capability.project_based` | 是否项目制定制 | 是 |

---

## 缺口 2 · 工艺码表是零件加工导向，装不下成套设备

`skills/schema/process-codes.json` 现有 41 个码，全部是**单工序**级别：
`cnc_turning` / `bending` / `stamping` / `welding` / `injection_molding` / `anodizing` …

企业自述的五项工艺里，**只有 `welding` 有码**：

| 企业原话 | 有没有码 | 处理 |
|---|---|---|
| 焊接 | 有 → `welding` | 已入 `processes` |
| 钣金（泛称） | 无。最接近的 `sheet_assembly`=「钣金组装」，属另一品类，套上去就是误标 | 留在 `process_self_report.quote` |
| 机加工（泛称） | 无。`cnc_turning`/`cnc_milling` 都是具体工序，企业没说做哪一种 | 同上 |
| 装配 | 无 | 同上 |
| 电控系统集成 | 无 | 同上 |

**后果（`减少客户 Agent 使用负荷` 这条原则下最要紧的一条）**：
客户 Agent 按工艺过滤时，这家企业只在 `welding` 这一个码上可见。
搜「输送线 / 成套产线 / 装配」都召回不到它——**成套设备制造在工艺检索里系统性不可见**。

建议新增一族 equipment 级工艺码（与现有工序级码并列，不合并）：

```
equipment_assembly      成套设备装配
line_integration        产线集成
control_panel_integration  电控系统集成
sheet_metal_general     钣金（泛称，未细化到工序）
machining_general       机加工（泛称，未细化到工序）
```

> 红线提醒：泛称码只能在**企业确实只说了泛称**时用。说了具体工序就必须落具体码——
> 泛称码是兜底，不是偷懒的出口。

---

## 缺口 3 · Schema 落后于国标迁移（存量 4126 张卡不合规）

`skills/schema/vendor-skill.schema.json` 顶层 `additionalProperties: false`，
**不认 `gb_code` / `gb_name` / `gb_path`**。

实测：

```
skills/registry/capability/*.json   4137 张，其中 4126 张带 gb_code  → 4126 张全部 Schema 不合规
skills/vendors/*/capability.json    4137 张，其中   1 张带 gb_code  → 只有我新建那张
```

即：**国标四级归档上线后，能力卡加了 `gb_code`，但没有回头改 Schema。**
这不是本次新增的问题，是存量欠账。

### 顺带查出的第 4 个问题：门禁验的是过期副本

`validate_vendor_skills.py` 校验的是 `skills/vendors/`（源目录），
而**现行对外发布的是 `skills/registry/capability/`**（由 `gen_capability_shards.py` 生成）。

实测 `CN-MFG-0000005` 两份副本的差异：

```
仅 registry 有: ['gb_code', 'gb_name', 'gb_path']
值不同的键:     []
```

`vendors/` 是国标迁移**之前**的快照，4116 张卡在这里「恰好」都合规——
因为缺的正是那三个字段。**门禁输出 `[PASS]`，验的却不是会发布的那一版。**
这属于本项目已经踩过四次的「静默成功」同类。

> 修法二选一：① 让 `gen_capability_shards.py` 生成后反向回写 `vendors/`；
> ② 校验器直接指向 `registry/capability/`。建议 ②（单一事实来源）。

---

## 缺口 4 · 认证模型的新字段 Schema 里没登记

以下字段都是认证流程带来的**真实已核验数据**，按「不自降精度去凑现有枚举」
的原则保留在卡里，因此 `validate_vendor_skills.py` 会持续报错（属预期）：

| 字段 | 内容 | 为什么不能删 |
|---|---|---|
| `identity.uscc` | 91310000766492256E | 平台已核验（GB 32100-2015 校验位通过），是认主的法律主体锚点 |
| `identity.legal_person` | 陆斌 | 公开记录 |
| `identity.registered_address` | 上海市闵行区庙泾路66号I175室 | 与生产地址不同，是地址差异核验的依据 |
| `identity.business_nature` | manufacturer | 执照登记为批发，企业自述制造商，按「自述 > 公开记录」保留 |
| `identity.employee_outsource` | 20（外包/共 23 人） | 在场 23 人里 20 人是劳务外包——不写就是误导 |
| `claim.app_id` | CERT-20260908-0317 | 认证档案溯源唯一入口 |
| `claim.valid_until` | 2027-09-08 | 凭证有效期，过期判断要看它 |
| `evidence.public_record` | 高企认定 / 专利软著 / 法人 | 「公开记录、未经第三方核验」是独立证据档，不能并进 self_declared |
| `not_applicable` | 公差/最大尺寸/批量交期 三项 | **「不适用」≠「未填」**，这个区分本身就是数据可信度 |
| `process_self_report` | 企业工艺原话 | 见缺口 2 |

建议 Schema 增块：`identity` 增 5 键、`claim` 增 2 键、`evidence` 增 `public_record`、
顶层增 `not_applicable` / `process_self_report`。

---

## 本次已修的真 bug（不是缺口，是写错了）

`make_naidesi_card.py` 里三处，已修并重新生成卡片：

1. `claim.verified_by` 原取 `review.reviewer`（`"reviewer-01"`）→ 不在枚举
   `['wechat','email','manual','third_party',null]` 内。
   该字段语义是**核验渠道**不是审核人，改取认证档案的 `identity.verified_by` = `"wechat"`；
   审核人 reviewer-01 与凭证有效期转记到 `evidence.platform_verified`（附「Schema 无该字段位」说明）。
2. `identity.export_markets` / `quality.inspection_equipment` / `service.payment_terms` /
   `service.quote_inputs_required` 原写 `null`，Schema 要求 array。
   改 `[]`（与存量卡 CN-MFG-0001501 同一惯例：[] = 未申报，不是「确认没有」）。
3. `service.installation` 为 null 且 Schema 未登记 → 删除（无信息损失）。

另外 SKILL.md 原为自定义 7 段结构，被 `check_skill_md` 白名单拒（5 处 ERROR）。
已改回标准 8 段——**这一步是无损的**：原 7 段里的「平台核验项 / 联系与地址 / 人员 /
工艺与材料 / 资质」全部归并进 §3/§4/§6/§7，事实一条没丢。
§5「我们不做的」按 `render.py` 的既有惯例写「待补充」，不代填边界。

---

## 遗留（未做，等确认）

| # | 事项 | 说明 |
|---|---|---|
| 1 | `deploy_pages.py` 推送 L1/L2 到 CDN | 对外发布动作，本地产物已就绪，未执行 |
| 2 | `profiles/*.json` 的 path 只能是 `capability.*` | `check_profile` 只在 `cap['capability'] 里取键，写 `service.installation` 会静默查错地方、永远取不到值。equipment_assembly 已全部用 `capability.*`；老 profile 若有非 capability 路径需排查 |
| 3 | 泛称工艺码的使用纪律 | `sheet_metal_general` / `machining_general` 只在企业**确实只说泛称**时用；说了具体工序必须落具体码 |
| 4 | 越界大类仍无工艺码 | 13/17/20/21 按红线不推断；24/25/30/42 样本 ≤10 家，码表里记为 `out_of_scope_gb` 留空不猜 |
