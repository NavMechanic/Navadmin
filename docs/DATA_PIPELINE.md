# ATHENA N0–N8 data acquisition, staging & validation pipeline

`data_pipeline/` fetches, snapshots, verifies and formats the 75 sources `SRC-001`–`SRC-075` across
nodes N0–N8. Every artifact is policy-gated and gets an immutable provenance sidecar. Datasets are
stored in five isolated partitions, separate from the production Qdrant store (`rag/vectorstore/`).

> **Catalog status: reconstructed.** The reference workbook
> `ATHENA_N0_N8_Pretraining_Data_Sources_and_Quality_Gateway_2026.xlsx` was not available.
> - **Fixed by the build specification:** the IDs and guardrails of SRC-011/013/014/027/038/039/041/045/046/047/049/050/054/055/059/060/064/067–070 (`catalog_status: PROMPT_SPECIFIED`).
> - **Best-effort reconstruction:** every other entry, all URLs and licenses, the 12 controls and the 54 DQ cases.
> - **Data steward action:** verify each entry and set `catalog_status: VERIFIED`.
>
> The hard guardrails don't depend on that review: they are hard-coded in `policy_gates.py`.

```bash
python -m data_pipeline.cli --nodes N0..N8 --priority P0 --mode all          # fetch → gate → format
python -m data_pipeline.cli --nodes N0..N8 --priority all --action plan       # no network, no writes
python -m data_pipeline.cli --action init                                     # create drop zones + vault stubs
python -m data_pipeline.cli --action report                                   # HC-01..HC-12 over the whole lake
python -m data_pipeline.cli --sources SRC-036 --action approve --steward steward.n4-quality \
       --sha256 <artifact sha256> --decision APPROVED --rationale "MIT licence verified"
python -m data_pipeline.cli --sources SRC-036 --action export --bundle deeppcb-v1
```

## Pipeline

```
registry (config/source_registry.yaml, validated against hard-coded guardrails)
  → fetcher_for(spec)        git | http_archive | api | drop_zone | vault  (only the adapter its access allows)
  → preflight                HTTPS/TLS, no internal:// over HTTP, drop-zone-only, credentials present
  → acquire into _staging/   retry + Retry-After, per-host rate limit, size cap, MIME sniff, safe extraction
  → provenance               SHA-256 (file or tree), license snapshot, provenance_manifest.json (write-once),
                             prov.jsonld (W3C PROV-O), spdx.json (SPDX 2.3), cyclonedx.json (CycloneDX 1.6)
  → promote                  <partition>/<node>/<SRC>/<version>__<sha12>/   (content-addressed, never overwritten)
  → evaluate_artifact        HC-01, 02, 04–11 → failing artifacts move to _rejected/
  → formatters               output in <artifact>/formatted/ (same partition) + record_index.json
  → export (optional)        evaluate_training_export adds HC-03 (steward ledger) and HC-12 (leakage)
```

## Partitions (mode isolation, HC-02)

| Partition | Intended use | Training |
|---|---|---|
| `data_lake/train_candidates/<node>/` | TRAIN-CANDIDATE | export only with a steward-ledger `APPROVED` decision for that exact SHA-256 |
| `data_lake/rag_reference/<node>/` | RAG/REFERENCE, LIVE-LOOKUP (`live_cache/`) | never |
| `data_lake/eval_benchmarks/<node>/` | EVAL/BENCHMARK | never |
| `data_lake/eval_noncommercial/<node>/` | NC / unasserted licenses (MVTec AD & AD 2, Fusion 360 Gallery, PKU-Market-PCB) | never |
| `data_lake/private_ground_truth/<node>/` | the 15 `internal://athena/*` vaults + synthetic fixtures | never via this pipeline |

Isolation is enforced in four places:

- Every write path is resolved through symlinks and must stay inside its partition.
- Node and source components are validated, and version tags are sanitized.
- `DataLake` refuses a root that overlaps `rag/vectorstore`, `.qdrant_db` or `data/qdrant_db`.
- Builders write only inside the source artifact's own partition.

