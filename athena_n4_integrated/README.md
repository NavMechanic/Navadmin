# ATHENA N4 — Quality, Verification & Reliability Engine (integrated with N0, N1, N2 and N3)

N4 takes N3-approved design revisions and N2 safety-critical requirements and does five things:

1. **Traceability.** It builds a requirements-to-test matrix that covers 100 % of the safety-critical
   requirements.
2. **PFMEA and control plan.** It keeps both, with CAPA-verified actions.
3. **Evidence import and metrology.** It imports bench ATE (SCPI/VISA), accredited-lab, AOI/vision and
   offline CSV evidence. It checks every measurement against SI units, expanded uncertainty, an in-date
   calibration and a guard band.
4. **Release blocks.** It enforces risk-based release blocks.
5. **Handoff.** Once a separate, qualified test authority approves the exact evidence hash, it releases
   N0-checked `VERIFICATION_RELEASE` packages:
   - to **N8** (lifecycle: unit genealogy and monitors)
   - to **N6** (commercial)
   - to **N7** (finance)

> This repository was **rebuilt from the specification**. No `athena_n4_integrated` starter existed in
> this workspace, its git history or Drive, and there was no N8. It contains:
>
> - **N0**: byte-identical copies from `athena_n0_starter`. That repo gained the verification contract
>   (`VerificationReleasePayload.v1`, rules N0-VER-001..016).
> - **N1**: byte-identical copies from `athena_n1_integrated`.
> - **N2 / N3**: byte-identical copies from `athena_n3_integrated`. N2 now carries hazards and
>   safety-critical requirement ids. N3 now has release listeners.
> - **N4**: built here.
> - **N8**: a **minimal N8 receiver** (`app/n8`). It is the slice N4 hands off to: N8 ingests only
>   RELEASED, hash-verified, non-synthetic packages, then records unit genealogy and characteristic monitors.
>
> The **128 baseline tests** are re-created:
>
> | Files | Tests |
> |---|---|
> | the six N0–N3 baseline files carried over from `athena_n3_integrated` | 84 |
> | `tests/test_n4_metrology.py` | 21 |
> | `tests/test_n4_verification.py` | 23 |
>
> They pass with the integration suites listed under [Tests](#tests).

> **Approval scope.** An N4 verification release means the listed requirements were verified, for the
> stated design revision, by the recorded and calibrated evidence. It feeds the next controlled
> lifecycle step (N8 genealogy and monitoring, N6 / N7 planning). It is **not** product safety
> certification or a regulatory approval, except where an accredited laboratory certificate in the
> package says so for its own scope. Every intake, plan, assessment, review, release and N0 payload
> carries this statement.

```
app/main.py                combined entry point: N0 gateway + N1 + N2 + N3 + N4 + N8 (one DB / store / scanner / audit chain)
app/gateway.py, app/*.py   N0, byte-identical to athena_n0_starter (tests/test_n0_sync.py)
app/n1/                    N1, byte-identical to athena_n1_integrated
app/n2/, app/n3/           N2 + N3, byte-identical to athena_n3_integrated
app/n4/models.py           contracts → schemas/n4_*.schema.json (21 families + 6 adapter / record schemas)
app/n4/metrology.py        SI unit registry, conversion, guard-band judgement, TUR, calibration checks
app/n4/engine.py           traceability, PFMEA attention heuristic / licensed AP, assessment, decision
app/n4/adapters/scpi.py    SCPI/VISA: log parsing, read-only query allowlist, TCP socket + optional PyVISA capture
app/n4/adapters/lab_report.py  accredited-lab PDF + manifest (accreditation, scope, exact PCB revision)
app/n4/adapters/aoi.py     AOI / YOLOv8 inference triage (human adjudication required)
app/n4/adapters/csv_offline.py offline CSV importer (mock-fixture marker)
app/n4/ai.py               opt-in, gated LLM + RAG suggestions (BM25, verbatim citations, no ratings, no approvals)
app/n4/reliability.py      research-only MTBF illustration (χ² lower bound)
app/n4/db.py               20 append-only N4 tables on the shared metadata
app/n4/intake.py           evidence intake: screening, provenance derivation, vault, adjudication
app/n4/service.py          intake lock, invalidation, plans, PFMEA / CAPA, assess, review, release, views
app/n4/router.py           /v1/n4/* API (35 paths)
app/n8/                    minimal N8 receiver: /v1/n8/intake, /units/{serial}, /monitors
examples/run_n4_demo.py    synthetic end-to-end demo (14-entry plan, PFMEA, mock 24 V reading, N8 draft)
migrations/versions/0004   N4 + N8 tables and their append-only triggers
docs/                      architecture, vendor I/O contract, training & vendor plan, backlog
```

## Invariants and where they are enforced

| Invariant | Enforcement |
|---|---|
| **1. Metrology** | Every measurement needs an SI unit (`metrology.unit_info`, otherwise `UNIT_NOT_SI`), a positive expanded uncertainty (`UNCERTAINTY_MISSING`), a registered instrument and a calibration that belongs to it, is not revoked and is in date (`calibration_due > captured_at`, otherwise `CALIBRATION_EXPIRED` / `_MISSING` / `_MISMATCH` / `_REVOKED` / `_NOT_YET_VALID`). The guard band is exact Decimal arithmetic with the band equal to U (`app.rules.guard_band_verdict`). If [v − U, v + U] crosses a limit the result is **INDETERMINATE**, never PASS. For example, 24.48 ± 0.05 V against 24.5 V gives `GUARD_BAND_INDETERMINATE`. N0 recomputes every verdict at release (`VERDICT_MISMATCH`). |
| **2. Hard blocks** | All of these are BLOCK findings, and the decision is BLOCK > REWORK > PASS: FAIL, INDETERMINATE, expired or missing calibration, a missing or unapproved SOP / method revision (`METHOD_SOP_MISSING`), an unmapped or untested safety-critical requirement (`UNMAPPED_SAFETY_CRITICAL`, `NOT_TESTED_SAFETY`, `SAFETY_REQUIRES_PHYSICAL`), and tampered / mismatched evidence (`EVIDENCE_HASH_MISMATCH`, `WRONG_PCB_REVISION`). No score, heuristic or LLM suggestion changes the decision. A BLOCK cannot be approved (`BLOCK_NOT_APPROVABLE`), and N0 BLOCKs the same conditions again (N0-VER-001..016, N0-CAL-001). |
| **3. Honest labelling** | PFMEA prioritisation is labelled `CUSTOM_ATTENTION_HEURISTIC`. The S×O×D product is shown as `rpn_illustrative`. AIAG-VDA Action Priority is used only when `N4_AP_TABLE_PATH` **and** `N4_AP_LICENSE_REF` are configured; otherwise N0 BLOCKs the claim (`STANDARD_OVERCLAIM`). Reliability output is labelled `RESEARCH_ONLY_STATISTICAL_ILLUSTRATION`, and N0 refuses it as a claim (`RELIABILITY_OVERCLAIM`). |
| **4. Synthetic never passes as physical** | Provenance is **derived**, not trusted. A PHYSICAL claim is rejected as `SYNTHETIC_AS_PHYSICAL` when it comes from a non-REAL source, from an instrument whose `*IDN?` says simulated / virtual, or from a file carrying the mock marker. Honest synthetic or mock evidence is kept and labelled, but `SYNTHETIC_NOT_QUALIFICATION` always BLOCKs. A synthetic intake can never be released, and N8 refuses synthetic packages. |
| **5. Separate, qualified approver on the exact hash** | `POST /v1/n4/reviews` must quote the assessment's `evidence_sha256` and design revision exactly. That hash covers the plan, intake, design / PCB revision, every evidence file hash, adjudications, PFMEA and CAPA status. The reviewer must hold `test_authority` or `quality_lead` and a current qualification for every method in the plan (`NOT_QUALIFIED`). The reviewer must also not be an evidence submitter, the plan author or the assessor (`SEPARATION_OF_DUTIES`). Release recomputes the hash and re-hashes every stored blob (`EVIDENCE_CHANGED`, `EVIDENCE_HASH_MISMATCH`). |

## Pipeline

| Stage | What it does | API |
|---|---|---|
| **N4.1 design intake** | Locks the N3 `DESIGN_RELEASE`s for the newest PRD revision into an `N4DesignIntake`. Intake requires: payload bytes re-hashed; the N0 manifest RELEASED to N4 with a verified approval chain; source rights permitting VERIFICATION; and the N3 release not invalidated. Safety-critical requirements are the explicit N2 ids plus the hazard-linked ones. A new N2 or N3 release invalidates the intake and every plan on it (listeners plus a lazy check, giving `INTAKE_INVALIDATED`). | `POST /v1/n4/intakes` |
| **N4.2 test planning** | `N4TestPlan`: each test case has a method (SIM / BENCH_ATE / ACCREDITED_LAB / INSPECTION), acceptance limits, an SOP revision, an owner and a sample size. The traceability matrix reports safety-critical coverage, uncovered requirements and unknown references. | `POST /intakes/{id}/plans`, `GET /plans/{id}/traceability` |
| **N4.3 PFMEA / control plan / CAPA** | Ratings are always set by a human. The attention heuristic, or a licensed AP table, ranks entries. An entry with S ≥ 9 needs a detection control linked to a plan test, and all its actions VERIFIED through a CAPA verified effective by someone other than its owner, with accepted evidence. | `POST /plans/{id}/pfmea`, `/control-plan`, `POST /capas`, `/capas/{id}/verify` |
| **N4.4 evidence intake** | SCPI log or live capture, accredited-lab PDF + manifest, AOI inference + images, offline CSV. Each submission is MIME-screened, malware-scanned, SHA-256-verified and stored write-once in the N0 object store. Rejected submissions are recorded with their hashes and reasons. | `POST /plans/{id}/evidence/{scpi,scpi/live,lab,aoi,csv}`, `POST /adjudications` |
| **N4.5 assessment** | Deterministic metrology, calibration, provenance, coverage, inspection and PFMEA checks produce `N4VerificationAssessment` + findings + `evidence_sha256`. | `POST /plans/{id}/assess` |
| **N4.6 review** | A qualified, separate test authority / quality lead approves the exact hash, recorded in the N0 approval chain. | `POST /v1/n4/reviews` |
| **N4.7 handoff** | `VERIFICATION_RELEASE` → N8 (LIFECYCLE), N6 (COMMERCIAL), N7 (FINANCE). Each target goes through the N0 pipeline as `svc-n4`, then a RELEASE approval and an N0 handoff manifest. The artifact is `VER-{prd}-{target}`, and its revision letter advances only when the payload changes. | `POST /plans/{id}/releases`, `GET /plans/{id}/n8-draft` |

Also available:

- Registries: `POST /methods` (+ `/approve`), `/instruments`, `/calibrations` (+ `/revoke`), `/labs`, `/qualifications`.
- AI and reliability: `POST /corpus`, `/plans/{id}/suggestions`, `/plans/{id}/reliability`.
- Views: `GET /status`, `/subjects/{kind}/{id}/status`, `/aoi/training-labels`.

## Roles (JWT only, no demo API keys)

| Role | May | Implied N0 roles |
|---|---|---|
| `test_engineer` | intake, plans, PFMEA, control plan, CAPA, assess, register instruments / SOPs | producer |
| `lab_importer` | submit evidence and calibration records | — |
| `quality_reviewer` | approve SOP revisions, adjudicate AOI, verify CAPAs | gateway_reviewer |
| `test_authority` | review and release (with a qualification) | gateway_reviewer, release_manager |
| `quality_lead` | everything a test authority may, plus accept labs and grant qualifications (never to themselves) | gateway_reviewer, release_manager |
| `lifecycle_operator` | N8 intake and views | — |

## Hybrid AI

| Component | Status |
|---|---|
| Deterministic policy engine | **ACTIVE**. It makes every decision. |
| LLM + RAG suggestions | **OPT_IN** (`N4_LLM_PROVIDER=anthropic`) and gated as follows. The corpus comes only from sources permitting `LLM_EXTRACTION`, with presidio redaction. Retrieval is BM25. A suggestion needs a verbatim citation of a retrieved chunk. Any S/O/D, RPN, AP or "approve" wording is stripped. A suggestion is only a draft until a human adopts it (`adopted_from_suggestion`). |
| SLM extractor | PLANNED |
| Vision (YOLOv8) release decisions, time-series ML, reliability models | **DEFERRED_GATED**. Today vision is triage only: a human adjudicates every ambiguous or flagged call. Unreviewed labels never become training data, and only double-labelled agreeing human labels are `training_eligible`. |

## Quick start

```bash
pip install -r requirements.txt
python -m pytest -q                        # full N0–N4 + N8 suite
python examples/run_n4_demo.py             # synthetic PSU-120W end to end (no network)
python -m app.schema_export                # regenerate schemas/ (tests fail on drift)
alembic upgrade head                       # N0_DATABASE_URL=postgresql+psycopg://… for PostgreSQL
uvicorn app.main:app                       # needs N0_JWT_SECRET (or N0_JWT_JWKS_URL) and N0_JWT_ISSUER
```

N4 settings come from `N4_*` environment variables (`app/n4/config.py`):

| Variable | Default |
|---|---|
| `N4_MIN_TUR` | 4.0 (a lower TUR is reported as INFO) |
| `N4_MAX_CLOCK_SKEW_S` | 300 |
| `N4_AP_TABLE_PATH`, `N4_AP_LICENSE_REF` | — |
| `N4_AOI_FLAG_THRESHOLD` | 0.85 |
| `N4_AOI_AMBIGUOUS_THRESHOLD` | 0.40 |
| `N4_SCPI_ALLOWED_HOSTS` | 127.0.0.1, localhost |
| `N4_SCPI_TIMEOUT_S` | 10 |
| `N4_LLM_PROVIDER` | disabled |
| `N4_LLM_MODEL` | see `app/n4/config.py` |
| `N4_RAG_TOP_K` | 6 |
| `N4_REQUIRE_QUALIFICATION` | true |
| `N4_MAX_EVIDENCE_FILES` | 200 |

## Tests

226 tests: the 128-test baseline plus 98 integration tests.

| File | What it covers |
|---|---|
| 6 N0–N3 baseline files (84) | unchanged behaviour of the gateway, LLM boundary, N1, N2, N3 CLI adapter and N3 design |
| `test_n4_metrology.py` (21) | units, conversion, guard band incl. `24.48 V ± 0.05 V` vs `24.5 V` → INDETERMINATE, TUR, calibration windows |
| `test_n4_verification.py` (23) | full verified flow, 14/14 PASS, release to N8 / N6 / N7, N8 genealogy and monitors |
| `test_n4_hard_blocks.py` (23) | expired calibration, wrong PCB revision on a lab PDF, unmapped critical hazard, synthetic-as-physical, tampering, FAIL / INDETERMINATE, missing SOP, AOI |
| `test_n4_adapters.py` (11) | SCPI parsing, live capture against a local TCP instrument, query allowlist, lab and AOI checks, CSV |
| `test_n4_pfmea.py` (16) | heuristic labelling, licensed AP, S ≥ 9 controls, CAPA verification |
| `test_n4_ai.py` (8) | opt-in LLM gate, grounding, citation validation, rating stripping, no approvals |
| `test_n4_revision_drift.py` (4) | invalidation on N2 / N3 revisions |
| `test_n4_rbac.py` (20) | role matrix, separation of duties, qualifications, tenants |
| `test_n4_persistence.py` (5) | append-only triggers on all N4 / N8 tables, restart, vault tampering after approval, Alembic, PostgreSQL DDL |
| `test_n4_schemas.py` (4) | 21 schema families, drift, OpenAPI coverage, live records validate |
| `test_n4_e2e.py` (4) | **N0 → N1 → N2 → N3 → N4 → N8** digital thread from TED market evidence to the N8 unit; synthetic never reaches N8; the demo |
| `test_n0_sync.py` (3) | vendored N0 / N1 / N2 / N3 copies are byte-identical to their source repositories |

## What was and was not exercised

- **SCPI/VISA**: only against a local TCP SCPI server in the tests. PyVISA is optional and was not run
  against real instruments.
- **Accredited-lab reports**: only generated PDFs. Real laboratory reports were not available.
- **AOI**: inference records are imported and triaged. No YOLOv8 model is trained or run here.
- **LLM**: tested with a fake provider only. The Anthropic provider is wired but disabled by default.
- **PostgreSQL**: DDL rendering and Alembic were checked on SQLite. No live PostgreSQL server was used.
  S3 and ClamAV come from N0 and were not exercised.
- **Upstream tools**: N3's KiCad stand-ins are inherited (KiCad 8+ was not installed). ngspice runs for
  real when installed.
