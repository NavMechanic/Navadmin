# ATHENA N0 — Data Intelligence, Quality & Governance Gateway

N0 is the policy gate in front of the ATHENA nodes N1–N8. Every incoming package and every
cross-node handoff passes through it. N0 decides **PASS | REWORK | BLOCK | REVIEW** using
deterministic code. It records the provenance of every value. It releases data only to the nodes,
models and vendors that the source's rights and the tenant's ACLs allow.

> This tree was **rebuilt from the specification**. The original `athena_n0_starter` repository
> was not available in this workspace, so the module layout, endpoint shapes and the eight
> baseline tests follow the spec rather than a diff against the old code.

## Invariants

| Invariant | Where it is enforced |
|---|---|
| PASS is not engineering qualification | `GatewayDecision.qualification_disclaimer` on every decision |
| A hard-policy BLOCK cannot be overridden by an LLM, a score, an approval or feedback | `rules.decide` ignores `quality_score`; `governance.record_approval` refuses BLOCK (`BLOCK_NOT_OVERRIDABLE`); feedback never re-decides |
| Strict provenance: CUSTOMER / MEASURED / DERIVED / MODEL_EXTRACTED | `rules.provenance_checks`; N0 computes DERIVED values itself (24 V × 5 A → `nominal_output_power_w = 120 W`, with formula and inputs) |
| No unapproved transmission to LLMs or vendors | `llm_adapter.LLMAdapter.check_boundary`, `vendor_contracts.authorize_work_package` |
| Quarantine first, SHA-256 addressed | `POST /v1/artifacts/upload` → `n0://quarantine/<tenant>/<sha256>`, write-once store; promotion to `released/` only on PASS |
| Audit cannot be rewritten | DB triggers make audit, approval, decision, manifest, model-run, feedback and lineage tables append-only; audit and approvals are hash-chained per tenant (`GET /v1/audit/verify`) |

## Pipeline (`app/pipeline.py`)

| Stage | What runs | Typical outcomes |
|---|---|---|
| **N0.1 intake & permissions** | role/node attribute, tenant isolation, SourceRecord.v1 rights (approval, purpose, licence, expiry), clearance, revision control (format, superseded, immutable), SHA-256 of files and payload | BLOCK |
| **N0.2 parsing & normalization** | Docling parsing of referenced files, Presidio PII detection, SI normalization of every requirement | REWORK (units, parse), REVIEW (PII) |
| **N0.3 context** | payload classification vs `artifact_type` (9 types), upstream `ID@REV` resolution, model-run linkage, evidence-in-source, critical-spec completeness → `missing_fields` | BLOCK (upstream), REWORK, REVIEW |
| **N0.4 data quality** | JSON Schema 2020-12, V × I power balance, efficiency, ranges, tolerance, 80 % derating, duplicates, contradictions, freshness, `value_origin` truthfulness | REWORK, BLOCK (mislabelled origin) |
| **N0.5 hard policy** | synthetic-as-real, synthetic beyond CONCEPT, calibration, > 60 V without a safety standard, EDA DRC/ERC and model output stage gates | BLOCK, REVIEW |
| **N0.6 release** | decision precedence BLOCK > REWORK > REVIEW > PASS, `allowed_destinations`, canonical artifact, release manifest, lineage, audit event | — |

A BLOCK in N0.1 short-circuits: the submission is **not admitted**. Only the audit event is kept,
so a foreign or unlicensed payload never lands in N0 storage. A refused resubmission also cannot
overwrite the decision of an already admitted revision. The catalog has 51 rules (23 BLOCK,
22 REWORK, 6 REVIEW). See `app/rules.py` or `GET /v1/rules`. `validator_version` embeds a digest
of the catalog.

Each evaluation returns the **seven deliverables** (`EvaluationResult`):

- `canonical_artifact`: units normalized and DERIVED values added
- `provenance`
- `quality_report`
- `rights_decision`
- `decision`: GatewayDecision.v1 with its rationale
- `release_manifest`
- `audit_event`

### How the critical negative cases are decided

Every case below yields **no destinations**. Its release approval is refused (403/404/409), and
so is its handoff (404/409). See `test_critical_negative_cases_never_release`.

| Case | Decision | Rule |
|---|---|---|
| unapproved source | BLOCK | N0-RIGHTS-002 SOURCE_NOT_APPROVED |
| cross-tenant artifact / file / blob | BLOCK | N0-TEN-001 CROSS_TENANT_REFERENCE |
| SHA-256 mismatch (payload, file, stored bytes) | BLOCK | N0-INT-001 SHA256_MISMATCH |
| superseded revision | BLOCK | N0-REV-001 SUPERSEDED_REVISION |
| synthetic presented as real | BLOCK | N0-SYN-001 SYNTHETIC_AS_REAL |
| unapproved vendor transmission | BLOCK | N0-VEND-001 VENDOR_TRANSMISSION_UNAPPROVED |
| RFQ power inconsistency (V × I ≠ P) | **REWORK** | N0-PHY-001 POWER_BALANCE_MISMATCH |
| missing critical specs | **REWORK** | N0-CTX-006 MISSING_CRITICAL_FIELD |

