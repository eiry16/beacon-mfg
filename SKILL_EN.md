---
name: beacon-mfg-en
description: Agent-facing search over a structured directory of Chinese manufacturing suppliers. Use when the user needs to find upstream manufacturers (CNC machining, sheet metal, injection molding, die casting, moulds, casting, rubber parts, gears, electronic components, etc.) or filter suppliers by GB/T 4754 industry code / product keyword / region. Data comes from public sources; only public contact info is provided; no transactions.
---

# BeaconMFG · Supplier Search Skill (English Dataset)

## Capability

Search a structured directory of China manufacturing suppliers by product keywords,
region, and **GB/T 4754-2017 national industry code**. Returns **structured, traceable**
supplier records with public contact info.

**Scope:** information only — no quoting, ordering, or transactions; no ratings or recommendations.

## Data location

This skill consumes the **English mirror** at `data/en/gb/`, archived by **GB/T 4754-2017**
four-level hierarchy (20 gates / 97 divisions / 473 groups / 1382 classes) — the same layout
as the Chinese dataset `data/gb/`, so `id` joins 1:1 across languages.

| Path | Purpose |
|---|---|
| `data/en/gb/{gate}/{division}/{class}.json` | English records (`company_en` / `address_en` / `keywords_en`) |
| `data/manifest.json` | **Recommended entry point.** Shard list with path / count / bytes / SHA1 |
| `data/industry-index.json` | GB class → supplier ID list (**use this when you know the industry**) |
| `data/region-index.json` | City → supplier ID list (**use this when you know the city**) |
| `data/gb-index.json` | Archive tree with per-level counts (**browse "which industries have stock"**) |
| `data/gb-alias.json` + `data/gb-alias-curated.json` | Procurement term → GB code alias table (**read both**) |
| `skills/registry/fingerprint/gb/.../{class}.jsonl` | L0 capability fingerprints (234 B/row, 100% coverage) |
| `schema/supplier.schema.json` | Field structure |

Each English record contains: `id`, `company_en`, `category_en`, `keywords_en`, `region`
(English), `address_en`, `contact_phone`, `source`, `verified_at`, `note_en`, `_bucket`,
plus `industry_en` = `{code, name_en, level, name, confidence}` — the **GB/T 4754-2017**
code for that company.

The data is plain JSON — **no scripts, no network, no API key required**.

### Only need one or two classes? Pull from the manifest — don't clone the whole repo

| Method | Transfer |
|---|---|
| `git clone` (full repo) | 4 MB |
| manifest + the shards you hit | **~0.3 MB** |

```python
import json, urllib.request
base = "https://raw.githubusercontent.com/eiry16/beacon-mfg/main"
man = json.loads(urllib.request.urlopen(f"{base}/data/manifest.json").read())
hit = [s for s in man["shards"] if s["t"] == "en" and s["c"] == "3525"]
rows = json.loads(urllib.request.urlopen(f"{base}/{hit[0]['p']}").read())
# Send If-None-Match: shard["h"] for 304 caching — repeat queries cost ~0 bytes
```

Reference implementation: `scripts/client_search.py`.

> ⚠️ Do **not** read `data/en/*.json` (e.g. `precision-machining.json`). Those are the retired
> 8-category files — a frozen snapshot that is missing thousands of newer records.

## Usage Flow

### Step 0 (do this first): translate the request into a GB code

**If the user names an industry or process (moulds, die casting, plating, rubber parts, gears…),
locate it by industry code — do not guess with keywords.** Keywords miss badly (factories are
listed as "plastic products", not "injection molding"); GB codes do not.

Three ways to get the code, in order:

1. **Alias table** (307 entries = 62 data-derived + 245 hand-curated): e.g. "CNC machining"→3484,
   "PCB"→3989, "conveyor line"→3434.
   - `gb-alias.json` — derived layer, only words that actually appear in company names, with real hit counts.
   - `gb-alias-curated.json` — curated layer, words buyers search for but factories never write
     (conveyor line / assembly line / seals / gears…). These entries have **no hit count** —
     hit counts can only come from real data; writing one by hand would be fabricating a number.

   ⚠️ **Results come in three evidence tiers — don't treat them equally.** Alias expansion is
   *whole-class* expansion, not synonym expansion: matching a GB code pulls in every company in
   that class. Searching "gears" would otherwise drag in all 3,606 machined-parts companies.

   | Tier | Meaning | How to use |
   |---|---|---|
   | literal keyword | the term is in the company's own keywords | strongest, use directly |
   | alias primary code | the class closest to the buyer's term | strong, use directly |
   | industry inference | only classified into that industry; company never claimed it | **weak — confirm before citing** |

   Filler codes are capped (`max_supply=500`): any non-primary code whose class holds more than
   500 companies is dropped. Primary codes are **never** dropped.

2. **Quick reference table** (below).
3. **Browse the archive tree** in `data/gb-index.json` → `tree`.

Quick reference (full list in `data/industry-index.json`):

