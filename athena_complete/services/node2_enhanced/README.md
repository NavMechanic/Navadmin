# Athena Node 2 — Micro-PLM & Stage-Gate Governance

The system of record for multi-level BOMs, revision history and engineering
change, with a gate engine that **refuses the workflow transition** when the
evidence a gate requires is missing.

A gate that reports problems but lets the workflow advance anyway is a dashboard.
This one returns `422` and leaves the gate closed.

```
POST /api/v1/plm/gates/{gate_id}/evaluate   evaluate, and transition only if it passes
POST /api/v1/plm/gates/{gate_id}/waive      pass over named failing rules, on the record
GET  /api/v1/bom/{part_number}/explode      multi-level explosion with cost roll-up
GET  /api/v1/bom/{part_number}/where-used   upward traversal — what a change reaches
POST /api/v1/ecn                            raise a change; impact analysis runs with it
POST /api/v1/webhooks/cad-vault/revision    signed STEP AP242 / Gerber / BOM ingest
POST /api/v1/compliance/refresh             verify BIS CRS, WPC ETA, UN 38.3
```

---

## Quick start

Python 3.11 or newer.

```bash
cd athena_node2
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Option A — PostgreSQL (the target)
export ATHENA_DATABASE_URL="postgresql+asyncpg://athena:athena@localhost:5432/athena_plm"
alembic upgrade head
python3 scripts/seed.py

# Option B — no server at all
export ATHENA_DATABASE_URL="sqlite+aiosqlite:///./athena_plm.db"
python3 scripts/seed.py            # creates the schema in place

uvicorn app.main:app --reload --port 8002
```

Then, in a second terminal:

```bash
./scripts/demo.sh
```

Thirteen steps: explode the kit, walk where-used, evaluate G2 and get the blocked
contract, remediate both blockers, watch the gate pass and transition, raise a
cell-substitution ECN and see what it flags, get the change board's approval
refused, exercise the signed webhook, and print the gate matrix.

With Docker:

```bash
docker compose up            # postgres → migrate → seed → service on :8002
```

---

## The gate contract

`POST /api/v1/plm/gates/G2_DESIGN_EVT/evaluate` with `{"sku": "KIT-NPA-01"}`
returns **HTTP 422** and this body:

```json
{
  "gate": "G2_DESIGN_EVT",
  "status": "BLOCKED",
  "evaluation_timestamp": "2026-08-15T10:50:14.842652Z",
  "blocking_reasons": [
    {
      "rule_id": "RULE_UNASSIGNED_FMEA",
      "status": "FAILED",
      "detail": "High AP item 'BMS: over-charge protection' (D-01, PB-10K-PKD) missing owner assignment, carries function 'Design Eng.'; High AP item 'Stn 5 — Hi-Pot safety test' (P-05, AD-20W-PKD) missing owner assignment, carries function 'Mfg. Eng. / Quality'",
      "severity": "BLOCKING",
      "remediation": "POST /api/v1/fmea/{row_ref}/owner with a named user and a target date. A responsibility function is not an owner.",
      "object_refs": ["D-01", "P-05"]
    },
    {
      "rule_id": "RULE_CREEPAGE_CHECK",
      "status": "FAILED",
      "detail": "Adapter isolation PCBA creepage distance is 2.4mm, minimum required is 3.0mm under IEC 62368-1",
      "severity": "BLOCKING",
      "remediation": "Measure on the released layout and record with a 20% guard band (DFMEA D-02 recommended action), then set verified=true.",
      "object_refs": ["CREEPAGE_AD_PCB_PRI_SEC"]
    }
  ],
  "passed_artifacts": ["CAD_MASTER_V2.STEP", "PRELIMINARY_BOM_REV3.CSV", "..."]
}
```

Same schema on 200 and on 422. The caller asked "may I proceed"; "no, and here is
exactly why" is the useful answer, so the failure carries the evaluation rather
than a bare error. Every field beyond the five above — `rules_evaluated`,
`advisories`, `gate_state`, `transition_applied`, `evaluation_id` — is optional,
so a consumer written against the base contract keeps working.

Both blocking conditions come from the source design package, not from invented
demo data: the adapter's creepage and Hi-Pot values are recorded there as
provisional, and the FMEA rows carry a *responsibility function* where AIAG-VDA
requires a named individual with a date.

---

## What the rules actually check

22 rules across six gates, 19 of them blocking. `GET /api/v1/plm/rules` lists them.

### G2 — Design / EVT

