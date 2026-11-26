# ATHENA Vendor & AI I/O Data Quality Gateway

Every vendor artifact, AI/LLM output, CAD/FEA export, supplier quote, financial model and
telemetry event passes the deterministic DQ gateway (`rag/dq_gateway/`) before it is
indexed into Qdrant, updates the Digital Twin, or crosses a node edge (E-001..E-030).

```
RIGHTS (G0) → INGEST (ING-01..12) → S01 feature bundle → S02 risk & anomaly
  → S03 hard policy (DQ01..DQ27) → S04 feasible actions → HUMAN APPROVAL (G0..G8)
  → VERSIONED RELEASE → S05 explain + learning (FB-01..FB-12)
```

## Modules

| File | Role |
|---|---|
| `manifest.py` | 33-field Canonical I-O Manifest (`CanonicalIOManifest`, frozen, `extra="forbid"`), SHA-256 helpers |
| `schemas.py` | Typed `*.v1` payload schemas, critical fields, freshness SLAs, released "facts" |
| `rules.py` | DQ01–DQ27 with severity, action on fail and error code |
| `scorer.py` | Weighted score (0.20 completeness, 0.20 validity, 0.15 consistency, 0.15 provenance, 0.10 freshness, 0.20 traceability) and the gate matrix |
| `handoffs.py` | The 30 edges, gates G0–G8, `HandoffLedger` and the FB-12 cascade |
| `registry.py` | Projects, evidence (NDA, calibration, ECO, waiver, signed tests), approvers, G0 grants, immutable revisions; optional JSON persistence |
| `gateway.py` | `DQGateway.submit / approve / authorize_connector / evaluate_telemetry / stats` |
| `telemetry.py` | `TelemetryGate`: PLC message → `lifecycle_event.v1` → DQ01/03/04/09/10/25 |
| `sources.py` | The exact bytes each ingest path hashes (shared by the pipeline and `dq-manifest`) |
| `builder.py` | `build_manifest(...)`: hashes and lineage computed from real inputs; governance fields have no defaults |
| `demo.py` | SYNTHETIC demo tenant for the Control Tower |

## Invariants and where they are enforced

1. **No Rights, No Access.** The RIGHTS stage runs DQ01 and DQ09 first and stops with BLOCK.
   When it stops, the payload and bytes are not retained. Connector runs (`CON-001..CON-047`)
   need released, ACTIVE, rights-approved artifacts and an active G0 grant. Customer RFQs in the
   lineage must have `consent_scope=VENDOR_ALLOWED`.
2. **Canonical manifest.** The manifest has exactly 33 fields (`MANIFEST_FIELDS`).
   - `output_artifact_uri` is `artifact://<project>/<id>@<rev>`, and revisions are immutable (DQ05).
   - `source_hash` is the lineage hash of the cited sources' registered hashes.
   - `output_file_sha256` is checked against the bytes the consumer actually reads.
3. **Hard controls override the score.** The matrix runs in this order:
   QUARANTINE → BLOCK (including declared `hard_policy_results` of FAIL/UNKNOWN) →
   REWORK (or score < 90) → HOLD (no verified sign-off) → RELEASED.
   - RELEASED is reachable only through `DQGateway.approve`.
   - The approver must be a registered approver holding the node's gate, and must not be the
     submitter (four-eyes).
   - Every rule is re-run at approval time.
   - A `reviewer_id` inside a submission is untrusted: if no recorded approval backs it, DQ13 blocks.
4. **DQ10 origin.** `origin_type` is immutable across revisions.
   - Simulator or `SIMULATED`-sensor data cannot be REAL, and neither can LLM/ML output.
   - Anything derived from synthetic sources cannot be REAL.
   - Only REAL, released data is field evidence (`dq_field_evidence` in Qdrant,
     `TwinSnapshot.is_field_evidence`) or training data (DQ27).
5. **DQ23/DQ26.** P&L arithmetic is recomputed with `Decimal`, and language-model tools are refused.
   A `CALIBRATED_ML` risk score needs an `ml` tool and in-date `CALIBRATION_REPORT` evidence;
   otherwise it is relabelled `HEURISTIC`.
6. **FB-12.** Only RELEASED, ACTIVE revisions can be cited across edges (DQ06 + `HandoffLedger`).
   Releasing a new revision marks the old one SUPERSEDED and, transitively, every consumer that
   pinned it REWORK. Each such consumer gets an FB-12 exception. Citing a superseded revision is
   `REVISION_DRIFT` (DQ08).

## Wiring

- **`rag/pipeline.py`**: `_chunk_and_index_documents` is the only path into Qdrant/BM25. It gates first.
  - `ingest`, `ingest_telemetry`, `ingest_fea`, `ingest_cad` and `ingest_pdf` take a `submission`
    and pass the bytes they read themselves.
  - In enforce mode, only RELEASED artifacts are indexed. In `dq_mode="sandbox"`, HOLD + SYNTHETIC
    artifacts are also indexed.
  - Chunks carry `dq_event_id`, `dq_quality_gate`, `dq_origin_type`, `dq_artifact_ref`,
    `dq_output_sha256`, `dq_rights`, `dq_field_evidence` and `dq_sandbox`.
