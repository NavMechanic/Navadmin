# ATHENA N1 — Market & Opportunity Intelligence Engine (integrated with the N0 gateway)

N1 takes market data, customer demand, procurement intelligence and buyer evidence that N0 has
approved. From them it produces:

- qualified buyer shortlists
- evidence-backed SAM estimates
- risk flags
- cost-weighted recommendations

These go to **N2** (product requirements), **N6** (commercial workflow) and **N7** (project
economics). Every hop in and out goes through the N0 Data Quality Gateway.

> This repository was **rebuilt from the specification**. The original `athena_n1_integrated` and
> its 27 baseline tests were not in this workspace, the git history or Drive. The baseline is
> re-created under the specified names: `tests/test_n0_gateway.py` (10 tests),
> `tests/test_n0_llm_boundary.py` (5) and `tests/test_n1_market.py` (12). They pass along with the
> new integration suites.

```
app/main.py            combined entry point (N0 gateway + N1 router, one DB / store / audit chain)
app/gateway.py         the N0 gateway — a byte-identical copy of athena_n0_starter/app/main.py
app/*.py               the rest of the N0 gateway, also byte-identical to athena_n0_starter (tests/test_n0_sync.py)
app/n1/models.py       N1 contracts → schemas/n1_*.schema.json
app/n1/engine.py       deterministic N1.2–N1.6 (no LLM, no I/O)
app/n1/connectors.py   UN Comtrade · SAM.gov · TED · India CPPP drop zone · CRM / interviews
app/n1/slm_extractor.py schema-constrained extraction, gated by a held-out gold set
app/n1/llm_adapter.py  optional report explainer (disabled by default)
app/n1/db.py, store.py append-only, revisioned N1 tables on the shared database
app/n1/service.py      intake, reports, attestations, optimizer, feedback, SLM, N2/N6/N7 handoff
app/n1/router.py       /v1/n1/* API
dashboard/n1_dashboard.py   Streamlit panel
examples/run_local_demo.py  synthetic end-to-end demo
```

## Invariants and where they are enforced

| Invariant | Enforcement |
|---|---|
| **N0 gate dependency** | `service.intake` reads only `MARKET_BATCH` / `BUYER_RECORD` artifacts whose latest N0 decision is PASS and routed to N1, and that are not superseded. Source rights are re-checked at intake. The stored payload bytes must still hash to the recorded SHA-256. Everything else is refused: `N0_NOT_PASS`, `N0_NOT_ROUTED_TO_N1`, `SUPERSEDED_REVISION`, `N0_RIGHTS_DENIED`, `SHA256_MISMATCH`, `NOT_A_MARKET_ARTIFACT`. |
| **Synthetic ≠ verified real SAM** | `verified_real_sam` counts only evidence that is non-synthetic, `VERIFIED_BUDGET`, attested by a reviewer for its exact content hash, fresh, in a serviceable category, de-duplicated and well-formed. `engine.build_report` raises `InvariantViolation` otherwise. Synthetic spend goes to `hypothetical_addressable_spend`. N0 blocks a synthetic row hidden in a real batch and rejects synthetic or unattested items in `SAM_EVIDENCE` (`N0-SAM-002`). Synthetic evidence cannot be attested, and a synthetic-only report is never handed off. |
| **Heuristic score ≠ win probability** | `heuristic_priority_score` (0–100) carries `score_kind=HEURISTIC_PRIORITIZATION`, `calibrated=false` and `win_probability=null`, with the component and penalty breakdown. The report's `ml_readiness` counts real labeled won/lost outcomes (200 required) before a predictive model is considered. The explainer rejects any "chance of winning" wording. |
| **Non-executing optimizer** | `engine.optimize_actions` is a pure function with no network code. It returns `executed: false` and `RECOMMENDED_NOT_EXECUTED`. Bids, portal scraping and automated outreach are always blocked. |
| **Deterministic arithmetic** | SAM sums, FX conversion with an explicit rate date (never a future-dated rate), duplicate exclusion and freshness windows are `Decimal` code in `engine.py`. N0 independently recomputes the `SAM_EVIDENCE` totals (`N0-SAM-001`). |

