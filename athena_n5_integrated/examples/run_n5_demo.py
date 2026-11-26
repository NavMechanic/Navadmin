"""End-to-end local N5 demo on SYNTHETIC data (no network, temp database and object store).

    python examples/run_n5_demo.py

A synthetic two-component BOM for 10 finished products goes N0 → N2 → N3 → N5 → N6 / N7:

1. a synthetic RFQ is admitted by N0 and released to N2; N2 writes a PRD (``synthetic_n5_prd.json``)
   for the schematic / PCB scope and releases it to N3;
2. N3 verifies the schematic, PCB and DFM packages of a two-line board and releases the engineering
   BOM (exact MPNs, quantities, revision B) to N5;
3. N5 locks the BOM, qualifies three distributors (``synthetic_n5_supplier.json``) and takes three
   quotations (``synthetic_n5_quotes.json``): a cheaper spot-market broker is not an authorized
   channel and is rejected before costing; a second distributor lacks stock on one line;
4. the eligible offers are costed exactly (Q_order = ceil(max(Q_required, MOQ) / pack) × pack):

       Component 1  20 × ₹6.50 + ₹12 = ₹142.00
       Component 2  10 × ₹9.00 + ₹12 = ₹102.00
       Total        ₹244.00 indicative material quote

5. a separate procurement approver approves the exact report and quote hashes, and N0-checked draft
   handoffs reach N6 (commercial) and N7 (finance);
6. N0 then revokes the quotation source's rights: the handoff and the N6 draft turn BLOCKED and N7
   can no longer ingest it.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
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
from app.n5.config import N5Settings  # noqa: E402
from app.rules import canonical_json, sha256_hex  # noqa: E402

HERE = Path(__file__).resolve().parent
N5_USERS = {"demo.pan": ["procurement_analyst"], "demo.sq": ["supplier_quality"],
            "demo.pap": ["procurement_approver"], "demo.co": ["commercial_operator"], "demo.fo": ["finance_operator"]}
BUILD = 10


def _n3_demo():
    spec = importlib.util.spec_from_file_location("run_n3_demo", HERE / "run_n3_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.USERS.update(N5_USERS)
    return mod


def rupees(v) -> str:
    return f"₹{Decimal(str(v)).quantize(Decimal('0.01'))}"


def design_to_n5(n3, d) -> dict:
    for sid, uses in (("SRC-RFQ", ["RFQ_ANALYSIS", "PRODUCT_DEFINITION"]), ("SRC-PRODUCT", ["PRODUCT_DEFINITION"]),
                      ("SRC-DESIGN", ["DESIGN_ENGINEERING", "VERIFICATION", "SOURCING"]),
                      ("SRC-QUOTES", ["SOURCING"]), ("SRC-PRC", ["COMMERCIAL", "FINANCE"])):
        d.call("post", "/v1/sources/register", "demo.steward",
               json={"source_id": sid, "tenant_id": "demo", "publisher": "demo", "license": "proprietary",
                     "permitted_use": uses, "data_origin": "SYNTHETIC"})
        d.call("post", f"/v1/sources/demo/{sid}/approve", "demo.rights",
               json={"decision": "APPROVED", "reason": "demo data, internal use"})
    rfq = json.loads((HERE / "synthetic_n3_rfq.json").read_text())
    rfq["source_sha256"] = sha256_hex(canonical_json(rfq["payload"]))
    d.call("post", "/v1/gateway/evaluate", "demo.analyst", json={"artifact": rfq, "origin_node": "N0"})
    ap = d.call("post", "/v1/approvals", "demo.approver", json={
        "artifact_id": rfq["artifact_id"], "exact_revision": "A", "scope": "RELEASE", "decision": "APPROVED",
        "reason": "RFQ cleared for product definition"}).json()
    d.call("post", "/v1/handoffs/release", "demo.approver", json={
        "artifact_id": rfq["artifact_id"], "revision": "A", "source_node": "N0", "target_nodes": ["N2"],
        "approval_id": ap["approval_event"]["approval_id"]})
    prd_req = json.loads((HERE / "synthetic_n5_prd.json").read_text())
    prd = d.call("post", "/v1/n2/prds", "demo.pm", json=prd_req).json()
    d.call("post", f"/v1/n2/prds/{prd['prd_id']}/revisions/A/release", "demo.pa",
           json={"reason": "PRD approved for design"})
    print(f"N2  PRD {prd['n0_artifact_id']}@{prd['revision']} ({prd['n0_decision']}) released to N3")

    gen = n3._gen()
    parts = [p for p in gen.PARTS if p[0] in ("C1", "C2", "L1")]
    files = {"psu.kicad_sch": gen.schematic(parts=parts).encode(),
             "psu.kicad_pcb": gen.board(refs=[p[0] for p in parts]).encode(),
             "psu.kicad_pro": gen.project().encode(), "psu_bom.csv": gen.bom_csv(parts=parts).encode()}
    b = d.call("post", "/v1/n3/baselines", "demo.engineer", json={
        "prd_id": prd["prd_id"], "project_id": prd_req["project_id"], "design_source_id": "SRC-DESIGN"}).json()
    wps = d.call("post", f"/v1/n3/baselines/{b['baseline_id']}/plan", "demo.engineer",
                 json={"budget_per_package": {"amount": "20000", "currency": "USD"}}).json()
    for wp in wps:
        a = n3.run_package(d, wp, files, revision="B")
        d.call("post", "/v1/n3/reviews", "demo.authority", json={
            "assessment_id": a["assessment_id"], "decision": "APPROVED", "reason": "evidence checked"})
        print(f"    {wp['kind']:<21} {a['decision']} → approved by demo.authority")
    rel = d.call("post", f"/v1/n3/baselines/{b['baseline_id']}/releases", "demo.authority", ok=(201, 207),
                 json={"scope": "schematic_pcb", "targets": ["N5"]}).json()
    for p in rel["packages"]:
        print(f"N3  {p['artifact_type']} {p['artifact_id']}@{p['revision']} → {p['target']} {p['status']}")
    return prd_req


def main() -> int:
    n3 = _n3_demo()
    kicad, ngspice, notes = n3._tools()
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Settings(database_url=f"sqlite:///{tmp}/demo.db", storage_root=f"{tmp}/objects", tokens={},
                                  jwt_secret=n3.SECRET, jwt_issuer="athena-demo"),
                         n1_settings=N1Settings(),
                         n3_settings=N3Settings(kicad_cli=kicad, ngspice=ngspice, jobs_inline=True,
                                                workroot=f"{tmp}/jobs"),
                         n4_settings=N4Settings(), n5_settings=N5Settings())
        d = n3.Demo(TestClient(app))
        print("ATHENA N5 demo: SYNTHETIC data only\n  " + "\n  ".join(notes) + "\n")
        prd_req = design_to_n5(n3, d)

        # N5.1 BOM intake
        i = d.call("post", "/v1/n5/bom-intakes", "demo.pan", json={
            "prd_id": prd_req["prd_id"], "project_id": prd_req["project_id"], "build_quantity": BUILD,
            "target_lead_time_days": 30, "release_source_id": "SRC-PRC"}).json()
        print(f"\nN5.1 intake {i['intake_id']}: {i['bom_ref']}, {BUILD} finished products, synthetic={i['synthetic']}")
        for ln in i["lines"]:
            print(f"    line {ln['line']} {ln['mpn_as_released']:<19} {ln['manufacturer']:<7} × {ln['qty_per_unit']} "
                  f"→ Q_required {ln['q_required']}")

        # N5.2 suppliers
        for body in json.loads((HERE / "synthetic_n5_supplier.json").read_text()):
            d.call("post", "/v1/n5/suppliers", "demo.pan", json=body)
            mfrs = [c["manufacturer"] for c in body["channel_proofs"]]
            s = d.call("post", f"/v1/n5/suppliers/{body['supplier_id']}/review", "demo.sq", json={
                "decision": "APPROVED", "approved_until": "2030-12-31", "verified_channel_manufacturers": mfrs,
                "attestation": "GSTIN checked; channel proofs checked on the manufacturers' distributor lists"}).json()
            print(f"N5.2 {s['supplier_id']:<16} {s['status']} by demo.sq, authorized channel for {mfrs}")

        # N5.3 quotations
        for body in json.loads((HERE / "synthetic_n5_quotes.json").read_text()):
            body["bom_intake_id"] = i["intake_id"]
            q = d.call("post", "/v1/n5/quotes", "demo.pan", json=body).json()
            print(f"N5.3 quote {body['quote_ref']:<8} {body['supplier_id']:<16} {q['status']} "
                  f"(sha256 {q['quote_sha256'][:12]}…)")
        st = d.call("get", "/v1/n5/status", "demo.pan").json()
        print(f"     live distributor APIs enabled: {st['live_apis_enabled']} (DigiKey / Mouser / Nexar stay off "
              "unless N5_ENABLE_LIVE_APIS=true)")

        # N5.4 – N5.5 report
        r = d.call("post", "/v1/n5/reports", "demo.pan", json={"bom_intake_id": i["intake_id"]}).json()
        print(f"\nN5.4 guardrails → report {r['report_id']} decision {r['decision']}")
        for ln in r["lines"]:
            for x in ln["offers"]:
                if not x["eligible"]:
                    print(f"    line {ln['line']} {x['supplier_id']:<16} {rupees(x['unit_price']):>7} INELIGIBLE "
                          f"{x['codes']} (filtered before costing)")
        print("N5.5 eligible offers ranked by extended material cost:")
        names = {}
        for n, ln in enumerate(sorted(r["lines"], key=lambda x: -x["qty_per_unit"]), 1):
            names[ln["line"]] = f"Component {n}"
            top = ln["offers"][0]
            print(f"    Component {n} ({ln['mpn']}, {top['supplier_id']}): {top['q_order']} × {rupees(top['unit_price'])} "
                  f"+ ₹{Decimal(top['line_charges_total']):.0f} = {rupees(top['extended_cost'])}   "
                  f"[MOQ / pack → Q_order {top['q_order']}]")
        print(f"    Total {rupees(r['indicative_total'])} indicative material quote for {BUILD} finished products "
              f"({rupees(r['indicative_unit_cost'])} per product)")
        print(f"    {r['indicative_scope']}")

        # N5.6 review + handoff
        rv = d.call("post", "/v1/n5/reviews", "demo.pap", json={
            "report_id": r["report_id"], "report_sha256": r["report_sha256"], "quote_hashes": r["quote_hashes"],
            "decision": "APPROVED", "reason": "report and quote hashes checked"}).json()
        print(f"\nN5.6 approved by demo.pap (review {rv['review_id']}, report {r['report_sha256'][:12]}…)")
        h = d.call("post", f"/v1/n5/reports/{r['report_id']}/handoffs", "demo.pap", ok=(201, 207),
                   json={"targets": ["N6", "N7"]}).json()
        pk = {x["target"]: x for x in h["packages"]}
        for x in h["packages"]:
            print(f"    {x['target']} {x['artifact_id']}@{x['revision']} N0 {x['n0_decision']} → {x['status']} "
                  f"({x['manifest_id']})")
        bid = d.call("post", "/v1/n6/intake", "demo.co", json={"artifact_id": pk["N6"]["artifact_id"],
                                                                "revision": pk["N6"]["revision"]}).json()
        print(f"    N6 draft bid input {bid['intake_id']}: {rupees(bid['indicative_unit_material_cost'])} per product, "
              f"risk flags {[f['flags'] for f in bid['supplier_risk_flags']]}")

        # dynamic rights revocation
        d.call("post", "/v1/sources/demo/SRC-QUOTES/approve", "demo.rights",
               json={"decision": "REVOKED", "reason": "supplier withdrew consent to use its quotation"})
        hs = d.call("get", f"/v1/n5/subjects/handoff/{h['handoff_id']}/status", "demo.pap").json()
        code = next((e.get("code") for e in hs["history"] if e["status"] == "BLOCKED"), None)
        print(f"\nN0 revoked SRC-QUOTES → N5 handoff {hs['status']} ({code}: {hs['reasons'][0]})")
        v = d.call("get", f"/v1/n6/bid-inputs/{bid['intake_id']}", "demo.co").json()
        print(f"    N6 draft {bid['intake_id']} is now {v['status']}")
        res = d.call("post", "/v1/n7/intake", "demo.fo", ok=None, json={"artifact_id": pk["N7"]["artifact_id"],
                                                                          "revision": pk["N7"]["revision"]})
        print(f"    N7 intake attempt → {res.status_code} {res.json()['detail']['code']}")
        verify = d.call("get", "/v1/audit/verify", "demo.auditor").json()
        print(f"    audit and approval chains verify: {verify['audit']['ok']}")
        print(f"\n{r['approval_scope']}")
        app.state.n3.worker.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
