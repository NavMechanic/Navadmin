# ATHENA N3 — Design Intelligence & CAD/CAE Execution Engine (integrated with N0, N1 and N2)

N3 turns an N2-approved Product Requirements Document (PRD) into bounded engineering work packages.
It executes or routes the CAD/CAE work across:

- automated tools: KiCad CLI and ngspice, in a sandbox
- qualified external engineering vendors

It then verifies the native deliverables (ERC/DRC, worst-case simulation corners, BOM, revision
lineage). A separate design authority must approve them. N3 finally releases N0-checked manifests to
**N4** (Verification & Testing) and **N5** (Procurement & BOM Sourcing).

> This repository was **rebuilt from the specification**. No `athena_n3_integrated` starter existed in
> this workspace, the git history or Drive. No N2 implementation existed either. The repository
> therefore contains:
>
> - **N0**: byte-identical copies from `athena_n0_starter`
> - **N1**: byte-identical copies from `athena_n1_integrated`
> - **N2**: a **minimal N2 PRD service** (`app/n2`). It is the slice N3 depends on: an N0-released RFQ
>   becomes an N0-checked PRD that a product authority releases to N3.
> - **N3**: built here.
>
> The **84 baseline tests** are re-created under the specified file names:
>
> | File | Tests |
> |---|---|
> | `tests/test_n0_gateway.py` | 10 |
> | `tests/test_n0_llm_boundary.py` | 5 |
> | `tests/test_n1_market.py` | 12 |
> | `tests/test_n2_product.py` | 14 |
> | `tests/test_n3_cli_adapter.py` | 23 |
> | `tests/test_n3_design.py` | 20 |
>
> They pass along with the integration suites listed under [Tests](#tests).

> **Approval scope.** N3 approval means a design package is suitable for the next controlled
> engineering step (N4 verification / N5 sourcing). It is **not** product safety certification,
> laboratory qualification or mass-manufacturing release. Every baseline, assessment, review,
> release and N0 payload carries this statement.

```
app/main.py              combined entry point: N0 gateway + N1 + N2 + N3 (one DB / store / scanner / audit chain)
app/gateway.py, app/*.py N0, byte-identical to athena_n0_starter (tests/test_n0_sync.py)
app/n1/                  N1, byte-identical to athena_n1_integrated
app/n2/                  minimal N2: PRD from an N0-released RFQ, four-eyes release to N3, release listeners
app/n3/models.py         contracts → schemas/n3_*.schema.json (13 families + job / vendor / routing records)
app/n3/parsers.py        KiCad 6–9 sch/pcb/pro, ERC/DRC JSON, BOM CSV, ngspice logs, STEP, thermal JSON
app/n3/engine.py         deterministic planning, verification, decision, rework ranking, release payloads
app/n3/tool_runner.py    sandboxed kicad-cli / ngspice runner + background JobWorker
app/n3/db.py             13 append-only N3 tables on the shared metadata
app/n3/service.py        the seven stages, invalidation on PRD revision, views
app/n3/router.py         /v1/n3/* API
scripts/run_tools.py     operator CLI over the same sandboxed runner
examples/run_n3_demo.py  synthetic end-to-end demo; examples/designs/ the synthetic PSU-120W package
migrations/versions/0003 N2 + N3 tables and their append-only triggers
```

## Invariants and where they are enforced

| Invariant | Enforcement |
|---|---|
| **1. Approval scope boundary** | `APPROVAL_SCOPE` (`app/n3/__init__.py`) is embedded in the N3 baseline, assessment, review, release and the N0 `DESIGN_RELEASE` payload (`approval_scope`). |
| **2. N2 baseline lock** | `lock_baseline` accepts only an N2 `PRDRelease` targeting N3, for the **newest** PRD revision. All of these must verify: (a) the N0 PRD artifact is PASS and not superseded; (b) its stored payload bytes re-hash to the released SHA-256; (c) the N0 manifest is RELEASED to N3 with the same payload hash and approval; (d) the approval event's hash recomputes against the approval chain; (e) the approver is not the submitter. A new PRD release triggers the N2 listener (`on_prd_release`), which appends `INVALIDATED` events for older baselines and their work packages and releases. Every N3 call also re-checks lazily. Further work returns `409 BASELINE_INVALIDATED`. |
| **3. Native editable deliverables** | Intake rejects PDF-only / screenshot-only deliveries (`NATIVE_FILES_MISSING`). It also rejects files whose content is not what the extension claims (MIME sniffing plus format detection). Every native file needs a SHA-256 (verified per file against the manifest) and a tool version, or the assessment BLOCKs (`DESIGN_FILE_UNVERIFIED`). N0 re-derives nativeness from extensions on release. |
| **4. Hard safety & DRC/ERC blocking** | `engine.assess` produces BLOCK findings for: critical ERC/DRC, creepage/clearance (DRC and the `.kicad_pro` rule set vs the PRD), unrouted nets, schematic/PCB parity, PCB/BOM revision mismatch, BOM↔PCB refdes mismatch, missing worst-case corner, undefinable corners, derating breach, thermal limit, and thermal analysis below the PRD worst case. The decision is BLOCK > REWORK > PASS and is computed before any cost logic. `rank_actions` never changes it: waivers of BLOCK findings are refused with `BLOCK_NOT_WAIVABLE`. A BLOCK cannot be approved (`BLOCK_NOT_APPROVABLE`), and N0 BLOCKs the same conditions again on release. |
| **5. Sandboxed tool execution** | See [Sandbox](#sandbox). Only fixed operations exist, argv is built from constants plus sanitised names, there is never a shell, and untrusted SPICE goes through a netlist policy. |

## Pipeline (N3.1–N3.7)

| Stage | What it does | API |
|---|---|---|
| **N3.1 intake** | lock the PRD release into an `N3DesignBaseline` (constraints, requirements, test limits, `baseline_sha256`); idempotent per PRD revision | `POST /v1/n3/baselines` |
| **N3.2 planning** | immutable, content-hashed `N3WorkPackage`s per scope with acceptance criteria derived from the PRD | `POST /baselines/{id}/plan`, `POST /work-packages` |
| **N3.3 routing** | TOOL: the tool must support the package kind and be installed. VENDOR: onboarded, qualified for the kind, NDA on file, IP terms recorded, budget ≤ package cap and vendor limit, **and** N0 authorizes a `VendorWorkPackage.v1` (the PRD's source must permit `VENDOR_TRANSMISSION` to that vendor) | `POST /vendors`, `POST /work-packages/{id}/route` |
| **N3.4 execution** | controlled delivery intake (manifest + per-file SHA-256 + MIME screening + malware scan + role / tool-version / revision detection), stored write-once in N0 quarantine. Vendor deliveries are also recorded as N0 `VendorResult.v1`. Sandboxed tool jobs: `sch_erc`, `pcb_drc` (with schematic parity), `pcb_export_gerbers` / `_drill` / `_step` / `_odb` (KiCad 9), `sch_export_bom`, `spice_corners` | `POST /work-packages/{id}/deliveries`, `POST /work-packages/{id}/jobs`, `GET /jobs/{id}` |
| **N3.5 verification** | parse the real ERC/DRC JSON, corner logs and BOM. Reconcile BOM MPN / refdes / qty against the PCB footprints and revision. Record `N3Assessment` + `N3Finding`s (`assessment_sha256`). Deliveries with `replaces_previous` replace the file set, and evidence derived from replaced files is dropped as stale | `POST /work-packages/{id}/assess` |
| **N3.6 decision** | deterministic PASS / REWORK / BLOCK. `N3Action` cost-aware proposals (`PROPOSED_NOT_EXECUTED`, `decision_unchanged: true`). `N3ReviewRequest` from a separate design authority, recorded in the N0 approval chain | `POST /assessments/{id}/actions`, `POST /reviews` |
| **N3.7 handoff** | for a scope whose every package has a PASS assessment approved by the design authority at one design revision: `DESIGN_RELEASE` → N4 (native file refs promoted to `released/`, ERC/DRC summaries, simulation corners, thermal evidence, derating, clearance, test limits) and `BOM_RELEASE` → N5 (`bom_item.v1` items locked to the design and PCB revision). Each goes through the N0 pipeline as `svc-n3`, then a RELEASE approval and an N0 handoff manifest | `POST /baselines/{id}/releases` |

Also: `GET /status`, `GET /baselines/{id}`, `GET /work-packages/{id}`, `GET /assessments/{id}`,
`GET /releases/{id}`, `POST /feedback`, `GET /subjects/{kind}/{id}/status`.

### The three design scopes

| Scope | Packages | Native files required | Verification evidence | N4 payload | N5 |
|---|---|---|---|---|---|
| `schematic_pcb` | ELECTRICAL_SCHEMATIC, PCB_LAYOUT, DFM | `.kicad_sch`, `.kicad_pcb` | ERC + DRC JSON, BOM CSV, Gerber + drill or ODB++ | erc / drc summaries, clearance | BOM (rev-locked to the PCB) |
| `circuit_simulation` | CIRCUIT_SIMULATION | SPICE netlist (`.cir/.sp/.spice/.net`) | converged `vin_min`, `vin_max`, `temp_max`, `load_max` corner logs, solver version | corners with parameters, measures and log hashes, derating | BOM if delivered |
| `mechanical_thermal` | MECHANICAL, THERMAL | STEP / FreeCAD geometry | thermal report: solver + version, boundary conditions, ambient ≥ PRD max, load ≥ PRD max, temperatures ≤ limit − margin | thermal evidence | NOT_APPLICABLE unless a BOM was delivered |

The worst-case corners come from the PRD constraints:

| Corner | `vin` | `iload` | Temperature |
|---|---|---|---|
| `vin_min` | min | `load_max_a` | 25 °C |
| `vin_max` | max | `load_max_a` | 25 °C |
| `temp_max` | nominal | `load_max_a` | `ambient_max_c` |
| `load_max` | min | `load_max_a` | `ambient_max_c` |

N3 sets `vin` / `iload` per corner with `.param` and `.temp` in a wrapper that `.include`s the
netlist, so the netlist must not define them. Derating compares `.meas` results named
`stress_<refdes>_<v|i|p>` with the BOM `V_Rating` / `I_Rating` / `P_Rating` columns × `derating_factor`.
Measures named like a PRD test limit (e.g. `vout_min`) are checked against it.

## Sandbox

`app/n3/tool_runner.py` (also used by `scripts/run_tools.py`):

- **Allowlist.** Eight operations, each mapped to a fixed argv. Tool binaries come only from
  `N3_KICAD_CLI` / `N3_NGSPICE` and are resolved once. Inputs must have the operation's extensions,
  and names are sanitised into the job directory.
- **No shell.** `subprocess` runs with an argv list, stdin closed, its own session / process group
  (`killpg` on timeout) and a minimal environment (`PATH=/usr/bin:/bin`, `HOME` = the private 0700
  work dir). No inherited variables, so no proxy credentials or tokens.
- **Resource limits.** CPU seconds, address space, max file size and open files, set via `prlimit`
  (or `setrlimit` in the child when `prlimit` is missing). There is also a per-job wall-clock
  timeout and caps on output size and count.
- **Network isolation.** A private network namespace (`unshare -rn`) when available.
  `N3_NETWORK_ISOLATION=required` refuses jobs without one. For filesystem isolation in production,
  set `N3_SANDBOX_PREFIX` to a bubblewrap / nsjail wrapper.
- **Netlist policy.** SPICE is untrusted, because ngspice `.control` blocks can run shell commands
  and `.include` can read any file. The policy refuses: control blocks, `*ng_script`, includes that
  are not files of the same job, absolute / home / parent paths, and netlists that set the corner
  parameters or temperature. This happens before anything is queued.
- **Output collection.** Only expected paths are collected. Symlinks, special files and anything
  resolving outside the work dir are ignored. Every output is malware-scanned, hashed and stored
  write-once.
- **Version gates.** `sch erc` / `pcb drc` need KiCad 8+ and ODB++ needs KiCad 9. The exact tool
  version is captured per job and on every output.
- **Async.** Jobs run on a bounded thread pool (`N3_MAX_CONCURRENT_JOBS`) and pass through
  QUEUED → RUNNING → SUCCEEDED / FAILED / TIMEOUT as append-only rows. `N3_JOBS_INLINE=true` runs
  them inside the request instead (tests, single-user demos).

## Roles (JWT RBAC, no static API keys)

| Role | Can | Implies (N0) |
|---|---|---|
| `design_engineer` | lock baselines, plan, route, deliver in-house, run tool jobs, assess, propose actions | `producer` |
| `vendor_submitter` | deliver to work packages routed to its JWT `vendor_id`; read those packages (own deliveries only) | — |
| `design_authority` | onboard vendors, review assessments, release to N4 / N5 | `gateway_reviewer`, `release_manager` |
| `product_manager` / `product_authority` | author / release N2 PRDs | — / `gateway_reviewer`, `release_manager` |
| `auditor` | read | |

Separation of duties:

- A reviewer or releaser may not have submitted a delivery or tool job in the assessed file set, and
  may not be the assessor (`SEPARATION_OF_DUTIES`).
- A PRD author cannot release it (`FOUR_EYES`).
- Only the `svc-n3` node-service identity can submit artifacts with `origin_node=N3`.

## N0 release policies for all three scopes

`app/rules.py` (shared with `athena_n0_starter`) re-checks each N3 payload independently:

- **`DESIGN_RELEASE`** — scope-specific native files with SHA-256 + tool version:
  - **schematic_pcb**: ERC/DRC summaries with zero critical
  - **circuit_simulation**: solver + version, all four worst-case corners, convergence
  - **mechanical_thermal**: native STEP/CAD geometry, thermal boundary conditions, ambient / load
    assumptions, limits minus margin
  - **all scopes**: derating and creepage/clearance
- **`BOM_RELEASE`** — PCB revision = design revision, MPN and qty per line, duplicate refdes.
- **`PRD`** — routes only to N3.

## Persistence and storage

- 13 append-only N3 tables plus 2 N2 tables share the N0 metadata. Database triggers enforce
  append-only (PL/pgSQL on PostgreSQL, `RAISE(ABORT)` on SQLite).
- Migration `0003` creates the N2 and N3 tables with a frozen copy of the trigger DDL.
- Design files and tool outputs live in N0's versioned object storage (local or S3): write-once,
  keyed by tenant + SHA-256, re-hashed on every read (`STORED_FILE_CORRUPT`). Released native files
  are promoted to `released/` by N0 on PASS.
- Lineage runs through one graph: market batch → N1 → RFQ → PRD → N3 baseline / work package /
  blobs / jobs / assessment → `DSN-…` / `BOM-…` artifacts → N4 / N5.

```bash
export N0_DATABASE_URL=postgresql+psycopg://n3:***@db:5432/athena N0_AUTO_CREATE_SCHEMA=false
alembic upgrade head        # 0001 (N0) + 0002 (N1) + 0003 (N2 + N3) tables and triggers
```

## Running it

```bash
pip install -r requirements.txt                    # plus ngspice / KiCad 8+ for real tool runs
python examples/run_n3_demo.py                     # synthetic: BLOCK, rework, review, N4/N5 release, invalidation
python -m pytest -q                                # 178 tests; the live-KiCad one skips without KiCad 8+

export N0_JWT_SECRET=... N0_JWT_ISSUER=https://idp.example   # or N0_JWT_PUBLIC_KEY_FILE for RS256
export N3_KICAD_CLI=/usr/bin/kicad-cli N3_NGSPICE=/usr/bin/ngspice N3_NETWORK_ISOLATION=required
uvicorn app.main:app --port 8000

python scripts/run_tools.py describe               # tool versions + sandbox limits
python scripts/run_tools.py corners examples/designs/psu_buck.cir --vin-min 43.2 --vin-max 52.8 \
    --ambient-max 85 --load-max 5 --out /tmp/corners
python -m app.schema_export --check                # schemas/ and N0_N1_N2_N3_OpenAPI.json are current
```

## Tests

| File | Covers |
|---|---|
| `test_n0_gateway.py`, `test_n0_llm_boundary.py`, `test_n1_market.py`, `test_n2_product.py`, `test_n3_cli_adapter.py`, `test_n3_design.py` | the 84 baseline tests |
| `test_n3_sandbox.py` | minimal env, network namespace, process-group timeout, crash, version gates, safe output collection, the async worker through the API; **live** ngspice (corners, broken netlist, through the API) and live KiCad 8+ ERC, each skipped when the tool is missing |
| `test_n3_parsers.py` | ngspice logs, KiCad ERC/DRC JSON, BOM CSV, native-format detection |
| `test_n3_hard_blocks.py` | every hard rule (critical ERC/DRC, clearance, unrouted, parity, PCB/BOM revision drift, refdes mismatch, `.kicad_pro` clearance, layers, BOM lines, derating, convergence, spec limits, thermal), missing native files, unversioned geometry, a skipped worst-case corner, incomplete PRD, a hostile netlist, and that the N0 release path cannot be bypassed |
| `test_n3_revision_drift.py` | PRD revision invalidates baseline / packages / releases (listener and lazy); every operation then refused; only the newest PRD locks; replaced deliveries make evidence stale; mixed revisions block or never release |
| `test_n3_rbac.py` | role matrix, no static keys, vendor routing / `vendor_id` binding, vendor visibility, submitter / assessor / releaser separation, tenant isolation |
| `test_n3_persistence.py` | append-only triggers on all 15 N2/N3 tables, restart and continue, immutable verified storage, Alembic upgrade / check / downgrade, PostgreSQL DDL |
| `test_n3_e2e.py` | **N0 → N1 → N2 → N3 → N4/N5 for all three scopes** from real TED evidence; a vendor-routed PCB package; the demo; synthetic examples |
| `test_n3_schemas.py`, `test_n0_sync.py` | 13 N3 schemas + drift + OpenAPI coverage + live records validate; the vendored N0 and N1 are byte-identical to their sources |

The suite uses the stand-ins `tests/fixtures/bin/kicad-cli` (KiCad-8-format ERC/DRC JSON, Gerber,
Excellon, STEP, BOM) and `tests/fixtures/bin/ngspice` (ngspice-format logs). Markers inside the design
files drive their behaviour. They still run inside the real sandbox (prlimit, unshare, process groups).

## Not verified here

- **KiCad was not run live.** The available package is KiCad 7.0, whose `kicad-cli` has no
  `sch erc` / `pcb drc`. All KiCad paths ran against the stand-in, which emits the KiCad 8 report
  format as documented. `test_live_kicad_cli_erc_on_an_empty_schematic` runs where KiCad 8+ is
  installed. The synthetic `examples/designs/*.kicad_*` files are hand-written: N3's parsers read
  them, but real KiCad may not open them as-is.
- **ngspice was run live** (ngspice 42): the demo, the live tests and the CLI ran the real worst-case
  corners.
- **Sandbox depth.** `unshare -rn` and `prlimit` were exercised. Filesystem isolation (bubblewrap /
  nsjail via `N3_SANDBOX_PREFIX`) was not.
- **PostgreSQL** was checked as compiled DDL only. **S3** storage, real identity-provider JWKS and
  ClamAV were not exercised.
- **N2 is minimal.** It does not cover requirement elicitation, trade studies or market fit.
- **Thermal and mechanical solvers are not executed.** N3 verifies the delivered report and native
  geometry but does not run FEA/CFD.