- **`rag/ingestion/pdf_pipeline.py`**: the PDF and Drive paths go through the same choke point.
  Drive text exports carry no verifiable bytes, so DQ05 quarantines them and they are reported
  as errors.
- **`rag/digital_twin/broker/mqtt_client.py`**:
  - Messages are UTC-aware and carry unit/lot, fixture, sensor, calibration and origin fields.
  - The simulator emits SYNTHETIC/SIMULATED.
  - Live streams take the operator-declared origin from `BrokerConfig.origin_type`; unknown keys
    are ignored, and parse errors are recorded instead of dropped.
- **`rag/digital_twin/twin.py`**: `ingest_plc_cycle(messages, gate)` predicts only if every
  A/B/C message passes.
  - Readings are never back-filled, and `fixture_id` comes from the message.
  - The snapshot is REAL only if every input was REAL.
  - `assemble_cycles` groups streamed messages by `unit_id`.
- **`api/gateway_router.py`**: A01, A03, A04, A06, A08, A09 and A21, with bearer auth (tenant and
  actor never taken from the body), a required `Idempotency-Key`, server-side SHA-256
  (`X-Content-SHA256`) and HMAC-signed vendor callbacks with nonce replay protection.
  Run it with `uvicorn api.gateway_router:create_app_from_env --factory`.
- **`dashboard/app.py`** has a "DQ Control Tower" tab (`dashboard/components/dq_control_tower.py`)
  showing:
  - gate counts;
  - the N1–N8 scorecard;
  - open FB exceptions;
  - telemetry gate stats;
  - a one-click HOLD → RELEASED approval form, which calls `DQGateway.approve`.
- **`main.py`**: every ingest command takes `--submission`, `--tenant`, `--dq-registry`,
  `--dq-sandbox`, `--actor` and `--approve-as`.
  - `dq-manifest` builds a manifest over the exact bytes the command will read.
  - `dq-seed-demo` seeds the demo tenant.
  - The old `ingest-drive` bypass (`_vector_store.add_chunks`) is gone.

```bash
python main.py dq-seed-demo
python main.py dq-manifest data/aviator/tester_log --kind telemetry_log --project AVIATOR-DEMO \
  --node X --artifact-id NEXTEST-LOGS --rev 1 --rights APPROVED --classification CONFIDENTIAL \
  --origin REAL --tool tool:nextest@1.0 --evidence EV-NDA-DEMO --out /tmp/nextest.sub.json
python main.py ingest-telemetry data/aviator/tester_log --submission /tmp/nextest.sub.json \
  --tenant demo --approve-as demo.qa-lead
```

## Decisions where the specification was ambiguous or conflicting

- **Root artifacts.** DQ06 requires non-empty `source_artifact_ids`, so root artifacts cite
  their G0 grant (`G0-<project>@n`).
- **DQ13 vs pending approval.** DQ13 (BLOCK) fails only on *unverified* approval claims.
  A side effect that is merely awaiting sign-off gives HOLD; blocking it would deadlock every approval.
- **Missing fields.** A missing payload field is DQ03 (REWORK, "no silent defaults"), not DQ02.
  DQ02 QUARANTINE is for type and format violations.
- **T10 action.** T10's wording says "quarantine", but DQ10's specified action is BLOCK.
  The rule table is followed: BLOCK.
- **FB codes.** Only FB-12 is defined by the spec. FB-01..FB-11 group the DQ rules by theme
  (see `FB_CATALOG`).
- **Approval gates.** Edge gates are recorded per handoff. The approval that releases an artifact
  uses the producing node's gate (`NODE_APPROVAL_GATE`).
- **Edge exemptions.** Intra-node citations and the shared layer X need no edge.
- **Telemetry approval.** Station telemetry uses the per-message rule subset
  (DQ01/03/04/09/10/25) with no human gate. A21 lifecycle events go through the full pipeline
  (N8, G7).
- **Transport.** `ConnectorMode` has no MQTT/OPCUA/webhook values, so the transport is a separate
  submission field used for the ING-xx route.
- **Tenancy.** The manifest has no `tenant_id`. Tenancy comes from project → tenant in the
  registry plus the authenticated caller.
- **Artifact ID namespace.** Artifact IDs form one namespace: an ID registered to another
  project is refused (DQ09), never aliased.
- **Approval across processes.** An approval recorded in a shared registry lets another process
  (for example the CLI) re-present the same released bytes without a second sign-off. Rules are
  still re-run.

## Known limits

- **In-memory state.** Gate decisions, the idempotency store and nonces are in memory.
  The registry can be persisted to JSON. A production deployment needs a database for all of them.
- **Physics models.** `rag/digital_twin/physics/pinn.py` and `stress_model.py` are not called by
  any code path; the node models embed their own physics. The gate sits in front of the node
  predictors, so it also covers any future caller that goes through `ingest_plc_cycle`.
- **Athena run summaries.** `athena/orchestrator.py` → `ingest_athena_run` has no manifest for
  run summaries yet, so the gateway rejects it and the error is recorded on the run.