The last two are data defects that the producer fixes in a new revision, so they are REWORK
rather than hard-policy BLOCKs. They are blocked from release all the same.

## Hybrid AI policy

1. Deterministic code owns integrity, permissions, schema, units, arithmetic and hard policy.
   No model is consulted by `rules.py`.
2. Docling (`app/parsing.py`) parses the document and Presidio redacts PII **before** any model call.
   The model only sees the redacted text.
3. `app/llm_adapter.py`:
   - disabled by default (`N0_LLM_PROVIDER=disabled`)
   - `deterministic`: a local rule extractor, the offline and test stand-in; it is not an LLM
   - `anthropic`: Claude, using JSON-schema `output_config.format` structured output and the
     server-side refusal fallback. The default model is `claude-opus-5-5`.
4. The boundary is checked before a provider call:
   - the source is in the caller's tenant and APPROVED
   - an external provider also needs `LLM_EXTRACTION` in `permitted_use`
5. The output schema forces exactly one entry per parameter:
   - `null` value, unit and evidence for anything the document does not state, listed in `missing_fields`
   - a verbatim `evidence_text` quote for anything it does state
6. The output is revalidated against that schema. `rules.py` then re-runs units, physics,
   provenance, evidence-in-source and completeness checks.
   - Extracted values stay `MODEL_EXTRACTED` and always trigger REVIEW (N0-PROV-006).
   - A model therefore never produces a PASS on its own. A gateway reviewer who did not submit
     the artifact resolves the REVIEW.
7. Every call is persisted as a `ModelRun.v1`, including refused and failed calls. It records:
   - model and served version
   - `prompt_revision`
   - `input_hash = SHA-256(prompt_revision ∥ redacted text)`
   - token usage
   - estimated cost

## API

| Endpoint | Role | Notes |
|---|---|---|
| `GET /health` | — | DB, object store, malware scanner, policy engine, parsers, LLM; 503 when degraded |
| `POST /v1/sources/register` | source_admin | SourceRecord.v1, starts PENDING |
| `POST /v1/sources/{tenant}/{source}/approve` | rights_approver ≠ registrant | APPROVED / REJECTED / REVOKED + ApprovalEvent.v1 |
| `POST /v1/artifacts/upload` | producer, node_service | multipart; size 413, type 415, malware/SHA 422; write-once quarantine |
| `POST /v1/gateway/evaluate` | producer, node_service (for its own node) | the six stages → seven deliverables |
| `POST /v1/extraction/run` | producer | schema-constrained RFQ extraction → `RFQDraft.v1` + `ModelRun.v1` |
| `GET /v1/artifacts/{id}` | any role in tenant | canonical artifact, latest decision, revisions, manifests |
| `GET /v1/artifacts/{id}/lineage` | any role in tenant | directed upstream + downstream graph, tenant-scoped |
| `POST /v1/approvals` | release_manager (RELEASE), gateway_reviewer (REVIEW_RESOLUTION) | four-eyes; BLOCK refused |
| `POST /v1/handoffs/release` | release_manager | requires PASS + a RELEASE ApprovalEvent issued after the decision, targets ⊆ `allowed_destinations`, not superseded |
| `POST /v1/gateway/feedback` | gateway_reviewer | corrections/labels; training only for `synthetic=false` with MODEL_TRAINING rights |
| `POST /v1/vendor/work-packages` | release_manager | VendorWorkPackage.v1 → TransmissionDecision |
| `POST /v1/vendor/results` | node_service, producer | VendorResult.v1; files must come through quarantine; checksum verified |
| `GET /v1/audit/events`, `GET /v1/audit/verify` | auditor | hash-chain verification |

Errors are `{"detail": {"code": ..., "message": ...}}`. Refusals are audited as `<EVENT>_REFUSED`.

## Schemas

`schemas/` is generated from `app/models.py` (JSON Schema Draft 2020-12). It contains:

- the eight families: SourceRecord, Artifact, Requirement, ModelRun, VendorWorkPackage, VendorResult,
  GatewayDecision, ApprovalEvent (`*.v1`)
- RFQDraft, ReleaseManifest and EvaluationResult
- the thirteen payload schemas under `schemas/payloads/`, including the market-intelligence types
  `MARKET_BATCH` and `BUYER_RECORD` (inputs to N1), plus `COMMERCIAL_SHORTLIST` (N1 → N6) and
  `SAM_EVIDENCE` (N1 → N7). N0 recomputes SAM totals (N0-SAM-001) and blocks synthetic or unattested
  spend presented as verified (N0-SAM-002).
- `openapi.json`

```bash
python -m app.schema_export          # regenerate
python -m app.schema_export --check  # CI: fail on drift (also covered by tests/test_schemas.py)
```

## Running it

