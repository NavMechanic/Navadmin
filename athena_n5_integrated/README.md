# ATHENA N5 — Procurement, Supplier Intelligence & BOM Optimization Engine (integrated with N0–N4)

N5 takes the engineering BOM that N3 approved and released (exact MPNs, quantities, design revision)
and does five things:

1. **BOM intake.** It locks the BOM as exact-MPN sourcing lines with Q_required = quantity per unit ×
   build quantity.
2. **Supplier qualification.** It qualifies manufacturers, distributors and EMS / ODM partners:
   business identity, Open Supply Hub sites, authorized-channel proof and a reviewer's attestation.
3. **Quote validation.** It validates manual, signed EMS and on-demand distributor quotations
   against a deterministic hard-rejection matrix.
4. **Costing.** It ranks only the *eligible* offers, by extended material cost calculated exactly in
   `Decimal`.
5. **Handoff.** After a separate procurement approver signs off the exact report hash and quote
   hashes, it emits N0-checked **draft** handoffs to **N6 (Commercial)** and **N7 (Finance)**.

> This repository was **rebuilt from the specification**. No `athena_n5_integrated` starter existed in
> this workspace, its git history, other branches or Drive. It contains:
>
> - **N0**: byte-identical copies from `athena_n0_starter`. That repo gained the
>   `SourcingHandoffPayload.v1` contract (rules N0-PRC-001..011, which recompute every quantity and
>   cost), source-rights listeners, a release-time rights re-check and X12 / EDIFACT upload
>   recognition.
> - **N1**: byte-identical copies from `athena_n1_integrated`.
> - **N2 / N3**: byte-identical copies from `athena_n3_integrated`.
> - **N4 / N8**: byte-identical copies from `athena_n4_integrated`.
> - **N5**: built here.
> - **N6 / N7**: **minimal receivers** (`app/n6`, `app/n7`). They are the slice N5 hands off to: they
>   ingest only RELEASED, hash-verified, live handoffs and record draft bid inputs and draft cost
>   records.
>
> The **169 baseline tests** are re-created:
>
> | Files | Tests |
> |---|---|
> | the six N0–N3 baseline files | 84 |
> | `tests/test_n4_metrology.py` and `tests/test_n4_verification.py` | 44 |
> | `tests/test_n5_sourcing.py` (20) and `tests/test_n5_quotes.py` (21) | 41 |
>
> They pass with the integration suites listed under [Tests](#tests).

> **Indicative material quote.** N5 totals are quoted component material only: order quantity ×
> applicable price break + quoted line charges (handling, reeling, cut tape, packaging, minimum-order
> surcharge). They **exclude** the following, which belong to N7 Finance:
>
> - GST and other taxes
> - tooling / NRE
> - financing
> - complete freight and duties
> - production labour
> - warranty
>
> Every report, handoff payload and N6 / N7 record carries this label. N0 BLOCKs a handoff without it
> (`INDICATIVE_SCOPE_MISSING`). Quoted NRE, freight or GST lines are passed to N7 as
> `excluded_items_reported`, never added to the total.

```
app/main.py                  combined entry point: N0 + N1 + N2 + N3 + N4 + N8 + N5 + N6 + N7 (one DB / store / audit chain)
app/gateway.py, app/*.py     N0, byte-identical to athena_n0_starter (tests/test_n0_sync.py)
app/n1 … app/n4, app/n8      vendored, byte-identical to their source repositories
app/n5/models.py             contracts → schemas/n5_*.schema.json (19 families + 5 adapter / record schemas)
app/n5/engine.py             hard rejection matrix, Decimal costing, ranking, decision (pure, no I/O)
app/n5/quotes.py             quote intake: manual athena-quote/1, signed EMS drop zone, on-demand lookups, vault re-hash
app/n5/service.py            BOM intake, suppliers, FX, report, review, handoff, revision + rights cascades, ECO
app/n5/connectors/           DigiKey (OAuth2), Mouser (API key), Nexar (GraphQL), Open Supply Hub (REST/CSV), EMS drop zone
app/n5/downstream.py         shared N6 / N7 intake checks (released, hash-verified, live)
app/n5/db.py                 10 append-only N5 tables
app/n5/router.py             /v1/n5/* API (21 paths)
app/n6/, app/n7/             minimal receivers: /v1/n6/intake + bid-inputs, /v1/n7/intake + cost-records
examples/run_n5_demo.py      synthetic 10-product demo: ₹142 + ₹102 = ₹244 indicative material quote
migrations/versions/0005     N5 / N6 / N7 tables and their append-only triggers
docs/                        architecture, data-quality rulebook, vendor integration plan, AI strategy, backlog
```

## Invariants and where they are enforced

| Invariant | Enforcement |
|---|---|
| **1. No price override of hard controls** | `engine.assess` runs the hard rejection matrix on every offer first. Any BLOCK or REWORK code makes the offer ineligible (`eligible: false`, `rank: null`). Only eligible offers are costed into the total and ranked. Offer-level findings never change the decision; line and report findings decide (BLOCK > REWORK > PASS). A BLOCK report cannot be approved (`BLOCK_NOT_APPROVABLE`), nor can a REWORK one (`REWORK_OPEN`). N0 re-checks the selected lines at release. |
| **2. Exact MPN and revision lock** | MPNs compare after whitespace removal and upper-casing only, so packaging and grade suffixes stay significant (`app.rules.normalize_mpn`). A different MPN gives `MPN_MISMATCH` (BLOCK) plus `ECO_CANDIDATE`. `POST /v1/n5/eco-proposals` records a `PENDING_N3_ECO` proposal with equivalence evidence and a parametric comparison. Nothing is substituted: the alternate becomes eligible only when N3 releases a BOM revision containing it. A quote for another BOM / design revision gives `DESIGN_REVISION_OBSOLETE`. N0 compares every handoff line with the pinned BOM_RELEASE (`MPN_NOT_EXACT`, `LINE_UNSOURCED`, `BOM_REVISION_UNLOCKED`). |
| **3. Licensing and ToS** | `N5_ENABLE_LIVE_APIS=false` by default. Lookups are on demand, one MPN per call, only for lines of an active BOM intake of the caller's tenant (`LOOKUP_OUT_OF_SCOPE`), within a per-line budget. Responses are cached in memory only (TTL ≤ the connector's limit) and never persisted: only decision fields and the response SHA-256 are kept, flagged `training_allowed: false` and `redistribution: INTERNAL_PROJECT_ONLY`. Rate limits are a token bucket plus a daily quota, and they refuse rather than queue. Secrets are redacted from every error and log. DigiKey Marketplace (third-party) variations are dropped. |
| **4. Indicative material scope** | `INDICATIVE_MATERIAL_LABEL` and `EXCLUDED_COST_CATEGORIES` (`app/models.py`) are on every report and handoff, and N0 BLOCKs their absence. |
| **5. Dynamic rights revocation** | N0 calls `source_rights_listeners` after a committed rights decision. N5 then marks the quotes RIGHTS_REVOKED and the reports STALE, and the handoffs BLOCKED (`SOURCE_RIGHTS_REVOKED`). It notifies N6 / N7, which mark their drafts BLOCKED. N0 also re-checks the artifact's own source and every `quote_source_ids` entry before any release (`SOURCE_RIGHTS_REVOKED`, 409). N6 / N7 intake re-checks live. |

