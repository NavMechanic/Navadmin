# ATHENA N5 — Data-Quality Rulebook

Every rule is deterministic: no score, no model and no price can override it. Rules apply at four
levels:

- **Intake**: the request is refused (4xx).
- **Offer**: the offer is made ineligible.
- **Line / report**: the decision changes.
- **N0**: the release is blocked.

## 1. Intake refusals

| Code | When |
|---|---|
| `PRD_NOT_RELEASED` / `BOM_NOT_RELEASED_TO_N5` | no N2 PRD release, or no N0-PASS BOM_RELEASE released to N5 |
| `BOM_PRD_MISMATCH` | the BOM was not released against the current PRD revision |
| `BOM_SIGNATURE_INVALID` | stored payload bytes, the manifest hash or the approval chain do not verify |
| `SOURCE_RIGHTS_DENIED` / `RELEASE_SOURCE_NOT_APPROVED` | the BOM source does not permit SOURCING, or the release source does not permit COMMERCIAL + FINANCE |
| `BOM_INVALIDATED` | N3 invalidated the release behind the BOM |
| `UNKNOWN_LINE` | a critical line is not a sourcing line, or is DNP |
| `QUALIFICATION_INCOMPLETE` | supplier approval without its prerequisites (see the architecture document, §2) |
| `SEPARATION_OF_DUTIES` | a supplier submitter tries to qualify their own record, an approver is a contributor, or a contributor tries to release |
| `FACILITY_NOT_FOUND` / `FACILITY_MISMATCH` | the OS ID does not resolve, or its country differs from the site |
| `QUOTE_SOURCE_NOT_APPROVED` / `QUOTE_SOURCE_PURPOSE` | the quote's N0 source is not APPROVED, or does not permit SOURCING |
| `LIVE_APIS_DISABLED` / `CONNECTOR_NOT_CONFIGURED` / `LOOKUP_OUT_OF_SCOPE` | the feature flag is off, credentials are missing, or the line is not of the active BOM |
| `NOT_AN_ALTERNATE` / `OFFER_MISMATCH` | an ECO proposal names the approved MPN, or an offer that does not quote the proposed MPN |

## 2. Quote rejections at intake (422, recorded with hashes)

| Code | When |
|---|---|
| `PRICE_BREAKS_INVALID` | break quantities are not unique and ascending |
| `UOM_MISMATCH` | an offer is priced per anything other than `EA` (e.g. per reel); re-quote per EA |
| `VALIDITY_INVALID` | `valid_until` is not after `issued_at` |
| `STOCK_MISSING` | a STOCK offer without a stock quantity |
| `UPLOAD_REJECTED` / `MALWARE_DETECTED` | N0 upload screening (content vs extension, size) or the scanner failed |
| `SHA256_MISMATCH` / `FILE_MISSING` / `FILE_UNDECLARED` | files do not match the manifest |
| `SIGNATURE_INVALID` | the key is not registered for the supplier, the algorithm differs, or the signature does not verify |
| `EDI_INVALID` / `CSV_INVALID` / `OFFER_INVALID` / `NO_OFFERS` | the quotation document cannot be parsed into offers |
| `CAPACITY_EVIDENCE_UNVERIFIED` | the capacity allocation's `document_sha256` matches no uploaded attachment |

## 3. Hard rejection matrix (offer level)

### BLOCK: the offer is ineligible, whatever it costs

| Code | Condition |
|---|---|
| `MPN_MISMATCH` | the quoted MPN ≠ the approved BOM MPN (exact comparison; suffixes are significant). Also raises `ECO_CANDIDATE`. |
| `DESIGN_REVISION_OBSOLETE` | the quote is for another BOM artifact / revision, or names a different design revision |
| `SUPPLIER_NOT_APPROVED` | the supplier is unregistered, PENDING, REJECTED, SUSPENDED or lapsed |
| `UNAUTHORIZED_CHANNEL` | a distributor without a verified current proof for the manufacturer, or a "manufacturer" that does not make the part |
| `QUOTE_EXPIRED` | `valid_until` ≤ now |
| `SOURCE_RIGHTS_REVOKED` | the quote's N0 source is no longer APPROVED |
| `QUOTE_HASH_MISMATCH` | stored vault files no longer match their SHA-256 |

### REWORK: the offer is ineligible until the supplier or the plan changes

