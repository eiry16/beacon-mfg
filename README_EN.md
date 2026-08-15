# BeaconMFG — Agent-Searchable Directory of China Manufacturing Suppliers

> A free, open, 24/7 "online Canton Fair" for finding upstream manufacturers in China —
> organized by product keywords, readable by AI agents, bilingual (中文/English).

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
- Data from **public sources only** ( POI, government lists, company websites,
  exhibition directories) — traceable and removable on request

## Why does this exist?

Finding a Chinese manufacturer is painful: Baidu results are noisy, B2B marketplaces bury
you in ads, trade shows are expensive and slow. When your procurement agent can query a
clean, structured directory by product term and get a public phone number — sourcing
collapses from days to minutes.

**Strategy:** data is free and open; the *position* is the asset. We become the place
agents look first.

## Quick Start

### For agents / developers

```bash
git clone https://github.com/eiry16/beacon-mfg.git
cd beacon-mfg

# Chinese dataset
python scripts/query.py --keyword "CNC加工" --city 深圳 --limit 5

# English dataset (overseas buyers)
python scripts/query.py --keyword "CNC Machining" --city Shenzhen --en --limit 5
python scripts/query.py --category-en "Die Casting" --en --limit 10
```

Then load `SKILL.md` / `SKILL_EN.md` into your agent and ask naturally:
"Find a CNC machining shop near Shenzhen that accepts small batches."

### For contributors

Edit `data/suppliers/*.json` (or `data/en/*.json`), follow
`schema/supplier.schema.json`, run `python scripts/validate.py`, open a PR.

## Data Status (2026-08-14)

**Chinese** (`data/suppliers/`): **1,739 records, 171 published & agent-searchable**
across 8 categories — precision machining (426), sheet metal (298), injection molding
(71), die casting (120), electronic components (175), surface treatment (171),
standard parts (241), raw materials (243). Cities: 16 across the Yangtze River
Delta & Pearl River Delta (Shanghai, Guangzhou, Shenzhen, Dongguan, Foshan, Zhongshan,
Zhuhai, Huizhou, Suzhou, Wuxi, Changzhou, Ningbo, Jiaxing, Hangzhou, Wenzhou, Kunshan).

**English** (`data/en/`): **1,739 full English mirrors** (auto-translated, GLM-4-Flash) —
for overseas buyers and agents.

## Compliance & Trust

- **Public info only**: company name, address, public phone, website, certifications.
  No personal data, no financials.
- **Personal mobile numbers are masked** (`135****9722`) — full numbers are never stored;
  companies submit their full business contact themselves during the **claim process**
  (lawful authorization). Masked numbers double as a claim hook.
- Every record carries `source` + `source_url` + `verified_at`. Companies may request
  correction/removal via GitHub Issue.
- **Red lines**: we do not scrape B2B platforms (1688 etc.) or paid directories.
  Only official public APIs () and public listings.
- License: code MIT · data CC BY-NC 4.0.

## Tooling (all stdlib, zero dependencies)

| Script | Purpose |
|---|---|
| `scripts/gui_app.py` | **One-click GUI**: fetch → mask → validate → translate → push to GitHub |
| `scripts/query.py` | Search (CN/EN, keyword/region/cert filters) |
| `scripts/fetch_gaode_poi.py` |  POI fetcher (paginated, incremental, masked on ingest) |
| `scripts/fetch_batch.py` | One-command full-matrix refresh (quota-aware, ~100 req/day) |
| `scripts/translate_en.py` | English mirror via free GLM-4-Flash |
| `scripts/validate.py` | Schema + compliance gate (rejects unmasked mobiles) |
| `scripts/audit_contacts.py` | Contact verification checklist generator |

## Roadmap

- [x] 8 categories × 1,739 records skeleton, bilingual
- [x] Agent skills (CN/EN) + search toolchain + CI validation
- [x] One-click GUI pipeline
- [ ] Contact verification → replace masked numbers with verified landlines
- [ ] Company **claim/update** portal (phase 1)
- [ ] Value-add API / agent-visibility services

## Anti-Copying

Data and code are open by design; moats are continuous maintenance, the claim mechanism
(data rights), and ecosystem position. See [docs/ANTI_COPYING.md](docs/ANTI_COPYING.md).

## License

- Code: MIT
- Data: CC BY-NC 4.0 (attribution, non-commercial)

## Contact / Contribute

GitHub Issues for corrections, claims, and new categories. PRs welcome.
This is a side project — maintainers respond on weekends.