## Pipeline

| Stage | What it does | API |
|---|---|---|
| **N5.1 BOM ingestion** | Locks `BOM-{prd}-schematic_pcb` for the newest PRD revision. Checks: N0 PASS; RELEASED to N5; stored payload re-hashed; approval chain verified; SOURCING rights; N3 release not invalidated; PRD pinned. Then normalises lines: exact MPN, UOM `EA`, quantity per unit, Q_required, critical flag; DNP lines are excluded. A PRODUCTION build needs an active N4 VERIFIED release of the same design revision (`DESIGN_NOT_VERIFIED`). | `POST /v1/n5/bom-intakes` |
| **N5.2 Supplier intelligence** | Records hold kind, registration ids, Open Supply Hub sites, authorized-channel proofs and signing keys. Sites are verified (live API or OSH CSV). A `supplier_quality` reviewer attests and approves (never their own submission). Requirements: a distributor needs verified, current channel proofs; an EMS / ODM needs an OSH-verified site; a manufacturer must name its brands. | `POST /suppliers`, `/revisions`, `/review`, `/sites/{id}/verify` |
| **N5.3 Quotations** | Three channels: manual `athena-quote/1`; the signed EMS drop zone `athena-ems-quote/1` (signature over the PDF / CSV / X12 843, capacity allocation, NRE, service terms); on-demand DigiKey / Mouser / Nexar lookups. Structural problems reject at intake (422): price breaks, UOM, validity, stock, signature, file hashes, malware. Files are stored write-once by SHA-256. | `POST /quotes`, `/quotes/ems`, `/bom-intakes/{id}/lookups` |
| **N5.4 Guardrails** | Offer-level BLOCK: `MPN_MISMATCH`, `DESIGN_REVISION_OBSOLETE`, `SUPPLIER_NOT_APPROVED`, `UNAUTHORIZED_CHANNEL`, `QUOTE_EXPIRED`, `SOURCE_RIGHTS_REVOKED`, `QUOTE_HASH_MISMATCH`. Offer-level REWORK: `STOCK_INSUFFICIENT`, `LEAD_TIME_EXCEEDS_TARGET`, `CAPACITY_EVIDENCE_MISSING`, `FX_ASSUMPTION_MISSING`, `PRICE_BREAK_MISSING`. Line-level REWORK: `CRITICAL_LINE_UNSOURCED`, `LINE_UNSOURCED`. Report-level BLOCK: `BOM_INTAKE_STALE`, `DESIGN_NOT_VERIFIED`. | `POST /v1/n5/reports` |
| **N5.5 Cost optimisation** | Q_order = ⌈max(Q_required, MOQ) / pack⌉ × pack. Unit price = the largest price break ≤ Q_order. Extended cost = Q_order × price + line charges, converted with a recorded FX assumption. Eligible offers are ranked by base-currency extended cost, then lead time, then supplier id. A cheaper next break is reported as advice (`STEP_UP_SAVES`), never applied. | (in the report) |
| **N5.6 Review and digital thread** | A `procurement_approver` quotes `report_sha256` and the selected `quote_hashes`. They must not be the analyst, the intake author, a quote submitter, or the submitter / qualifier of a selected supplier. Approval re-hashes the vault files and records an N0 approval event. Release re-checks expiry, rights, supplier status and the vault. N0 then checks `PRC-{prd}-N6` (COMMERCIAL: bidding inputs, supplier risk flags, lead-time constraints) and `PRC-{prd}-N7` (FINANCE: cost lines, FX assumptions, quote validity). | `POST /reviews`, `/reports/{id}/handoffs` |

