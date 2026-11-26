# ATHENA N5 — Technical Architecture

N5 is a FastAPI router (`/v1/n5`) mounted with N0–N4, N8 and the minimal N6 / N7 receivers in one
process (`app/main.py`). They share:

- one SQLAlchemy metadata / database
- one write-once object store (the quote vault)
- one malware scanner
- one JWT verifier
- the hash-chained audit and approval logs

N5 never writes N0 tables. It submits handoffs through the N0 pipeline as `svc-n5`, like every other
node.

```
N2 PRD release ─────listener─┐       N0 source-rights decision ─listener─┐
N3 BOM_RELEASE → N5 ─listener┤                                           │
                             ▼                                           ▼
           N5.1 BOM intake (exact-MPN lines, Q_required)       rights cascade: quotes RIGHTS_REVOKED,
                             │                                 reports STALE, handoffs BLOCKED → N6/N7
   N5.2 suppliers ──┐        │
   (OSH sites, channel proofs, attestation)
                    ▼        ▼
   N5.3 quotes: manual · signed EMS drop zone · DigiKey / Mouser / Nexar (on demand, TTL, in memory)
                             │            (files → write-once vault, SHA-256)
                             ▼
   N5.4 hard rejection matrix  ──►  N5.5 Decimal costing of ELIGIBLE offers only  ──►  report_sha256
                             ▼
   N5.6 procurement_approver on report_sha256 + quote_hashes  ──►  N0 SOURCING_HANDOFF (N0-PRC-*)
                             ▼
                N6 draft bid input        N7 draft cost record      (live-checked on every read)
```

## 1. BOM intake (N5.1)

`N5Service.lock_intake` performs these checks in order:

1. Resolve the newest N2 PRD release and the latest `BOM-{prd}-schematic_pcb` revision.
2. The BOM must be N0 PASS, with an N0 manifest RELEASED to N5.
3. Re-hash the stored payload bytes, and recompute the approval event against the approval chain
   (`_verify_manifest`, identical to N4's).
4. The PRD must be in `source_artifact_ids`.
5. The source's rights must permit `SOURCING`, and the N3 release must not be INVALIDATED.
6. The release source must permit `COMMERCIAL` and `FINANCE`.

Lines are normalised as follows:

- `mpn` = `normalize_mpn` (whitespace removed, upper-cased), with `mpn_as_released` kept verbatim.
- UOM is `EA` (BOM quantities are counts).
- `q_required = qty × build_quantity`.
- Critical lines come from the request, with reasons; DNP lines are listed and never sourced.

`verification_status` is VERIFIED only if an active N4 `VERIFICATION_RELEASE` exists for the same
design revision. The intake is content-hashed (`intake_sha256`) and idempotent per BOM revision and
build parameters.

## 2. Suppliers (N5.2)

Supplier records are versioned, append-only rows (`n5_suppliers`, `seq`). A revision returns the
record to PENDING.

**Approval prerequisites** (`QUALIFICATION_INCOMPLETE` otherwise):

| Kind | Needs |
|---|---|
| any | registration ids, and a future `approved_until` |
| DISTRIBUTOR | the reviewer verified at least one current channel proof, and the verified manufacturers each have a proof on the record |
| EMS / ODM | at least one site whose OS ID resolved in Open Supply Hub with a matching country (live API or OSH CSV snapshot; the evidence and its hash are stored on the site) |
| MANUFACTURER | the brand names it makes |

**Eligibility at assessment** (`engine.supplier_problems`):

- The record must be APPROVED and not lapsed.
- A distributor must have a verified, current proof for the BOM line's manufacturer. Names are
  normalised: case, punctuation and suffixes such as Inc / Ltd / GmbH are ignored.
- A manufacturer must make the part.
- EMS / ODM turnkey offers are flagged (`EMS_TURNKEY_SOURCING`) and need capacity evidence.

## 3. Quotes and the vault (N5.3)

Every channel produces the same `N5Quote` with `N5Offer`s and a `quote_sha256`. The hash is over the
canonical record: offers, files, signature status, provenance and data-use flags.

| Channel | Specifics |
|---|---|
| `MANUAL` | `athena-quote/1` JSON; structural checks only |
| `EMS_DROPZONE` | `athena-ems-quote/1` manifest + files, each hash-listed. The detached signature over the quotation document is verified with a key on the supplier record. Offers come from the manifest, a CSV document or the X12 843 profile. The capacity allocation must reference an uploaded attachment's hash. NRE / service terms are recorded on the quote. |
| `DIGIKEY_API` / `MOUSER_API` / `NEXAR_API` | One quote per (connector, MPN, seller), valid for `api_offer_validity_h`. Lead time = `distributor_stock_lead_days`. Provenance is the call id, response SHA-256, cache hit and licence terms. |

Accepted files are stored write-once in N0's object store (`quarantine/{tenant}/{sha256}`, read-only)
and re-hashed:

- at every report (`QUOTE_HASH_MISMATCH` makes the offer ineligible);
- at review, and again at release (409).

## 4. Guardrails and costing (N5.4–N5.5)

`engine.assess(ctx, quotes, stale)` is pure. For each offer:

1. **Match the line.** Use the explicit `line`, else an exact-MPN match.
2. **Apply the rejection matrix.** Run the BLOCK checks, then REWORK, then INFO (see the rulebook).
3. **Cost it** (`cost_offer`):
   - `Q_order = order_quantity(Q_required, MOQ, pack)`
   - `applicable_break`
   - `extended_cost`
   - FX conversion with the latest recorded assumption valid today
   - step-up advice

The costing uses `app.rules`, which N0 shares.

For each line:

1. Sort the eligible offers by `(extended_cost_base, lead_time_days, supplier_id, offer_id)` and give
   them ranks 1..n.
2. Append the ineligible offers unranked.
3. The selected offer is rank 1. A line with no eligible offer gives `CRITICAL_LINE_UNSOURCED` or
   `LINE_UNSOURCED`.

**Totals.**

- `indicative_total` = Σ base cost of the selected offers. It is null while any line is unsourced.
- `indicative_unit_cost` = total / build quantity, rounded HALF_UP to 4 decimals.
- Excluded items (offer `excluded_charges`, quote NRE) are listed, never summed.

**Decision.** BLOCK > REWORK > PASS, over LINE and REPORT findings only.

`report_sha256` binds the intake hash, the lines with all offers and codes, every quote hash, the FX
assumptions used and the totals.

## 5. Review and handoff (N5.6)

**Review.** Checks, with the refusal each gives:

- the report must be the latest for its intake (`STALE_REPORT`) and not STALE or superseded
  (`REPORT_STALE`);
- it must still match its stored hash;
- `report_sha256` and `quote_hashes` must be quoted exactly;
- the decision must be PASS;
- the approver must not be a contributor (`SEPARATION_OF_DUTIES`). Contributors are the report
  author, the intake author, the submitters of the selected quotes, and the submitters and
  supplier-quality reviewers of the selected suppliers;
- the vault files must still re-hash.

The approval is recorded as an N0 `EVIDENCE_ATTESTATION` approval event on `n5-report:{id}` @
`report_sha256[:32]`.

**Release.** The latest review must be APPROVED and match the current hashes. Release then re-checks:

- the quotes have not expired since approval;
- every quote source is still APPROVED;
- the selected suppliers are still APPROVED;
- the vault files still re-hash.

Then, per target, it builds a `SourcingHandoffPayload.v1`, evaluates it through N0 as `svc-n5`
(origin N5), records a RELEASE approval and creates the N0 handoff manifest.

| Target | Artifact | Processing purpose |
|---|---|---|
| N6 | `PRC-{prd}-N6` | COMMERCIAL |
| N7 | `PRC-{prd}-N7` | FINANCE |

The release stage is CONCEPT if any input is synthetic, else PROTOTYPE. The artifact revision
advances only when the payload changes.

**What N0 recomputes** (`app.rules.sourcing_checks`), with the code each failure gives:

| Check | Code |
|---|---|
| q_required = qty × build, and q_order | `ORDER_QUANTITY_MISMATCH` |
| unit price at the applicable break | `PRICE_BREAK_MISMATCH` |
| extended and base cost, total, unit cost | `EXTENDED_COST_MISMATCH` |
| quote expiry | `QUOTE_EXPIRED` |
| MPN and quantity vs the pinned BOM | `MPN_NOT_EXACT` |
| every BOM line sourced | `LINE_UNSOURCED` |
| BOM revision pinned | `BOM_REVISION_UNLOCKED` |
| the indicative label | `INDICATIVE_SCOPE_MISSING` |
| quote-source rights | `SOURCE_RIGHTS_REVOKED` |
| separation of duties | `SEPARATION_OF_DUTIES` |
| supplier qualification | `SUPPLIER_NOT_APPROVED` |
| FX assumption | `FX_ASSUMPTION_MISSING` |

## 6. Cascades

| Trigger | Mechanism | Effect |
|---|---|---|
| new N3 BOM revision released to N5 | `n3.release_listeners` → `on_n3_release`, plus a lazy `_stale_reason` check against N0's latest revision | intake SUPERSEDED, quotes STALE, reports STALE, handoffs BLOCKED (`DESIGN_REVISION_CHANGED`), N6 / N7 drafts BLOCKED |
| new N2 PRD revision | `n2.release_listeners` → `on_prd_release`, plus a lazy check | same |
| N3 release invalidated | lazy check of N3 events | same |
| N0 rights decision ≠ APPROVED | `app.state.source_rights_listeners` → `on_source_rights` | quotes RIGHTS_REVOKED, reports STALE, handoffs BLOCKED (`SOURCE_RIGHTS_REVOKED`), N6 / N7 drafts BLOCKED; N0 refuses releases that depend on the source |

N6 / N7 reads are live. `handoff_status` reports BLOCKED if a cascade blocked the handoff, if any
quote source is no longer APPROVED, or if the intake is superseded. This holds even if a listener
was not running.

## 7. Persistence

- **N5 tables (10):** intakes, suppliers, quotes, fx, reports, reviews, handoffs, eco proposals,
  connector calls, events.
- **N6 / N7 tables (2 each):** intakes and events.

All are append-only, enforced by SQLite / PostgreSQL triggers (`migrations/versions/0005`). State
changes are new rows. `n5_connector_calls` holds call metadata and the response hash only, never a
payload.