| Code | Condition |
|---|---|
| `STOCK_INSUFFICIENT` | STOCK availability and stock < Q_order |
| `LEAD_TIME_EXCEEDS_TARGET` | lead time > the intake's `target_lead_time_days` |
| `CAPACITY_EVIDENCE_MISSING` | an EMS / ODM offer, or a FACTORY_ORDER, without a hash-backed allocation. The allocation must be ≥ the build quantity (EMS) or ≥ Q_order (factory), and its period must cover today + lead time. |
| `FX_ASSUMPTION_MISSING` | the quote currency differs from the base and no valid assumption exists |
| `PRICE_BREAK_MISSING` | no break applies at Q_order |

### INFO: risk flags, never a change to eligibility

| Code | Condition |
|---|---|
| `LEAD_TIME_NEAR_TARGET` | lead time ≥ 80 % of the target |
| `QUALIFICATION_EXPIRING` | the supplier's qualification ends within 30 days |
| `STEP_UP_SAVES` | ordering up to the next break would cost less; advice only |
| `ECO_CANDIDATE` | an alternate MPN was quoted |
| `EMS_TURNKEY_SOURCING` | an EMS / ODM turnkey offer |

## 4. Line and report level (decide the report)

| Code | Outcome | Condition |
|---|---|---|
| `CRITICAL_LINE_UNSOURCED` | REWORK | a critical BOM line has no eligible quotation |
| `LINE_UNSOURCED` | REWORK | any other BOM line has no eligible quotation |
| `SINGLE_ELIGIBLE_SOURCE` | INFO | only one eligible offer (risk flag to N6) |
| `BOM_INTAKE_STALE` | BLOCK | the intake was superseded (new BOM / PRD revision, N3 invalidation) |
| `DESIGN_NOT_VERIFIED` | BLOCK | a PRODUCTION build without an active N4 VERIFIED release of the revision |
| `DESIGN_NOT_YET_VERIFIED` | INFO | a prototype / pilot build without one |

**Decision.** BLOCK > REWORK > PASS over line- and report-level findings. Only PASS can be approved.

## 5. N0 release rules for `SOURCING_HANDOFF` (all BLOCK)

| Rule | Code | Recomputes / checks |
|---|---|---|
| N0-PRC-001 | `ORDER_QUANTITY_MISMATCH` | q_required = qty × build; q_order = ⌈max(Q_req, MOQ) / pack⌉ × pack |
| N0-PRC-002 | `PRICE_BREAK_MISMATCH` | unit price = the applicable break at q_order |
| N0-PRC-003 | `EXTENDED_COST_MISMATCH` | extended cost, FX conversion, total, unit cost |
| N0-PRC-004 | `QUOTE_EXPIRED` | every selected quote is still valid at N0's clock |
| N0-PRC-005 | `MPN_NOT_EXACT` | quoted MPN = BOM MPN; line MPN and quantity = the pinned BOM item |
| N0-PRC-006 | `BOM_REVISION_UNLOCKED` | `bom_ref` is pinned, resolvable and of the stated design revision |
| N0-PRC-007 | `INDICATIVE_SCOPE_MISSING` | the label and all six excluded categories are present |
| N0-PRC-008 | `SOURCE_RIGHTS_REVOKED` | every `quote_source_ids` entry is APPROVED |
| N0-PRC-009 | `SUPPLIER_NOT_APPROVED` | every selected supplier is APPROVED |
| N0-PRC-010 | `FX_ASSUMPTION_MISSING` | foreign-currency lines have a matching assumption |
| N0-PRC-011 | `LINE_UNSOURCED` | every non-DNP BOM line is in the handoff |
| (shared) | `SEPARATION_OF_DUTIES` | the approver ∉ submitters |

At handoff release, N0 additionally refuses (409 `SOURCE_RIGHTS_REVOKED`) when the artifact's own
source, or a quote source, is no longer APPROVED.

## 6. Arithmetic conventions

- Quantities are integers. Money is `Decimal`, serialised as a string, never a float.
- Line and total amounts are exact (no intermediate rounding).
- The unit cost per finished product is rounded HALF_UP to 4 decimals. That is the only rounding
  step.
- FX: `base = extended × rate`, where `rate` is the most recent recorded assumption with
  `as_of ≤ today ≤ valid_until`.