## Pipeline

| Stage | What it does |
|---|---|
| **N1.1 ingestion** (`connectors.py`) | Comtrade (`Ocp-Apim-Subscription-Key` from `COMTRADE_API_KEY`, else the public preview API), SAM.gov Opportunities v2 (`SAM_GOV_API_KEY`), TED Search v3, the India CPPP **drop zone** (no HTTP client at all: corrigendum ordering, mandatory-attachment SHA-256 checks, path-traversal guard), and CRM / interview CSV or JSON. Live calls are token-bucket rate-limited. Credentials are removed from the raw payload before it is hashed and stored write-once in N0 quarantine. Previews are imported as N0 artifacts. |
| **N1.2 features** (`engine.py`) | Entity resolution: registry identifier, then exact normalized name, then fuzzy name within the same country. Category tagging: CPV / NAICS / HS codes, then keywords. Spend type: `VERIFIED_BUDGET` \| `INDICATIVE` \| `HYPOTHETICAL`. FX normalization with the rate date and source recorded. Freshness scoring against per-type SLAs, which a report request can override. |
| **N1.3 scoring** | buyer access (25) + requirements fit (30) + procurement evidence strength × freshness (30) + timing (15), minus missing-information penalties (5 each, capped at 30) |
| **N1.4 guardrails** | Hard exclusions: `STALE_EVIDENCE`, `SUPERSEDED_CORRIGENDUM`, `MISSING_MANDATORY_ATTACHMENT`, `ATTESTATION_REJECTED`, `UNSUPPORTED_CLAIM`, `RIGHTS_REVOKED`, `FUTURE_DATED`. SAM-only exclusions: `DUPLICATE_SPEND` (explicit links, the same record across connectors, or the same account and amount within 45 days), `MALFORMED_AMOUNT`, `IMPLAUSIBLE_AMOUNT`, `FX_MISSING`, `OUT_OF_CATEGORY`, `NO_AMOUNT`. |
| **N1.5 cost-weighted decisions** | Policy gates run first: `BID_SUBMISSION_IS_N6`, `SCRAPING_PROHIBITED`, `AUTONOMOUS_OUTREACH_PROHIBITED`, `PROCUREMENT_QUIET_PERIOD` (public buyer with an open tender), `DO_NOT_CONTACT`, `SYNTHETIC_TARGET`, `CURRENCY_MISMATCH`, `UNKNOWN_TARGET`. Then actions are ranked by user-supplied loss avoided − execution cost − delay cost, within an optional budget cap. |
| **N1.6 explainability & feedback** | `MarketReport` as JSON and Markdown. Every figure cites `evidence_id` and `source_url`, plus the N0 `ARTIFACT@REV` that admitted it. Reviewers record `EvidenceAttestation`s (an N0 ApprovalEvent, four-eyes, bound to the content hash) and `FeedbackCase` labels. Labels on synthetic targets are never training-eligible. |

## Hybrid AI policy

- **Deterministic engine (active):** everything in `engine.py`.
- **SLM extraction worker** (`N1_SLM_PROVIDER=rules|anthropic`, disabled by default):
  - Unstructured tender or interview text becomes a `MarketObservation` draft, after PII redaction.
  - The output is constrained by a JSON schema. Every value must quote verbatim evidence, and
    values without findable evidence are dropped.
  - Extraction is refused (`SLM_NOT_QUALIFIED`) until an approver runs `POST /v1/n1/slm/evaluate`
    and the provider/model/prompt revision passes the held-out gold set (`N1_SLM_GOLD_PATH`).
    The thresholds are per-field accuracy plus zero hallucinated values.
  - External models need `LLM_EXTRACTION` in the source's rights. The `anthropic` provider uses
    `claude-haiku-5-5` (without the server-side refusal fallback, which Haiku does not offer).
  - **The bundled gold set** (`app/n1/gold/tender_extraction_heldout.jsonl`, 10 synthetic examples)
    was written alongside the rule-based baseline, so for that baseline it is not a true held-out test.
    Replace it with a real labeled held-out set before qualifying any model for production.
