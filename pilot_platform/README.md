# Pilot Programme Platform: backend framework

A FastAPI backend built from the **Pilot CPM Network: Gates, Layers & Logic** workflow:
26 activities (A–Z), AND-join dependencies, PERT durations, and an acceptance gate on every activity.

It does two jobs:

1. **Runs the programme.** Loads the CPM network, computes the schedule (ES/EF/LS/LF, float, critical path, P80),
   enforces "start only when **all** predecessors have passed their gate", and runs gate reviews. Each review
   checks the submitted evidence, runs automated platform checks, and keeps reviewers separate from submitters.
2. **Implements the technical layers the plan builds.** These are the platform (D), digital thread (E), data
   contracts (F), LLM gateway with secure RAG (G), extraction (H), durable orchestration (I), vendor adapters (L, T),
   product nodes N1–N8 (J, K, M, N, O, P, Q, R), the evaluation/security harness (S) and the RFQ→quote integration (U).

Most gates are **executable**: for example, gate E ("100% pilot artifacts revision-linked") measures digital-thread
coverage, and gate U ("one RFQ spans nodes with lineage") walks an issued quote's lineage back to its RFQ.

## Quick start

```bash
cd pilot_platform
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                                  # 92 tests
uvicorn app.main:app --reload              # http://127.0.0.1:8000/docs
```

On first start it creates `pilot.db` (SQLite) and seeds tenant `pilot`, the 26-activity programme and demo users.
Each demo user's password is `<username>-demo`: `admin`, `founder`, `pm`, `reviewer`, `engineer`, `engineer2`,
`analyst`, `procurement`, `finance`, `qa`, `vendor_lead`, `sme`, `viewer`.

With Docker (Postgres, API and worker): `docker compose up --build`.

## Activity → code map

| Act | Layer | Gate (from the plan) | How the gate is checked | Code |
|---|---|---|---|---|
| A | Discovery | 10 buyer interviews; 1 bounded use case | evidence `buyer_interviews ≥ 10`, `bounded_use_cases = 1` | `programme/` |
| B | Governance | Permitted datasets and customer sign-off | evidence | `programme/` |
| C | Platform | Trust boundaries and system design approved | evidence (ADR, threat model refs) | `programme/` |
| D | Platform | Role tests pass; unauthorized access denied | **auto**: every non-public route requires auth | `core/security.py`, `core/audit.py` |
| E | Platform | 100% pilot artifacts revision-linked | **auto**: thread coverage = 1.0 | `thread/service.py` |
| F | Data | Defined fields, conflicts and provenance | **auto**: schema contract probe | `contracts/` |
| G | LLMOps | No cross-tenant retrieval; capped spend | **auto**: isolation probe + budget enforcement | `ai/gateway.py`, `ai/retrieval.py` |
| H | SLMOps | Meets acceptance accuracy on held-out cases | **auto**: gold-set accuracy ≥ 0.90 | `ai/extraction.py`, `ai/evals.py` |
| I | Orchestration | Resumes failures; approvals enforced | **auto**: engine self-test (isolated DB) | `orchestration/engine.py` |
| J | N1 Market | No unsupported TAM→SAM conversion | **auto**: every SAM entry has a named account + cited evidence | `nodes/n1_market.py` |
| K | N2 Product | Critical requirements traceable to tests | **auto**: PRD ↔ TEST traceability | `nodes/n2_product.py` |
| L | Vendor layer | At least 2 usable vendor handoffs | **auto**: ≥ 2 qualified vendors (atlas-linked ones need current due diligence) | `vendors/`, `atlas/` |
| M | N3 Design | Simulation assumptions + engineer review | **auto**: latest SIM review passed | `nodes/n3_design.py` |
| N | N4 Quality | Safety-critical gaps block release | **auto**: no uncovered critical reqs / unmitigated S≥9 | `nodes/n4_quality.py` |
| O | N5 Sourcing | 2 comparable valid bids where attainable | **auto**: ≥ 2 comparable bids or written justification | `nodes/n5_sourcing.py` |
| P | N6 Commercial | End-to-end bid with no auto-award | **auto**: awarded bids were awarded by a different human | `nodes/n6_commercial.py` |
| Q | N7 Finance | Deterministic calculations, no LLM math | **auto**: latest costing re-derives to the same hash | `nodes/n7_finance.py` |
| R | N8 Lifecycle | No synthetic data presented as actual | **auto**: replay events all flagged synthetic | `nodes/n8_lifecycle.py` |
| S | Cross-cutting QA | Critical policy and isolation tests pass | **auto**: security harness | `ai/evals.py` |
| T | Vendor QA | Accepted or rejected with evidence | **auto**: evidenced scorecards + every vendor result reviewed, no open job exceptions | `vendors/`, `atlas/jobs.py` |
| U | Integration | One RFQ spans nodes with lineage | **auto**: issued QUOTE lineage ⊇ RFQ, PRD, SIM, TEST, BOM | `integration/rfq_pipeline.py` |
| V | Verification | Critical scenarios and abuse tests pass | evidence + **auto** security harness | `programme/` |
| W–Z | Pilot → Decision | (customer deliverables, acceptance, P&L, go/no-go) | evidence | `programme/` |