## Hard-coded legal guardrails

| Guardrail | Sources | Enforcement |
|---|---|---|
| Non-commercial hard block | SRC-038 MVTec AD, SRC-039 MVTec AD 2 (`CC-BY-NC-SA-4.0`) | `commercial_training_allowed = False` is hard-coded; EVAL/BENCHMARK at N4 only → `eval_noncommercial/N4`; YOLO output is eval-only (`train: null`). Because the MVTec download page requires accepting the license, it is a manual drop-zone intake. |
| No scraping (manual drop zones) | SRC-013 / SRC-055 CPPP, SRC-059 GeM, SRC-027 IEC/UL/IS, SRC-050 TI/ST/Infineon datasheets | `DropZoneIntake` has no HTTP client. Each file needs a `<file>.entitlement.json` with matching SHA-256, source, steward, validity window and permitted use; `training_permitted` must be `false`. |
| API-key live lookups | SRC-011/049 Comtrade, SRC-014/054 SAM.gov, SRC-045/064 Nexar, SRC-046 DigiKey, SRC-047 Mouser, SRC-060 FRED | Keys come only from env vars (`COMTRADE_API_KEY`, `SAM_GOV_API_KEY`, `NEXAR_CLIENT_ID/SECRET`, `DIGIKEY_CLIENT_ID/SECRET`, `MOUSER_API_KEY`, `FRED_API_KEY`). A missing key means no request at all. Answers are cached under `live_cache/` within their TTL, a stale entry is never served, and everything is tagged `training_allowed = False`. Keys are redacted from stored bodies, and the manifest writer refuses any text that contains a key. |
| Domain boundary | SRC-041, SRC-067, SRC-068, SRC-070 (NASA PCoE / C-MAPSS / Battery), SRC-069 (Paderborn) | `domain_transfer_warning = BENCHMARK_ONLY_NOT_ELECTRONICS_PROOF` is required in the registry and the manifest. These sources are never exported to training. |
| Private ground truth | 15 `internal://athena/*` vaults | `VaultStubFetcher` has no HTTP client. It creates a 0700 drop zone with `schema.json` exported from the Pydantic intake model, and validates every record. |

## The 12 Hard Dataset & Model Promotion Controls (reconstructed)

| Control | Rule |
|---|---|
| HC-01 | Provenance completeness: the sidecar manifest has all required fields, a license snapshot and an SPDX id, all matching the registry |
| HC-02 | Mode isolation: one partition per intended use; nothing outside `data_lake/` or inside `rag/vectorstore` |
| HC-03 | Training rights: export only for TRAIN-CANDIDATE with a steward-ledger APPROVED decision; the manifest's own field is informational |
| HC-04 | Non-commercial block: NC and unasserted licenses stay in `eval_noncommercial` and are never trained on |
| HC-05 | No scraping: portals and paid content come in only through a manual drop zone with hash + entitlement |
| HC-06 | Live lookups: env-var credentials, TTL cache, fail closed, `training_allowed = False` |
| HC-07 | Domain boundary: benchmark-only datasets are never electronics field proof or training data |
| HC-08 | Private ground truth: no network fetch for `internal://`; schema-validated vault intake |
| HC-09 | Synthetic isolation: SYNTHETIC_TEST_CASE data is never exported or relabelled |
| HC-10 | Integrity & immutability: SHA-256 verified on every gate; manifests are write-once; the ledger is hash-chained |
| HC-11 | Transport safety: HTTPS + TLS verification, MIME sniffing, size caps, zip-slip/symlink/zip-bomb-safe extraction |
| HC-12 | Leakage: non-overlapping temporal or group splits; no evaluation record inside a training export |

The training-rights ledger is `data_lake/_governance/training_rights_ledger.jsonl`. It is append-only
and hash-chained, and only the source's registered `data_steward` can record a decision. The latest
decision for (source, SHA-256) is the one that applies.