- **LLM explainer** (`N1_EXPLAINER_PROVIDER=template|anthropic`, disabled by default):
  - It only summarizes a sealed report's verified figures, flags, missing information and next steps.
  - It rejects any output that cites unknown evidence, contains a number not in the report or talks
    about a chance of winning.
  - External explanation needs `LLM_EXTRACTION` rights on every contributing source.

## N1 → N2 / N6 / N7 handoff

`POST /v1/n1/reports/{report_id}/handoff` (approver role) has three stages.

**1. Checks on the report.** The report must:

- be the latest revision in its scope;
- still match its SHA-256;
- have been written by someone other than the approver;
- not be synthetic-only.

Then every cited evidence item is re-checked: it must be unchanged, its N0 artifact still PASS and
current, and its source rights still granted. **Source rights propagate:** evidence whose source does
not permit a package's purpose (`RFQ_ANALYSIS` / `COMMERCIAL` / `FINANCE`) is left out of that package.

**2. The packages.**

| Target | N0 artifact | Contents |
|---|---|---|
| N2 | `RFQ` (opportunity → product seed) | One per seed-ready opportunity: attested real evidence with requirement clauses and a quantity. SLM-derived requirements stay `MODEL_EXTRACTED`, so N0 holds them for REVIEW. |
| N6 | `COMMERCIAL_SHORTLIST` | Qualified real buyers, heuristic scores, tender deadlines, eligibility risk flags |
| N7 | `SAM_EVIDENCE` | Verified items with FX rates and dates, indicative and hypothetical totals from real data, caveats, costed recommended actions |

**3. Release through N0.**

- Each package is submitted to N0 as the N1 service identity, pinned to its upstream MARKET artifacts.
- On PASS, it is released through N0's RELEASE ApprovalEvent and handoff by the human approver. That
  keeps four-eyes: the submitter is the service, the approver is a person.
- A REVIEW or BLOCK is reported back per package. Repeating a handoff does not release twice.

## API (`/v1/n1`)

| Endpoint | Role |
|---|---|
| `POST /batches`, `POST /buyers` | analyst, service — submit to N0, then intake on PASS |
| `POST /intake` | analyst, service — ingest an artifact already passed by N0 |
| `POST /connectors/{name}/preview`, `POST /connectors/import` | analyst, service |
| `GET /buyers/{id}`, `GET /evidence/{id}` | any role in the tenant (latest + all revisions) |
| `POST /attestations`, `GET /attestations/pending` | approver / readers |
| `POST /reports`, `GET /reports`, `GET /reports/{id}`, `GET /reports/{id}/markdown` | analyst / readers |
| `POST /reports/{id}/explain` | analyst, approver |
| `POST /reports/{id}/handoff` | approver |
| `POST /optimize` | analyst, approver |
| `POST /feedback` | approver, analyst |
| `POST /slm/evaluate`, `POST /slm/extract` | approver / analyst |
| `GET /status` | readers |

All N0 endpoints (`/v1/gateway/evaluate`, `/v1/approvals`, `/v1/handoffs/release`,
`/v1/artifacts/{id}/lineage`, `/v1/audit/verify`, …) are served by the same app.

**Auth.** Signed JWTs only. Static API keys are off: leave `N0_TOKENS_JSON` empty. The claims are
`sub`, `tenant_id` and `exp`, and optionally `roles`, `nodes` and `clearance`. The tenant always
comes from the token.

N1 roles imply the N0 capabilities they need:

- `analyst` → producer
- `approver` → gateway_reviewer + release_manager
- `service` → producer + node_service

Source rights stay with N0's `source_admin` and `rights_approver`.

## Persistence

