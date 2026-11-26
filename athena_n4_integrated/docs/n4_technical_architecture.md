# ATHENA N4 — Technical Architecture

N4 is a FastAPI router (`/v1/n4`) mounted with N0, N1, N2, N3 and a minimal N8 in one process
(`app/main.py`). All of them share:

- one SQLAlchemy metadata and database
- one write-once object store
- one malware scanner
- one JWT verifier
- one hash-chained audit / approval log

N4 never writes N0 tables directly. It submits artifacts through the N0 pipeline as the service
principal `svc-n4`, like any other node.

```
N2 PRD release ──listener──┐                         ┌──────────────── N0 pipeline (N0.1–N0.6)
N3 DESIGN_RELEASE ─listener┤                         │   schema, rights, rules N0-VER-001..016,
                           ▼                         │   approval chain, handoff manifest
                   N4.1 design intake ──► N4.2 plan ─┤
                           │                 │       │
                           │          N4.3 PFMEA / control plan / CAPA
                           ▼                 ▼       │
       SCPI · lab · AOI · CSV ──► N4.4 evidence vault (write-once, SHA-256)
                                             ▼       │
                                  N4.5 deterministic assessment ──► evidence_sha256
                                             ▼       │
                                  N4.6 qualified, separate review (exact hash)
                                             ▼       │
                                  N4.7 VERIFICATION_RELEASE ─┴─► N8 · N6 · N7
```

## 1. Design intake (N4.1)

`N4Service.lock_intake` accepts a PRD id and does the following:

1. It resolves the newest N2 PRD revision released to N4.
2. It finds every N3 `DESIGN_RELEASE` (`DSN-{prd}-{scope}`) released to N4 for that PRD.
3. For each design release, it verifies:
   - the N0 artifact is PASS and not superseded;
   - the stored payload bytes re-hash to the manifest's `payload_sha256`;
   - the approval event recomputes against the approval chain;
   - the PRD is in `source_artifact_ids`;
   - the N3 release carries no `INVALIDATED` event;
   - the source rights permit `VERIFICATION`;
   - the release source (`release_source_id`) permits `LIFECYCLE`.
4. It pins the design revision and PCB revision and copies the simulation corners.
5. It records the requirements with `safety_critical` = the explicit N2 ids ∪ requirements linked to an
   N2 hazard.

The intake is idempotent per PRD revision and design-reference set.

**Invalidation.** Two listeners mark the intake and every plan built on it as `INVALIDATED`:

- N2's `on_prd_release`, for a newer PRD revision;
- N3's `release_listeners`, for a newer design release of the same PRD.

Every later N4 call also re-checks lazily (`_active`): N2 latest revision, N0 latest DSN revisions, and
N3 invalidation events. Work on an invalidated intake returns `409 INTAKE_INVALIDATED`. A release
already made is not rewritten. Its status endpoint reports the invalidation, and N8 refuses to ingest
it (`N4 release status active` check).

## 2. Plan, traceability, PFMEA (N4.2–N4.3)

**Test plan.** `N4TestPlan` is content-hashed (`plan_sha256`) and immutable. A test case holds:

- a method, one of `SIM | BENCH_ATE | ACCREDITED_LAB | INSPECTION`;
- acceptance limits (`characteristic`, SI unit, lower / upper);
- an SOP reference (`method_id` + `revision`);
- an owner and a sample size;
- for SIM only, the corner and the model uncertainty.

**Traceability.** `engine.traceability(plan, intake)` returns the matrix. For each requirement it gives
the covering tests, safety-critical coverage and percentage, the uncovered safety-critical and other
requirements, and unknown references. Requirements derived by N0 (`value_origin = DERIVED`) are not
counted as uncovered.

**PFMEA.**

- Ratings (S, O, D) are always set by the human submitter (`rated_by`).
- `engine.attention` is the **custom attention heuristic**:
  - HIGH if S ≥ 9, or S ≥ 7 & O ≥ 4, or S ≥ 5 & O ≥ 5 & D ≥ 6;
  - MEDIUM if S ≥ 5 & (O ≥ 3 or D ≥ 5);
  - LOW otherwise.
