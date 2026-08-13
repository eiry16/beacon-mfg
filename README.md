# BeaconMFG · 供应商灯塔

> 面向 Agent 的制造业供应商结构化名录 —— 按产品关键词分类，让下游厂商的 Agent 一眼看到上游供应商。

**只提供公开联系方式，不参与交易。数据免费开源，先占生态位。**

## 这是什么

BeaconMFG（供应商灯塔）是一个**面向 Agent 检索**的制造业供应商名录：

- 按**产品关键词 / 采购品类**组织（不是按行业归属）
- 每条供应商记录含：公司名、主营关键词、地区、官网、公开联系方式、资质标签、数据来源
- Agent（WorkBuddy、GPTs、各类 MCP 客户端）加载本仓库的 `SKILL.md` 后即可按需求检索
- 数据全部来自**公开渠道**（工商公示、政府名单、企业官网、展会名录），可溯源、可申诉

## 为什么做这个

制造业采购找上游供应商是真实痛点：百度搜索信息乱、B2B 平台广告干扰多、展会成本高。
当采购者的 Agent 能直接查到一个**结构化、可按产品词精确检索**的供应商库时，找供应商的成本大幅下降。

我们的打法：**数据免费公开 → Agent 生态习惯使用 → 位置占住 → 再谈增值**。
数据是免费的，位置才是资产。

## 快速开始

### 给 Agent 开发者

1. 克隆本仓库（或仅 `data/` 目录）：
   ```bash
   git clone https://github.com/your-name/beacon-mfg.git
   ```
2. 将 `SKILL.md` 配置为 Agent 的 Skill（WorkBuddy 直接放 skills 目录即可）
3. 检索示例：
   ```bash
   # 命令行直接查（不依赖 Agent）
   python scripts/query.py --keyword "CNC加工" --city 深圳 --limit 5
   python scripts/query.py --keyword "小批量" --cert 高新技术企业 --limit 10
   ```

### 给数据贡献者

- 新供应商数据：编辑对应品类的 JSON 文件，遵循 `schema/supplier.schema.json`，提交 PR
- 提交前本地校验：`python scripts/validate.py`
- 规则见 `docs/CONTRIBUTING.md`

## 数据现状

| 品类 | 数量 | 数据状态 |
|---|---|---|
| 精密机械加工 | 模板 3 条 | 首发品类，待高德 POI + 官网核实填充 |
| 钣金冲压 / 注塑成型 / 压铸 / 电子元器件 / 表面处理 / 标准件 / 原材料 | 0 | 品类骨架已建，待填充 |

> 当前为模板数据（`is_template: true`），联系方式为占位符，仅用于验证数据结构与检索逻辑。
> 真实数据通过 `scripts/fetch_gaode_poi.py`（高德开放 API，合规绿区）拉取骨架后人工核实填充。
> 更新数据状态可运行 `python scripts/stats.py`。

## 数据来源与合规

| 来源 | 状态 | 说明 |
|---|---|---|
| 国家企业信用信息公示系统 | ✅ 绿区 | 企业骨架：名称、地址、登记电话 |
| 政府名单（专精特新/高新技术企业） | ✅ 绿区 | 资质标签 |
| 企业官网 | ✅ 绿区 | 业务联系方式、主营产品 |
| 展会/协会名录 | ✅ 绿区 | 品类集中，冷启动高效 |
| 工商数据服务商（企查查等） | ⚠️ 黄区 | 仅人工参考，不批量抓取 |
| B2B 平台（1688 等） | ❌ 红区 | 不爬取 |

**红线：** 只发布企业公开经营信息，不发布个人隐私；每条数据标注 `source + source_url + verified_at`；
企业可提交 PR 或 Issue 更新/删除自己的信息。

## 仓库结构

```
beacon-mfg/
├── SKILL.md                    # Agent 主指令
├── data/
│   ├── index.json              # 品类 → 关键词 → 文件路径 索引
│   └── suppliers/*.json        # 按品类拆分的数据
├── schema/supplier.schema.json # 数据结构定义
├── scripts/
│   ├── query.py                # 检索脚本
│   ├── validate.py             # 数据校验
│   ├── stats.py                # 数据统计
│   └── fetch_gaode_poi.py      # 高德 POI 抓取框架（需填 API Key）
└── docs/                       # 贡献指南、品类维护规则
```

## 路线图

- [ ] MVP：CNC 品类 200 家（深圳+东莞）
- [ ] 扩展品类：钣金 / 注塑 / 压铸
- [ ] CI 数据校验自动化
- [ ] 供应商"认领/更新"入口
- [ ] 增值 API / Agent 可见性服务

## License

- 代码：MIT
- 数据：CC BY-NC 4.0（署名-非商业使用）

## 声明

本项目的定位是"Agent 时代的制造业黄页"。数据来自公开渠道，仅供参考，
不构成对供应商的推荐与评级，不参与任何交易环节。