Gate specs live in `app/programme/seed_plan.json` (field `gate_spec`); checks are in `app/programme/checks.py`.
Checks fail closed: with no data, the gate cannot pass.

## How the programme works

```
NOT_STARTED --start--> IN_PROGRESS --submit--> IN_REVIEW --pass--> GATE_PASSED
                            ^                      |
                            +------rework------ GATE_FAILED <--fail--+
```

* **start**: only when every predecessor is `GATE_PASSED`, by the activity's owner role or a programme manager.
  Otherwise the API returns `409 gate_blocked` and lists the blocking predecessors.
* **submit**: attaches evidence. Evidence rules and automated checks run and are stored with the review.
* **decide**: by a `gate_reviewer` or programme manager who is **not** the submitter. Checks are re-run at decision
  time, so a pass cannot go through on stale results.
* `POST …/check` is a dry run that changes no state.

```bash
B=http://127.0.0.1:8000
tok() { curl -s -X POST $B/auth/login -H 'Content-Type: application/json' \
          -d "{\"username\":\"$1\",\"password\":\"$1-demo\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'; }
F=$(tok founder); R=$(tok reviewer)
curl -s $B/programmes/1 -H "Authorization: Bearer $F"                                  # schedule summary
curl -s -X POST $B/programmes/1/activities/B/start -H "Authorization: Bearer $F"       # 409: A not passed
curl -s -X POST $B/programmes/1/activities/A/start -H "Authorization: Bearer $F"
curl -s -X POST $B/programmes/1/activities/A/submit -H "Authorization: Bearer $F" -H 'Content-Type: application/json' \
     -d '{"evidence":{"buyer_interviews":10,"bounded_use_cases":1},"actual_duration_days":6}'
curl -s -X POST $B/programmes/1/activities/A/decide -H "Authorization: Bearer $R" -H 'Content-Type: application/json' \
     -d '{"decision":"pass"}'
curl -s "$B/programmes/1/schedule?use_actuals=true" -H "Authorization: Bearer $F"     # forecast with actuals
curl -s "$B/programmes/1/monte-carlo?iterations=5000" -H "Authorization: Bearer $F"
```

### Schedule results for the plan as supplied

The engine reproduces the source exactly: every ES/EF/LS/LF/float value matches to within 1e-9, along with the
16-activity critical path `A B C E G H K M N R U V W X Y Z`, the expected 124.67 days (25 weeks), σ = 5.37 days,
P80 ≈ 129.2 days by normal approximation, and near-critical D, F, O, Q, T.

Two findings beyond what the source shows:

* **Monte Carlo P80 is about 131.8 days**, roughly 2.6 days later than the normal approximation. Several
  near-critical branches merge at U, and the normal approximation ignores that merge bias.
* **The source's reading notes list J→P as a slack link, but it is driving.** J's EF and P's ES are both 48.5.
  P still has 16.7 days of total float. The source's own drawing logic agrees; only the prose note is off.

## RFQ → quote integration (activity U)

```
ingest_rfq → extract_rfq → [review_extraction] → build_prd ─┬→ design_pack → quality_plan ─┐
                                                            └→ sourcing ───────────────────┴→ costing → [approve_quote] → issue_quote
```

`POST /workflows {"definition":"rfq_to_quote","input":{…}}` starts a durable run. `[…]` steps wait for a human:
the run's initiator can never approve its own steps, and the approver needs the step's role. Each task writes a
digital-thread revision derived from its inputs. If the quote approval is rejected, a compensation voids the
draft. Business-rule failures (for example only one comparable bid with no justification) stop the run without
retrying. See `tests/conftest.py::pipeline_input` for a complete input payload.

## Layers in brief

* **Platform (`core/`)**: PBKDF2 passwords and HS256 JWTs. Roles are re-read from the database on every request, so
  revoking access takes effect immediately. Tenant scoping, plus a hash-chained, append-only audit log
  (`GET /audit/verify` detects edits made even with raw SQL). Production mode refuses the dev secret and demo users.
