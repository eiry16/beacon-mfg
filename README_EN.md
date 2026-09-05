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
- Data from **public sources only** (public POI, government lists, company websites,
  exhibition directories) — traceable and removable on request

## The data is just JSON

This repo **does not depend on any scripts or API keys**. The data lives under `data/`
as plain JSON files:

```
beacon-mfg/
├── SKILL.md                    # Agent instructions (Chinese dataset)
├── SKILL_EN.md                 # Agent instructions (English dataset)
├── data/
│   ├── index.json              # category → keywords → file path index
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

**Chinese** (`data/suppliers/`): **7,958 real records** across 8 categories:

| Category | Count |
|---|---|
| Precision Machining (CNC) | 55 |
| Sheet Metal & Stamping | 38 |
| Injection Molding | 22 |
| Raw Materials | 19 |
| Standard Parts | 11 |
| Die Casting | 10 |
| Electronic Components | 8 |
| Surface Treatment | 8 |

**English** (`data/en/`): **7,915 English-mirror records** for overseas agents/buyers.

Regions: mainly the Pearl River Delta (Shenzhen / Dongguan / Guangzhou / Foshan), with the
Yangtze River Delta (Jiaxing, etc.) being added continuously.

> Contact numbers come from the public directory — landlines / 400 hotlines / mobiles are
> shown in full (published by the businesses themselves, no masking). Some records show
> "待核实" (pending verification); never fabricate digits.

## Compliance

- Public business info only. No personal data. Each record carries `source + source_url + verified_at`.
- Companies may request correction/removal of their info via GitHub Issue.
- Anti-forking / anti-scraping strategy: [docs/ANTI_COPYING.md](docs/ANTI_COPYING.md).

## License

- Code: MIT
- Data: CC BY-NC 4.0 (Attribution-NonCommercial)