| Code | Industry (EN) | Code | Industry (EN) |
|---|---|---|---|
| 3484 | Machined Parts & Components (CNC) | 3391 | Ferrous Metal Casting |
| 3525 | Moulds & Dies | 3392 | Non-ferrous Metal Casting (die casting) |
| 3311 | Metal Structure (sheet metal/stamping) | 3393 | Forgings & Powder Metallurgy |
| 3360 | Metal Surface Treatment & Heat Treatment | 2913 | Rubber Parts |
| 2929 | Plastic Parts & Other Plastic Products | 3451 | Rolling Bearings |
| 2921 | Plastic Film | 3453 | Gears & Gearboxes |
| 2926 | Plastic Packaging Box & Container | 3482 | Fasteners |
| 2651 | Primary-form Plastics & Synthetic Resin | 3483 | Springs |
| 3982 | Electronic Circuits (PCB) | 3660 | Automotive Parts & Accessories |
| 3989 | Other Electronic Components | 5164 | Metals & Metal Ores Wholesale (**not manufacturing**) |

### Step 1: industry index → ID list

```python
import json
idx = json.load(open("data/industry-index.json", encoding="utf-8"))
item = idx["index"]["3525"]                  # Moulds & Dies
print(item["name"], item["count"])           # 模具制造 1545
target_ids = set(item["ids"])
```

Code prefixes work as levels: `29` = Rubber & Plastics (division), `339` = Casting & other metal
products (group), `C` = Manufacturing (gate).

> **Note:** `industry-index.json` is a snapshot. After new data is fetched it must be regenerated
> (`scripts/gen_industry_index.py`, an internal maintenance script not shipped in this repo),
> otherwise newly added companies can never be found by industry — a **silent** failure.
> If a class looks suspiciously small, suspect a stale index before suspecting missing data.

### Step 2: intersect with the region index (if a city is given)

```python
city_ids = set(json.load(open("data/region-index.json", encoding="utf-8"))
               ["index"]["Zhejiang-Ningbo"]["ids"])
hit_ids = target_ids & city_ids
```

> The region index covers **18 cities** (Suzhou 2210, Dongguan 1943, Ningbo 1898, Zhongshan 1731,
> Shanghai 1624, Shenzhen 1586…). If the target city is not in the index, fall back to industry
> only and check `region` per record.

### Step 3: read details by ID

IDs carry no path information. Two options:

1. **Read everything and filter** (23,698 records ≈ 27 MB — simplest if your environment allows):

```python
import glob, json
hits = []
for f in glob.glob("data/en/gb/*/*/*.json"):
    hits += [r for r in json.load(open(f, encoding="utf-8")) if r["id"] in hit_ids]
```

2. **Read only the target class file** — each class key in `gb-index.json` is also the file path
   `data/en/gb/{gate}/{division}/{class}.json`.

Field notes:
- `company_en`: company name (English or transliterated)
- `keywords_en`: product keyword array (substring match)
- `region`: `{ "province": "...", "city": "..." }` (English, e.g. `"Zhejiang"`, `"Ningbo"`)
- `contact_phone`: landlines / 400 hotlines / mobiles shown **in full**; `"Pending verification"` when missing
- `industry_en`: GB/T 4754 class; `null` = unclassified — **do not guess one**
- `source` / `verified_at`: provenance and import date

One-liner filter:

```python
import json
recs = json.load(open("data/en/gb/C/34/3484.json", encoding="utf-8"))
hits = [r for r in recs
        if r["region"]["city"] == "Shenzhen"
        and any("CNC" in k for k in r["keywords_en"])]
# Records with contact_phone == "Pending verification" are real companies too — keep them.
```

### Step 4: present results (2-3 best matches)

```
Company: Jiaxing Precision Technology Co., Ltd. (Zhejiang · Jiaxing)
Products: CNC Machining / Precision Components / Small Batch Custom
Industry: 3484 Machined Parts & Components (confidence: high)
Phone: 0573-XXXXXXXX     ← full landline, call directly
Source: public directory · imported 2026-08-13
```

Rules:
- Contact numbers are shown in full — present as-is. If a number is missing (pending
  verification), tell the user to verify via the company website or other public channels;
  never fabricate digits.
- Never invent prices, lead times, or capacity.
- Always state "data from public sources, imported on YYYY-MM-DD".

## Capability layer (can they do the job vs. do they merely exist)

The flow above answers "**does this factory exist**". To judge "**can this factory do my job**",
use `skills/registry/fingerprint/gb/{gate}/{division}/{class}.jsonl` — one capability fingerprint
per company, 234 B, **covering all 23,698**.

**Coarse-filter first, then read deeply** — do not load all self-reports at once (23,698 rows
will blow up your context):

| Stage | Read what | Scale |
|---|---|---|
| 1 filter | the class's `fingerprint/gb/.../{class}.jsonl`, numeric rules | one class → 10-30 companies |
| 2 compare | `capability/{id}.json` | 30 → 5 |
| 3 deep read | `vendors/{id}/SKILL.md` | 5 → 3 |

