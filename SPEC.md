# BeaconMFG 项目规范（SPEC）

> ⚠️ **本文档已弃用（2026-09-19）。**
> 本文件描述的是 **2026-09-05 的旧架构**（`data/suppliers/` 按品类拆 8 个文件 + `data/index.json` + `data/en/` 8 文件同步）。
> 该架构已在「国标四级归档」重构后废弃——继续按本文档的数据结构 / 品类定义操作会**严重误导**。
> 历史决策与 Phase 1/2 路线图请查 git 历史（`git log -- SPEC.md`）。

---

## 当前权威文档（以这些为准）

| 文档 | 内容 |
|---|---|
| `README.md` / `README_EN.md` | 数据现状、覆盖范围、仓库结构、合规——**流水线生成，唯一权威数据事实** |
| `项目调试说明.md` | 架构总览、数据流动、分层（L0/L1/L2）规则、发布链、Git/Cloudflare 要点、避坑清单——**调试与排障第一手** |
| `SKILL.md` / `SKILL_EN.md` | Agent 主指令：如何检索 `data/gb/`（国标四级归档）、`industry-index.json`、`region-index.json`、`manifest.json`、Cloudflare Pages 分发 |
| `docs/` | 贡献指南、品类规则、防抄袭、各专项设计文档 |

---

## 仍有效的核心原则（不变）

1. **项目定位**：面向 Agent 检索的中国制造业供应商结构化名录；纯 JSON、无需 API Key、Agent 加载 SKILL 后直接读文件检索。
2. **数据红线**（见 `项目调试说明.md` §7.3、README「数据来源与合规」）：只发布公开经营信息；`id` 全局唯一；联系方式公开展示不编造；不批量爬 B2B 平台（1688 等）。
3. **合规**：代码 MIT；数据 CC BY 4.0（见 `DATA_LICENSE.md`）；企业可经 GitHub Issue 申诉更正 / 删除自己的信息。
4. **CI 不变量**：`scripts/validate.py --strict` 校验 id 唯一、schema 合法、README 数字与 `data/DATA_STATS.md` 一致；EN 镜像必须带 `industry` 且跨分片 id 唯一。

---

## 已废弃（本文档旧版，勿再参考）

| 旧（本文档） | 现状 |
|---|---|
| `data/suppliers/*.json`（按品类 8 文件） | → `data/gb/{门类}/{大类}/{小类}.json`（国标四级归档，46 大类 / 270 小类） |
| `data/index.json`（品类 → 文件） | → `data/gb-index.json` + `data/gb-alias.json` + `data/industry-index.json` |
| `data/en/*.json`（8 文件同步） | → `data/en/gb/`（与中文同构，由 `scripts/en_backfill.py` 增量补齐、`scripts/en_sync_industry.py` 补 `industry_en`） |
| `category` 字段（精确匹配品类列表） | → 以国标行业 `industry` 字段 + `is_manufacturer` 为准 |

---

*本规范不再作为「唯一规范来源」。项目当前以 README.md（数据事实）+ 项目调试说明.md（架构/发布/排障）+ SKILL.md（Agent 指令）共同描述，三者互相印证。*
