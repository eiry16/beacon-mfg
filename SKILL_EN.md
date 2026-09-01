---
name: beacon-mfg-en
description: Agent-facing search over a structured directory of Chinese manufacturing suppliers. Use when the user needs to find upstream manufacturers (CNC machining, sheet metal, injection molding, die casting, electronic components, etc.) or filter suppliers by product keyword / region. Data comes from public sources; only public contact info is provided; no transactions.
---

# BeaconMFG · Supplier Search Skill (English Dataset)

## Capability

Search a structured directory of China manufacturing suppliers by product keywords,
region, and category. Returns **structured, traceable** supplier records with public contact info.

**Scope:** information only — no quoting, ordering, or transactions; no ratings or recommendations.

## Data

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

### Step 2: Search

Use `scripts/query.py` with `--en` mode (reads English dataset):

```bash
python scripts/query.py --keyword "CNC Machining" --city Shenzhen --en --limit 5
python scripts/query.py --category-en "Die Casting" --en --limit 10
```

Filters: `--keyword` (space-separated AND), `--city` / `--province` (English or
Chinese region names), `--cert` (certification), `--category-en` (English category).
Records with `is_template: true` are excluded by default.

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
- No fabrication of prices, lead times, or capacity.

## Compliance (important)

- Contact numbers are stored and shown in full —  public POI directory data published
  by the businesses themselves. No asterisk masking is applied.
- Only public business info is published (company name, address, public phone, website).
- If the user asks for personal data (owner's ID, personal accounts) → refuse.

## Contribution

- Fix/update records: edit `data/en/*.json` and open a PR (validate with
  `python scripts/validate.py`).
- Companies may claim/update their own info via GitHub Issue.

## Project

Repo: https://github.com/eiry16/beacon-mfg (English docs / data mirror maintained via
GLM-4-Flash translation; regenerate with `python scripts/translate_en.py` after data updates).
