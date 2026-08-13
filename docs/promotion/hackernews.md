# Show HN 发布文案（Hacker News）

> 发布方式：https://news.ycombinator.com/submit
> 标题（≤80 字符）+ 正文（≤2000 字符，纯文本，支持 URL 自动链接）

## 标题

```
Show HN: BeaconMFG – open, agent-searchable directory of 263 China manufacturers
```

## 正文

Finding a Chinese supplier is painful: Baidu results are noise, B2B marketplaces bury
you in ads, trade shows cost a fortune. So I built BeaconMFG — an open directory of
China manufacturing suppliers organized by product keywords, designed to be read
directly by AI agents.

What's inside:
- 263 manufacturers across 8 categories (CNC machining, sheet metal, injection molding,
  die casting, electronic components, surface treatment, standard parts, raw materials)
- Bilingual: full English mirror (data/en/) for overseas buyers, translated with a free
  LLM model — think "a 24/7 online Canton Fair"
- Every record: company, product keywords, region, address, public contact info,
  source + verification date

How agents use it:
- git clone the repo, load SKILL.md (CN) or SKILL_EN.md (EN), then ask naturally:
  "find a CNC shop near Shenzhen that accepts small batches"
- No API key, no signup, no rate limits — it's just JSON in a repo

Design decisions I'd love feedback on:
1. Compliance-first: personal mobile numbers are masked (135****9722); full numbers only
   via a future "company claim" mechanism. Public info only. No scraping of B2B platforms.
2. Info-only: no quoting, no transactions, no ratings. The directory just wants to be
   the place procurement agents look first.
3. Zero-dependency tooling: fetch (AMap public API) → mask → validate → translate →
   push, all Python stdlib, one-click GUI included.

Repo: https://github.com/eiry16/beacon-mfg

Happy to hear what categories matter most for your sourcing needs — this is an early
MVP and I'd rather spend effort on data quality than features.

---

## 备选标题（A/B）

- `Show HN: An open directory of China manufacturers, searchable by AI agents`
- `Show HN: I made a free "online Canton Fair" for AI agents to find China suppliers`
