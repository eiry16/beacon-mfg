---
name: beacon-mfg
description: Agent-facing directory of Chinese enterprises and businesses, filed by GB/T 4754-2017 national industry codes across 7 gates — C Manufacturing, F Wholesale & Retail, H Accommodation & Food Service, I Information Technology Services, M Scientific & Technical Services, O Repair & Personal Services, R Culture, Sports & Recreation. Use it to find suppliers, factories, OEM/ODM partners (CNC machining, sheet metal, injection molding, die casting, molds, fasteners, electronic components), wholesalers, distributors and traders; restaurants, cafes, hotels and guesthouses; local services (hair & beauty, laundry, auto repair, appliance repair, fitness, KTV, internet cafes, cinemas, amusement parks, pet services); and technical service providers (software development, systems integration, cybersecurity, big data, third-party testing, calibration, certification, industrial design, environmental monitoring) — or to filter by GB industry code, product keyword, or city. **The retrieval entry point is the MCP service (beacon-mfg-mcp).** Public POI directory data and contact details only; we take no part in transactions.
---

# BeaconMFG · Supplier Search Skill

> ## ⚠️ This repository carries only the MCP service and read-only data
>
> Data collection, translation, derived rebuilding and publishing pipelines are **not** in this
> repository, so this document does not describe a "check out the whole repo and run local
> scripts" workflow.

---

## 1. Retrieval entry point: MCP (recommended — hosted, zero maintenance, no key)

```bash
npx -y beacon-mfg-mcp          # or wire it into any MCP client as shown in mcp/README.md
```

No API key, no need to clone this repository. **8 read-only tools** are provided:

| Tool | Purpose | Key parameters |
|---|---|---|
| `search_vendors` | Directory search (keyword / city / GB code) | `query` (**the user's own wording works**), `city` (accepts prefecture-level and county-level cities), `gb` (GB code), `limit` (1–200), `offset` |
| `get_vendor` | Full Chinese record by id | `id`, `gb` (optional) |
| `get_capability_card` | Capability card by id (process stations / equipment / capacity / certifications / MOQ) | `id`; returns `has_card: false` plus a reason when absent |
| `suggest_filters` | User wording → candidate GB class codes, so you can switch to `gb=` | `query`, `city`, `limit` |
| `list_industries` | GB class shelf (only classes that hold data, with counts) | `keyword`, `parent`, `limit`, `offset` |
| `start_sourcing` | **Auto-triggered on sourcing/RFQ intent**: category detection → broad recall → requirement normalisation → 1–2 clarification rounds | `demand_text`, `audience_id` (`domestic_downstream` / `intl_buyer`) |
| `answer_sourcing` | Continue a clarification round, or return a shortlist scored against the requirement | `session_id`, `answers` |
| `refine_sourcing` | Recommendation rounds: `details` / `more` / `best` | `session_id`, `action`, `value` |

**For multi-turn sourcing conversations, start with `start_sourcing`** — it returns a
`session_id` that `answer_sourcing` / `refine_sourcing` carry forward. It is not a thin wrapper
around keyword matching.

---

## 2. Two kinds of `SKILL.md` — don't confuse them

| | Name prefix | Location | Nature |
|---|---|---|---|
| **Search entry skill** (this doc) | `beacon-mfg` / `beacon-mfg-en` | repo root | **Instructions** — how to search |
| **Vendor data card** | `beacon-mfg-vendor-*` | served from the cloud, not from this repo | **Data** — one company's profile, no instructions |

Those vendor files are **data records indexed by vendor id**, not installable sub-skills.
If one shows up in a skill list, it is a capability card (fetched on demand) —
**do not** load it as a standalone skill.

---

## 3. Coverage: 7 national industry gates

| Gate | Name | Typical needs |
|---|---|---|
| C | Manufacturing | CNC machining, sheet metal, injection molding, die casting, molds, fasteners, electronic components |
| F | Wholesale & Retail | Wholesalers, distributors, traders, hardware & building materials, convenience stores, pharmacies |
| H | Accommodation & Food Service | Restaurants, hot pot, fast food, cafes, milk tea, bakeries, bars, hotels, guesthouses |
| R | Culture, Sports & Recreation | Fitness, KTV, internet cafes, cinemas, amusement parks, sports halls |
| O | Repair & Personal Services | Hair & beauty, laundry, auto repair, appliance repair, pet services |
| I | Information Technology Services | Software development, systems integration, cybersecurity, big data, operations |
| M | Scientific & Technical Services | Third-party testing, calibration, certification, industrial design, environmental monitoring |

> **Per-gate counts change daily — always take them from `data/DATA_STATS.md`**, never from
> numbers quoted in prose.
>
> **If a gate returns nothing**: check the class breakdown in `data/DATA_STATS.md` to confirm the
> entry count, then tell the user the current scale honestly. **Do not** paper over it by pulling
> near-miss results from another gate. A small number of records have `industry` set to null
> (industry not yet determined) — **do not force a label on them**; they remain keyword-searchable.

---

## 4. `search_vendors` return fields

| Field | Meaning |
|---|---|
| `id` · `company` · `city` · `district` | Identity and name, city / district |
| `gb` | GB class code |
| `badge` | Certification badge (`L0` = not yet certified) |
| `score` | **Capability profile score** (non-zero only when a published capability card exists) — **not relevance** |
| `has_phone` | Whether a dialable number is available |
| `process` · `material` · `cert` | Process / material / certifications |
| `via` | Which recall source matched (`text` / `cap_text` / `alias` / `cap` / `gb`) |

A few easy traps:

- **District is not always present**: county-level cities are filed under their prefecture city
  (Kunshan → Suzhou).
- `industry` null means "not yet determined" — **do not force a label**; those records stay in the
  directory and remain keyword-searchable.
- Do not treat `score` as a relevance ranking — the returned `order_note` states the ordering rule.

---

## 5. Boundaries (mandatory)

- **Public contact details and basic information only** — no quoting, ordering or transactions.
- **No ranking or recommendation.** If the user asks for "the best one", say results are ordered by
  fit and must be verified independently.
- **Never invent** price, lead time or capacity — if the data does not have it, say so.
- Weak evidence must not override strong evidence: process / material come from the company name and
  the capability card; search keywords only fill gaps.
- "Not filled in ≠ not offered": keep records with missing fields at reduced weight; do not drop them.
- Fix mislabels: a labelling problem must never make a real company unsearchable.
- Prefer `search_vendors` / `start_sourcing` — **do not guess GB codes yourself**; search first.

---

## 6. Data provenance & disputes

- Data comes from **public POI directories** (maps / public business listings) and is traceable.
- **Code** is MIT; **data** is CC BY 4.0 (see `DATA_LICENSE.md`).
- Companies may dispute, correct or request removal of their entry via a GitHub Issue.

---

*This repository carries only the public artefacts that let a customer's agent find manufacturing suppliers.*
