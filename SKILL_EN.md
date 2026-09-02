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

### Step 2: Read the JSON directly

Open the matching category file and filter by fields. Example — "CNC machining in Shenzhen":

1. Read `data/index.json` → confirm "CNC Machining" belongs to `precision-machining`
2. Read `data/en/precision-machining.json`
3. Filter: `region.city == "Shenzhen"` and `keywords_en` contains "CNC"

Field notes:
- `company_en`: company name
- `keywords_en`: product keyword array (substring match)
- `region`: `{ "province": "...", "city": "..." }` (English)
- `contact_phone`: landlines / 400 hotlines / mobiles shown **in full**; `"待核实"` when missing
- `certifications`: certification tags
- `source` / `source_url` / `verified_at`: provenance and verification date

If the agent environment can run code, a one-liner filter works:
```python
import json
recs = json.load(open("data/en/precision-machining.json", encoding="utf-8"))
hits = [r for r in recs
        if r["region"]["city"] == "Shenzhen"
        and any("CNC" in k for k in r["keywords_en"])]
```

### Step 3: Present results (2-3 best matches)

```
Company: Dongguan Baijiang Precision Die Casting Mold Co., Ltd. (Guangdong · Dongguan)
Products: Die Casting / Die Casting Molds
Phone: 0769-88007830          ← full landline, can call directly
Source:  public directory · verified 2026-08-13
```

Rules:
- Contact numbers (landlines, 400 hotlines, mobiles) are shown in full — present as-is.
  If a number is missing (pending verification), tell the user to verify via the company
  website or other public channels; never fabricate digits.
- Never invent prices, lead times, or capacity.
- Always state "data from public sources, verified on YYYY-MM-DD".

## Compliance (important)

- Contact numbers are stored and shown in full —  public POI directory data published
  by the businesses themselves. No asterisk masking is applied.
- Only public business info is published (company name, address, public phone, website).
- If the user asks for personal data (owner's ID, personal accounts) → refuse.

## Contribution

- Fix/update records: edit `data/en/*.json` (or `data/suppliers/*.json`) and open a PR.
  See `docs/CONTRIBUTING.md`.
- Companies may claim/update their own info via GitHub Issue.
