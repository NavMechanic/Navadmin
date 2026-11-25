# ATHENA N4 — Vendor I/O Contract (ATE, accredited labs, AOI, offline data)

This is what ATE integrators, accredited test laboratories, AOI vendors and operators must deliver
to N4. Machine-readable schemas are in `schemas/`:

- `n4_scpi_log`, `n4_lab_report_manifest`, `n4_aoi_inference`, `n4_evidence_intake`, `n4_measurement`;
- `payloads/VerificationReleasePayload.v1` for what downstream nodes receive.

Examples: `examples/synthetic_n4_scpi_log.json`, `examples/synthetic_n4_lab_manifest.json`.

## Rules for every submission

| Rule | Code on violation |
|---|---|
| The submission names an evidence `source_id` that is APPROVED and permits `VERIFICATION` | `EVIDENCE_SOURCE_NOT_APPROVED` / `EVIDENCE_SOURCE_PURPOSE` (409) |
| Every file is listed with its SHA-256, and the hash matches the uploaded bytes | `SHA256_MISMATCH` (hard rejection) |
| File content matches its extension, is within the size limit and passes the malware scan | `UPLOAD_REJECTED`, `MALWARE_DETECTED` |
| Units are SI (or SI-derived: `V`, `mV`, `A`, `mA`, `W`, `Mohm`, `mm`, `degC`, `%`, `count`, …) | `UNIT_NOT_SI` |
| Every reading has an expanded uncertainty U > 0 and a coverage factor k (default 2) | `UNCERTAINTY_MISSING` |
| Every physical reading names a registered instrument and its calibration, valid at capture time | `INSTRUMENT_UNREGISTERED`, `CALIBRATION_*` |
| Timestamps are UTC ISO-8601 (`…Z`) and not in the future (beyond 300 s skew) | `CAPTURE_IN_FUTURE` |
| Test ids exist in the plan, and the test's method matches the adapter | `UNKNOWN_TEST`, `METHOD_ADAPTER_MISMATCH` |
| `claimed_provenance` is `PHYSICAL` only for real hardware from a REAL source; otherwise `SYNTHETIC` | `SYNTHETIC_AS_PHYSICAL` (hard rejection) |

Accepted submissions return **201** with an `N4EvidenceIntake`. Rejected submissions return **422**
with the same record, carrying `status = REJECTED`, `rejection_codes` and `problems`. Rejected
submissions are kept (with their hashes) for audit. A hard rejection keeps blocking the affected tests
until a corrected submission for the same tests is accepted.

## 1. Bench ATE — SCPI/VISA log (`athena-scpi-log/1`)

`POST /v1/n4/plans/{plan_id}/evidence/scpi` takes a multipart body:

- `log_name`: the JSON log's file name
- `source_id`
- `files`: the JSON log plus any attachments (raw waveforms / CSV)

```json
{"format": "athena-scpi-log/1", "station_id": "ATE-01", "operator": "op-1", "test_id": "T-01",
 "unit_serial": "PSU-0001", "method_ref": {"method_id": "SOP-ATE-001", "revision": "C"},
 "instrument": {"resource": "TCPIP0::10.0.0.5::5025::SOCKET",
                "idn": "Keysight Technologies,34465A,MY59012345,A.03.01-02.40", "calibration_id": "CAL-DMM-1"},
 "records": [{"t": "2026-11-20T09:00:00Z", "command": "MEAS:VOLT:DC?", "response": "+2.40012000E+01",
              "characteristic": "output_voltage", "unit": "V", "expanded_uncertainty": "0.05", "k": 2}],
 "attachments": [{"path": "ripple_waveform.csv", "sha256": "…", "role": "WAVEFORM"}],
 "claimed_provenance": "PHYSICAL"}
```

- The `*IDN?` string must match the registered instrument's manufacturer, model, **serial** and
  **firmware**. An IDN that says simulated / virtual / mock / pyvisa-sim is treated as synthetic.
- Commands must be read-only queries: `*IDN?`, `*OPC?`, `SYST:ERR?`, `READ?`, `FETCh?`, `MEASure:…?`.
  Responses must be numeric. The SCPI overload value `9.9E37` is refused.
- The method revision in `method_ref` must be the plan's approved SOP revision (`METHOD_REVISION_MISMATCH`).

**Live capture.** `POST /v1/n4/plans/{plan_id}/evidence/scpi/live` takes `test_id`, `instrument_id`,
`unit_serial`, `source_id`, `station_id`, `method_ref` and `steps[] {query, characteristic, unit,
expanded_uncertainty, k}`.

- N4 connects over raw SCPI socket (`TCPIP0::host::port::SOCKET`), or through PyVISA when it is
  installed.
- The host must be in `N4_SCPI_ALLOWED_HOSTS` (otherwise 403 `HOST_NOT_ALLOWED`), and commands outside
  the allowlist get 403 `COMMAND_NOT_ALLOWED`.
- N4 queries `*IDN?`, then the steps, then `SYST:ERR?`. It stores the generated log as evidence.
- An unreachable instrument gives 502 `INSTRUMENT_UNREACHABLE`.

## 2. Accredited laboratory report (`athena-lab-report/1`)