| Rule | Checks |
|---|---|
| `RULE_CAD_MASTER_MODEL` | A released **parametric** master exists. A dumb STEP export is a derived file; the tool maker cannot drive a change from it, so it does not satisfy this. |
| `RULE_SCHEMATIC_RELEASED` | Released schematics for every SKU carrying a PCBA of its own. A kit does not need one; its children do. |
| `RULE_DFMEA_DRAFTED` | DFMEA rows exist for every SKU in scope. |
| `RULE_UNASSIGNED_FMEA` | Zero High AP items without a **named owner**. A department cannot be held to a date. |
| `RULE_CREEPAGE_CHECK` | Numeric, not documentary: reads the measured value, compares it to the minimum, and refuses a value still flagged as a placeholder. |
| `RULE_PRELIMINARY_BOM_RELEASED` | Sourcing cannot place EVT material against an unreleased structure. |
| `RULE_NO_PENDING_RE_REVIEW` | Nothing left flagged by an ECN — otherwise the gate is judged on pre-change analysis. |
| `RULE_NO_UNAPPROVED_ECN` | No draft or in-review change open. The design being gated must be the design that will be built. |
| `RULE_UN38_3_BEFORE_CELL_SHIPMENT` | See below. |

Plus three advisories that report without blocking: constituent-SKU sign-off,
provisional safety parameters, and NRND components designed in.

### The UN 38.3 finding

UN 38.3 is normally filed as a G4 compliance item. **That is too late.** The
cells are SKD imports: they must be air-freighted to reach the EVT build, and no
carrier accepts a lithium cell without a test summary. The constraint binds at
**G2 exit**, when the EVT build is authorised — so it is a G2 rule here, scoped
to the SKUs that actually import cells. The kit is excluded: it does not import
cells, the powerbank does, and the transport summary belongs to the shipment that
moves them.

### Gate inheritance and ordering

A gate does not un-require what an earlier gate required, so every gate re-runs
the blocking rules of the gates before it. Without that, a G4 pass could sit on
a G2 criterion that has since regressed. Ordering is enforced separately:
`RULE_PREDECESSOR_GATE_PASSED` refuses to pass G4 while G3 is open. You may still
*evaluate* out of order — knowing where you stand should not need permission.

Two rules deliberately change severity with the gate. Provisional safety values
are advisory at G2 and **blocking at G3**, because G3 releases the design to
tooling and a provisional isolation value cut into steel is a recall, not a
rework.

---

## Multi-SKU hierarchy

`KIT-NPA-01` rolls up `NB-100-PKD`, `PB-10K-PKD` and `AD-20W-PKD` plus three
kit-level accessories — 48 lines over 2 levels, $14.74 rolled up, computed from
the children rather than stored.

Both traversals are **recursive CTEs executed in the database**, not N+1 round
trips: ECN impact analysis calls `where_used` for every affected part.

BOM lines hang off a **parent revision**, not a parent part. That is the whole
point of a PLM — rev B of the neckband may consume a different cell from rev A,
and both must stay readable. `child_revision_id` is nullable: `NULL` floats to
the child's latest released revision, a value pins it. The seed pins every
safety-critical child and floats packaging, because a floating line would let a
new revision enter a certified construction without anyone deciding to accept it.

Structural guards: released structure is immutable (`409`, with the ECN route in
the message), cycles are refused before insert, and the traversal carries a depth
cap so a cycle that somehow exists returns an answer instead of spinning.

---

## ECN / ECO and impact analysis

Raising a change also finds what it breaks. Substituting the powerbank cell flags
**eleven records** across five categories, each with a stated reason and the BOM
path that pulled it in:

```
PFMEA_ROW          P-03   Analyses PB-CEL-10K3S or an assembly containing it.
DFMEA_ROW          D-01   Severity 9 record on PB-10K-PKD, which contains the cell.
                          Flagged conservatively: a change to a constituent part can
                          invalidate a high-severity analysis without a structural link.
CONTROL_PLAN_ITEM  TC-PB-03  Derived from PFMEA row P-03, flagged by this change.
CERTIFICATE        UN38_3:PB-10K-PKD, IEC_62133_2, BIS_CRS
                          The cell is safety-critical and inside the certified
                          construction; certification is granted against a specific build.
ARTIFACT           PRELIMINARY_BOM_REV3.CSV (KIT-NPA-01)
                          via KIT-NPA-01 / PB-10K-PKD / PB-CEL-10K3S
```

Three properties matter more than the list itself. **Selection is explained** —
an impact list nobody can judge gets closed without being read, so every row
carries why it was pulled in and the path it came through. **It is bounded** —
certificates are flagged only for safety-critical parts, because a label artwork
change does not invalidate a UN 38.3 summary and flagging it every time is how
impact lists become noise. **It is idempotent** — re-analysis preserves completed
re-reviews and clears flags that are no longer in scope.

The flag is written in two places: the ECN's impact row, and `review_state` on
the record itself. The gate engine reads the second, so both have to move.

Approval is refused while any impacted record is still Pending Re-Review.
Approving a change whose consequences have not been assessed is exactly how a
cell swap reaches the line with a stale control plan behind it.

---

## RBAC

Seven roles, deliberately separated. `engineer` raises an ECN but cannot approve
it; `change_board` approves changes but cannot pass a gate; only `gatekeeper`
transitions gate state. A single "write" permission would make the whole
stage-gate model decorative.

