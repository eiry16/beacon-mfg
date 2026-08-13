# 贡献指南

欢迎通过 PR 为 BeaconMFG 补充和更正供应商数据。你的每一次贡献都在让"Agent 找供应商"更靠谱。

## 新增 / 更正供应商

1. 找到对应品类的数据文件（`data/suppliers/{品类}.json`），或从 `data/index.json` 确认品类归属
2. 新增一条记录，**严格遵循** `schema/supplier.schema.json` 的字段结构
3. 本地校验：`python scripts/validate.py`（必须通过）
4. 提交 PR，CI 会自动再次校验

### 数据规则（必读）

| 规则 | 说明 |
|---|---|
| 只放公开信息 | 企业名称、公开电话、官网、公开地址、资质。**禁止**法人个人手机号、身份证、财务信息 |
| 联系方式可核实 | 电话/邮箱必须来自企业官网或公开名录，禁止编造 |
| 必须标注来源 | `source` + `source_url`（可溯源），`verified_at` 填核实日期 |
| 模板数据勿混入 | 真实数据 `is_template: false`；示例占位保持 `is_template: true` |
| id 规则 | `CN-MFG-0001` 起递增，全局唯一 |

### 联系方式核实标准

- 优先：企业官网"联系我们"页
- 其次：工商公示登记电话（可能非业务电话，请在 note 注明"登记电话"）
- 政府名单（专精特新等）通常附联系方式，可直接采用

## 数据申诉（企业方）

如果你的企业信息有误或希望下架：

- 在 GitHub 创建 Issue，标题注明公司名
- 说明需要更正/删除的字段与依据
- 维护者核实后 5 个工作日内处理

## 新品类扩展

1. 在 `data/index.json` 的 `categories` 增加品类（含关键词词典）
2. 创建 `data/suppliers/{品类}.json`（空数组即可）
3. 更新 `docs/CATEGORY.md` 的关键词维护规则
4. 同步 `schema/supplier.schema.json` 的 `category.enum`

## 代码 / 脚本

- `scripts/` 一律使用 Python 标准库，不引入第三方依赖（降低贡献门槛）
- 修改检索逻辑后请跑 `examples/demo_agent.py` 验证