* **Digital thread (`thread/`)**: immutable, content-hashed revisions (RFQ, PRD, BOM, SIM, TEST, QUOTE, BUILD,
  MARKET) with parent links. Non-root kinds must cite a parent, lineage cannot cross tenants, and identical
  rewrites are idempotent. Includes lineage, descendants and structural diff.
* **LLM gateway (`ai/gateway.py`)**: routes each task to a provider and model (default `claude-opus-5-5`, overridable
  per task with `LLM_ROUTES_JSON`). Before every call it reserves the worst-case cost against the tenant's monthly
  budget. Every call is traced, including refused and failed ones. Retrieval is tenant-filtered in SQL and then
  ACL-filtered. Retrieved text is wrapped as data, and injection patterns are flagged. The default `local` provider
  is offline and free. `LLM_PROVIDER=anthropic` uses the official SDK with server-side refusal fallbacks
  (`fallbacks="default"`) enabled.
* **Extraction (`ai/extraction.py`)**: a rule-based baseline that returns typed, SI-normalised fields, each with a
  confidence score and a source span, plus conflicts, missing required fields and a `needs_review` flag. It sits
  behind an `Extractor` protocol, so an SLM can replace it and be scored by the same eval.
* **Orchestration (`orchestration/`)**: a persisted DAG engine with retries, backoff, lease-based crash recovery,
  reverse-order compensation, approval states and idempotent starts. `python -m app.orchestration.worker` advances
  pending runs.
* **Nodes (`nodes/`)**: pure-function cores, which the API, the pipeline and the gate checks all reuse. N7 uses
  `Decimal` throughout, and every costing carries a reproducibility hash.

## Vendor atlas integration

This section implements the vendor parts of the reference workbook *ATHENA N1–N8 Data IO & Vendor Ingestion
Architecture* using the data in *ATHENA Global Specialized Vendor Atlas 2026*.

| Reference element | Implementation |
|---|---|
| Atlas VENDOR MASTER (1,163 records) | `POST /atlas/imports` (raw .xlsx body). The demo seed loads `data/reference/…Atlas_2026.xlsx` |
| Atlas CATEGORY ANALYTICS | `GET /atlas/facets` reproduces the sheet exactly for all 31 segments (tested) |
| Atlas VENDOR DUE DILIGENCE (10 criteria, weights = 100%) | `PUT /atlas/vendors/{id}/due-diligence/{criterion}`, `POST …/due-diligence/decision` |
| Vendor Connector Registry (CON-001…047) | `GET /vendor-connectors`, with each connector cross-walked to atlas IDs |
| Ingestion Methods (ING-01…16) | Each connector lists its routes; the file routes (ING-06/08/10/14) can do a manual handoff |
| Proposed APIs A08 / A09 / A10 | `POST /vendor-connectors/{id}/run`, `POST /vendor-callbacks/{tenant}/{id}`, `GET /vendor-jobs/{id}` |
| Gate G4 (supplier) | Connector activation needs verified entitlement, a linked atlas record that passed due diligence, and a qualified engagement vendor |
| DF-026 `consent_scope` / test T35 | A job is `BLOCKED_PERMISSION` unless every RFQ in its input lineage is `VENDOR_ALLOWED` |
| Tests T10 / T27 / T32 / T33 | Licence hold · rate-limit retry with no double charge · charge-without-artifact exception · checksum quarantine |

**Atlas records are candidates, not partners.** Every record starts as the atlas states it: API "Not assessed",
rights "Requires review", outreach "Uncontacted". Price text is stored verbatim, and no number is ever derived
from it. Only the 10 records classed "Published numerical entry price" are flagged as having a numeric public
entry point, and even that is not a quotation.

**From candidate to working connector:**

```
atlas record ──due diligence (10 weighted criteria, knock-outs)──▶ qualified
     └─promote──▶ engagement vendor ──qualify──▶ bound to connector
connector: reference ──entitlement evidence──▶ entitlement_verified ──activate (G4)──▶ active (+ webhook secret, shown once)
job:  run ──consent? licence?──▶ AWAITING_VENDOR ──signed result, checksums OK──▶ RESULT_RECEIVED ──human review──▶ ACCEPTED
                └─ BLOCKED_PERMISSION / HELD_LICENSE (POST /vendor-jobs/{id}/retry once cleared)     └─ QUARANTINED
```

