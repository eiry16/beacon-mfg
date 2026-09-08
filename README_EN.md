# BeaconMFG · Agent-Searchable Directory of China Manufacturing Suppliers

> A free, open, 24/7 "online Canton Fair" for finding upstream manufacturers in China —
> organized by product keywords, readable by AI agents, bilingual (中文 / English).

**Information only. No transactions. Open data. Own the position.**

## What is this?

BeaconMFG is a **structured supplier directory built for AI agents**:

- Organized by **product keywords / procurement categories** (CNC machining, sheet metal,
  injection molding, die casting, electronic components, surface treatment, standard parts,
  raw materials) — not by industry taxonomy
- Every record: company name, product keywords, region, address, **public contact info**,
  data source, verification date
- Any agent (WorkBuddy, GPTs, MCP clients) loads `SKILL.md` (Chinese) or `SKILL_EN.md`
  (English) and searches directly — no API key, no signup, just `git clone`
- Data from **public POI directories** (public_directory source) — traceable and removable on request

## The data is just JSON

This repo **does not depend on any scripts or API keys**. The data lives under `data/`
as plain JSON files:

```
beacon-mfg/
├── SKILL.md                    # Agent instructions (Chinese dataset)
├── SKILL_EN.md                 # Agent instructions (English dataset)
├── data/
│   ├── index.json              # category → keywords → file path index
│   ├── industry-index.json     # GB/T 4754 industry index: code → supplier IDs
│   ├── region-index.json       # region index: city → supplier IDs
│   ├── suppliers/*.json        # Chinese data (split by category, 8 files)
│   └── en/*.json               # English mirror data (8 files)
├── schema/supplier.schema.json # record structure definition
└── docs/                       # contribution guide, category rules, anti-copying
```

The agent reads the JSON directly to search. See `SKILL.md` / `SKILL_EN.md` for usage.

## Quick Start

### For agents / developers

```bash
git clone https://github.com/eiry16/beacon-mfg.git
cd beacon-mfg
```

Then load `SKILL.md` (Chinese) or `SKILL_EN.md` (English) into your agent and ask naturally:
"Find a CNC machining shop near Shenzhen that accepts small batches."

No dependencies, no keys, no network required.

## Data Status

**Chinese** (`data/suppliers/`): **14,625 records total** across 8 categories:

| Category | Total | Verified | Pending |
|---|---|---|---|
| Precision Machining (CNC) | 3,914 | 2,132 | 1,782 |
| Sheet Metal & Stamping | 1,693 | 1,258 | 435 |
| Injection Molding | 1,968 | 867 | 1,101 |
| Die Casting | 464 | 226 | 238 |
| Electronic Components | 657 | 299 | 358 |
| Surface Treatment | 1,333 | 643 | 690 |
| Standard Parts | 2,075 | 1,453 | 622 |
| Raw Materials | 2,521 | 2,016 | 505 |

> **Verified**: 8,894 records with confirmed phone numbers; 5,731 pending.

### Industry classification (GB/T 4754-2017)

Every supplier carries a national industry class code:

```json
"industry": { "code": "3525", "name": "模具制造", "confidence": "high", "source": "name" },
"is_manufacturer": true
```

- **14,098 / 14,625 records classified** into **47 national industry classes** (527 unclassified)
- `confidence`: `high` = company name matched directly; `medium` / `low` = inferred from keywords or category
- `is_manufacturer=false` → the company falls under **F51 Wholesale** (trader, not a factory)
- Rule of thumb: **trust the company name**; search keywords may only fill gaps;
  **an empty code is better than a wrong one**

Top classes (full list in `data/industry-index.json`): 3484 Machined Parts 2,735 ·
2929 Plastic Parts 1,589 · 3311 Metal Structure 1,283 · 3360 Surface Treatment 1,259 ·
3399 Other Metal Products 1,247 · 3451 Bearings 652 · 3482 Fasteners 642 · 3525 Moulds & Dies 621.
> **Pending**: 5,731 records — real businesses from public POI directories, phone numbers pending manual verification. These are **not** placeholder data; they are real companies. Retain in search results.

**English** (`data/en/`): **14,625 English-mirror records** for overseas agents/buyers
(1:1 with the Chinese dataset by `id`; `industry_en` carries the English GB/T 4754 class
name — 14,098 records, same as the classified count in Chinese).

> **Fast region search:** use `data/region-index.json` to locate companies by city without scanning full category files.

> **Contact numbers** come from public POI directories — landlines / 400 hotlines / mobiles are shown in full (published by the businesses themselves, no masking or asterisks). Records marked "待核实" (pending verification) have unconfirmed phone data from public maps; never fabricate digits — contact the supplier via their website to confirm.

## Compliance

- Public business info only. No personal data. Each record carries `source + verified_at`.
- Companies may request correction/removal of their info via GitHub Issue.
- Anti-forking / anti-scraping strategy: [docs/ANTI_COPYING.md](docs/ANTI_COPYING.md).

## License

- Code: MIT
- Data: CC BY 4.0 (Attribution — commercial use allowed)
