# 发布清单与 GitHub 仓库配置

## 1. GitHub 仓库 About 描述（已通过 gh 设置，见下方）

```
Agent-searchable directory of China manufacturing suppliers (8 categories, 1,739 records, bilingual). Free 24/7 "online Canton Fair" for sourcing. Info-only, compliance-first.
```

## 2. GitHub Topics（已通过 gh 设置）

`agent` `skill` `mcp` `manufacturing` `supply-chain` `supplier-directory`
`china` `b2b` `opendata` `sourcing` `procurement` `cnc`

> 手动补充（网页操作，可选）：Settings → General → Topics（或仓库主页 About → Edit）

## 3. GitHub Release（已通过 gh 创建）

- Tag: `v0.1.0`
- Title: `BeaconMFG v0.1.0 — Agent-searchable China supplier directory`
- 内容：双语数据集 1,739 条、SKILL.md/SKILL_EN.md、工具链、合规说明

## 4. 外部平台发布顺序（需要你登录操作）

| 平台 | 文件 | 建议时间 |
|---|---|---|
| Hacker News | `docs/promotion/hackernews.md` | 周一~周四 9:00-11:00 ET（流量高峰） |
| Reddit r/manufacturing | `docs/promotion/reddit.md` | 同日稍后 |
| X (Twitter) | `docs/promotion/social.md` 推文 1-3 | 与 HN 同步，间隔 2-4 小时 |
| LinkedIn | `docs/promotion/social.md` LinkedIn 段 | 工作日早晨（欧美时区） |

## 5. 发布后运营建议

- 48 小时内回复所有评论（HN 的前几条评论决定生死）
- 把有价值的反馈（新品类建议、字段需求）收集到 GitHub Issue
- 数据量增长后（≥500 条）做第二次发布（v0.2.0）
- 可投稿渠道：Product Hunt（英文）、V2EX 制造版（中文）、掘金/InfoQ（中文技术）

## 6. 仓库英文可读性检查（发布前确认）

- [x] README_EN.md 已提交（根目录）
- [x] SKILL_EN.md 已提交
- [x] README.md 顶部含英文简介
- [x] Release notes 含英文
- [ ] 建议：README.md 顶部加语言切换链接（English | 中文）→ 指向 README_EN.md