## Provenance manifest

`provenance_manifest.json` has the 12 required fields:

- `source_id`, `node_id`, `intended_use`, `official_url`
- `fetched_at_utc`
- `license_snapshot_text`, `license_spdx_id`
- `sha256`, `version_tag`
- `origin_type` (`PUBLIC_OPEN | LIVE_API | PRIVATE_GROUND_TRUTH | SYNTHETIC_TEST_CASE`)
- `training_rights_approval` (always `NOT_REVIEWED` at fetch)
- `data_steward`

It also declares these extras:

- `partition`, `fetch_method`, `artifact_files[]`
- `license_snapshot_sha256`, `license_source`
- `commercial_training_allowed`, `training_allowed`, `domain_transfer_warning`
- `entitlement`, `transport.tls_verified`
- `cache_expires_at_utc`, `query_key`, `redactions`

The model is frozen with `extra="forbid"`, so any other field fails validation.

How the license snapshot is chosen:

1. The repo `LICENSE` file, if there is one.
2. Otherwise the license page, which must return HTTP 200.
3. Otherwise `license_text` from the registry.
4. Otherwise the fetch fails (HC-01).

For drop zones and vaults the snapshot comes from the entitlement or the vault policy text instead.

## Formatters

| Builder | Nodes | Output |
|---|---|---|
| `slm_jsonl_builder` | N0, N1, N2, N5, N6 (+ N8 text) | `slm.jsonl`: ChatML pairs whose assistant answer validates against an embedded Draft 2020-12 `response_schema` (`additionalProperties: false`). Answers are derived deterministically (mapped fields, jsonschema verdicts, modal-verb regex), with `null` for anything absent. Also `pretrain.jsonl` for PDFs and `rag_chunks.jsonl` (with citation, `training_allowed: false`) for reference and live sources. |
| `eda_physics_builder` | N3 | `feature_pack.jsonl/.parquet` built from KiCad symbols (pins, types, footprint links), footprints (pads, pitch, courtyard), STEP models (schema, solids, bbox in m) and `.npy/.npz/.h5` arrays (CircuitNet maps, PDEBench fields) |
| `vision_yolo_builder` | N4 | YOLOv8 `images/`, `labels/`, `data.yaml`. Supports DeepPCB (its own trainval/test lists), MVTec AD/AD 2 (mask → connected-component boxes, eval-only), VisA and Pascal VOC. |
| `timeseries_builder` | N7, N8 | Parquet with SI units (shared registry plus exact °R/psia/rpm/lbm/s/Ah/MW conversions) and UTC timestamps (naive only with a declared `source_timezone`). Splits are contiguous time ranges, or whole-entity group splits for run-to-failure data, checked by HC-12. Reference and live data produce a single `series.parquet`. |

## Verified in this environment (live network)

These sources were staged and formatted over the real network:

- SRC-001 JSON Schema: 42 schema-validation pairs.
- SRC-002 PROV-O and SRC-003 SPDX license list: RAG chunks.
- SRC-005 NIST SP 330: 236 pretraining chunks.
- SRC-008 OpenAlex and SRC-012 USAspending: 50 pairs each.
- SRC-010 TED: 50 pairs.
- SRC-016 NASA SE Handbook: 578 pretraining chunks.
- SRC-018 PROMISE NFR (eval): 625 classification pairs.
- SRC-021 eCFR Part 15: 597 RAG chunks.
- SRC-036 DeepPCB: git clone → 1,500 images (800 train / 200 val / 500 test), 10,013 boxes; steward-approved and exported.
- SRC-048 OSV, SRC-053 OCDS, SRC-057 World Bank.
- SRC-063 OPSD: 50,400 hourly rows, split train/val/test without overlap.
- SRC-071 NHTSA: 680 pairs.

Key-gated sources were correctly refused without making any request, because no keys are configured.

## Known gaps