These N1 tables live on the shared SQLAlchemy metadata: `n1_batches`, `n1_observations`,
`n1_buyer_accounts`, `n1_attestations`, `n1_reports`, `n1_feedback_cases`, `n1_connector_previews`,
`n1_optimizations`, `n1_slm_evaluations` and `n1_handoffs`.

- All are append-only, enforced by database triggers (PL/pgSQL on PostgreSQL, `RAISE(ABORT)` on SQLite).
- Changes are new revisions, never updates.
- N1 writes its lineage edges into N0's lineage graph, so one query traces source → N0 artifact →
  N1 evidence → report → handoff artifact → node.

```bash
export N0_DATABASE_URL=postgresql+psycopg://n1:***@db:5432/athena N0_AUTO_CREATE_SCHEMA=false
alembic upgrade head        # 0001 (N0) + 0002 (N1) tables and triggers
```

## Running it

```bash
pip install -r requirements.txt
python examples/run_local_demo.py                         # synthetic: verified_real_sam stays 0, handoff refused
python -m pytest -q                                       # 92 tests

export N0_JWT_SECRET=... N0_JWT_ISSUER=https://idp.example   # or N0_JWT_PUBLIC_KEY_FILE for RS256
export SAM_GOV_API_KEY=... COMTRADE_API_KEY=... N1_CPPP_DROPZONE=/srv/cppp-dropzone   # optional connectors
uvicorn app.main:app --port 8000

streamlit run dashboard/n1_dashboard.py                   # demo mode; set N1_API_URL + N1_ANALYST_TOKEN +
                                                          # N1_APPROVER_TOKEN for live mode
python -m app.schema_export --check                       # schemas/ and openapi.json are current
```

## Tests

| File | Covers |
|---|---|
| `test_n0_gateway.py`, `test_n0_llm_boundary.py`, `test_n1_market.py` | the 27 baseline tests |
| `test_n1_handoff_e2e.py` | N0 → N1 → N2/N6/N7: an attested opportunity becomes an N0-checked RFQ seed released to N2; rights propagation; seed stopped at N0 REVIEW; synthetic-only report never handed off |
| `test_n1_guardrails.py` | zero synthetic leakage (incl. mixed batches); duplicate spend; corrigenda; stale evidence; malformed / FX / attachments / unsupported / rejected / revoked; changed evidence voids its attestation |
| `test_n1_persistence.py` | DB round-trip across an app restart; evidence, account and report revisions; append-only N1 tables; tenant isolation; every N0-gate refusal at intake |
| `test_n1_connectors.py` | auth header vs public preview; SAM.gov key never stored or logged; rate limiting; TED body and multilingual fields; CPPP drop zone; CRM CSV; error mapping |
| `test_n1_slm.py`, `test_n1_explainer.py` | gold-set gate; grounding; external-model rights; PII redaction; explainer citation and number checks |
| `test_n1_auth.py` | JWT validation, role separation, four-eyes, audited refusals |
| `test_n1_dashboard.py` | Streamlit panel renders and its one-click handoff goes through N0 |
| `test_n1_schemas.py`, `test_n0_sync.py` | schema and migration drift; the vendored N0 equals `athena_n0_starter` |

## Not verified here

- **Live source APIs:** the connector mappings follow the documented response shapes of Comtrade,
  SAM.gov Opportunities v2 and TED v3, but were tested only against fictional offline samples and a
  mock transport. In particular, check TED's value field names (`total-value`, `estimated-value-glo`).
- **PostgreSQL** was checked as offline SQL only. **Live Claude calls** were not made. RS256 / JWKS
  keys from a real identity provider were not exercised.
- **Docling and Presidio** are not installed here, so the regex fallbacks were used.
- **Entity resolution** is deterministic but simple (identifiers, normalized names, a 0.92
  similarity threshold within one country). Review its merges with `CORRECT_ENTITY_MATCH` /
  `WRONG_ENTITY_MATCH` feedback.