**Read only the class shard you need** (e.g. 3525 Moulds & Dies ≈ 0.25 MB), not the whole 5.3 MB.

**L1 capability cards are also served by class** (deployed to CDN 2026-09-09):

```
GET https://beacon-mfg.pages.dev/manifest.json              # shard list + SHA1
GET https://beacon-mfg.pages.dev/full/gb/C/35/3525.json     # every capability card in 3525
GET https://beacon-mfg.pages.dev/skills/vendors/{id}/SKILL.md   # vendor self-report (L2)
```

One request returns all capability cards for that class. Note that **L0 fingerprints live on
GitHub (jsDelivr), not here** — the two layers have different base URLs. Don't mix them.

Honest fill rates: `processes` 100% (inferred from company name, **not confirmed by the company**),
`materials` 15.3%, `limits` **0.1% (6 of 4136)**. A missing hard metric means "not reported" — never 0.

Fingerprint fields (short keys on purpose, to save tokens):

```
id / co(company) / city / gb(GB code) / mf(is manufacturer) / proc(process codes) / mat(materials)
cert / cl(evidence level) / pv(source: auto capability card / vendor_claimed / derived) / sc(profile score) / tel(has phone)
tol / size / moq / lt / rt —— only present for capability-card companies; absent keys are omitted
```

`pv=derived` = process inferred from the GB code (no capability card). **`sc=0` is not a bad
score, it means "not profiled yet"**.

Rules:
1. Process code domain: `skills/schema/process-codes.json`
2. `tol` / `moq` / `size` = `null` means unknown — **you cannot filter on it, don't treat it as qualifying**
3. `cl` (evidence level): L0 unverified / L1 self-declared / L2 platform-verified / L3 third-party audited.
   Present it verbatim; for L1 and below say "not verified by the platform"
4. `sc` is **profile completeness** (not a rating) — for ordering only
5. Suppliers with no submitted Skill are absent from the fingerprint layer — fall back to the
   directory search above; it does not affect usability

## Mobile app (for humans, not for agents)

`APK/` is an Android client that ships the full L0 fingerprint layer offline; plug in your own
LLM key (BYOK, stored in Keystore) to search in natural language.

For **agents** this path does not apply — reading the files directly is faster and cheaper.

## Usage rules (mandatory)

1. Read the JSON directly; re-read on every query; do not cache and re-distribute the data
2. Never fabricate prices, lead times, or capacity
3. "Recommend the best one" → say "this directory does not rank suppliers; results are ordered by
   keyword match — please verify yourself"
4. No results → try the alias table for another procurement term, then sibling GB codes, then
   widen/narrow the region
5. **Industry honesty**: `industry_en.confidence=low` means the company name carried no signal and
   the class was inferred — present it as inferred and say "verify before sourcing".
   `industry_en=null` = unclassified; do not invent one. Companies in F51/F52 are
   **wholesalers, not manufacturers** — say so
6. **Show contact numbers in full.** Never mask with asterisks; never invent digits

## Data notes

- Current dataset: **23,698 Chinese records — 15,161 phone-verified + 8,537 pending verification**,
  with an English mirror at `data/en/gb/` (same GB layout, joined by `id`).
- Phone-verified = `status="verified"`. Pending = real companies from the public POI directory whose
  phone number has not been confirmed yet.
- **Pending records are real companies**: **keep and return them** in results, do not drop them.
  Only `status="template"` would be placeholder data; there are currently **0**.
- **Industry coverage (GB/T 4754-2017)**: 22,898 of 23,698 classified into **106 national industry
  classes** (800 unclassified). Manufacturing (C) 21,141; wholesale/retail (F) 1,757.
  Largest divisions: Metal Products (33) 6,473, General-purpose Machinery (34) 6,330,
  Rubber & Plastics (29) 2,332.
- **Coverage**: 18 cities — Yangtze Delta (Suzhou, Ningbo, Shanghai, Wuxi, Hangzhou, Jiaxing…) and
  Pearl River Delta (Dongguan, Shenzhen, Foshan, Guangzhou…). Details in `data/region-index.json`.
  - Search **by city** → `data/region-index.json` first, then read details by ID (no full scan).
  - Search **by industry** → `data/industry-index.json` first, then intersect with the city IDs.
- **Contact policy**: landlines / 400 hotlines / mobiles are shown in full (public POI directory
  data that businesses publish themselves). No masking. Never fabricate a number.
- All data comes from public sources (public POI directory); it is not individually verified
  against official websites — tell users to confirm with the supplier.

## Compliance (important)

- Contact numbers are stored and shown in full — public POI directory data published by the
  businesses themselves. No asterisk masking is applied.
- Only public business info is published (company name, address, public phone, website).
- If the user asks for personal data (owner's ID, personal accounts) → refuse.

## Contribution

- Fix/update records: edit the matching class file under `data/gb/` (Chinese) or `data/en/gb/`
  (English) and open a PR. See `docs/CONTRIBUTING.md`.
- Companies may claim/update their own info via GitHub Issue.