**Revision cascade.** A new N3 BOM revision triggers the N3 release listener; a new N2 PRD revision
triggers the N2 listener; there is also a lazy check against N0's latest BOM revision and N3
invalidation events. Any of these marks the intake SUPERSEDED, its quotes and reports STALE and its
handoffs BLOCKED (`DESIGN_REVISION_CHANGED`). N6 / N7 drafts follow.

## Roles (JWT only; the demo keys `N0_DEMO_API_KEY` / `N0_APPROVER_KEY` do not exist)

| Role | May | Implied N0 roles |
|---|---|---|
| `procurement_analyst` | BOM intake, supplier proposals, FX assumptions, quotes, lookups, reports, ECO proposals | producer |
| `supplier_quality` | verify sites, qualify / suspend suppliers (never their own submission) | — |
| `procurement_approver` | approve report + quote hashes, release N6 / N7 handoffs | gateway_reviewer, release_manager |
| `commercial_operator` | N6 intake and views | — |
| `finance_operator` | N7 intake and views | — |

## Quick start

```bash
pip install -r requirements.txt
python -m pytest -q                        # full N0–N5 suite (+ N6 / N7 / N8 receivers)
python examples/run_n5_demo.py             # synthetic 10-product demo, ₹244 indicative material quote
python -m app.schema_export                # regenerate schemas/ (tests fail on drift)
alembic upgrade head                       # N0_DATABASE_URL=postgresql+psycopg://… for PostgreSQL
```

