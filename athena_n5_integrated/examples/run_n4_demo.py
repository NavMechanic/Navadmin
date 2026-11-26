"""End-to-end local N4 demo on SYNTHETIC data (no network, temp database and object store).

    python examples/run_n4_demo.py

A synthetic 24 V / 5 A PSU-120W goes N0 → N2 → N3 → N4:

1. a synthetic RFQ (``synthetic_n4_rfq.json``) is admitted by N0 and released to N2; N2 writes a PRD
   with three hazards (``synthetic_n4_prd.json``) and releases it to N3;
2. N3 runs and verifies the schematic/PCB and simulation packages (ngspice when installed, the test
   stand-ins otherwise), a design authority approves them and they are released to N4;
3. N4 locks the design intake, builds the 14-entry requirements-to-test plan
   (``synthetic_n4_plan.json``) and the PFMEA (``synthetic_n4_pfmea.json``), ranked with the custom
   attention heuristic, never AIAG-VDA Action Priority;
4. the guard-band rule is shown on four readings, then a mock offline CSV is imported: T-01 24.00 ± 0.05 V
   (inside 23.5–24.5 V) and T-02 24.48 ± 0.05 V (the band crosses 24.5 V, so INDETERMINATE);
5. a SCPI log that claims PHYSICAL from a synthetic source is rejected as SYNTHETIC_AS_PHYSICAL;
6. the assessment BLOCKs, the test authority cannot approve it, the release is refused, and the N8
   package stays a DRAFT_NOT_RELEASED preview. Synthetic data never reaches N8.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.n1.config import N1Settings  # noqa: E402
from app.n3.config import N3Settings  # noqa: E402
from app.n4.config import N4Settings  # noqa: E402
from app.rules import canonical_json, guard_band_verdict, sha256_hex  # noqa: E402

HERE = Path(__file__).resolve().parent
NOW = datetime.now(timezone.utc).replace(microsecond=0)
N4_USERS = {"demo.te": ["test_engineer"], "demo.lab": ["lab_importer"], "demo.qr": ["quality_reviewer"],
            "demo.ta": ["test_authority"], "demo.ql": ["quality_lead"], "demo.lo": ["lifecycle_operator"]}
SOPS = {"BENCH_ATE": ("SOP-ATE-001", "C"), "ACCREDITED_LAB": ("SOP-LAB-001", "A"),
        "INSPECTION": ("SOP-INS-001", "B"), "SIM": ("SOP-SIM-001", "A")}


def _n3_demo():
    """Reuse the N3 demo's client, tool discovery and package runner."""
    spec = importlib.util.spec_from_file_location("run_n3_demo", HERE / "run_n3_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.USERS.update(N4_USERS)
    return mod


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF (for SOP documents and calibration certificates)."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    content = "BT /F1 11 Tf 14 TL 50 790 Td " + " ".join(f"({esc(x)}) Tj T*" for x in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> "
            "/Contents 5 0 R >>", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()


def pm(value, u, unit) -> str:
    return f"{value} ± {u} {unit}"


def design_to_n4(n3, d) -> dict:
    """N0 → N2 → N3: synthetic RFQ, PRD with hazards, verified design packages released to N4."""
    for sid, uses in (("SRC-RFQ", ["RFQ_ANALYSIS", "PRODUCT_DEFINITION"]), ("SRC-PRODUCT", ["PRODUCT_DEFINITION"]),
                      ("SRC-DESIGN", ["DESIGN_ENGINEERING", "VERIFICATION", "SOURCING"]),
                      ("SRC-SIMDATA", ["VERIFICATION"]), ("SRC-VER", ["LIFECYCLE", "COMMERCIAL", "FINANCE"])):
        d.call("post", "/v1/sources/register", "demo.steward",
               json={"source_id": sid, "tenant_id": "demo", "publisher": "demo", "license": "proprietary",
                     "permitted_use": uses, "data_origin": "SYNTHETIC"})
        d.call("post", f"/v1/sources/demo/{sid}/approve", "demo.rights",
               json={"decision": "APPROVED", "reason": "demo data, internal use"})
    rfq = json.loads((HERE / "synthetic_n4_rfq.json").read_text())
    rfq["source_sha256"] = sha256_hex(canonical_json(rfq["payload"]))
    d.call("post", "/v1/gateway/evaluate", "demo.analyst", json={"artifact": rfq, "origin_node": "N0"})
    ap = d.call("post", "/v1/approvals", "demo.approver", json={
        "artifact_id": rfq["artifact_id"], "exact_revision": "A", "scope": "RELEASE", "decision": "APPROVED",
        "reason": "RFQ cleared for product definition"}).json()
    d.call("post", "/v1/handoffs/release", "demo.approver", json={
        "artifact_id": rfq["artifact_id"], "revision": "A", "source_node": "N0", "target_nodes": ["N2"],
        "approval_id": ap["approval_event"]["approval_id"]})
    prd_req = json.loads((HERE / "synthetic_n4_prd.json").read_text())
    prd = d.call("post", "/v1/n2/prds", "demo.pm", json=prd_req).json()
    d.call("post", f"/v1/n2/prds/{prd['prd_id']}/revisions/A/release", "demo.pa",
           json={"reason": "PRD approved for design"})
    print(f"N2  PRD {prd['n0_artifact_id']}@{prd['revision']} ({prd['n0_decision']}) with "
          f"{len(prd_req['hazards'])} hazards, released to N3")

    gen = n3._gen()
    files = {"psu.kicad_sch": gen.schematic().encode(), "psu.kicad_pcb": gen.board().encode(),
             "psu.kicad_pro": gen.project().encode(), "psu_bom.csv": gen.bom_csv().encode(),
             "psu_buck.cir": gen.NETLIST.encode()}
    b = d.call("post", "/v1/n3/baselines", "demo.engineer", json={
        "prd_id": prd["prd_id"], "project_id": prd_req["project_id"], "design_source_id": "SRC-DESIGN"}).json()
    wps = d.call("post", f"/v1/n3/baselines/{b['baseline_id']}/plan", "demo.engineer",
                 json={"budget_per_package": {"amount": "20000", "currency": "USD"}}).json()
    print(f"N3  baseline {b['baseline_id']}: {len(wps)} work packages, design revision B")
    for wp in wps:
        a = n3.run_package(d, wp, files, revision="B")
        d.call("post", "/v1/n3/reviews", "demo.authority", json={
            "assessment_id": a["assessment_id"], "decision": "APPROVED", "reason": "evidence checked"})
        print(f"    {wp['kind']:<21} {a['decision']} → approved by demo.authority")
    for scope in ("schematic_pcb", "circuit_simulation"):
        rel = d.call("post", f"/v1/n3/baselines/{b['baseline_id']}/releases", "demo.authority",
                     ok=(201, 207), json={"scope": scope, "targets": ["N4"]}).json()
        for p in rel["packages"]:
            print(f"    release {scope:<19} → {p['target']} {p['status']}")
    return prd_req


def registries(d) -> None:
    """Instrument + in-date calibration, approved SOP revisions, a qualified test authority."""
    d.call("post", "/v1/n4/instruments", "demo.te", json={
        "instrument_id": "DMM-1", "tenant_id": "demo", "kind": "DMM", "manufacturer": "Keysight Technologies",
        "model": "34465A", "serial": "MY59012345", "firmware": "A.03.01-02.40"})
    cal = {"calibration_id": "CAL-DMM-1", "instrument_id": "DMM-1", "certificate_number": "CERT-CAL-DMM-1",
           "calibration_lab": "Example Calibration Services (fictional)", "accreditation_ref": "D-K-99999-01-00",
           "calibrated_at": iso(NOW - timedelta(days=120)), "calibration_due": iso(NOW + timedelta(days=240))}
    d.call("post", "/v1/n4/calibrations", "demo.lab", data={"body": json.dumps(cal)},
           files=[("certificate", ("cert.pdf", pdf(["Calibration certificate CERT-CAL-DMM-1"]), "application/pdf"))])
    for method, (mid, rev) in SOPS.items():
        d.call("post", "/v1/n4/methods", "demo.te", data={"body": json.dumps({
            "method_id": mid, "revision": rev, "title": f"{method} procedure", "method": method})},
            files=[("document", ("sop.pdf", pdf([f"{mid} rev {rev}"]), "application/pdf"))])
        d.call("post", f"/v1/n4/methods/{mid}/revisions/{rev}/approve", "demo.qr")
    d.call("post", "/v1/n4/qualifications", "demo.ql", json={
        "principal_id": "demo.ta", "methods": list(SOPS), "basis": "competence record (demo)",
        "valid_until": (NOW + timedelta(days=365)).date().isoformat()})
    print("N4  registered DMM-1 (calibration due "
          f"{(NOW + timedelta(days=240)).date()}), 4 approved SOP revisions, qualified demo.ta")


def mock_csv() -> bytes:
    cols = ["test_id", "characteristic", "unit_serial", "value", "unit", "expanded_uncertainty", "k",
            "instrument_id", "calibration_id", "captured_at", "method_id", "method_revision", "operator"]
    rows = [["T-01", "output_voltage", "DEMO-0001", "24.00", "V", "0.05", "2", "DMM-1", "CAL-DMM-1",
             iso(NOW - timedelta(days=1)), "SOP-ATE-001", "C", "demo-op"],
            ["T-02", "output_voltage", "DEMO-0001", "24.48", "V", "0.05", "2", "DMM-1", "CAL-DMM-1",
             iso(NOW - timedelta(days=1)), "SOP-ATE-001", "C", "demo-op"]]
    lines = ["# ATHENA_MOCK_FIXTURE, generated fixture, never physical evidence", ",".join(cols)]
    return ("\n".join(lines + [",".join(r) for r in rows]) + "\n").encode()


def main() -> int:
    n3 = _n3_demo()
    kicad, ngspice, notes = n3._tools()
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Settings(database_url=f"sqlite:///{tmp}/demo.db", storage_root=f"{tmp}/objects", tokens={},
                                  jwt_secret=n3.SECRET, jwt_issuer="athena-demo"),
                         n1_settings=N1Settings(),
                         n3_settings=N3Settings(kicad_cli=kicad, ngspice=ngspice, jobs_inline=True,
                                                workroot=f"{tmp}/jobs"),
                         n4_settings=N4Settings())
        d = n3.Demo(TestClient(app))
        print("ATHENA N4 demo: SYNTHETIC data only\n  " + "\n  ".join(notes) + "\n")

        prd_req = design_to_n4(n3, d)
        registries(d)
        i = d.call("post", "/v1/n4/intakes", "demo.te", json={
            "prd_id": prd_req["prd_id"], "project_id": prd_req["project_id"], "release_source_id": "SRC-VER"}).json()
        safety = sorted(r["req_id"] for r in i["requirements"] if r["safety_critical"])
        print(f"\nN4.1 intake {i['intake_id']}: design revision {i['design_revision']}, "
              f"{len(i['design_refs'])} N3 releases hash-verified, synthetic={i['synthetic']}, "
              f"safety-critical {safety}")

        plan_req = json.loads((HERE / "synthetic_n4_plan.json").read_text())
        p = d.call("post", f"/v1/n4/intakes/{i['intake_id']}/plans", "demo.te", json=plan_req).json()
        pid = p["plan_id"]
        tm = d.call("get", f"/v1/n4/plans/{pid}/traceability", "demo.te").json()
        print(f"N4.2 plan {pid}: {len(p['test_cases'])} test cases, safety-critical coverage "
              f"{tm['safety_critical_covered']}/{tm['safety_critical_total']} ({tm['safety_critical_coverage_pct']} %), "
              f"traceability complete={tm['complete']}")

        fm = d.call("post", f"/v1/n4/plans/{pid}/pfmea", "demo.te",
                    json=json.loads((HERE / "synthetic_n4_pfmea.json").read_text())).json()
        print(f"N4.3 PFMEA rev {fm['revision']}: {fm['prioritization_label']}")
        for e in fm["entries"]:
            print(f"    {e['entry_id']} {e['failure_mode']:<26} S{e['severity']} O{e['occurrence']} D{e['detection']} "
                  f"→ attention {e['attention_level']:<6} (illustrative S×O×D {e['rpn_illustrative']})")

        print("\nN4.4 guard band (value ± U against 23.5–24.5 V; U is the expanded uncertainty, k = 2):")
        for v, u in (("24.00", "0.05"), ("24.48", "0.05"), ("24.45", "0.05"), ("24.60", "0.05")):
            print(f"    {pm(v, u, 'V'):<16} → {guard_band_verdict(Decimal(v), Decimal(u), Decimal('23.5'), Decimal('24.5'))}")

        ev = d.call("post", f"/v1/n4/plans/{pid}/evidence/csv", "demo.lab", data={"source_id": "SRC-SIMDATA"},
                    files=[("files", ("mock_bench.csv", mock_csv(), "text/csv"))]).json()
        print(f"\n    offline CSV import {ev['evidence_id']}: {ev['status']}, provenance {ev['derived_provenance']}")
        log = json.loads((HERE / "synthetic_n4_scpi_log.json").read_text())
        log.update(test_id="T-03", unit_serial="DEMO-0001", claimed_provenance="PHYSICAL")
        for rec in log["records"]:
            rec["t"] = iso(NOW - timedelta(hours=2))
        r = d.call("post", f"/v1/n4/plans/{pid}/evidence/scpi", "demo.lab", ok=None,
                   data={"log_name": "scpi_T-03.json", "source_id": "SRC-SIMDATA"},
                   files=[("files", ("scpi_T-03.json", json.dumps(log).encode(), "application/json"))])
        print(f"    SCPI log claiming PHYSICAL from a synthetic source → {r.status_code} {r.json()['status']} "
              f"{r.json()['rejection_codes']}")

        a = d.call("post", f"/v1/n4/plans/{pid}/assess", "demo.te").json()
        print(f"\nN4.5 assessment {a['assessment_id']} → {a['decision']}")
        for tv in a["test_verdicts"]:
            ms = tv["measurements"]
            if tv["test_id"] not in ("T-01", "T-02", "T-14") or not ms:
                continue
            m = min(ms, key=lambda x: Decimal(x["value"]))           # T-14: the worst of the N3 corners
            u = Decimal(m["expanded_uncertainty"])
            value = Decimal(m["value"]).quantize(u) if u.as_tuple().exponent >= -6 else m["value"]
            lim = f"{m.get('lower_limit') or '−∞'}–{m.get('upper_limit') or '∞'} {m['unit']}"
            corners = f" (worst of {len(ms)} N3 corners)" if len(ms) > 1 else ""
            print(f"    {tv['test_id']} {m['characteristic']:<15} {pm(value, u, m['unit']):<16} vs {lim:<15} → reading "
                  f"{m['verdict']:<13} test {tv['verdict']:<13} [{m['provenance']}]{corners}")
        blocks = sorted({f["code"] for f in a["findings"] if f["outcome"] == "BLOCK"})
        print(f"    blocking: {', '.join(blocks)}")
        print("    (an in-limit MOCK_FIXTURE reading is recorded, but the test stays INDETERMINATE: mock and synthetic "
              "data never qualify)")

        r = d.call("post", "/v1/n4/reviews", "demo.ta", ok=None, json={
            "assessment_id": a["assessment_id"], "evidence_sha256": a["evidence_sha256"],
            "design_revision": a["design_revision"], "decision": "APPROVED", "reason": "try to approve"})
        print(f"\nN4.6 test-authority approval attempt → {r.status_code} {r.json()['detail']['code']}")
        r = d.call("post", f"/v1/n4/plans/{pid}/releases", "demo.ta", ok=None, json={"targets": ["N8", "N6", "N7"]})
        print(f"     release attempt → {r.status_code} {r.json()['detail']['code']}")
        draft = d.call("get", f"/v1/n4/plans/{pid}/n8-draft", "demo.te").json()
        print(f"     N8 package: {draft['status']} ({draft['note']})")

        st = d.call("get", "/v1/n4/status", "demo.te").json()["ai"]
        print(f"\nAI   deterministic engine {st['deterministic_policy_engine']}, LLM+RAG {st['llm_rag_suggestions']}, "
              f"vision {st['vision_release_decisions']}, reliability {st['reliability']}")
        verify = d.call("get", "/v1/audit/verify", "demo.auditor").json()
        print(f"     audit and approval chains verify: {verify['audit']['ok']}")
        print(f"\n{a['approval_scope']}")
        app.state.n3.worker.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
