---
name: beacon-mfg-en
description: Agent-facing directory of Chinese companies and local businesses, archived by GB/T 4754-2017 national industry gate. Covers 7 gates — C Manufacturing, F Wholesale & Retail, H Accommodation & Catering, I IT & Software, M Scientific & Technical Services, O Residential Services & Repair, R Culture/Sports/Entertainment. Use when the user needs suppliers, factories, OEM/ODM vendors (CNC machining, sheet metal, injection molding, die casting, moulds, fasteners, electronic components), wholesalers / distributors / trading companies, food & beverage venues (restaurant, hotpot, fast food, coffee, tea & bubble tea, bakery, bar, hotel, B&B), local service businesses (hair & beauty salon, laundry, auto repair, appliance repair, gym, KTV, internet cafe, cinema, amusement park, pet services), or technical service providers (software development, system integration, cybersecurity, big data, third-party testing, calibration, certification, industrial design, environmental monitoring); also for filtering by GB/T 4754 industry code, product keyword, or city/region. Handles Chinese-language sourcing requests too. **Query through the MCP server (beacon-mfg-mcp) or the thin-framework client — not local scripts.** Public POI directory with contact info only — no transactions.
---

# BeaconMFG · Supplier Search Skill (English Dataset)

> ## ⚠️ As of 2026-09-22 this repo holds only the MCP server and the read-only data surface
>
> Scraping / translation / derived-layer rebuild / publishing (Cloudflare Pages, R2, pushing to
> the cloud) belong to the **local factory** and are **not** in this repo. **This document
> therefore no longer describes the "clone the whole repo and run local scripts" path** —
> the `scripts/*.py` it used to reference ship with the factory, so running those commands
> now just yields "file not found".

---

## 1. Two entry points

### A. MCP (recommended — hosted, zero maintenance, no key)

```bash
npx -y beacon-mfg-mcp          # or wire it into any MCP client via mcp/README.md
```

No API key, no cloning. Six tools:

| Tool | Purpose | Key params |
|---|---|---|
| `search_vendors` | Directory search (keyword / city / GB code) | `query` (company·process·material·cert substring), `city` (accepts prefecture cities and county-level cities), `gb` (GB code — restricts the scan to those shards), `limit` (1–200), `offset` |
| `get_vendor` | Full Chinese record by id | `id`, `gb` (omit it and the code is reverse-looked-up from fingerprint shards, slower) |
| `get_capability_card` | L1 capability card by id (process slots, equipment, capacity, certifications, MOQ) | `id`; returns `has_card: false` with a reason when absent |
| `start_sourcing` | **Auto-triggered on find-a-factory / sourcing / RFQ intent**: category detection → wide fingerprint recall → requirement normalisation → 1–2 rounds of clarifying questions | `demand_text`, `audience_id` (`domestic_downstream` / `intl_buyer`) |
| `answer_sourcing` | Continue the clarification rounds, or return an initial shortlist | `session_id`, `answers` |
| `refine_sourcing` | Recommendation round: `details` / `more` / `best` | `session_id`, `action`, `value` |

**For multi-turn sourcing conversations start with `start_sourcing`** — it runs the protocol
kernel in `skills/rfq-kernel/` (category detection + clarifying-question generation +
requirement normalisation), not plain keyword matching.

### B. Thin framework (when you don't want to run MCP)

Two files, **no data clone**:

- `agent-skill/SKILL.md` — protocol and flow
- `agent-skill/client_search.py` — on-demand fetch wrapper with UA and ETag caching

```bash
python client_search.py --industry 3525 --city 宁波 --limit 5
```

Shards are pulled **on demand from the CDN**, < 1 MB per query on average.
Both entry points return the same results from the same source.

### About `data/` on GitHub

