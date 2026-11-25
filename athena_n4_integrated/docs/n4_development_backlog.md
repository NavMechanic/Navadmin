# ATHENA N4 — Development Backlog

Status at this revision:

- **Done.** Implemented and covered by tests in this repository.
- **Open.** Not implemented, or implemented but not exercised against the real system.

## Done

| Id | Item |
|---|---|
| N4-D01 | Design intake from N3 `DESIGN_RELEASE`. It checks the N0 manifest, payload re-hash and approval chain, pins the design / PCB revision, and takes safety-critical requirements from N2 hazards. It invalidates on new N2 / N3 releases (listeners + lazy check). |
| N4-D02 | Test plan with four methods, SOP revision, owner and sample size. Traceability matrix with 100 % safety-critical coverage, enforced as a BLOCK. |
| N4-D03 | Metrology: SI registry, conversion, Decimal guard band (INDETERMINATE when crossing), TUR, calibration windows and revocation. N0 recomputes it at release. |
| N4-D04 | PFMEA with human ratings, the custom attention heuristic (labelled), licensed-AP hook, safety detection mapping, and CAPA-verified actions with separation of duties. Control plan. |
| N4-D05 | Evidence adapters: SCPI log + live TCP capture (read-only allowlist, host allowlist), accredited-lab PDF + manifest (accreditation, scope, exact PCB revision), AOI triage + human adjudication + training-label export, offline CSV with mock marker. |
| N4-D06 | Provenance derivation, `SYNTHETIC_AS_PHYSICAL`, hard rejections that persist until superseded. |
| N4-D07 | Evidence hash, qualified and separated review on the exact hash, release re-hash, N0 `VERIFICATION_RELEASE` to N8 / N6 / N7. |
| N4-D08 | Minimal N8 receiver: genealogy and monitors only from released, non-synthetic packages. |
| N4-D09 | JWT RBAC with six N4 roles, tenant isolation, no demo keys. |
| N4-D10 | 20 append-only N4 tables + 3 N8 tables, Alembic 0004 with triggers, PostgreSQL DDL. |
| N4-D11 | Opt-in, gated LLM + RAG suggestions with verbatim citations and rating stripping. Research-only MTBF illustration. |
| N4-D12 | 21 JSON Schema families + adapter schemas + combined OpenAPI, drift-checked. |
| N4-D13 | End-to-end N0 → N1 → N2 → N3 → N4 → N8 test and synthetic demo. |

## Open — priority order

| Id | Priority | Item | Notes |
|---|---|---|---|
| N4-B01 | P0 | **Real instrument validation.** Run `PyVisaTransport` against the bench DMM / power analyser / scope over VISA (USB-TMC, LXI). Record IDN formats per model and confirm timeouts and `SYST:ERR?` handling. | So far exercised only against a local TCP SCPI server. |
| N4-B02 | P0 | **Real accredited-lab reports.** Collect 3–5 real reports per lab. Tune `lab_report.revisions_named` (PCB revision phrasing, multi-page tables) and add signed-PDF signature verification (PAdES). | Only generated PDFs were seen. The PCB-revision regex may need lab-specific phrasing. |
| N4-B03 | P0 | **PostgreSQL + S3 + ClamAV in staging.** `alembic upgrade head` on PostgreSQL 16, trigger behaviour under concurrent writers, the S3 object-lock (WORM) bucket for the evidence vault, and a ClamAV daemon. | DDL renders and SQLite migrations pass. No live server was used. |
| N4-B04 | P1 | Licensed AIAG-VDA AP table: obtain the licence, load the table through `N4_AP_TABLE_PATH`, and validate it against the handbook examples. | The heuristic stays the default. |
| N4-B05 | P1 | Statistical sampling plans (ISO 2859-1 / c=0 plans) to derive `sample_size` and accept / reject numbers for production lots. | Currently a per-test integer. |
| N4-B06 | P1 | Measurement system analysis (gauge R&R) records linked to instruments. Require them for special characteristics. | |
| N4-B07 | P1 | LLM suggestion evaluation set: grounding rate, citation validity, forbidden-content rate. Run it before enabling any tenant. | Only a fake provider was tested. |
| N4-B08 | P1 | N8 beyond the minimal receiver: field returns, monitor alarms, closed-loop CAPA back to N4. | `app/n8` is intentionally minimal. |
| N4-B09 | P2 | SLM extractor for lab-report tables (behind the N0 extraction boundary), with human confirmation. | PLANNED |
| N4-B10 | P2 | AOI model training pipeline outside N4, fed only by `training_eligible` labels, with frozen hold-outs. | DEFERRED_GATED |
| N4-B11 | P2 | Time-series anomaly detection on ATE trends and burn-in. Its findings can only raise, never clear. | DEFERRED_GATED |
| N4-B12 | P2 | Reliability models (Weibull, accelerated life with Arrhenius / Peck). Remain labelled research-only until N8 field data and statistician review exist. | DEFERRED_GATED |
| N4-B13 | P2 | Operator UI for adjudication, review (evidence-hash diff view) and the traceability matrix. | API only today. |
| N4-B14 | P3 | Upstream: real KiCad 8+ in the N3 sandbox (the N3 stand-ins are inherited here). | ngspice already runs for real. |
