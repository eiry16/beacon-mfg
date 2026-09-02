# BeaconMFG · 供应商灯塔

> **🌐 [English Version](README_EN.md)** · 中文为主，英文数据集见 `data/en/` · 落地页 https://eiry16.github.io/beacon-mfg/

[![License](https://img.shields.io/badge/code-MIT-3fb68a)](LICENSE)
[![Data](https://img.shields.io/badge/data-CC%20BY--NC%204.0-blue)](DATA_LICENSE.md)
[![Records](https://img.shields.io/badge/records-342-58a6ff)](data/)
[![Categories](https://img.shields.io/badge/categories-8-58a6ff)](data/index.json)
[![Stars](https://img.shields.io/github/stars/eiry16/beacon-mfg?style=social)](https://github.com/eiry16/beacon-mfg)

> 面向 Agent 的制造业供应商结构化名录 —— 按产品关键词分类，让下游厂商的 Agent 一眼看到上游供应商。

**只提供公开联系方式，不参与交易。数据免费开源，先占生态位。**

## 这是什么

BeaconMFG（供应商灯塔）是一个**面向 Agent 检索**的制造业供应商名录：

- 按**产品关键词 / 采购品类**组织（不是按行业归属）
- 每条供应商记录含：公司名、主营关键词、地区、官网、公开联系方式、资质标签、数据来源
- Agent（WorkBuddy、GPTs、各类 MCP 客户端）加载本仓库的 `SKILL.md` 后即可按需求检索
- 数据全部来自**公开渠道**（工商公示、政府名单、企业官网、展会名录），可溯源、可申诉
- **海外版**：`SKILL_EN.md` + `data/en/`（英文数据集）

## 数据就是 JSON 文件

本仓库**不依赖任何脚本或 API Key**。数据存放在 `data/` 下，是普通 JSON 文件：

```
beacon-mfg/
├── SKILL.md                    # Agent 主指令（中文数据集）
├── SKILL_EN.md                 # Agent 主指令（English / 海外版）
├── data/
│   ├── index.json              # 品类 → 关键词 → 文件路径 索引
│   ├── suppliers/*.json        # 中文数据（按品类拆分，8 个文件）
│   └── en/*.json               # 英文镜像数据（8 个文件）
├── schema/supplier.schema.json # 数据结构定义
└── docs/                       # 贡献指南、品类规则、防抄袭策略
```

Agent 直接读取 JSON 即可检索，使用方式见 `SKILL.md` / `SKILL_EN.md`。

## 快速开始

### 给 Agent 开发者

1. 克隆本仓库：`git clone https://github.com/eiry16/beacon-mfg.git`
2. 将 `SKILL.md`（中文）或 `SKILL_EN.md`（英文）配置为 Agent 的 Skill
3. 让 Agent 读取 `data/` 下的 JSON 按关键词/地区检索

无需安装依赖、无需配置 Key、无需联网。

## 数据现状

**中文数据集**（`data/suppliers/`）：**171 条真实数据**，覆盖 8 个品类：

| 品类 | 条数 |
|---|---|
| 精密机械加工 | 55 |
| 钣金冲压 | 38 |
| 注塑成型 | 22 |
| 原材料 | 19 |
| 标准件 | 11 |
| 压铸 | 10 |
| 电子元器件 | 8 |
| 表面处理 | 8 |

**英文数据集**（`data/en/`）：**171 条英文镜像**，供海外 Agent/买家使用。

覆盖地区：以珠三角（深圳/东莞/广州/佛山）为主，长三角（嘉兴等）持续补充中。

> 联系方式来自公开名录——座机/400/手机号完整展示（企业自行公开的经营联系方式，不做脱敏）。
> 部分记录电话为"待核实"，可通过企业官网或其他公开渠道补全，禁止编造。

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
企业可提交 PR 或 Issue 更新/删除自己的信息。被 fork/抄袭的应对策略见 [docs/ANTI_COPYING.md](docs/ANTI_COPYING.md)。

## 联系方式数据策略

- **座机 / 400 / 800 / 手机号**：完整展示（来源为公开名录 POI 数据，企业自行公开的经营联系方式）
- 数据不做星号脱敏；如企业要求更正/删除联系方式，可通过 GitHub Issue 提出

## 仓库结构

```
beacon-mfg/
├── SKILL.md                    # Agent 主指令（中文数据集）
├── SKILL_EN.md                 # Agent 主指令（English / 海外版）
├── data/                      # 供应商数据（中文 + 英文镜像）
│   ├── index.json             # 品类 → 关键词 → 文件路径 索引
│   ├── suppliers/*.json       # 中文数据（按品类拆分）
│   └── en/*.json              # 英文镜像数据
├── schema/supplier.schema.json # 数据结构定义
├── docs/                      # 贡献指南、品类规则、防抄袭策略
├── LICENSE                    # 代码 MIT
└── DATA_LICENSE.md            # 数据 CC BY-NC 4.0
```

## License

- 代码：MIT
- 数据：CC BY-NC 4.0（署名-非商业使用）

## 声明

本项目的定位是"Agent 时代的制造业黄页"。数据来自公开渠道，仅供参考，
不构成对供应商的推荐与评级，不参与任何交易环节。