- `rpn_illustrative` = S × O × D is shown for orientation only.
- `ApTable` replaces the heuristic only when both `ap_table_path` and `ap_license_ref` are configured.
  The prioritisation method then becomes `AIAG_VDA_AP_LICENSED` and the licence reference travels with
  the release.

**Safety PFMEA rules.** For S ≥ 9 the entry needs a detection control linked to a plan test
(`PFMEA_SAFETY_DETECTION_UNMAPPED`). Every action must be VERIFIED (`PFMEA_SAFETY_ACTION_OPEN`), and
VERIFIED is accepted only through a CAPA that a person other than its owner verified effective against
accepted evidence (`ACTION_VERIFICATION_UNSUPPORTED`).

## 3. Evidence vault (N4.4)

Every adapter follows the same path (`app/n4/intake.py`):

1. **Source check.** The source must be APPROVED and permit `VERIFICATION`.
2. **Screening.** N0 `validate_upload` checks the name, size and content sniffing. The scanner checks for
   malware. The per-file SHA-256 must match the manifest or log.
3. **Adapter checks.**
   - SCPI: IDN ↔ registered instrument serial / firmware, the query allowlist, overload values, `SYST:ERR?`.
   - Lab: an accepted lab, accreditation scope, the PDF hash, and the exact PCB revision named in both the
     manifest and the PDF text.
   - AOI: image hashes, undeclared images, PCB revision.
   - CSV: columns and the mock marker.
4. **Provenance derivation** (`_derive`). The result is `PHYSICAL_ATE | PHYSICAL_BENCH |
   PHYSICAL_INSPECTION | ACCREDITED_LAB | SIMULATION | SYNTHETIC | MOCK_FIXTURE`. A PHYSICAL claim from
   a non-REAL source, a simulated IDN or a mock file becomes `SYNTHETIC_AS_PHYSICAL`, and the submission
   is rejected.
5. **Persistence.** Accepted files go write-once into the N0 object store (`quarantine/{tenant}/{sha256}`,
   read-only). Measurements go into `n4_measurements`, and the intake record (status, codes, problems,
   hashes) into `n4_evidence`. Rejected submissions are recorded too, with `blob_id = "not-stored"`.

**Hard rejections** keep blocking the assessment until a later ACCEPTED submission from the same
adapter covers the same test ids:

| Rejection | Assessment finding |
|---|---|
| `WRONG_PCB_REVISION` | `WRONG_PCB_REVISION` |
| `SYNTHETIC_AS_PHYSICAL` | `SYNTHETIC_NOT_QUALIFICATION` |
| `SHA256_MISMATCH`, `MALWARE_DETECTED` | `EVIDENCE_HASH_MISMATCH` |

**AOI.** Triage marks a detection *flagged* at confidence ≥ 0.85 and *ambiguous* at ≥ 0.40. The unit
verdict for an INSPECTION count test comes only from an independent human adjudication, never from
the importer (`NOT_ADJUDICABLE`). `/aoi/training-labels` exports only human labels. A label is
`training_eligible` only when two different adjudicators agree.

## 4. Assessment (N4.5)

`engine.assess` is pure and deterministic. It takes the plan, intake, evidence, registry, PFMEA, CAPAs,
the current time, `min_tur` and the clock skew.

For each test case and acceptance limit, every measurement is checked in this order:

1. Convert to the limit's unit (`metrology.convert`; a dimension mismatch gives `DIMENSION_MISMATCH`).
2. Require a positive expanded uncertainty.
3. Judge the guard band (`guard_band_verdict`):
   - **FAIL** if [v − U, v + U] lies entirely outside a limit;
   - **INDETERMINATE** if the band crosses a limit;
   - otherwise **PASS**.
4. TUR (span / 2U) below `min_tur` gives INFO.
5. Calibration (`calibration_problems`). The capture time must not be in the future beyond the skew,
   and the calibration must be valid at `captured_at`. For lab results, the lab's stated calibration due
   date is used.