- **License pages:** ECHA (SRC-020), Nexar (SRC-045/064) and DigiKey (SRC-046) block automated access or have moved. Those sources fail closed on HC-01 until a steward supplies the terms text.
- **India portals:** CPPP and GeM were unreachable from this environment. They are manual intake anyway.
- **MATLAB files:** `.mat` sources (NASA PCoE, Paderborn) need `scipy` and a per-dataset adapter. The builder refuses them rather than guessing a layout.
- **Separately distributed data:** the CircuitNet and PDEBench repositories carry code; their dataset shards are distributed separately.
- **OpenAlex paging:** OpenAlex needs a free key (`OPENALEX_API_KEY`, optional) for cursor paging; page paging is used without one.

## Sources (as reconstructed)

| ID | Node | Source | Intended use | Partition | Access | Priority | License | Status |
|---|---|---|---|---|---|---|---|---|
| SRC-001 | N0 | JSON Schema 2020-12 meta-schemas | TRAIN-CANDIDATE | train_candidates | http_archive | P0 | `AFL-3.0` | RECONSTRUCTED |
| SRC-002 | N0 | W3C PROV-O ontology | RAG/REFERENCE | rag_reference | http_archive | P1 | `W3C-20150513` | RECONSTRUCTED |
| SRC-003 | N0 | SPDX License List data | RAG/REFERENCE | rag_reference | http_archive | P0 | `CC-BY-3.0` | RECONSTRUCTED |
| SRC-004 | N0 | CycloneDX specification | RAG/REFERENCE | rag_reference | git | P1 | `Apache-2.0` | RECONSTRUCTED |
| SRC-005 | N0 | NIST SP 330 — The International System of Units (SI) | TRAIN-CANDIDATE | train_candidates | http_archive | P0 | `LicenseRef-PD-USGov` | RECONSTRUCTED |
| SRC-006 | N0 | NIST AI Risk Management Framework 1.0 (NIST AI 100-1) | RAG/REFERENCE | rag_reference | http_archive | P1 | `LicenseRef-PD-USGov` | RECONSTRUCTED |
| SRC-007 | N0 | ATHENA data-steward decision vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P0 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-008 | N1 | OpenAlex works (scholarly & technology signals) | TRAIN-CANDIDATE | train_candidates | api | P1 | `CC0-1.0` | RECONSTRUCTED |
| SRC-009 | N1 | GDELT 2.0 event database (latest update) | RAG/REFERENCE | rag_reference | http_archive | P2 | `LicenseRef-GDELT-Terms` | RECONSTRUCTED |
| SRC-010 | N1 | TED — Tenders Electronic Daily notices (EU) | TRAIN-CANDIDATE | train_candidates | api | P1 | `CC-BY-4.0` | RECONSTRUCTED |
| SRC-011 | N1 | UN Comtrade — HS 85 electronics trade flows | LIVE-LOOKUP | rag_reference | api | P0 | `LicenseRef-UN-Comtrade-Terms` | PROMPT_SPECIFIED |
| SRC-012 | N1 | USAspending.gov federal awards | TRAIN-CANDIDATE | train_candidates | api | P2 | `LicenseRef-PD-USGov` | RECONSTRUCTED |
| SRC-013 | N1 | India CPPP — Central Public Procurement Portal (manual intake) | RAG/REFERENCE | rag_reference | drop_zone | P1 | `LicenseRef-GoI-CPPP-Terms` | PROMPT_SPECIFIED |
| SRC-014 | N1 | SAM.gov contract opportunities | LIVE-LOOKUP | rag_reference | api | P1 | `LicenseRef-SAM-gov-Terms` | PROMPT_SPECIFIED |
| SRC-015 | N1 | ATHENA RFQ win/loss history vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P0 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-016 | N2 | NASA Systems Engineering Handbook (SP-2016-6105 Rev 2) | TRAIN-CANDIDATE | train_candidates | http_archive | P0 | `LicenseRef-PD-USGov` | RECONSTRUCTED |
| SRC-017 | N2 | PURE — public requirements documents dataset | TRAIN-CANDIDATE | train_candidates | http_archive | P1 | `CC-BY-4.0` | RECONSTRUCTED |
| SRC-018 | N2 | PROMISE NFR requirements classification benchmark | EVAL/BENCHMARK | eval_benchmarks | http_archive | P1 | `CC-BY-4.0` | RECONSTRUCTED |
| SRC-019 | N2 | EUR-Lex — RoHS Directive 2011/65/EU (consolidated) | RAG/REFERENCE | rag_reference | http_archive | P0 | `CC-BY-4.0` | RECONSTRUCTED |
| SRC-020 | N2 | ECHA REACH SVHC Candidate List | RAG/REFERENCE | rag_reference | http_archive | P1 | `LicenseRef-ECHA-Legal-Notice` | RECONSTRUCTED |
| SRC-021 | N2 | eCFR 47 CFR Part 15 — radio frequency devices | RAG/REFERENCE | rag_reference | api | P1 | `LicenseRef-PD-USGov` | RECONSTRUCTED |
| SRC-022 | N2 | ATHENA approved PRD baselines vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P0 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-023 | N3 | KiCad symbol libraries | TRAIN-CANDIDATE | train_candidates | git | P0 | `CC-BY-SA-4.0 WITH KiCad-libraries-exception` | RECONSTRUCTED |
| SRC-024 | N3 | KiCad footprint libraries | TRAIN-CANDIDATE | train_candidates | git | P0 | `CC-BY-SA-4.0 WITH KiCad-libraries-exception` | RECONSTRUCTED |
| SRC-025 | N3 | KiCad 3D packages (STEP) | TRAIN-CANDIDATE | train_candidates | git | P2 | `CC-BY-SA-4.0 WITH KiCad-libraries-exception` | RECONSTRUCTED |
| SRC-026 | N3 | OpenROAD (RTL-to-GDS flow, test designs) | TRAIN-CANDIDATE | train_candidates | git | P1 | `BSD-3-Clause` | RECONSTRUCTED |
| SRC-027 | N3 | IEC / UL / BIS IS safety & EMC standards (licensed copies, manual intake) | RAG/REFERENCE | rag_reference | drop_zone | P0 | `LicenseRef-Paid-Standard-Single-User` | PROMPT_SPECIFIED |
| SRC-028 | N3 | CircuitNet (EDA congestion / DRC / IR-drop prediction) | TRAIN-CANDIDATE | train_candidates | git | P1 | `BSD-3-Clause` | RECONSTRUCTED |
| SRC-029 | N3 | PDEBench (scientific ML benchmark — physics fields) | TRAIN-CANDIDATE | train_candidates | git | P2 | `CC-BY-4.0` | RECONSTRUCTED |
| SRC-030 | N3 | Fusion 360 Gallery dataset (CAD reconstruction / segmentation) | EVAL/BENCHMARK | eval_noncommercial | git | P2 | `LicenseRef-Autodesk-Fusion360-Gallery` | RECONSTRUCTED |
| SRC-031 | N3 | NIST AM-Bench additive manufacturing benchmarks | EVAL/BENCHMARK | eval_benchmarks | http_archive | P2 | `LicenseRef-PD-USGov` | RECONSTRUCTED |
| SRC-032 | N3 | SkyWater SKY130 open PDK | TRAIN-CANDIDATE | train_candidates | git | P2 | `Apache-2.0` | RECONSTRUCTED |
| SRC-033 | N3 | ATHENA released design packages vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P0 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-034 | N3 | ATHENA DRC/ERC waiver log vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P1 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-035 | N3 | EPFL combinational logic benchmark suite | EVAL/BENCHMARK | eval_benchmarks | git | P2 | `MIT` | RECONSTRUCTED |
| SRC-036 | N4 | DeepPCB defect detection dataset | TRAIN-CANDIDATE | train_candidates | git | P0 | `MIT` | RECONSTRUCTED |
| SRC-037 | N4 | PKU-Market-PCB defect dataset | EVAL/BENCHMARK | eval_noncommercial | http_archive | P2 | `NOASSERTION` | RECONSTRUCTED |
| SRC-038 | N4 | MVTec AD (anomaly detection) | EVAL/BENCHMARK | eval_noncommercial | drop_zone | P1 | `CC-BY-NC-SA-4.0` | PROMPT_SPECIFIED |
| SRC-039 | N4 | MVTec AD 2 (anomaly detection) | EVAL/BENCHMARK | eval_noncommercial | drop_zone | P1 | `CC-BY-NC-SA-4.0` | PROMPT_SPECIFIED |
| SRC-040 | N4 | VisA — Visual Anomaly dataset (incl. PCB1–PCB4) | EVAL/BENCHMARK | eval_benchmarks | http_archive | P2 | `CC-BY-4.0` | RECONSTRUCTED |
| SRC-041 | N4 | NASA PCoE — IGBT / MOSFET accelerated aging | EVAL/BENCHMARK | eval_benchmarks | http_archive | P1 | `LicenseRef-PD-USGov` | PROMPT_SPECIFIED |
| SRC-042 | N4 | ATHENA AOI defect label vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P0 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-043 | N4 | ATHENA ICT/FCT test-station log vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P0 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-044 | N5 | Open Supply Hub facility data | RAG/REFERENCE | rag_reference | http_archive | P2 | `CC-BY-SA-4.0` | RECONSTRUCTED |
| SRC-045 | N5 | Nexar (Octopart) part search — live lookup | LIVE-LOOKUP | rag_reference | api | P0 | `LicenseRef-Nexar-API-Terms` | PROMPT_SPECIFIED |
| SRC-046 | N5 | DigiKey Product Information API v4 — live lookup | LIVE-LOOKUP | rag_reference | api | P0 | `LicenseRef-DigiKey-API-Terms` | PROMPT_SPECIFIED |
| SRC-047 | N5 | Mouser Search API — live lookup | LIVE-LOOKUP | rag_reference | api | P1 | `LicenseRef-Mouser-API-Terms` | PROMPT_SPECIFIED |
| SRC-048 | N5 | OSV.dev open-source vulnerability database (firmware supply chain) | TRAIN-CANDIDATE | train_candidates | api | P1 | `CC-BY-4.0` | RECONSTRUCTED |
| SRC-049 | N5 | UN Comtrade — component import/export flows (sourcing) | LIVE-LOOKUP | rag_reference | api | P1 | `LicenseRef-UN-Comtrade-Terms` | PROMPT_SPECIFIED |
| SRC-050 | N5 | TI / ST / Infineon component datasheets (manual intake) | RAG/REFERENCE | rag_reference | drop_zone | P0 | `LicenseRef-Vendor-Datasheet-Copyright` | PROMPT_SPECIFIED |
| SRC-051 | N5 | ATHENA supplier quote history vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P0 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-052 | N5 | ATHENA AVL & ECO register vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P1 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-053 | N6 | Open Contracting Data Standard — releases (data registry) | TRAIN-CANDIDATE | train_candidates | api | P1 | `Apache-2.0` | RECONSTRUCTED |
| SRC-054 | N6 | SAM.gov contract opportunities (bid pipeline) | LIVE-LOOKUP | rag_reference | api | P0 | `LicenseRef-SAM-gov-Terms` | PROMPT_SPECIFIED |
| SRC-055 | N6 | India CPPP — tender documents for bids (manual intake) | RAG/REFERENCE | rag_reference | drop_zone | P0 | `LicenseRef-GoI-CPPP-Terms` | PROMPT_SPECIFIED |
| SRC-056 | N6 | TED — contract award notices (bid benchmarking) | TRAIN-CANDIDATE | train_candidates | api | P1 | `CC-BY-4.0` | RECONSTRUCTED |
| SRC-057 | N6 | World Bank procurement notices | TRAIN-CANDIDATE | train_candidates | api | P2 | `CC-BY-4.0` | RECONSTRUCTED |
| SRC-058 | N6 | ATHENA bid outcomes vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P0 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-059 | N6 | GeM — Government e-Marketplace India (manual intake) | RAG/REFERENCE | rag_reference | drop_zone | P1 | `LicenseRef-GeM-Terms` | PROMPT_SPECIFIED |
| SRC-060 | N7 | FRED — Federal Reserve Economic Data (rates, PPI) — live lookup | LIVE-LOOKUP | rag_reference | api | P0 | `LicenseRef-FRED-Terms` | PROMPT_SPECIFIED |
| SRC-061 | N7 | BLS Producer Price Index — electronic components | TRAIN-CANDIDATE | train_candidates | api | P1 | `LicenseRef-PD-USGov` | RECONSTRUCTED |
| SRC-062 | N7 | Reserve Bank of India — reference & policy rates (DBIE) | RAG/REFERENCE | rag_reference | http_archive | P1 | `LicenseRef-RBI-Terms` | RECONSTRUCTED |
| SRC-063 | N7 | Open Power System Data — time series (energy load & generation) | TRAIN-CANDIDATE | train_candidates | http_archive | P2 | `CC-BY-4.0` | RECONSTRUCTED |
| SRC-064 | N7 | Nexar — part price history for cost models (live lookup) | LIVE-LOOKUP | rag_reference | api | P1 | `LicenseRef-Nexar-API-Terms` | PROMPT_SPECIFIED |
| SRC-065 | N7 | ATHENA project actuals (P&L) vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P0 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-066 | N7 | ATHENA cash-flow ledger vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P1 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-067 | N8 | NASA PCoE — capacitor electrical stress aging | EVAL/BENCHMARK | eval_benchmarks | http_archive | P1 | `LicenseRef-PD-USGov` | PROMPT_SPECIFIED |
| SRC-068 | N8 | NASA C-MAPSS turbofan engine degradation | EVAL/BENCHMARK | eval_benchmarks | http_archive | P1 | `LicenseRef-PD-USGov` | PROMPT_SPECIFIED |
| SRC-069 | N8 | Paderborn University bearing damage dataset (KAt) | EVAL/BENCHMARK | eval_benchmarks | http_archive | P2 | `LicenseRef-Paderborn-KAt-Terms` | PROMPT_SPECIFIED |
| SRC-070 | N8 | NASA PCoE — Li-ion battery aging | EVAL/BENCHMARK | eval_benchmarks | http_archive | P1 | `LicenseRef-PD-USGov` | PROMPT_SPECIFIED |
| SRC-071 | N8 | NHTSA vehicle complaints & recalls (electronics field failures) | TRAIN-CANDIDATE | train_candidates | api | P2 | `LicenseRef-PD-USGov` | RECONSTRUCTED |
| SRC-072 | N8 | NIST NVD CVE API 2.0 (embedded / firmware advisories) | RAG/REFERENCE | rag_reference | api | P1 | `LicenseRef-PD-USGov` | RECONSTRUCTED |
| SRC-073 | N8 | ATHENA field returns (RMA) vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P0 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-074 | N8 | ATHENA PLC telemetry & genealogy vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P0 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |
| SRC-075 | N8 | ATHENA warranty claims vault | PRIVATE-GROUND-TRUTH | private_ground_truth | vault | P1 | `LicenseRef-ATHENA-Internal` | RECONSTRUCTED |

