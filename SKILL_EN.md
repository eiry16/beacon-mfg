---
name: beacon-mfg-en
description: Agent-facing search over a structured directory of Chinese manufacturing suppliers. Use when the user needs to find upstream manufacturers (CNC machining, sheet metal, injection molding, die casting, electronic components, etc.) or filter suppliers by product keyword / region. Data comes from public sources; only public contact info is provided; no transactions.
---

# BeaconMFG · Supplier Search Skill (English Dataset)

## Capability

Search a structured directory of China manufacturing suppliers by product keywords,
region, and category. Returns **structured, traceable** supplier records with public contact info.

**Scope:** information only — no quoting, ordering, or transactions; no ratings or recommendations.

## Data location

This skill consumes the **English mirror** dataset at `data/en/`:

| File | Category |
|---|---|
| `data/en/precision-machining.json` | Precision Machining (CNC) |
| `data/en/sheet-metal.json` | Sheet Metal & Stamping |
| `data/en/injection-molding.json` | Injection Molding |
| `data/en/die-casting.json` | Die Casting |
| `data/en/electronic-components.json` | Electronic Components |
| `data/en/surface-treatment.json` | Surface Treatment |
| `data/en/standard-parts.json` | Standard Parts |
| `data/en/raw-materials.json` | Raw Materials |

Each record contains: `id`, `company_en`, `category_en`, `keywords_en`, `region`
(English), `address_en`, `contact_phone`, `source`, `verified_at`, `note_en`.

**Region index** (`data/region-index.json`) provides fast city-level lookups:
`index["province-city"] → list of CN-MFG IDs` — use this to avoid scanning entire category files.

The data is plain JSON — **no scripts, no network, no API key required**. The agent
just reads the files.

## Usage Flow

### Step 1: Map user need → keywords → category

Extract product terms from the request and match them to a category
(see `data/index.json` for the keyword dictionary). Examples:

| User says | Category | Keywords |
|---|---|---|
| "small-batch aluminum CNC parts" | Precision Machining | CNC Machining, small batch, aluminum |
| "sheet metal enclosure" | Sheet Metal & Stamping | sheet metal, enclosure |
| "PCB prototyping" | Electronic Components | PCB, PCBA |

If nothing matches, ask for a more specific product description — do not guess.

### Step 2: Region index fast lookup (if region is specified)

**Prefer the region index** over scanning full category files:

1. Read `data/region-index.json`
2. Search for the target city key in `index` (e.g. `"Zhejiang-Jiaxing"` or key containing "Jiaxing")
3. Get `ids` list → group by category → read from `data/en/{category}.json` by `id`

```python
import json
idx = json.load(open("data/region-index.json", encoding="utf-8"))
# find key containing "Jiaxing"
city_key = next((k for k in idx["index"] if "Jiaxing" in k), None)
target_ids = idx["index"][city_key]["ids"]

# read full records from category file
recs = json.load(open("data/en/precision-machining.json", encoding="utf-8"))
hits = {r["id"]: r for r in recs if r["id"] in target_ids}
```

> **Fallback**: if target city is not in the 18-city index, fall back to category scan in Step 3.
> Current index covers: Shanghai (666), Shenzhen (713), Dongguan (857), Guangzhou (396), Foshan (545),
> Jiaxing (507), and 12 other cities.

### Step 3: Category file keyword + region filter

1. Read `data/index.json` → confirm keyword belongs to a category
2. Read `data/en/{category}.json`
3. Filter: `region.city == "TargetCity"` and `keywords_en` contains target keyword

Field notes:
- `company_en`: company name (English or transliterated)
- `keywords_en`: product keyword array (substring match)
- `region`: `{ "province": "...", "city": "..." }` (English, e.g. `"Zhejiang"`, `"Jiaxing"`)
- `contact_phone`: landlines / 400 hotlines / mobiles shown **in full**; `"Pending verification"` when missing
- `certifications`: certification tags
- `source` / `verified_at`: provenance and verification date

If the agent environment can run code, a one-liner filter works:
```python
import json
recs = json.load(open("data/en/precision-machining.json", encoding="utf-8"))
hits = [r for r in recs
        if r["region"]["city"] == "Shenzhen"
        and any("CNC" in k for k in r["keywords_en"])]
# Note: is_template=true records are real business POIs too; only their phone is pending verification — keep them in results.
```

### Step 4: Present results (2-3 best matches)

```
Company: Jiaxing Precision Technology Co., Ltd. (Zhejiang · Jiaxing)
Products: CNC Machining / Precision Components / Small Batch Custom
Phone: 0573-XXXXXXXX     ← full landline, call directly
Source: public directory · verified 2026-08-13
```

Rules:
- Contact numbers (landlines, 400 hotlines, mobiles) are shown in full — present as-is.
  If a number is missing (pending verification), tell the user to verify via the company
  website or other public channels; never fabricate digits.
- Never invent prices, lead times, or capacity.
- Always state "data from public sources, verified on YYYY-MM-DD".

## Data notes

- Current dataset: **4,792 verified suppliers + 3,170 pending records (7,962 total Chinese records)**, plus a **7,915-record English mirror** (this skill's `data/en/`).
- Verified records (`is_template=false`) = 4,792 — phone confirmed. Pending records (`is_template=true`) = 3,170 — phone sourced from map POI, pending manual confirmation.
- **`is_template=true` are still real business POIs**: these are genuine companies from the public POI directory; only their phone number is not yet confirmed. The agent should **keep and return them** in search results, not drop them.
- All data comes from public sources (public POI directory); not individually verified against official websites — contact the supplier to confirm.

## Compliance (important)

- Contact numbers are stored and shown in full — public POI directory data published
  by the businesses themselves. No asterisk masking is applied.
- Only public business info is published (company name, address, public phone, website).
- If the user asks for personal data (owner's ID, personal accounts) → refuse.

## Contribution

- Fix/update records: edit `data/en/*.json` (or `data/suppliers/*.json`) and open a PR.
  See `docs/CONTRIBUTING.md`.
- Companies may claim/update their own info via GitHub Issue.