A non-gatekeeper's evaluate request is downgraded to a dry run and the response
says so, rather than returning 403 — you should not need permission to find out
where you stand.

Tokens are verified for signature, expiry, issuer **and** audience: a token
minted for another Athena node does not open this one.

---

## Vault webhooks

The payload decides whether a gate passes, so an unsigned webhook is an
unauthenticated write to the gate criteria. HMAC-SHA256 over the **raw body**
(verifying a re-serialised body compares a signature against something the sender
never signed), constant-time compare, timestamp window against replay.

Beyond that: ingestion is idempotent on `event_id`; a file format that does not
match its declared artifact type is rejected at the edge; the same name and
revision arriving with a different checksum is a `409` rather than a silent
overwrite of a released document; and a revision against a part that already has
a released revision **raises an ECN automatically**, so a change cannot quietly
reach a gate.

---

## Compliance vault

Without `ATHENA_COMPLIANCE_VAULT_BASE_URL` the service replays local certificate
state and labels it `replay` — an unverified record must never read as a verified
one. Expiry is evaluated against real time even in replay, so a certificate that
has aged past its date does not keep reporting VALID.

A certificate is not a boolean. It has a status *and* an expiry, and one that
lapses mid-ramp is a production stop with a renewal lead time attached —
`certificate_min_validity_days` (default 90) is what turns that into an
enforceable rule rather than a note.

---

## What the seeded programme actually looks like

Running `POST /plm/gates/G4/evaluate` on the adapter today returns eight blocking
reasons. Two are worth naming:

**33 of 38 Critical control-plan characteristics are not traceable to a PFMEA
row.** A control with no failure mode behind it cannot be justified — and cannot
be safely removed when someone later asks why the line runs it.

**No WPC ETA record exists for the adapter, and BIS CRS is still in testing for
all three SKUs.** Both are market-access blockers with multi-week lead times, and
neither is on anyone's critical path today.

---

## Layout

```
app/
  main.py            app factory, lifespan, middleware, error handlers
  config.py          pydantic-settings; refuses a sync DB driver at startup
  db.py              async engine, session factory, declarative base
  models.py          SQLAlchemy 2.0 ORM — the system of record
  schemas.py         Pydantic v2 contracts; GateEvaluationResponse is frozen
  enums.py           controlled vocabularies
  security.py        PyJWT verification and the role dependencies
  gates/
    registry.py      rule decorator, registry, cached evaluation context
    rules.py         the 22 rules
    engine.py        evaluate, transition, waive
  services/
    bom.py           recursive-CTE explosion and where-used, cycle guard
    ecn.py           change workflow and impact analysis
    compliance.py    Compliance Vault client with replay fallback
    vault.py         signed CAD/EDA webhook ingestion
  routers/           auth, bom, gates, ecn, records, webhooks
  data/              node2_seed.json — regenerable from the NPI package
alembic/             migrations
scripts/
  make_seed.py       rebuild the seed from the NPI source modules
  seed.py            load it (idempotent)
  demo.sh            13-step walkthrough
tests/               60 tests
```

---

## Tests

```bash
pytest                                    # SQLite, no server needed
ATHENA_TEST_DATABASE_URL=postgresql+asyncpg://athena:athena@localhost:5432/athena_test pytest
```

60 tests, passing on both backends. Nothing is written to be dialect-specific
precisely so the recursive CTEs and the constraint behaviour can be checked on
both — the grouped sub-select that resolves floating revisions is used instead of
PostgreSQL's `DISTINCT ON` for that reason.

They cover the published contract field by field, the 422-with-body behaviour,
the kit hierarchy and cost roll-up, extended quantity through two levels, pinned
versus floating revisions, released-structure immutability, cycle refusal,
where-used reaching the kit, scope so a kit sees its children's evidence, gate
inheritance and ordering, all four RBAC boundaries, waiver refusal for
non-failing and partial waivers, the full remediate-and-pass path, impact
selection and its exclusions, re-analysis preserving completed reviews, approval
blocking, and six webhook rejection paths.

Each test module gets its own freshly seeded database. Sharing one across modules
makes the suite order-dependent, which is a slower failure to diagnose than the
second of setup this costs.

---

## Notes on scaling

All state is in PostgreSQL, so the service scales horizontally — the Dockerfile
runs two workers and more is fine. Migrations are a separate one-shot job in
compose rather than a step in the service entrypoint: running them from every
replica of a scaled service is a race.

The gate engine runs rules sequentially against one session on purpose.
`AsyncSession` is not concurrency-safe, and the rules are cheap because the
evaluation context loads each collection once and caches it for the life of the
evaluation.

## Standing caveats

The seed is extracted from the NPI package — BOM lines and costs from the BOM
data, FMEA rows from the FMEA workbook, control-plan items from the 62-case test
plan — but the values inside that package remain reference figures, not supplier,
calibration or laboratory-committed data. The creepage minimum in particular is
configurable because the real number depends on working voltage, pollution degree
and material group, and must come from the safety assessment rather than from a
constant in a service.