`POST /v1/n4/plans/{plan_id}/evidence/lab` takes a multipart body: `manifest` (JSON), `source_id`, and
`files` (exactly one PDF report).

```json
{"format": "athena-lab-report/1",
 "lab": {"name": "Example Test Lab GmbH", "accreditation_body": "DAkkS", "accreditation_number": "D-PL-99999-01-00"},
 "certificate_number": "LAB-2026-0042", "issued_at": "2026-11-21T09:00:00Z", "product": "PSU-120W",
 "pcb_revision": "B", "report_sha256": "<sha256 of the PDF>",
 "results": [{"test_id": "T-09", "characteristic": "hipot_leakage_current", "unit_serial": "PSU-0001",
              "value": "1.2", "unit": "mA", "expanded_uncertainty": "0.1", "k": 2,
              "instrument_id": "LAB-HIPOT-7", "calibration_id": "LABCAL-7",
              "calibration_due": "2027-06-01T00:00:00Z", "captured_at": "2026-11-20T09:00:00Z",
              "method_ref": {"method_id": "SOP-LAB-001", "revision": "A"},
              "standard_clause": "IEC 62368-1 cl. 5.4"}],
 "claimed_provenance": "PHYSICAL"}
```

- The lab must be accepted by a quality lead (`POST /v1/n4/labs`) with an accreditation number and
  scopes, and must be valid on the issue date (`LAB_NOT_ACCREDITED`). Every `standard_clause` must fall
  inside the accepted scope (`LAB_SCOPE_EXCEEDED`).
- **Exact PCB revision (T12).** The manifest `pcb_revision` must equal the intake's PCB revision. The
  PDF text must name the same revision (`PCB Rev B`) and no other (`WRONG_PCB_REVISION`, a hard
  rejection). The certificate number must appear in the PDF text (`CERTIFICATE_NOT_IN_REPORT`).
- The lab's own instrument calibration due date is stated per result and must be later than
  `captured_at`.

## 3. AOI / vision (`athena-aoi/1`)

`POST /v1/n4/plans/{plan_id}/evidence/aoi` takes a multipart body: `inference` (JSON), `source_id`, and
`files` (the images).

```json
{"format": "athena-aoi/1", "station_id": "AOI-LINE-1", "instrument_id": "AOI-1", "test_id": "T-13",
 "unit_serial": "PSU-0001", "pcb_revision": "B", "captured_at": "2026-11-20T09:00:00Z",
 "model": {"name": "yolov8n-solder", "version": "1.3.0", "framework": "ultralytics-yolov8", "weights_sha256": "…"},
 "images": [{"path": "top.png", "sha256": "…",
             "detections": [{"class": "solder_bridge", "confidence": 0.55, "bbox_xyxy": [10, 10, 20, 20]}]}],
 "claimed_provenance": "PHYSICAL"}
```

- Every image must be uploaded, hash-matched and declared.
- Detections are triaged as follows:
  - flagged at confidence ≥ 0.85;
  - ambiguous at 0.40 to 0.85;
  - below 0.40 they are noted, but still listed for the adjudicator.
- **A model call never decides a unit.** A `quality_reviewer` who did not import the evidence labels
  each detection (`CONFIRMED_DEFECT` / `FALSE_CALL`) and gives the unit verdict through
  `POST /v1/n4/adjudications`. Until that happens the test is `AOI_UNADJUDICATED`.
- Only human adjudications become training labels. A label is `training_eligible` only with two
  agreeing independent adjudicators. Model outputs are never exported as labels.

## 4. Offline CSV (manual and legacy data)

`POST /v1/n4/plans/{plan_id}/evidence/csv` takes a multipart body: `source_id` and `files` (exactly one
CSV, plus optional attachments).

Required columns: `test_id, characteristic, unit_serial, value, unit, expanded_uncertainty,
instrument_id, calibration_id, captured_at, method_id, method_revision`. Optional columns: `k`,
`operator`.

- One CSV holds either BENCH_ATE or INSPECTION results. Mixing them gives `MIXED_METHODS`.
  ACCREDITED_LAB and SIM results are refused.
- A file whose first line starts with `# ATHENA_MOCK_FIXTURE,` is a generated fixture. It is imported as
  `MOCK_FIXTURE` and can never qualify.

## 5. What N4 sends downstream

`VerificationReleasePayload.v1`, through N0, one per target (N8 / N6 / N7). It contains:

- the release id, target, PRD reference and design references;
- `design_revision` and `pcb_revision`;
- the plan and assessment ids and hashes;
- `evidence_sha256`, the review id and the approver;
- the evidence submitters;
- `verification_status = VERIFIED`;
- every `VerifiedMeasurement`, with value, U, k, limits, verdict, method, provenance, instrument,
  calibration id and due date, capture time and raw SHA-256;
- attribute inspections and the traceability rows;
- unit serials, the prioritisation method (and AP licence when used), and the control plan;
- lab certificates;
- for N6 / N7 only, commercial or finance summaries;
- the approval scope statement.

N8 receives it at `POST /v1/n8/intake` and refuses anything not RELEASED to N8, superseded, synthetic
or hash-mismatched.