## Adversarial suite (`tests/test_dq_adversarial.py`)

Each case starts from a valid baseline, applies one mutation, and must be refused with the listed control.
`test_catalogue_is_complete` asserts that all 54 cases exist and that every control is covered.

| Case | Control | Mutation |
|---|---|---|
| DQ-001 | HC-01 | manifest without source_id |
| DQ-002 | HC-01 | source_id not in the registry (SRC-076) |
| DQ-003 | HC-01 | node_id disagrees with the registry |
| DQ-004 | HC-01 | intended_use relabelled |
| DQ-005 | HC-01 | sha256 is not a 64-hex digest |
| DQ-006 | HC-10 | payload bytes changed after staging |
| DQ-007 | HC-01 | blank license snapshot |
| DQ-008 | HC-01 | license_spdx_id swapped to a different license |
| DQ-009 | HC-01 | naive (non-UTC) fetched_at_utc |
| DQ-010 | HC-01 | fetched_at_utc in the future |
| DQ-011 | HC-01 | origin_type outside the enum |
| DQ-012 | HC-01 | training_rights_approval outside the enum |
| DQ-013 | HC-01 | data_steward missing |
| DQ-014 | HC-01 | official_url points somewhere else |
| DQ-015 | HC-10 | re-writing an existing manifest with different content |
| DQ-016 | HC-01 | unknown extra field smuggled into the manifest |
| DQ-017 | HC-02 | TRAIN-CANDIDATE artifact stored in rag_reference |
| DQ-018 | HC-02 | RAG/REFERENCE artifact stored in train_candidates |
| DQ-019 | HC-02 | path traversal through node / version components |
| DQ-020 | HC-02 | data lake rooted inside the production vector store |
| DQ-021 | HC-02 | partition directory symlinked outside the lake |
| DQ-022 | HC-02 | invalid node id N9 |
| DQ-023 | HC-03 | export while training_rights_approval is NOT_REVIEWED |
| DQ-024 | HC-03 | export after a REJECTED or PENDING steward decision |
| DQ-025 | HC-06 | approved LIVE-LOOKUP data pushed into a training export |
| DQ-026 | HC-03 | EVAL/BENCHMARK artifact exported to training |
| DQ-027 | HC-03 | APPROVED written into the manifest instead of the steward ledger |
| DQ-028 | HC-04 | MVTec AD flipped to commercial_training_allowed: true |
| DQ-029 | HC-04 | MVTec AD 2 artifact routed to eval_benchmarks |
| DQ-030 | HC-04 | steward-approved MVTec AD exported to training |
| DQ-031 | HC-04 | NC license on a TRAIN-CANDIDATE source |
| DQ-032 | HC-05 | portal / paid-standard source wired to an HTTP fetcher |
| DQ-033 | HC-05 | drop-zone file without entitlement metadata |
| DQ-034 | HC-05 | entitlement SHA-256 does not match the file |
| DQ-035 | HC-05 | expired entitlement |
| DQ-036 | HC-05 | entitlement issued for a different source |
| DQ-037 | HC-06 | API-key source called without its key |
| DQ-038 | HC-06 | API key literal written into the registry |
| DQ-039 | HC-06 | live lookup tagged training_allowed: true |
| DQ-040 | HC-06 | expired TTL cache served when the API is down |
| DQ-041 | HC-06 | API response echoes the credential |
| DQ-042 | HC-07 | Paderborn bearing data without its domain-transfer warning |
| DQ-043 | HC-07 | C-MAPSS promoted to training as electronics evidence |
| DQ-044 | HC-08 | internal://athena vault fetched over HTTP |
| DQ-045 | HC-08 | vault record that fails its intake schema |
| DQ-046 | HC-08 | vault artifact labelled PUBLIC_OPEN |
| DQ-047 | HC-09 | synthetic demo fixtures exported to training |
| DQ-048 | HC-09 | synthetic fixtures relabelled as private ground truth |
| DQ-049 | HC-11 | plain-HTTP source URL |
| DQ-050 | HC-11 | HTML login/error page served instead of JSON |
| DQ-051 | HC-11 | archive member escaping the extraction directory (zip slip) |
| DQ-052 | HC-11 | download larger than the configured limit |
| DQ-053 | HC-12 | train and validation time ranges overlap |
| DQ-054 | HC-12 | evaluation records duplicated into a training export |