* **Due diligence.** Each criterion is scored 0–5 by the role that owns its evidence: vendor lead, engineer, QA
  (security), finance or programme manager. Every assessment needs evidence references. To qualify, a vendor needs
  all 10 criteria assessed, the three knock-outs passed (legal identity, commercial rights, model-input
  confidentiality), and a weighted score ≥ `PILOT_VENDOR_DD_MIN_SCORE` (default 3.5 / 5). Any new assessment
  reopens a decided record, so a later failing finding revokes qualification. Gate L stops counting that vendor,
  and its jobs go to `HELD_LICENSE`.
* **Vendor callbacks.** Each callback carries `X-Athena-Timestamp`, `X-Athena-Nonce` and
  `X-Athena-Signature: sha256=HMAC(secret, "{ts}.{nonce}." + body)`. Callbacks outside a ±300 s window are rejected,
  a replayed nonce gets 409, and a duplicate `event_id` is ignored. Result files arrive base64-encoded with their
  declared SHA-256. A mismatch quarantines the result, and nothing goes downstream.
* **Results enter the digital thread.** Each result becomes a revision whose kind comes from the connector's
  canonical schema (e.g. `supplier_quote.v1` → BOM, `design_package.v1` → SIM). It is derived from the job's input
  revisions, so lineage reaches back to the RFQ. It starts in `REVIEW` state, and someone other than the job's
  creator must accept or reject it.
* **Dispatch.** Connectors have no programmatic adapters yet (Nexar GraphQL, Altium, Flux MCP and so on). Jobs go
  out as file-handoff manifests, using each connector's documented fallback from the registry. Register a real
  adapter with `@register_adapter("CON-027")` in `app/atlas/jobs.py`.
* **Gates.** Gate T adds `vendor_results_decided`: no result left unreviewed and no open job exception.

The connector → atlas crosswalk is in `app/atlas/data/connectors.json`. It holds 27 exact name matches,
14 curated matches (each with a reason, e.g. "Altium 365" → `AT-0312 Altium`), and 6 providers that are not in the
atlas (HubSpot, Salesforce, DocuSign, AWS IoT Core, the GeM portal, and the internal cost engine). Those 6 cannot be
activated until a record is added. To regenerate the reference JSON:
`python -I scripts/extract_reference.py ARCHITECTURE.xlsx ATLAS.xlsx app/atlas/data`.

## Configuration

Settings come from environment variables; see `.env.example` (export them, e.g. `set -a; . ./.env; set +a`).
Key settings: `DATABASE_URL`, `PILOT_ENV`, `PILOT_JWT_SECRET`, `PILOT_SEED_DEMO`, `LLM_PROVIDER`,
`LLM_ROUTES_JSON`, `PILOT_LLM_BUDGET_USD`, `PILOT_GST_RATE` (default 0.18), `PILOT_EXTRACTION_ACCURACY_GATE`,
`PILOT_PFMEA_RPN_THRESHOLD`.

## Known limits

* **The gold RFQ corpus is a starter regression set, not a held-out benchmark.** Its 8 cases were written alongside
  the extractor, which scores 100% on them. Replace it with real, permitted customer RFQs before treating gate H as
  evidence of accuracy.
* **Tested on SQLite only.** The Postgres and Docker paths are standard but were not run here. The schema is created
  with `create_all`; add Alembic before you need migrations.
* **The live Anthropic call path was not exercised.** Only its request shape and parsing are tested, against a fake
  client. Budget checks run before any provider call.
* **EDA/CAE tools are external.** N3 records and reviews their outputs (ERC/DRC, simulation results with
  assumptions) and computes a first-order junction temperature; it does not run KiCad or SPICE. API vendor adapters
  are an extension point that refuses to fake a call; the file-handoff adapter works.
* **Concurrency is single-worker.** The budget reservation and the workflow worker assume one writer. On Postgres,
  add row locking (`SELECT … FOR UPDATE SKIP LOCKED`) before scaling out.
* **Vendor integration limits.**
  * Webhook secrets are stored in the database; move them to a secrets manager for production.
  * Vendor result files must be sent inline (≤ 5 MB). Object-storage import (ING-14) is not built.
  * No programmatic vendor adapters exist yet (see above).
  * The due-diligence threshold (3.5) and the choice of the three knock-out criteria are defaults I chose; the
    atlas gives weights but no pass mark.
  * The 14 curated crosswalk matches should be confirmed by someone who knows the vendors.
* **Durations are illustrative**, as the source states ("revise after pilot scoping"). Update `seed_plan.json`, or
  `POST /programmes` with a revised plan.
