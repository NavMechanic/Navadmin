# ATHENA N5 — Developer Sprint Backlog

- **Done** means implemented and covered by tests in this repository.
- **Open** means not implemented, or implemented but not exercised against the real system.

## Done

| Id | Item |
|---|---|
| N5-D01 | BOM intake from the N3 BOM_RELEASE released to N5: N0 decision, manifest, payload re-hash, approval chain, rights, invalidation. Exact-MPN normalisation, UOM, Q_required, critical lines, DNP. |
| N5-D02 | Supplier records: versioned, Open Supply Hub site verification (live / CSV), authorized-channel proofs, signing keys, supplier_quality attestation with separation of duties. |
| N5-D03 | Quote intake: manual `athena-quote/1`, signed EMS drop zone (Ed25519 / RSA, PDF / CSV / X12 843 profile, capacity, NRE), structural rejection, write-once vault. |
| N5-D04 | Connectors: DigiKey OAuth2, Mouser, Nexar GraphQL, Open Supply Hub. Feature flag, project scope, in-memory TTL cache, per-line budget, rate limit and quota, provenance log, secret redaction. |
| N5-D05 | Hard rejection matrix (7 BLOCK, 5 REWORK offer codes; line / report codes), Decimal costing shared with N0, ranking, tie-breaks, step-up advice. |
| N5-D06 | Review on the exact report hash and quote hashes with three-way separation of duties; N0-checked `SOURCING_HANDOFF` drafts to N6 / N7. |
| N5-D07 | N0: `SourcingHandoffPayload.v1`, N0-PRC-001..011, source-rights listeners, release-time rights re-check, X12 / EDIFACT recognition. |
| N5-D08 | Cascades: BOM / PRD revision and N3 invalidation (listener + lazy), rights revocation through to the N6 / N7 drafts. |
| N5-D09 | ECO proposals with equivalence evidence; no substitution. |
| N5-D10 | Persistence: 10 N5 + 4 N6 / N7 append-only tables, Alembic 0005, PostgreSQL DDL. |
| N5-D11 | 19 N5 JSON Schema families + adapter / record schemas + combined OpenAPI, drift-checked. |
| N5-D12 | E2E N0 → N1 → N2 → N3 → N4 → N5 → N6 / N7 and the synthetic ₹244 demo. |

## Open — prioritised

| Id | Priority | Item | Notes |
|---|---|---|---|
| N5-B01 | P0 | **Live distributor validation.** Run each connector against its sandbox / production API with real credentials. Check the response parsers (DigiKey v4 variations, Mouser price strings, Nexar seller fields) and the rate-limit headers. | Mocked endpoints only so far. |
| N5-B02 | P0 | **Legal review of API agreements.** Confirm the retention of decision records, the cache TTL and internal redistribution to N6 / N7 for DigiKey, Mouser and Nexar. Encode any limits in `Policy`. | The posture follows the brief; the agreements were not available. |
| N5-B03 | P0 | **PostgreSQL + S3 (object lock) + ClamAV in staging** for the quote vault. | DDL and SQLite migrations pass. |
| N5-B04 | P1 | PAdES verification of signatures embedded in PDF quotations, with a certificate trust list per EMS partner. | Detached signatures only today. |
| N5-B05 | P1 | Full EDI: X12 840 / 843 / 997 and EDIFACT REQOTE / QUOTES, envelope validation, acknowledgements. | Documented 843 subset only. |
| N5-B06 | P1 | Multi-currency treasury feed for FX assumptions, with approval of the rate source. | Assumptions are recorded manually. |
| N5-B07 | P1 | Order-quantity policy options: attrition / spares percentage per line class, consolidating lines with the same MPN into one order. | Q_required = qty × build today, per line. |
| N5-B08 | P1 | Lifecycle / obsolescence data (PCN, EOL notices) as risk flags. | |
| N5-B09 | P2 | N6 / N7 beyond the minimal receivers: bid composition, landed-cost model (GST, duties, freight), financing. | N7 owns everything outside the material quote. |
| N5-B10 | P2 | Gated assistive models from the AI strategy (quote extractor, lead-time estimator). | Not implemented; no model in the loop. |
| N5-B11 | P2 | Supplier scorecards from receipts and quality escapes, fed by N8. | |
| N5-B12 | P3 | Operator UI: report diff view, ineligible-offer explanations, ECO queue for N3. | API only today. |