```bash
pip install -r requirements.txt                       # requirements-prod.txt adds psycopg, boto3, docling, presidio
export N0_TOKENS_JSON='{"dev-producer": {"principal_id": "alice", "tenant_id": "acme", "roles": ["producer"]},
                        "dev-admin":    {"principal_id": "sam",   "tenant_id": "acme", "roles": ["source_admin"]},
                        "dev-rights":   {"principal_id": "rita",  "tenant_id": "acme", "roles": ["rights_approver"]},
                        "dev-release":  {"principal_id": "rm",    "tenant_id": "acme", "roles": ["release_manager"]}}'
uvicorn app.main:app --port 8000

curl -s -XPOST localhost:8000/v1/sources/register -H 'Authorization: Bearer dev-admin' \
     -H 'content-type: application/json' -d @examples/source_register.json
curl -s -XPOST localhost:8000/v1/sources/acme/SRC-ACME-RFQ-PORTAL/approve -H 'Authorization: Bearer dev-rights' \
     -H 'content-type: application/json' -d '{"decision": "APPROVED", "reason": "MNDA countersigned"}'
jq '{artifact: .}' examples/valid_rfq.json | curl -s -XPOST localhost:8000/v1/gateway/evaluate \
     -H 'Authorization: Bearer dev-producer' -H 'content-type: application/json' -d @- | jq .decision
```

Configuration is environment-driven (`app/config.py`):

- `N0_DATABASE_URL`
- `N0_STORAGE_BACKEND=local|s3`, `N0_S3_BUCKET`
- `N0_CLAMD_HOST` (ClamAV), `N0_REQUIRE_AV`
- `N0_LLM_PROVIDER`, `N0_LLM_MODEL`, `N0_LLM_EFFORT`
- `N0_MAX_UPLOAD_BYTES`

`N0_LLM_PROVIDER=anthropic` needs `ANTHROPIC_API_KEY` in the environment.

**Authentication.** In production, use JWTs from your identity provider:

- set `N0_JWT_SECRET` (HS256), or `N0_JWT_PUBLIC_KEY` / `N0_JWT_PUBLIC_KEY_FILE` (RS256/ES256);
- optionally `N0_JWT_ISSUER` and `N0_JWT_AUDIENCE`;
- leave `N0_TOKENS_JSON` empty.

Tokens must carry `sub`, `tenant_id` and `exp`, and may carry `roles`, `nodes` and `clearance`. The
`none` algorithm is refused. Unknown roles, a bad signature, the wrong issuer or an expired token
all return 401.

**Extensions.** `create_app(..., extensions=[fn])` calls each `fn(app)` with the finished app,
so another node can mount its router on the same database, store, pipeline and authenticator.
`athena_n1_integrated` uses this to mount N1.

### PostgreSQL + object storage

```bash
export N0_DATABASE_URL=postgresql+psycopg://n0:***@db:5432/athena_n0 N0_AUTO_CREATE_SCHEMA=false
alembic upgrade head                 # tables + PL/pgSQL append-only triggers (migrations/versions/0001)
export N0_STORAGE_BACKEND=s3 N0_S3_BUCKET=athena-n0   # enable S3 Object Lock on the bucket for WORM
```

## Tests

```bash
python -m pytest -q
```

| File | Covers |
|---|---|
| `tests/test_gateway.py` | baseline (health, source approval, quarantine upload, valid PASS, invalid REWORK); the 8 critical negative cases parametrized; vendor transmission; value_origin separation; BLOCK not overridable; revision control; stage gates, calibration, clearance, licence, safety, units; upload screening; authN/Z; append-only + hash chain |
| `tests/test_llm_boundary.py` | baseline (disabled by default, unapproved source never reaches the model, nulls + missing_fields + evidence); external provider rights; PII redaction before transmission; schema-invalid / hallucinated / evidence-less output; Claude request shape, refusal handling, cost |
| `tests/test_handoff_e2e.py` | `examples/valid_rfq.json` → PASS → RELEASE approval → N2; `examples/invalid_rfq.json` → refused → fixed revision B → released; stale/rejected approvals; extraction → REVIEW → resolution → N2; downstream pinning; feedback training gates; vendor round trip; lineage |
| `tests/test_schemas.py` | 8 families are valid 2020-12 with the specified fields and vocabularies; committed schemas match the models; API outputs conform; Alembic migration equals the ORM and installs the triggers |
| `tests/test_market_and_jwt.py` | MARKET_BATCH routes only to N1; one synthetic observation taints a batch; SAM_EVIDENCE totals recomputed, unattested spend blocked; JWT signature / expiry / issuer / role checks; role implications; `none` algorithm refused |

## Not verified here

- **Docling and Presidio are not installed** in this environment. The suite runs on the documented
  fallbacks: pypdf, python-docx, an XLSX XML reader and regex PII recognizers. `/health` reports
  which mode is active.
- **PostgreSQL was not run live.** The migration was checked as offline SQL
  (`alembic upgrade head --sql`). Tests use SQLite.
- The **S3** backend and the **clamd** scanner have not been exercised.
- The **Claude provider** was tested against a faked SDK client, so no live API call was made.
- **Authentication:** JWT verification was tested with HS256 tokens minted in the tests.
  RS256/ES256 keys from a real identity provider (and JWKS rotation) have not been exercised.
  Static tokens (`N0_TOKENS_JSON`) remain available for local development only.
- **Model prices** in `llm_adapter.PRICING_USD_PER_MTOK` are list prices for estimates. Check them
  before using them for billing.