N5 settings come from `N5_*` environment variables (`app/n5/config.py`):

| Variable | Default / meaning |
|---|---|
| `N5_ENABLE_LIVE_APIS` | `false` |
| `N5_BASE_CURRENCY` | `INR` |
| `N5_CACHE_TTL_S` | `900`, capped per connector |
| `N5_DIGIKEY_CLIENT_ID` / `_SECRET`, `N5_DIGIKEY_LOCALE_SITE` / `_CURRENCY` | DigiKey credentials and locale |
| `N5_MOUSER_API_KEY` | Mouser API key |
| `N5_NEXAR_CLIENT_ID` / `_SECRET` | Nexar credentials |
| `N5_OSH_API_TOKEN` | Open Supply Hub token |

Credentials are read from the environment (or a platform secret store) and never stored or logged.

## Tests

258 tests in total.

| File | What it covers |
|---|---|
| 8 baseline N0–N4 files (128) | unchanged gateway, LLM boundary, N1, N2, N3 and N4 behaviour |
| `test_n5_sourcing.py` (20) | BOM intake and normalisation, supplier qualification, the ₹244 report, ranking, review, N6 / N7 handoff |
| `test_n5_quotes.py` (21) | manual quote structure checks, signed EMS drop zone (PDF / CSV / X12 843), FX, ECO proposals, live APIs off |
| `test_n5_order_math.py` (16) | Q_order ceiling / MOQ / pack cases, price-break boundaries, step-up advice, exact Decimal, tie-breaks, FX |
| `test_n5_guardrails.py` (16) | all nine BLOCK / REWORK conditions, the cheaper unauthorized alternate, rights revoked before the report / after approval / after the handoff, PRODUCTION without N4 |
| `test_n5_connectors.py` (14) | DigiKey OAuth2, Mouser, Nexar GraphQL, OSH live verification; cache, scope, budget, rate limits, up-front validation, no raw payload or secret persisted |
| `test_n5_revision_cascade.py` (6) | new BOM / PRD revision → SUPERSEDED / STALE / BLOCKED downstream; obsolete-revision quotes; the lazy check |
| `test_n5_rbac.py` (20) | role matrix, no demo keys, separation of duties, tenant isolation |
| `test_n5_persistence.py` (5) | append-only triggers on all N5 / N6 / N7 tables, restart, vault tampering after approval, Alembic, PostgreSQL DDL |
| `test_n5_schemas.py` (5) | 19 schema families, no float money, drift, OpenAPI coverage, live records validate |
| `test_n5_e2e.py` (3) | **N0 → N1 → N2 → N3 → N4 → N5 → N6 / N7** from TED market evidence to the N6 / N7 drafts; examples; the demo |
| `test_n0_sync.py` (4) | vendored N0 / N1 / N2 / N3 / N4 / N8 copies are byte-identical to their sources |

## What was and was not exercised

- **Distributor APIs**: DigiKey, Mouser, Nexar and Open Supply Hub were exercised only against mocked
  HTTP endpoints shaped like their published APIs. No live call was made and no credentials were
  used. The response parsers will need checking against real payloads.
- **Licence terms**: the ToS posture follows the constraints in the brief. Confirm the exact retention
  allowed for decision records with each provider's agreement before production.
- **EMS signatures**: detached Ed25519 / RSA signatures over the quotation bytes are verified. PAdES
  signatures embedded in a PDF are not parsed.
- **X12 843**: a documented subset profile only, not a full EDI translator.
- **Infrastructure**: PostgreSQL was checked through DDL rendering and Alembic on SQLite. S3 and
  ClamAV come from N0 and were not exercised.
- **N6 / N7**: minimal receivers only.
- **Upstream tools**: KiCad 8+ is not installed, so the N3 stand-ins were used; ngspice runs for real.
