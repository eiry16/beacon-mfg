# Reddit 发布文案

> 推荐板块：r/manufacturing、r/China、r/Entrepreneur、r/Procurement（看主题选 1-2 个）
> 社区规则：先回答问题、别硬广。以下是"分享 + 求反馈"口吻的版本。

## r/manufacturing（推荐）

**标题：**

```
I built an open directory of 1,739 Chinese manufacturers, searchable by AI agents — feedback welcome
```

**正文：**

I run a small sourcing consultancy, and the #1 recurring pain is the same every time:
finding a reliable upstream manufacturer in China takes days of Baidu noise, 1688 ad
spam, or expensive trade-show trips.

So I started an experiment: an **open GitHub directory of Chinese manufacturers**
organized by product keywords (CNC machining, sheet metal, injection molding, die
casting, electronic components, surface treatment, standard parts, raw materials),
designed to be consumed directly by AI agents.

Current state:
- 1,739 companies across 8 categories (Shenzhen/Dongguan/Suzhou/Ningbo, more to come)
- Full English mirror for overseas buyers
- Each record: company name, product keywords, region, address, public phone, source
- Compliance-first: public info only; personal mobiles are masked; no scraping of
  B2B platforms; companies can remove their listing on request

How it works for an agent: `git clone` → load the skill → "find a CNC shop near
Shenzhen that accepts small batches" → structured results with contact info.
No API key, no signup.

I'm not trying to sell anything — it's open source and information-only (no
transactions, no ratings). I'd genuinely like feedback from people who source
in China:

1. Which categories/cities should I prioritize next? (thinking 3D printing/contract
   manufacturing, or Ningbo/Chongqing next)
2. Would you actually use an agent-based directory like this, or is it a solution
   looking for a problem?
3. For US/EU buyers: what info is missing from a listing that you'd want before
   making first contact?

Repo: https://github.com/eiry16/beacon-mfg

## 备选板块与标题

- r/China: `Chinese manufacturers directory, now searchable by AI — built it, feedback?`
- r/Entrepreneur: `I built a free open-source "online Canton Fair" for finding suppliers — what would make it useful to you?`