The `data/**` in this repo is a **masked public snapshot** (phone numbers look like
`138****0000`), kept so the offline "read committed content only" mode works
(MCP's `BEACON_REPO` mode). Full contact details come from the cloud on demand.
Neither entry point requires you to clone it.

---

## 2. Data source

| Item | Value |
|---|---|
| Primary (CDN) | `https://beacon-mfg.pages.dev` |
| Shard manifest | `data/manifest.json` |
| Data as-of date | see `data/DATA_STATS.md` |

1. Fetch `data/manifest.json` (~143 KB, cacheable).
2. Filter to the shards you need by GB code.
3. Cache the response `ETag`; next time send `If-None-Match` — a hit returns `304` (zero transfer).
4. Fetch details (or L1/L2) by `id`.

⚠️ **Always send a User-Agent.** Cloudflare returns `403` (error 1010) for UA-less requests;
the symptom is "the source is down", which is easy to misdiagnose.

---

## 3. Two kinds of `SKILL.md` in this repo — don't confuse them

| | Name prefix | Location | Nature |
|---|---|---|---|
| **Search entry skill** (this doc) | `beacon-mfg` / `beacon-mfg-en` | repo root | **Instructions** — how to search |
| **Vendor data card** | `beacon-mfg-vendor-*` | `skills/vendors/{id}/SKILL.md` | **Data** — one company's profile, no instructions |

Those vendor files are **data records indexed by vendor id**, not installable sub-skills.
If one shows up in a skill list, it is a capability card (fetched on demand) —
**do not** load it as a standalone skill. They are served from the cloud, not from this repo.

---

## 4. Coverage: 7 national industry gates

| Gate | Name | Typical queries |
|---|---|---|
| C | Manufacturing | CNC machining, sheet metal, injection molding, die casting, moulds, fasteners, electronics |
| F | Wholesale & Retail | wholesalers, distributors, trading companies, hardware/building materials, convenience stores, pharmacies |
| H | Accommodation & Catering | restaurant, hotpot, fast food, coffee, bubble tea, bakery, bar, hotel, B&B |
| R | Culture, Sports & Entertainment | gym, KTV, internet cafe, cinema, amusement park, sports venues |
| O | Residential Services & Repair | hair & beauty, laundry, auto repair, appliance repair, pet services |
| I | IT & Software | software development, system integration, cybersecurity, big data, ops |
| M | Scientific & Technical | third-party testing, calibration, certification, industrial design, environmental monitoring |

> **Per-gate counts change daily — always read `data/DATA_STATS.md`, never quote stale numbers
> from this file.**
>
> **When a gate comes up short:** check the per-class breakdown in `data/DATA_STATS.md`,
> then report that number honestly. **Do not** substitute approximate results from another gate —
> that would be fabrication. Some records have `industry: null` (gate not yet determined) —
> **no label is invented** for them; they remain searchable by keyword.

**Prefer a GB code over keywords.** Factories are registered as "plastic products", not
"injection molding" — keyword search misses badly, GB codes do not. There are three ways to
get the code: the curated alias table (`data/gb-alias-curated.json`), the derived alias table
(`data/gb-alias.json`, only words that actually occur in company names), and the full GB
industry table (`data/gb4754-full.json`). Or just call `search_vendors` and let it resolve.

---

## 5. Manifest contract (`data/manifest.json`)

`metadata.total_shards = 839`:

| Type `t` | Shards | Records | Content | Path |
|---|---|---|---|---|
| `fp` | 273 | 139,318 | L0 fingerprints (recall surface, smallest) | `skills/registry/fingerprint/gb/{gate}/{division}/{class}.jsonl` |
| `zh` | 284 | 139,318 | Full Chinese records | `data/gb/{gate}/{division}/{class}.json` |
| `en` | 281 | 135,072 | English mirror | `data/en/gb/{gate}/{division}/{class}.json` |
| `phone` | 1 | 103,137 | Phone index | `data/phone-index.jsonl` |

Shard entry fields: `p` path · `b` bucket key · `c` GB code · `n` GB name · `t` type ·
`k` record count · `z` bytes · `h` content SHA1 · `u` last updated.

⚠️ **Use the HTTP `ETag` for incremental updates, not `h`.** `h` is a content digest,
not a version. Its only job is verifying a downloaded shard was not truncated.

⚠️ **A GB code may be split across continuation shards** (`3484.json` + `3484-p2.json`).
Scanning only the first one silently drops records.

Counts above are a 2026-09-22 snapshot; live values are in `metadata.by_type`.

---

## 6. Layers: L0 / L1 / L2

| Layer | Content | Where |
|---|---|---|
| **L0 fingerprint** | company / city / GB code / process / material / cert / has-phone (compact) | `skills/registry/fingerprint/**`, in this repo |
| **L1 capability card** | process slots, equipment, capacity, certifications, MOQ | `skills/registry/capability/{id}.json`, **cloud only** (`get_capability_card`) |
| **L2 self-description** | vendor narrative, one Skill per factory | `skills/vendors/{id}/SKILL.md`, **cloud only** |

**L0 deliberately stores no phone numbers** — numbers come only from the `phone` shard.
That way the fingerprint layer can be scanned in full without touching contact data.

---

## 7. Fields

**The authoritative definition is `schema/supplier.schema.json`.** Summary — note that the
three representations use different key sets:

| Layer | Fields | Purpose |
|---|---|---|
| Chinese record `zh` | `id` · `company` · `category` · `keywords` · `region{province,city}` · `address` · `lat/lng` · `industry{code,name,level,path,confidence}` · `is_manufacturer` · `contact_phone` · `source`/`source_url` · `status` · `is_template` · `amap{poi_id,adcode,…}` · `note` | Full record |
| L0 fingerprint `fp` | `id` · `co` · `city` · `dist` · `gb` · `mf` · `proc` · `mat` · `cert` · `cl` · `pv` · `sc` · `tel` | Recall surface (short keys save bytes) |
| `search_vendors` result | `id` · `company` · `city` · `district` · `gb` · `badge` · `score` · `has_phone` · `process` · `material` · `cert` | Readable summary (fingerprint rows normalised) |

Things that trip people up:

- **`name` / `gb` / `proc` are not in the Chinese record** — it uses `company` and
  `industry.code`. Short keys exist only in fingerprint rows. Don't write field names from intuition.
- **The district is not in the Chinese record's `region`** (province + city only). County-level
  cities are filed under their prefecture city (Kunshan → Suzhou); match the fingerprint `dist`.
- `category` (product category, e.g. 注塑成型) and `industry` (GB code) are **two separate
  vocabularies** — don't substitute one for the other.
- `industry: null` means the GB code is not yet determined — **no label is invented**;
  the record is still in the directory and keyword-searchable.
- `is_template: true` does **not** mean a placeholder — it is a real business POI whose phone is
  **awaiting verification**, and should be kept in results.
- English mirror fields: `company_en` / `address_en` / `keywords_en` / `industry_en`
  (`{code, name_en, level, name, confidence}`). Every English record **must carry `industry`**,
  and the same `id` must not appear in more than one shard.

---

## 8. Boundaries (mandatory)

- **Information only** — no quoting, ordering, or transactions.
- **No ratings or recommendations.** Asked for "the best one" → say it is ranked by fit and
  the user should verify independently.
- **Never invent** price, lead time, or capacity. If it is not in the data, say so.
- Weak evidence never overrides strong evidence: process/material come from the company name
  and capability card; search keywords only fill gaps.
- "Not filled in ≠ doesn't do it" — downweight missing fields, don't drop the company.
- A misapplied label must be corrected: no real company should be unfindable because of a label.

---

## 9. License & disputes

- **Code** MIT; **data** CC BY 4.0 (see `DATA_LICENSE.md`).
- Businesses may dispute, correct, or request removal of their own listing via a GitHub Issue.

---

## 10. Repo layout

```
mcp/                        read-only MCP server (npm package beacon-mfg-mcp)
agent-skill/                thin framework: SKILL.md + client_search.py
data/
  manifest.json             shard manifest (the entry point)
  gb/**  en/**              Chinese / English record shards
  phone-index.jsonl         phone index
  gb-index.json  industry-index.json  region-index.json
  gb-alias.json  gb-alias-curated.json  gb4754-full.json
  endpoint.json             app data-source discovery pointer
  DATA_STATS.md             statistics (source of truth for counts)
schema/supplier.schema.json authoritative record schema
skills/
  registry/fingerprint/**   L0 fingerprint shards
  registry/index/**         inverted index (meta / city / terms)
  rfq-kernel/**             sourcing protocol kernel (used by the sourcing tools)
README.md  README_EN.md     data status and coverage
SPEC.md                     data format specification
```

---

*Scraping / translation / derived-layer rebuild / publishing pipelines are local factory assets
and are not in this repo. This repo carries only the public artifacts that let a customer's
agent find Chinese manufacturers.*