6. Method ↔ provenance compatibility (`METHOD_PROVENANCE`).
7. Provenance: SYNTHETIC / MOCK_FIXTURE gives `SYNTHETIC_NOT_QUALIFICATION`.
8. A safety-critical requirement requires physical provenance (`SAFETY_REQUIRES_PHYSICAL`).

**Test verdict.** A test is PASS only if it has results, all of them PASS and every check above holds.
Any FAIL makes it FAIL; anything else makes it INDETERMINATE. A mock in-limit reading therefore yields
a PASS *reading* but an INDETERMINATE *test*.

**Decision.** The outcome is BLOCK > REWORK > PASS (`decide`). No component outside `engine.RULES` can
lower an outcome.

**Evidence hash.** `evidence_sha256` = SHA-256 of the canonical JSON of:

- `plan_sha256` and `intake_sha256`;
- the design and PCB revisions;
- the sorted `(evidence_id, evidence_sha256)` pairs;
- the adjudication ids;
- `pfmea_sha256`;
- the CAPA statuses.

## 5. Review and release (N4.6–N4.7)

**Review.** The review must name the newest assessment for the plan (`STALE_ASSESSMENT`) and quote its
`evidence_sha256` and design revision. The review is refused:

- for a BLOCK assessment (`BLOCK_NOT_APPROVABLE`) or an open REWORK (`REWORK_OPEN`);
- when the reviewer contributed to the evidence (`SEPARATION_OF_DUTIES`);
- when the reviewer is unqualified (`NOT_QUALIFIED`).

Approval re-hashes every stored blob. It records an N0 approval event with scope
`EVIDENCE_ATTESTATION` and `exact_revision = evidence_sha256[:32]`.

**Release.** Release recomputes the evidence hash (`EVIDENCE_CHANGED`) and re-hashes the blobs again
(`EVIDENCE_HASH_MISMATCH`). It then builds one `VerificationReleasePayload.v1` per target, submits it
through N0 as `svc-n4`, records a RELEASE approval and creates the N0 handoff manifest.

| Target | Processing purpose |
|---|---|
| N8 | LIFECYCLE |
| N6 | COMMERCIAL |
| N7 | FINANCE |

Release stage is PROTOTYPE, or CONCEPT for synthetic data (which never reaches this point).
`source_artifact_ids` = the DSN references + the PRD.

**What N0 re-checks** (`app.rules.verification_checks`):

- SI units, uncertainty, the guard-band verdict, calibration and provenance per measurement;
- attribute inspections;
- passing physical verification for every safety-critical requirement;
- separation of duties;
- the AP licence and the reliability label;
- design references pinned in `source_artifact_ids`.

**N8 intake** (`app/n8/service.py`) accepts only:

- a `VERIFICATION_RELEASE` that is RELEASED to N8 and not superseded;
- whose payload bytes are hash-verified;
- that is VERIFIED, targets N8, and is non-synthetic with no synthetic measurements;
- that has not been ingested before;
- whose N4 release status is still active.

It then appends unit genealogy (design / PCB revision, tests passed, artifact) and one monitor per
characteristic, with limits.

## 6. Persistence

- 20 N4 tables and 3 N8 tables. Every one is append-only: SQLite and PostgreSQL triggers raise
  `append-only` on UPDATE / DELETE (`migrations/versions/0004`).
- The current state of a subject is the newest record, plus the event history in `n4_events`.
- `alembic upgrade head` / `downgrade 0003` / `check` are tested. `create_all` is used in tests, and
  `alembic check` confirms the models and migrations agree.
- Objects use the N0 store (local, or S3 in production), always written once and keyed by SHA-256.

## 7. Security

- JWT bearer only, issued by the configured issuer / JWKS. Static API keys are disabled.
- Role checks happen per endpoint. Tenant isolation: every lookup is filtered by the principal's
  tenant, and cross-tenant references give 404 / 403.
- Live SCPI capture connects only to `N4_SCPI_ALLOWED_HOSTS` and sends only allowlisted read-only
  queries (`*IDN?`, `SYST:ERR?`, `READ?`, `FETCh?`, `MEASure:…?`). There are no configuration or output
  commands.
- Uploads are size-limited, MIME-sniffed and malware-scanned. File names are normalised and path
  traversal is refused.
