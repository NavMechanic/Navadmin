"""ATHENA DQ Gateway acceptance tests.

T01 tenant isolation            T07 stale quote
T02 missing critical field      T08 unapproved MPN / signed ECO
T03 unit normalization          T09 unfunded cash gap
T04 ERC/DRC failure             T10 synthetic data presented as real
T05 revision drift + FB-12      T11 rights / connector access (No Rights, No Access)
T06 expired calibration         T12 hard controls override the score; HOLD → RELEASED

plus integrity (DQ05), arithmetic (DQ23), calibration labelling (DQ26), training feedback
(DQ27), forged approvals (DQ13), idempotency, and the wiring into the RAG pipeline, the
Digital Twin telemetry path, the HTTP API, the dashboard and the CLI.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from rag.dq_gateway import (EDGES, MANIFEST_FIELDS, RULES, ArtifactRegistry, ArtifactSubmission, DQGateRejected,
                            DQGateway, DQGatewayError, EvidenceRecord, TelemetryGate, build_manifest, decide_gate)
from rag.dq_gateway.rules import RuleResult
from rag.dq_gateway.units import UnitError, normalize

NOW = datetime.now(timezone.utc)
PRODUCT = "AV-PCBA-1"


# ----------------------------------------------------------------------------------------------
# test world
# ----------------------------------------------------------------------------------------------
class World:
    """One tenant with a project, a signed NDA, a G0 rights grant and two approvers."""

    def __init__(self, gw: DQGateway, tenant: str = "acme", project: str = "PRJ-1", rights: str = "APPROVED",
                 grant: bool = True) -> None:
        self.gw, self.reg, self.tenant, self.project = gw, gw.registry, tenant, project
        self.nda = f"EV-NDA-{tenant}"
        self.reg.register_project(project, tenant, [PRODUCT])
        self.reg.add_evidence(EvidenceRecord(evidence_id=self.nda, tenant_id=tenant, project_id=project, kind="NDA",
                                             signed=True, signed_by="legal", valid_from=NOW - timedelta(days=60)))
        self.approver, self.engineer = f"{tenant}.qa", f"{tenant}.eng"
        self.reg.add_approver(self.approver, tenant, ["G1", "G2", "G3", "G4", "G5", "G7"])
        self.reg.add_approver(f"{tenant}.buyer", tenant, ["G4"])
        self.g0 = self.reg.grant_rights(project, tenant, rights, self.nda, approved_by="legal").ref if grant else None

    def evidence(self, evidence_id: str, kind: str, **kw) -> str:
        self.reg.add_evidence(EvidenceRecord(evidence_id=evidence_id, tenant_id=self.tenant, project_id=self.project,
                                             kind=kind, **kw))
        return evidence_id

    def submission(self, node, artifact_id, rev, schema, payload, *, sources=None, evidence=None,
                   tool="tool:athena@1.0", origin="REAL", rights="APPROVED", content=None, files=None,
                   required_critical=(), side_effect=None, use_for_training=False, ai_provenance=None,
                   simulator_source=False, transport="HTTP", **kw) -> ArtifactSubmission:
        content = content if content is not None else json.dumps(payload, sort_keys=True, default=str).encode()
        manifest = build_manifest(
            registry=self.reg, project_id=self.project, node_id=node, artifact_id=artifact_id, revision=rev,
            output_schema=schema, content=content, sources=sources if sources is not None else [self.g0 or f"G0-{self.project}@1"],
            evidence_ids=evidence if evidence is not None else [self.nda], rights=rights,
            classification="CONFIDENTIAL", origin=origin, model_or_tool=tool, **kw)
        return ArtifactSubmission(manifest=manifest, payload=payload, content=content, files=files or {},
                                  required_critical=list(required_critical), side_effect=side_effect,
                                  use_for_training=use_for_training, ai_provenance=ai_provenance,
                                  simulator_source=simulator_source, transport=transport)

    def submit(self, *args, actor=None, tenant=None, **kw):
        return self.gw.submit(self.submission(*args, **kw), tenant or self.tenant, actor or self.engineer)

    def release(self, *args, **kw):
        d = self.submit(*args, **kw)
        assert d.quality_gate == "HOLD", (d.quality_gate, d.reasons, [(r.rule_id, r.message) for r in d.failed_rules])
        d = self.gw.approve(d.event_id, self.approver, self.tenant, "test sign-off")
        assert d.quality_gate == "RELEASED", (d.reasons, [(r.rule_id, r.message) for r in d.failed_rules])
        return d

    # canned payloads --------------------------------------------------------------------------
    @staticmethod
    def prd(**over):
        return {"product_id": PRODUCT, "specs": {"input_voltage": 24.0},
                "requirements": [{"req_id": "REQ-1", "text": "Operate from 24 V", "critical": True,
                                  "verification_ids": ["VER-1"]}], **over}

    def release_prd(self, rev="A"):
        return self.release("N2", "PRD-1", rev, "product_prd.v1", self.prd(), measurement_units={"input_voltage": "V"})

    @staticmethod
    def bom(rev="A", mpns=("NRF52840-QIAA-R",), avl=("NRF52840-QIAA-R",)):
        return {"product_id": PRODUCT, "bom_revision": rev, "avl_mpns": list(avl),
                "lines": [{"mpn": m, "manufacturer": "Nordic", "qty_per_unit": 1} for m in mpns]}

    def release_bom(self, rev="A", mpns=("NRF52840-QIAA-R",), avl=("NRF52840-QIAA-R",), **kw):
        return self.release("N5", "BOM-1", rev, "bom_item.v1", self.bom(rev, mpns, avl), **kw)

    @staticmethod
    def quote(**over):
        q = {"supplier_quote_id": "SQ-1", "supplier_id": "VEND-1", "bom_revision": "A", "mpn": "NRF52840-QIAA-R",
             "quoted_quantity": 1000, "unit_price": "310.00", "currency": "INR", "gst_included": False,
             "valid_until": (date.today() + timedelta(days=20)).isoformat(), "min_order_qty": 100,
             "lead_time_days": 21}
        q.update(over)
        return q

    def submit_quote(self, artifact_id="SQ-1", rev="1", bom_ref="BOM-1@A", **over):
        return self.submit("N5", artifact_id, rev, "supplier_quote.v1", self.quote(**over), sources=[bom_ref],
                           tool="vendor:luminovo@2026", vendor_id="AT-0582", source_revision=bom_ref.split("@")[1])

    @staticmethod
    def pnl(wc="900000", gap="480000", commitment="PO", **over):
        p = {"currency": "INR", "gst_rate": "0.18", "commitment": commitment,
             "lines": {"bom": "310000", "nre": "150000", "tooling": "40000", "lab": "60000", "finance": "12000"},
             "declared": {"subtotal_ex_gst": "572000.00", "gst": "102960.00", "total_inc_gst": "674960.00"},
             "funding": {"available_working_capital": wc, "peak_cash_gap": gap}}
        p.update(over)
        return p


def rule(decision, rule_id) -> RuleResult:
    return next(r for r in decision.rule_results if r.rule_id == rule_id)


def failed(decision) -> set[str]:
    return {r.rule_id for r in decision.failed_rules}


@pytest.fixture
def world(gateway):
    return World(gateway)


# ----------------------------------------------------------------------------------------------
# structure
# ----------------------------------------------------------------------------------------------
def test_contract_sizes():
    assert len(MANIFEST_FIELDS) == 33
    assert sorted(RULES) == [f"DQ{i:02d}" for i in range(1, 28)]
    assert [e.edge_id for e in EDGES] == [f"E-{i:03d}" for i in range(1, 31)]


# ----------------------------------------------------------------------------------------------
# T01 tenant isolation
# ----------------------------------------------------------------------------------------------
def test_t01_tenant_isolation(gateway):
    a = World(gateway, tenant="acme", project="PRJ-A")
    b = World(gateway, tenant="globex", project="PRJ-B")
    bom = a.release_bom()

    # B submits into A's project: stopped at RIGHTS (G0), payload not retained
    d = gateway.submit(a.submission("N5", "BOM-X", "A", "bom_item.v1", a.bom()), "globex", b.engineer)
    assert d.quality_gate == "BLOCK" and {"DQ01", "DQ09"} <= failed(d)
    assert d.stages["rights"]["stopped"] is True
    assert gateway._submissions[d.event_id][0].payload == {} and gateway._submissions[d.event_id][0].content is None

    # B cites A's released artifact from its own project
    d = b.submit("N5", "SQ-B", "1", "supplier_quote.v1", World.quote(), sources=[bom.released_ref],
                 tool="vendor:luminovo@2026", vendor_id="AT-0582", source_revision="A")
    assert d.quality_gate == "BLOCK" and "DQ09" in failed(d)

    # B reusing A's artifact ID (even with identical bytes) is not aliased onto A's record
    d = b.submit("N5", "BOM-1", "A", "bom_item.v1", World.bom())
    assert d.quality_gate == "BLOCK" and "DQ09" in failed(d)
    assert gateway.registry.get("BOM-1@A").tenant_id == "acme"

    # B cannot read, approve or export A's artifacts
    assert gateway.decision(bom.event_id, "globex") is None
    with pytest.raises(DQGatewayError):
        gateway.approve(bom.event_id, b.approver, "globex")
    assert not gateway.authorize_connector("CON-012", "PRJ-A", [bom.released_ref], "globex").ok
    assert all(x.tenant_id == "globex" for x in gateway.decisions("globex"))


# ----------------------------------------------------------------------------------------------
# T02 missing critical field (no silent defaults)
# ----------------------------------------------------------------------------------------------
def test_t02_missing_critical_field(world):
    payload = World.prd()
    del payload["product_id"]
    d = world.submit("N2", "PRD-1", "A", "product_prd.v1", payload, measurement_units={"input_voltage": "V"})
    assert d.quality_gate == "REWORK"          # a missing field is REWORK (DQ03), not a schema QUARANTINE
    assert failed(d) == {"DQ03"} and "product_id" in rule(d, "DQ03").message

    d = world.submit("N2", "PRD-2", "A", "product_prd.v1", World.prd(), measurement_units={"input_voltage": "V"},
                     required_critical=["specs.max_current"])
    assert d.quality_gate == "REWORK" and "specs.max_current" in rule(d, "DQ03").message

    d = world.submit("N2", "PRD-3", "A", "product_prd.v1", World.prd(product_id=""),
                     measurement_units={"input_voltage": "V"})
    assert d.quality_gate == "REWORK" and "DQ03" in failed(d)


def test_t02_telemetry_missing_reading_never_defaulted(gateway):
    from rag.digital_twin.broker.mqtt_client import BrokerConfig, MQTTBroker
    from rag.digital_twin.twin import DigitalTwin

    w = World(gateway)
    gate = TelemetryGate(gateway, w.tenant, w.project, rights="APPROVED", simulator=True)
    broker, twin = MQTTBroker(BrokerConfig(simulate=True)), DigitalTwin()
    broker._sim_unit = 1
    msgs = [broker._generate_plc_message(n) for n in ("A_FASTENING", "B_DISPENSING", "C_PRESS")]
    msgs[0].torque_nm = None
    assert twin.ingest_plc_cycle(msgs, gate) is None
    assert twin._history == []
    rej = twin.telemetry_rejections[-1]["rejected"][0]
    assert rej["rules"] == ["DQ03"] and "readings.torque_nm" in rej["messages"][0]

    broker._sim_unit = 2
    msgs = [broker._generate_plc_message(n) for n in ("A_FASTENING", "B_DISPENSING", "C_PRESS")]
    msgs[2].fixture_id = None                  # previously hard-coded to "BE-45" by the dashboard
    assert twin.ingest_plc_cycle(msgs, gate) is None
    assert "fixture_id" in twin.telemetry_rejections[-1]["rejected"][0]["messages"][0]


# ----------------------------------------------------------------------------------------------
# T03 unit normalization
# ----------------------------------------------------------------------------------------------
def test_t03_unit_normalization(world):
    assert normalize(25.4, "mm") == (pytest.approx(0.0254), "m")
    assert normalize(3.0, "bar") == (pytest.approx(300000.0), "Pa")
    assert normalize(300.0, "K")[1] == "degC"
    with pytest.raises(UnitError):
        normalize(1.0, "in")

    d = world.submit("N2", "PRD-1", "A", "product_prd.v1", World.prd(specs={"board_length": 120.0, "input_voltage": 24.0}),
                     measurement_units={"board_length": "mm", "input_voltage": "V"})
    assert d.quality_gate == "HOLD"
    si = d.stages["S01_feature_bundle"]["si_values"]
    assert si["board_length"]["value"] == pytest.approx(0.12) and si["board_length"]["unit"] == "m"

    d = world.submit("N2", "PRD-2", "A", "product_prd.v1", World.prd(specs={"board_length": 4.7}),
                     measurement_units={"board_length": "in"})
    assert d.quality_gate == "REWORK" and failed(d) == {"DQ04"}

    d = world.submit("N2", "PRD-3", "A", "product_prd.v1", World.prd(specs={"board_length": 120.0}),
                     measurement_units={})
    assert d.quality_gate == "REWORK" and "board_length" in rule(d, "DQ04").message


# ----------------------------------------------------------------------------------------------
# T04 ERC/DRC failure (and native design files)
# ----------------------------------------------------------------------------------------------
def _design(world, finding_status="OPEN", waiver=None, files=None):
    pcb = b"(kicad_pcb (version 20240108))"
    files = files if files is not None else {"board.kicad_pcb": pcb}
    payload = {"product_id": PRODUCT, "design_revision": "A",
               "files": [{"name": n, "sha256": hashlib.sha256(b).hexdigest(), "format": n.rsplit(".", 1)[-1]}
                         for n, b in files.items()],
               "eda_report": {"tool": "kicad-drc@8.0", "findings": [
                   {"check": "DRC", "rule": "clearance", "severity": "CRITICAL", "status": finding_status,
                    "waiver_evidence_id": waiver}]}}
    return world.submit("N3", "DES-1", "A", "design_package.v1", payload, sources=["PRD-1@A"], files=files,
                        tool="tool:kicad@8.0")


def test_t04_erc_drc_failure(world):
    world.release_prd()
    d = _design(world)
    assert d.quality_gate == "BLOCK" and "DQ16" in failed(d) and "DRC:clearance" in rule(d, "DQ16").message

    unsigned = world.evidence("EV-WAIVER-0", "WAIVER", signed=False)
    assert "DQ16" in failed(_design(world, "WAIVED", unsigned))

    waiver = world.evidence("EV-WAIVER-1", "WAIVER", signed=True, signed_by="chief.engineer")
    d = _design(world, "WAIVED", waiver)
    assert "DQ16" not in failed(d) and d.quality_gate == "HOLD"
    assert world.gw.handoffs.authorize(world.reg.get("PRD-1@A"), "N3").edge_id == "E-007"


def test_t04_pdf_only_design_is_rework(world):
    world.release_prd()
    d = _design(world, "RESOLVED", files={"board.pdf": b"%PDF-1.7"})
    assert d.quality_gate == "REWORK" and failed(d) == {"DQ17"}


# ----------------------------------------------------------------------------------------------
# T05 revision drift + FB-12 cascade
# ----------------------------------------------------------------------------------------------
def test_t05_revision_drift_and_fb12(world):
    world.release_bom("A")
    q = world.submit_quote()
    q = world.gw.approve(q.event_id, world.approver, world.tenant)
    assert q.quality_gate == "RELEASED"

    bump = world.release_bom("B")
    assert world.reg.get("BOM-1@A").status == "SUPERSEDED"
    assert world.reg.get("SQ-1@1").status == "REWORK"
    fb12 = [e for e in world.gw.open_exceptions(world.tenant) if e.fb_code == "FB-12"]
    assert fb12 and fb12[0].artifact_ref == "SQ-1@1" and bump.event_id == fb12[0].event_id

    d = world.submit_quote("SQ-2")                   # still pinned to BOM-1@A
    assert d.quality_gate == "BLOCK"
    assert rule(d, "DQ08").error_code == "REVISION_DRIFT" and "DQ06" in failed(d)
    assert not world.gw.authorize_connector("CON-012", world.project, ["SQ-1@1"], world.tenant).ok

    d = world.submit_quote("SQ-3", bom_ref="BOM-1@B", bom_revision="B")
    assert d.quality_gate == "HOLD"


# ----------------------------------------------------------------------------------------------
# T06 expired calibration
# ----------------------------------------------------------------------------------------------
def _verification(world, cal_id):
    payload = {"product_id": PRODUCT, "instruments": [{"instrument_id": "DMM-7", "calibration_id": cal_id}],
               "tests": [{"test_id": "T-HIPOT", "req_ids": ["REQ-1"], "safety_critical": True, "result": "PASS",
                          "instrument_id": "DMM-7", "signed_evidence_id": "EV-SIGNED-1"}]}
    return world.submit("N4", "VER-1", "A", "verification_result.v1", payload, sources=["PRD-1@A"],
                        tool="tool:nextest@4.2")


def test_t06_expired_calibration(world):
    world.release_prd()
    world.evidence("EV-SIGNED-1", "SIGNED_TEST", signed=True, signed_by="qa")
    world.evidence("CAL-OLD", "CALIBRATION", valid_from=NOW - timedelta(days=400), valid_until=NOW - timedelta(days=35))
    world.evidence("CAL-OK", "CALIBRATION", valid_from=NOW - timedelta(days=30), valid_until=NOW + timedelta(days=335))

    d = _verification(world, "CAL-OLD")
    assert d.quality_gate == "BLOCK" and failed(d) == {"DQ18"} and "DMM-7:CAL-OLD" in rule(d, "DQ18").message
    d = _verification(world, "CAL-OK")
    assert d.quality_gate == "HOLD"


def test_t06_real_telemetry_needs_in_date_calibration(gateway):
    from rag.digital_twin.broker.mqtt_client import PLCMessage

    w = World(gateway)
    w.evidence("CAL-S22-OLD", "CALIBRATION", valid_from=NOW - timedelta(days=400), valid_until=NOW - timedelta(days=1))
    w.evidence("CAL-S22", "CALIBRATION", valid_from=NOW - timedelta(days=10), valid_until=NOW + timedelta(days=355))
    gate = TelemetryGate(gateway, w.tenant, w.project, rights="APPROVED", simulator=False)

    def msg(cal, seconds):
        return PLCMessage(node="A_FASTENING", station_id="S22", torque_nm=0.45, depth_mm=3.2, cycle_time_s=2.0,
                          timestamp=(NOW - timedelta(seconds=seconds)).isoformat(), unit_id="U-0001", lot_id="LOT-01",
                          sensor_id="S22-TQ", sensor_status="CALIBRATED", calibration_id=cal, origin_type="REAL")

    v = gate.check(msg("CAL-S22-OLD", 20))
    assert not v.admitted and "DQ25" in v.failed_rules
    assert gate.check(msg("CAL-S22", 10)).admitted
    v = gate.check(msg("CAL-S22", 15))                 # older than the last admitted event
    assert not v.admitted and "non-monotonic" in " ".join(v.messages)


# ----------------------------------------------------------------------------------------------
# T07 stale quote
# ----------------------------------------------------------------------------------------------
def test_t07_stale_quote(world):
    world.release_bom()
    d = world.submit_quote("SQ-OLD", valid_until=(date.today() - timedelta(days=1)).isoformat())
    assert d.quality_gate == "REWORK" and failed(d) == {"DQ20"} and rule(d, "DQ20").error_code == "QUOTE_STALE"

    d = world.submit_quote("SQ-USD", currency="USD")
    assert d.quality_gate == "REWORK" and "fx_rate_to_inr" in rule(d, "DQ20").message

    assert world.submit_quote("SQ-OK").quality_gate == "HOLD"


def test_t07_quote_expiring_before_approval_is_reworked_at_approval(gateway):
    clock = {"now": NOW}
    gw = DQGateway(registry=gateway.registry, clock=lambda: clock["now"])
    w = World(gw)
    w.release_bom()
    d = w.submit_quote(valid_until=(date.today() + timedelta(days=1)).isoformat())
    assert d.quality_gate == "HOLD"
    clock["now"] = NOW + timedelta(days=3)               # approval comes after expiry: rules are re-run
    d = gw.approve(d.event_id, w.approver, w.tenant)
    assert d.quality_gate == "REWORK" and "DQ20" in failed(d)
    assert gw.registry.get("SQ-1@1") is None


# ----------------------------------------------------------------------------------------------
# T08 unapproved MPN / signed ECO
# ----------------------------------------------------------------------------------------------
def test_t08_unapproved_mpn(world):
    world.release_bom()
    d = world.submit_quote("SQ-ALT", mpn="NRF52833-QDAA-R")
    assert d.quality_gate == "BLOCK" and {"DQ08", "DQ21"} <= failed(d)

    world.evidence("ECO-9", "ECO", signed=False, attributes={"from_mpn": "NRF52840-QIAA-R", "to_mpn": "NRF52833-QDAA-R"})
    d = world.submit_quote("SQ-ALT2", mpn="NRF52833-QDAA-R", substitution_eco_id="ECO-9")
    assert "DQ21" in failed(d)

    world.evidence("ECO-10", "ECO", signed=True, signed_by="design.authority",
                   attributes={"from_mpn": "NRF52840-QIAA-R", "to_mpn": "NRF52833-QDAA-R"})
    d = world.submit_quote("SQ-ALT3", mpn="NRF52833-QDAA-R", substitution_eco_id="ECO-10")
    assert d.quality_gate == "HOLD" and "under signed ECO ECO-10" in rule(d, "DQ21").message


# ----------------------------------------------------------------------------------------------
# T09 unfunded cash gap
# ----------------------------------------------------------------------------------------------
def test_t09_unfunded_cash_gap(world):
    d = world.submit("N7", "PNL-1", "1", "project_pnl.v1", World.pnl(wc="250000"), tool="engine:athena-cost@1.0")
    assert d.quality_gate == "BLOCK" and failed(d) == {"DQ24"}
    assert d.weighted_score >= 90                       # advisory score is high; the hard control still blocks

    d = world.submit("N7", "PNL-2", "1", "project_pnl.v1", World.pnl(wc="250000", commitment="NONE"),
                     tool="engine:athena-cost@1.0")
    assert not rule(d, "DQ24").applicable and d.quality_gate == "HOLD"

    d = world.submit("N7", "PNL-3", "1", "project_pnl.v1", World.pnl(commitment="NONE"),
                     tool="engine:athena-cost@1.0", side_effect="PO",
                     candidate_actions=[{"action": "po", "cost_inr": "0"}, {"action": "approve", "cost_inr": "0"}])
    assert d.quality_gate == "HOLD"                     # funded; the PO side effect waits for sign-off
    po = next(a for a in d.feasible_actions if a["action"] == "po")
    assert not po["feasible"] and "HOLD" in po["reason"]


# ----------------------------------------------------------------------------------------------
# T10 synthetic data presented as real
# ----------------------------------------------------------------------------------------------
def test_t10_synthetic_as_real(world):
    from rag.digital_twin.broker.mqtt_client import BrokerConfig, MQTTBroker
    from rag.digital_twin.twin import DigitalTwin

    # simulator output relabelled REAL never reaches the twin
    gate = TelemetryGate(world.gw, world.tenant, world.project, rights="APPROVED", simulator=True)
    broker, twin = MQTTBroker(BrokerConfig(simulate=True)), DigitalTwin()
    broker._sim_unit = 1
    msgs = [broker._generate_plc_message(n) for n in ("A_FASTENING", "B_DISPENSING", "C_PRESS")]
    for m in msgs:
        m.origin_type = "REAL"
    assert twin.ingest_plc_cycle(msgs, gate) is None and twin.field_evidence_history() == []
    assert "DQ10" in twin.telemetry_rejections[-1]["rejected"][0]["rules"]

    # honest synthetic data is admitted but is never field evidence
    broker._sim_unit = 2
    snap = twin.ingest_plc_cycle([broker._generate_plc_message(n) for n in ("A_FASTENING", "B_DISPENSING", "C_PRESS")], gate)
    assert snap is not None and snap.origin_type == "SYNTHETIC" and not snap.is_field_evidence
    assert len(snap.dq_event_ids) == 3 and twin.field_evidence_history() == []

    # an artifact built on synthetic sources cannot claim REAL
    world.release("N5", "BOM-SIM", "A", "bom_item.v1", World.bom(), origin="SYNTHETIC")
    d = world.submit_quote("SQ-SIM", bom_ref="BOM-SIM@A")
    assert d.quality_gate == "BLOCK" and "DQ10" in failed(d)

    # origin_type is immutable across revisions
    d = world.submit("N5", "BOM-SIM", "B", "bom_item.v1", World.bom("B"), origin="REAL")
    assert d.quality_gate == "BLOCK" and "immutable" in rule(d, "DQ10").message

    # language-model output is not REAL field data
    d = world.submit("X", "AI-1", "1", "ai_output.v1",
                     {"task": "summarise", "prompt_revision": "p1", "input_hashes": ["a" * 64],
                      "claims": [{"text": "x", "kind": "opinion"}]}, tool="llm:claude@2026", origin="REAL")
    assert "DQ10" in failed(d)


# ----------------------------------------------------------------------------------------------
# T11 rights / connector access
# ----------------------------------------------------------------------------------------------
def test_t11_no_rights_no_access(gateway):
    w = World(gateway, tenant="acme", project="PRJ-R")
    d = w.submit("N5", "BOM-1", "A", "bom_item.v1", World.bom(), rights="UNVERIFIED")
    assert d.quality_gate == "BLOCK" and "DQ01" in failed(d) and d.stages["rights"]["stopped"]
    assert "ingest" not in d.stages                      # nothing past G0 ran

    no_grant = World(gateway, tenant="initech", project="PRJ-N", grant=False)
    d = no_grant.submit("N5", "BOM-1", "A", "bom_item.v1", World.bom())
    assert d.quality_gate == "BLOCK" and "no active G0" in rule(d, "DQ01").message


def test_t11_connector_authorization(gateway):
    w = World(gateway)
    w.release_bom()
    ok = gateway.authorize_connector("CON-012", w.project, ["BOM-1@A"], w.tenant)
    assert ok.ok and ok.ticket.startswith("CON-012-") and not ok.no_train
    assert not gateway.authorize_connector("CON-048", w.project, ["BOM-1@A"], w.tenant).ok
    assert not gateway.authorize_connector("CON-012", w.project, ["BOM-1@Z"], w.tenant).ok

    rfq = {"rfq_id": "RFQ-1", "customer_id": "CUST-1", "service_mode": "SINGLE_NODE", "requested_nodes": ["N5"],
           "product_category": "IoT", "application_use": "asset tracking", "delivery_country": "IN",
           "consent_scope": "INTERNAL_ONLY"}
    w.release("N1", "RFQ-1", "1", "customer_rfq.v1", rfq)
    w.release("N1", "MO-1", "1", "market_opportunity.v1",
              {"opportunity_id": "MO-1", "account_id": "ACC-77", "account_name": "Fleet Co", "segment": "logistics",
               "basis": "SAM", "spend_provenance": [w.nda]}, sources=["RFQ-1@1"])
    auth = gateway.authorize_connector("CON-001", w.project, ["MO-1@1"], w.tenant)
    assert not auth.ok and "consent_scope is INTERNAL_ONLY" in " ".join(auth.reasons)

    nt = World(gateway, tenant="umbrella", project="PRJ-NT", rights="APPROVED_NO_TRAIN")
    nt.release("N5", "BOM-NT", "A", "bom_item.v1", World.bom(), rights="APPROVED_NO_TRAIN")
    assert gateway.authorize_connector("CON-005", nt.project, ["BOM-NT@A"], nt.tenant).no_train


# ----------------------------------------------------------------------------------------------
# T12 hard controls override the score; HOLD → RELEASED approval
# ----------------------------------------------------------------------------------------------
def _r(rule_id, passed, action, severity="CRITICAL"):
    return RuleResult(rule_id, severity, passed, action, "" if passed else "X", "")


def test_t12_gate_matrix_order():
    ok = [_r("DQ01", True, "BLOCK")]
    assert decide_gate([_r("DQ05", False, "QUARANTINE"), _r("DQ01", False, "BLOCK")], {}, 100, True)[0] == "QUARANTINE"
    assert decide_gate([_r("DQ16", False, "BLOCK")], {}, 100, True)[0] == "BLOCK"
    assert decide_gate(ok, {"ext_safety_review": "UNKNOWN"}, 100, True)[0] == "BLOCK"
    assert decide_gate([_r("DQ20", False, "REWORK", "MAJOR")], {}, 99, True)[0] == "REWORK"
    assert decide_gate(ok, {}, 89.9, True)[0] == "REWORK"
    assert decide_gate(ok, {}, 100, False)[0] == "HOLD"
    assert decide_gate(ok, {}, 100, True)[0] == "RELEASED"


def test_t12_hold_to_released_approval(world):
    d = world.submit("N2", "PRD-1", "A", "product_prd.v1", World.prd(), measurement_units={"input_voltage": "V"})
    assert d.quality_gate == "HOLD" and d.weighted_score >= 90 and world.reg.get("PRD-1@A") is None

    world.reg.add_approver(world.engineer, world.tenant, ["G3"])      # even a qualified submitter
    with pytest.raises(DQGatewayError, match="four-eyes"):
        world.gw.approve(d.event_id, world.engineer, world.tenant)
    with pytest.raises(DQGatewayError, match="not an authorized G3"):
        world.gw.approve(d.event_id, f"{world.tenant}.buyer", world.tenant)     # holds G4 only

    rel = world.gw.approve(d.event_id, world.approver, world.tenant, "baseline")
    assert rel.quality_gate == "RELEASED" and rel.released_ref == "PRD-1@A"
    assert rel.evaluated_manifest["reviewer_id"] == world.approver and rel.approval["gate"] == "G3"
    assert world.reg.get("PRD-1@A").approved_by == world.approver
    with pytest.raises(DQGatewayError, match="only HOLD"):
        world.gw.approve(d.event_id, world.approver, world.tenant)


def test_t12_forged_approval_claim_is_blocked(world):
    sub = world.submission("N2", "PRD-1", "A", "product_prd.v1", World.prd(), measurement_units={"input_voltage": "V"})
    sub.manifest.update(reviewer_id=world.approver, approval_timestamp=sub.manifest["processed_at"],
                        quality_gate="RELEASED")
    d = world.gw.submit(sub, world.tenant, world.engineer)
    assert d.quality_gate == "BLOCK" and failed(d) == {"DQ13"} and world.reg.get("PRD-1@A") is None


# ----------------------------------------------------------------------------------------------
# integrity, arithmetic, calibration labelling, learning, idempotency
# ----------------------------------------------------------------------------------------------
def test_dq05_checksum_mismatch_quarantines(world):
    sub = world.submission("N5", "BOM-1", "A", "bom_item.v1", World.bom())
    d = world.gw.submit(sub.model_copy(update={"content": b"tampered"}), world.tenant, world.engineer)
    assert d.quality_gate == "QUARANTINE" and "DQ05" in failed(d)


def test_dq23_no_language_model_arithmetic(world):
    d = world.submit("N7", "PNL-1", "1", "project_pnl.v1", World.pnl(), tool="llm:claude@2026", origin="DERIVED",
                     ai_provenance={"prompt_revision": "p1", "input_hashes": ["b" * 64]})
    assert d.quality_gate == "BLOCK" and "DQ23" in failed(d)
    bad = World.pnl()
    bad["declared"] = {**bad["declared"], "gst": "102960.01"}
    d = world.submit("N7", "PNL-2", "1", "project_pnl.v1", bad, tool="engine:athena-cost@1.0")
    assert d.quality_gate == "BLOCK" and "gst" in rule(d, "DQ23").message
    assert Decimal("572000.00") * Decimal("0.18") == Decimal("102960.0000")


def test_dq26_uncalibrated_score_is_relabelled_heuristic(world):
    d = world.submit("N2", "PRD-1", "A", "product_prd.v1", World.prd(), measurement_units={"input_voltage": "V"},
                     risk_method="CALIBRATED_ML", risk_score=0.12)
    assert "DQ26" in failed(d) and d.quality_gate == "REWORK"
    assert d.evaluated_manifest["risk_method"] == "HEURISTIC"


def test_dq27_training_feedback(world):
    world.release_bom()
    sub = world.submission("N5", "SQ-1", "1", "supplier_quote.v1", World.quote(), sources=["BOM-1@A"],
                           tool="vendor:luminovo@2026", vendor_id="AT-0582", source_revision="A",
                           use_for_training=True, origin="SYNTHETIC")
    d = world.gw.submit(sub, world.tenant, world.engineer)
    msg = rule(d, "DQ27").message
    assert "feedback_authorized is false" in msg and "cannot train" in msg and "labeled_by" in msg
    assert world.gw.learning_set == []


def test_idempotent_submit_and_event_reuse(world):
    sub = world.submission("N5", "BOM-1", "A", "bom_item.v1", World.bom())
    first = world.gw.submit(sub, world.tenant, world.engineer)
    assert world.gw.submit(sub, world.tenant, world.engineer) is first
    other = world.submission("N5", "BOM-1", "A", "bom_item.v1", World.bom(mpns=("X-1",)), event_id=sub.manifest["event_id"])
    d = world.gw.submit(other, world.tenant, world.engineer)
    assert d.quality_gate == "QUARANTINE" and "idempotency" in d.reasons[0]
    assert world.gw.decision(first.event_id, world.tenant) is first


def test_recorded_approval_survives_process_restart(tmp_path):
    path = tmp_path / "registry.json"
    gw1 = DQGateway(registry=ArtifactRegistry(path))
    w = World(gw1)
    sub = w.submission("N5", "BOM-1", "A", "bom_item.v1", World.bom())
    d = gw1.submit(sub, w.tenant, w.engineer)
    gw1.approve(d.event_id, w.approver, w.tenant)

    gw2 = DQGateway(registry=ArtifactRegistry(path))     # e.g. the CLI, sharing the registry file
    assert gw2.submit(sub, w.tenant, w.engineer).quality_gate == "RELEASED"
    forged = sub.model_copy(update={"manifest": {**sub.manifest, "event_id": str(uuid.uuid4())}})
    assert DQGateway(registry=ArtifactRegistry(path)).submit(forged, w.tenant, w.engineer).quality_gate == "HOLD"


# ----------------------------------------------------------------------------------------------
# RAG pipeline wiring (Qdrant only sees admitted artifacts)
# ----------------------------------------------------------------------------------------------
def _points(pipeline):
    store = pipeline._vector_store
    pts, _ = store._client.scroll(store.config.collection_name, limit=10_000, with_payload=True)
    return [p.payload for p in pts]


def _corpus_submission(world, path, artifact_id, origin="REAL", node="X"):
    from rag.dq_gateway.sources import read_kind
    content, _ = read_kind(path, "document")
    return world.submission(node, artifact_id, "1", "document_corpus.v1", {}, content=content, origin=origin,
                            tool="tool:doc-loader@1.0", input_schema="raw_file.v1")


@pytest.fixture
def pipeline_world(gateway, tmp_path):
    from rag.pipeline import RAGPipeline
    w = World(gateway)
    doc = tmp_path / "spec.txt"
    doc.write_text("Station 22 torque spec is 0.45 N·m. Dispense 352 mg TMQ-100 per unit. " * 5)
    return w, RAGPipeline(dq_gateway=gateway, tenant_id=w.tenant), doc


def test_pipeline_indexes_only_released(pipeline_world):
    w, pipeline, doc = pipeline_world
    with pytest.raises(DQGateRejected, match="no Canonical I-O Manifest"):
        pipeline.ingest(str(doc))
    sub = _corpus_submission(w, doc, "DOC-1")
    with pytest.raises(DQGateRejected) as exc:
        pipeline.ingest(str(doc), sub)
    assert exc.value.decision.quality_gate == "HOLD" and _points(pipeline) == []

    w.gw.approve(exc.value.decision.event_id, w.approver, w.tenant)
    assert pipeline.ingest(str(doc), sub) > 0
    payloads = _points(pipeline)
    assert payloads and all(p["dq_quality_gate"] == "RELEASED" and p["dq_origin_type"] == "REAL"
                            and p["dq_field_evidence"] is True and p["dq_artifact_ref"] == "DOC-1@1" for p in payloads)


def test_pipeline_tampered_bytes_index_nothing(pipeline_world):
    w, pipeline, doc = pipeline_world
    sub = _corpus_submission(w, doc, "DOC-2")
    doc.write_text("edited after the manifest was signed")
    with pytest.raises(DQGateRejected) as exc:
        pipeline.ingest(str(doc), sub)
    assert exc.value.decision.quality_gate == "QUARANTINE" and _points(pipeline) == []


def test_pipeline_sandbox_tags_synthetic(pipeline_world):
    from rag.pipeline import RAGPipeline
    w, _, doc = pipeline_world
    sandbox = RAGPipeline(dq_gateway=w.gw, tenant_id=w.tenant, dq_mode="sandbox")
    assert sandbox.ingest(str(doc), _corpus_submission(w, doc, "DOC-S", origin="SYNTHETIC")) > 0
    payloads = _points(sandbox)
    assert all(p["dq_quality_gate"] == "HOLD" and p["dq_origin_type"] == "SYNTHETIC" and p["dq_sandbox"] is True
               and p["dq_field_evidence"] is False for p in payloads)
    with pytest.raises(DQGateRejected):                   # sandbox never admits REAL data that lacks sign-off
        sandbox.ingest(str(doc), _corpus_submission(w, doc, "DOC-R", origin="REAL"))


def test_pipeline_rejects_other_tenant(pipeline_world):
    from rag.pipeline import RAGPipeline
    w, _, doc = pipeline_world
    other = RAGPipeline(dq_gateway=w.gw, tenant_id="globex")
    with pytest.raises(DQGateRejected) as exc:
        other.ingest(str(doc), _corpus_submission(w, doc, "DOC-T"))
    assert exc.value.decision.quality_gate == "BLOCK" and _points(other) == []


# ----------------------------------------------------------------------------------------------
# telemetry → twin, REAL path
# ----------------------------------------------------------------------------------------------
def test_twin_real_cycle_is_field_evidence(gateway):
    from rag.digital_twin.broker.mqtt_client import PLCMessage
    from rag.digital_twin.twin import DigitalTwin, assemble_cycles

    w = World(gateway)
    for s in ("S22", "S31", "S42"):
        w.evidence(f"CAL-{s}", "CALIBRATION", valid_from=NOW - timedelta(days=5), valid_until=NOW + timedelta(days=360))
    gate = TelemetryGate(gateway, w.tenant, w.project, rights="APPROVED", simulator=False)
    common = dict(unit_id="U-1001", lot_id="LOT-7", sensor_status="CALIBRATED", origin_type="REAL",
                  timestamp=NOW.isoformat())
    msgs = [PLCMessage(node="A_FASTENING", station_id="S22", torque_nm=0.45, depth_mm=3.2, cycle_time_s=2.1,
                       sensor_id="S22-TQ", calibration_id="CAL-S22", **common),
            PLCMessage(node="B_DISPENSING", station_id="S31", mass_tmq_mg=352.0, mass_lm_mg=45.0,
                       nozzle_pressure_bar=3.0, humidity_pct=45.0, sensor_id="S31-DS", calibration_id="CAL-S31", **common),
            PLCMessage(node="C_PRESS", station_id="S42", inlay_force_n=1400.0, display_force_n=1210.0, dwell_time_s=15.0,
                       frame_offset_mm=0.0, fixture_id="PA-49", sensor_id="S42-PR", calibration_id="CAL-S42", **common)]
    pending: dict = {}
    assert assemble_cycles(msgs[:2], pending) == [] and "U-1001" in pending
    cycles = assemble_cycles(msgs[2:], pending)
    assert len(cycles) == 1 and pending == {}

    twin = DigitalTwin()
    snap = twin.ingest_plc_cycle(cycles[0], gate)
    assert snap is not None and snap.origin_type == "REAL" and snap.is_field_evidence
    assert twin.field_evidence_history() == [snap]
    assert twin.run_batch_simulation(n=3)[0].origin_type == "SYNTHETIC"
    assert len(twin.field_evidence_history()) == 1

    from rag.digital_twin.broker.mqtt_client import PLCMessage as P
    assert P.from_json(json.dumps({"node": "C_PRESS", "station_id": "S42", "vendor_extra": 1})).station_id == "S42"


# ----------------------------------------------------------------------------------------------
# HTTP API
# ----------------------------------------------------------------------------------------------
@pytest.fixture
def api(gateway):
    from fastapi.testclient import TestClient
    from api.gateway_router import create_app
    w = World(gateway, tenant="demo", project="PRJ-API")
    w.release_bom()
    app = create_app(gateway, {"tok-eng": {"tenant_id": "demo", "actor_id": w.engineer},
                               "tok-qa": {"tenant_id": "demo", "actor_id": w.approver},
                               "tok-x": {"tenant_id": "globex", "actor_id": "globex.eng"}},
                     connector_secrets={"CON-012": {"secret": "s3cr3t", "tenant_id": "demo"}})
    return w, TestClient(app)


def _wire(sub: ArtifactSubmission) -> dict:
    return {"manifest": json.loads(json.dumps(sub.manifest, default=str)), "payload": sub.payload,
            "content_b64": base64.b64encode(sub.content).decode()}


def _h(token, key=None, **extra):
    h = {"Authorization": f"Bearer {token}", **extra}
    if key:
        h["Idempotency-Key"] = key
    return h


def test_api_submit_idempotency_and_approve(api):
    w, c = api
    body = _wire(w.submission("N2", "PRD-1", "A", "product_prd.v1", World.prd(), measurement_units={"input_voltage": "V"}))
    r = c.post(f"/api/v1/projects/{w.project}/artifacts", json=body, headers=_h("tok-eng", "k-1"))
    assert r.status_code == 202 and r.json()["quality_gate"] == "HOLD"
    again = c.post(f"/api/v1/projects/{w.project}/artifacts", json=body, headers=_h("tok-eng", "k-1"))
    assert again.status_code == 202 and again.headers["Idempotent-Replay"] == "true"
    assert again.json()["event_id"] == r.json()["event_id"]
    changed = {**body, "required_critical": ["specs.input_voltage"]}
    assert c.post(f"/api/v1/projects/{w.project}/artifacts", json=changed, headers=_h("tok-eng", "k-1")).status_code == 409
    assert c.post(f"/api/v1/projects/{w.project}/artifacts", json=body, headers=_h("tok-eng")).status_code == 400
    assert c.post(f"/api/v1/projects/{w.project}/artifacts", json=body).status_code == 401
    assert c.post("/api/v1/projects/OTHER/artifacts", json=body, headers=_h("tok-eng", "k-2")).status_code == 400

    ev = r.json()["event_id"]
    assert c.post(f"/api/v1/workflows/{ev}/approve", json={}, headers=_h("tok-eng", "a-1")).status_code == 403
    assert c.post(f"/api/v1/workflows/{ev}/approve", json={}, headers=_h("tok-x", "a-1")).status_code == 404
    ok = c.post(f"/api/v1/workflows/{ev}/approve", json={"notes": "ok"}, headers=_h("tok-qa", "a-1"))
    assert ok.status_code == 200 and ok.json()["quality_gate"] == "RELEASED"
    assert c.get(f"/api/v1/decisions/{ev}", headers=_h("tok-x")).status_code == 404


def test_api_rfq_upload_and_rejections(api):
    w, c = api
    rfq = {"rfq_id": "RFQ-9", "customer_id": "CUST-1", "service_mode": "SINGLE_NODE", "requested_nodes": ["N5"],
           "product_category": "IoT", "application_use": "tracking", "delivery_country": "IN",
           "consent_scope": "VENDOR_ALLOWED"}
    body = _wire(w.submission("N1", "RFQ-9", "1", "customer_rfq.v1", rfq))
    assert c.post("/api/v1/rfqs", json=body, headers=_h("tok-eng", "r-1")).status_code == 202
    prd = _wire(w.submission("N2", "PRD-9", "A", "product_prd.v1", World.prd(), measurement_units={"input_voltage": "V"}))
    assert c.post("/api/v1/rfqs", json=prd, headers=_h("tok-eng", "r-2")).status_code == 422

    sha = json.loads(json.dumps(body))["manifest"]["output_file_sha256"]
    up = f"/api/v1/projects/{w.project}/artifact-upload"
    assert c.post(up, json=body, headers=_h("tok-eng", "u-1")).status_code == 400                    # header required
    assert c.post(up, json=body, headers=_h("tok-eng", "u-2", **{"X-Content-SHA256": "0" * 64})).status_code == 400
    assert c.post(up, json=body, headers=_h("tok-eng", "u-3", **{"X-Content-SHA256": sha})).status_code == 202

    stale = _wire(w.submission("N5", "SQ-9", "1", "supplier_quote.v1",
                               World.quote(valid_until=(date.today() - timedelta(days=2)).isoformat()),
                               sources=["BOM-1@A"], tool="vendor:luminovo@2026", vendor_id="AT-0582", source_revision="A"))
    r = c.post(f"/api/v1/projects/{w.project}/artifacts", json=stale, headers=_h("tok-eng", "s-1"))
    assert r.status_code == 422 and r.json()["quality_gate"] == "REWORK" and r.json()["failed_rules"] == ["DQ20"]


def test_api_connector_and_signed_callback(api):
    from api.gateway_router import sign_callback
    w, c = api
    r = c.post("/api/v1/vendor-connectors/CON-012/run", json={"project_id": w.project, "artifact_refs": ["BOM-1@A"]},
               headers=_h("tok-eng", "c-1"))
    assert r.status_code == 200 and r.json()["ticket"].startswith("CON-012-")
    assert c.post("/api/v1/vendor-connectors/CON-099/run", json={"project_id": w.project, "artifact_refs": ["BOM-1@A"]},
                  headers=_h("tok-eng", "c-2")).status_code == 403

    raw = json.dumps(_wire(w.submission("N5", "SQ-CB", "1", "supplier_quote.v1", World.quote(), sources=["BOM-1@A"],
                                        tool="vendor:luminovo@2026", vendor_id="AT-0582", source_revision="A"))).encode()
    ts, nonce = str(int(time.time())), uuid.uuid4().hex
    hdr = {"Content-Type": "application/json", "Idempotency-Key": "cb-1", "X-Athena-Timestamp": ts,
           "X-Athena-Nonce": nonce, "X-Athena-Signature": sign_callback("s3cr3t", ts, nonce, raw)}
    url = "/api/v1/vendor-callbacks/CON-012"
    assert c.post(url, content=raw, headers={**hdr, "X-Athena-Signature": "sha256=" + "0" * 64}).status_code == 401
    old = str(int(time.time()) - 3600)
    assert c.post(url, content=raw, headers={**hdr, "X-Athena-Timestamp": old,
                                             "X-Athena-Signature": sign_callback("s3cr3t", old, nonce, raw)}).status_code == 401
    r = c.post(url, content=raw, headers=hdr)
    assert r.status_code == 202 and r.json()["quality_gate"] == "HOLD" and r.json()["actor_id"] == "vendor:CON-012"
    assert c.post(url, content=raw, headers={**hdr, "Idempotency-Key": "cb-2"}).status_code == 409      # nonce replay
    assert c.post("/api/v1/vendor-callbacks/CON-013", content=raw, headers=hdr).status_code == 401


def test_api_lifecycle_batch(api):
    w, c = api
    w.evidence("CAL-S22", "CALIBRATION", valid_from=NOW - timedelta(days=5), valid_until=NOW + timedelta(days=360))
    events = []
    for i, unit in enumerate(("U-0001", "bad id!")):
        t = (NOW - timedelta(seconds=10 - i)).isoformat()
        payload = {"event_kind": "station", "unit_id": unit, "lot_id": "LOT-1", "station_id": "S22", "event_time": t,
                   "readings": {"torque_nm": 0.45}, "sensor": {"sensor_id": "S22-TQ", "status": "CALIBRATED",
                                                               "calibration_id": "CAL-S22"}}
        events.append(_wire(w.submission("N8", f"LE-{i}", "1", "lifecycle_event.v1", payload, tool="sensor:S22",
                                         evidence=[w.nda, "CAL-S22"], measurement_units={"torque_nm": "N·m"},
                                         captured_at=datetime.fromisoformat(t))))
    r = c.post("/api/v1/lifecycle/events", json={"events": events}, headers=_h("tok-eng", "l-1"))
    assert r.status_code == 200
    assert r.json()["summary"] == {"HOLD": 1, "BLOCK": 1}
    assert "DQ25" in r.json()["results"][1]["failed_rules"]
    wrong = [_wire(w.submission("N5", "BOM-Z", "A", "bom_item.v1", World.bom()))]
    assert c.post("/api/v1/lifecycle/events", json={"events": wrong}, headers=_h("tok-eng", "l-2")).status_code == 422


# ----------------------------------------------------------------------------------------------
# dashboard
# ----------------------------------------------------------------------------------------------
def test_control_tower_data_and_approval(gateway, demo):
    from dashboard.components.dq_control_tower import approve_from_ui, control_tower_data

    data = control_tower_data(gateway, "demo")
    assert data["gates"] == {"Released": 1, "Blocked": 2, "Rework": 1, "Held": 1, "Quarantined": 0}
    assert [row["node"] for row in data["scorecard"]] == [f"N{i}" for i in range(1, 9)]
    assert {e["fb_code"] for e in data["exceptions"]} >= {"FB-07", "FB-11"}
    assert len(data["fb_summary"]) == 12
    assert control_tower_data(gateway, "someone-else")["total_events"] == 0

    ok, msg = approve_from_ui(gateway, "demo", demo["hold_event"], "demo.engineer")
    assert not ok and "not an authorized G3 approver" in msg
    ok, msg = approve_from_ui(gateway, "demo", demo["hold_event"], demo["approver"])
    assert ok and "RELEASED" in msg
    assert control_tower_data(gateway, "demo")["gates"]["Held"] == 0


def test_dashboard_renders_control_tower_tab():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("../dashboard/app.py", default_timeout=60)
    at.session_state["live_streaming"] = False
    at.run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Digital Twin", "DQ Control Tower"]
    assert {m.label for m in at.metric} >= {"Released", "Blocked", "Rework", "Held"}
    next(b for b in at.button if "Approve" in b.label).click().run()
    assert any("RELEASED" in s.value for s in at.success)


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------
def test_cli_manifest_then_gated_ingest(tmp_path, capsys):
    import main

    reg = str(tmp_path / "reg.json")
    doc = tmp_path / "notes.txt"
    doc.write_text("Press fixture PA-49 dwell time is 15 s. " * 10)
    sub_path = str(tmp_path / "sub.json")

    def run(*argv):
        args = main.build_parser().parse_args(list(argv))
        args.func(args)

    run("dq-seed-demo", "--dq-registry", reg)
    run("dq-manifest", str(doc), "--kind", "document", "--project", "AVIATOR-DEMO", "--node", "X",
        "--artifact-id", "NOTES-1", "--rev", "1", "--rights", "APPROVED", "--classification", "INTERNAL",
        "--origin", "REAL", "--tool", "tool:doc-loader@1.0", "--evidence", "EV-NDA-DEMO",
        "--dq-registry", reg, "--out", sub_path)
    with pytest.raises(SystemExit) as exc:          # HOLD without a sign-off: nothing indexed
        run("ingest", str(doc), "--submission", sub_path, "--tenant", "demo", "--dq-registry", reg)
    assert exc.value.code == 2 and "DQ gate: HOLD" in capsys.readouterr().out

    run("ingest", str(doc), "--submission", sub_path, "--tenant", "demo", "--dq-registry", reg,
        "--approve-as", "demo.qa-lead")
    out = capsys.readouterr().out
    assert "RELEASED" in out and "Ingested" in out and "Ingested 0" not in out
    # a later process re-presenting the released artifact needs no second sign-off
    run("ingest", str(doc), "--submission", sub_path, "--tenant", "demo", "--dq-registry", reg)
    assert "Ingested" in capsys.readouterr().out
