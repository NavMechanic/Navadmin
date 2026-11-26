"""End-to-end local N3 demo on SYNTHETIC design data (no network, temp database and object store).

    python examples/run_n3_demo.py

A synthetic RFQ is released through N0 to N2, which writes a PRD (``synthetic_n3_prd.json``) that a
product authority releases to N3. N3 then:

1. locks the PRD into a baseline and plans work packages for all three design scopes;
2. runs KiCad / ngspice jobs in the sandbox and verifies the deliverables;
3. shows a hard BLOCK (a DRC clearance violation) that a cost-optimizer waiver cannot clear, then the
   reworked revision that passes;
4. has a separate design authority approve each package and release N0-checked manifests to N4 / N5;
5. revises the PRD, which invalidates the baseline and everything built on it.

Real ``ngspice`` / ``kicad-cli`` (KiCad 8+) are used when installed. Otherwise the stand-ins in
``tests/fixtures/bin`` are used, and the demo says so.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.n1.config import N1Settings  # noqa: E402
from app.n3 import parsers  # noqa: E402
from app.n3.config import N3Settings  # noqa: E402
from app.rules import canonical_json, sha256_hex  # noqa: E402

HERE = Path(__file__).resolve().parent
FAKES = ROOT / "tests" / "fixtures" / "bin"
SECRET = "athena-n3-local-demo-secret-" + "d" * 32
USERS = {"demo.steward": ["source_admin"], "demo.rights": ["rights_approver"], "demo.analyst": ["analyst"],
         "demo.approver": ["approver"], "demo.pm": ["product_manager"], "demo.pa": ["product_authority"],
         "demo.engineer": ["design_engineer"], "demo.authority": ["design_authority"], "demo.auditor": ["auditor"]}
PACKAGES = {
    "ELECTRICAL_SCHEMATIC": (["psu.kicad_sch", "psu.kicad_pro"],
                             [("kicad-cli", "sch_erc", ["psu.kicad_sch", "psu.kicad_pro"]),
                              ("kicad-cli", "sch_export_bom", ["psu.kicad_sch", "psu.kicad_pro"])]),
    "PCB_LAYOUT": (["psu.kicad_pcb", "psu.kicad_sch", "psu.kicad_pro", "psu_bom.csv"],
                   [("kicad-cli", "pcb_drc", ["psu.kicad_pcb", "psu.kicad_sch", "psu.kicad_pro"])]),
    "DFM": (["psu.kicad_pcb", "psu.kicad_pro"],
            [("kicad-cli", "pcb_drc", ["psu.kicad_pcb", "psu.kicad_pro"]),
             ("kicad-cli", "pcb_export_gerbers", ["psu.kicad_pcb", "psu.kicad_pro"]),
             ("kicad-cli", "pcb_export_drill", ["psu.kicad_pcb", "psu.kicad_pro"])]),
    "CIRCUIT_SIMULATION": (["psu_buck.cir", "psu_bom.csv"], [("ngspice", "spice_corners", ["psu_buck.cir"])]),
    "MECHANICAL": (["enclosure.step"], []),
    "THERMAL": (["enclosure.step", "thermal_report.json"], []),
}
TOOL_VERSIONS = {"NETLIST": ("ngspice", "42"), "PROJECT": ("kicad", "8.0")}


def _gen():
    spec = importlib.util.spec_from_file_location("gen_designs", HERE / "designs" / "gen_designs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tools() -> tuple[str, str, list[str]]:
    notes = []
    ngspice = shutil.which("ngspice")
    if ngspice:
        notes.append(f"ngspice: real binary {ngspice}")
    else:
        ngspice = str(FAKES / "ngspice")
        notes.append("ngspice: not installed, using the test stand-in")
    kicad = shutil.which("kicad-cli")
    major = 0
    if kicad:
        try:
            major = int(subprocess.run([kicad, "version"], capture_output=True, text=True, timeout=30)
                        .stdout.strip().split(".")[0])
        except (OSError, ValueError, subprocess.TimeoutExpired):
            major = 0
    if kicad and major >= 8:
        notes.append(f"kicad-cli: real KiCad {major} at {kicad}")
    else:
        notes.append("kicad-cli: KiCad 8+ not installed (sch erc / pcb drc need 8.0), using the test stand-in")
        kicad = str(FAKES / "kicad-cli")
    return kicad, ngspice, notes


class Demo:
    def __init__(self, client: TestClient) -> None:
        self.c = client

    def call(self, method: str, path: str, who: str, ok=(200, 201), **kw):
        tok = jwt.encode({"sub": who, "tenant_id": "demo", "roles": USERS[who], "iss": "athena-demo",
                          "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
        r = getattr(self.c, method)(path, headers={"Authorization": f"Bearer {tok}"}, **kw)
        if ok and r.status_code not in ok:
            raise SystemExit(f"{method.upper()} {path} → {r.status_code}: {r.text[:800]}")
        return r


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _manifest(files: dict[str, bytes], revision: str) -> str:
    entries = []
    for name, data in files.items():
        role = parsers.detect(name, data).role
        tool, version = TOOL_VERSIONS.get(role, (None, None))
        entries.append({"path": name, "role": role, "sha256": _sha(data), "tool": tool, "tool_version": version})
    return json.dumps({"design_revision": revision, "entries": entries})


def run_package(d: Demo, wp: dict, files: dict[str, bytes], revision: str = "B", route: bool = True) -> dict:
    kind, wid = wp["kind"], wp["work_package_id"]
    names, jobs = PACKAGES[kind]
    pkg = {n: files[n] for n in names}
    if route and jobs:
        d.call("post", f"/v1/n3/work-packages/{wid}/route", "demo.engineer",
               json={"executor": "TOOL", "tool": jobs[0][0], "budget": {"amount": "500", "currency": "USD"}})
    d.call("post", f"/v1/n3/work-packages/{wid}/deliveries", "demo.engineer", data={"manifest": _manifest(pkg, revision)},
           files=[("files", (n, b, "application/octet-stream")) for n, b in pkg.items()])
    for tool, op, inputs in jobs:
        job = d.call("post", f"/v1/n3/work-packages/{wid}/jobs", "demo.engineer",
                     json={"tool": tool, "operation": op, "input_sha256": [_sha(pkg[n]) for n in inputs]}).json()
        stand_in = " (test stand-in, not the real tool)" if job["argv"][0][0].startswith(str(FAKES)) else ""
        print(f"      job {op:<20} {job['status']:<9} {tool} {job['tool_version']}{stand_in}")
    return d.call("post", f"/v1/n3/work-packages/{wid}/assess", "demo.engineer").json()


def main() -> int:
    gen = _gen()
    kicad, ngspice, notes = _tools()
    files = {"psu.kicad_sch": gen.schematic().encode(), "psu.kicad_pcb": gen.board().encode(),
             "psu.kicad_pro": gen.project().encode(), "psu_bom.csv": gen.bom_csv().encode(),
             "psu_buck.cir": gen.NETLIST.encode(), "thermal_report.json": json.dumps(gen.THERMAL).encode(),
             "enclosure.step": gen.step().encode()}
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Settings(database_url=f"sqlite:///{tmp}/demo.db", storage_root=f"{tmp}/objects", tokens={},
                                  jwt_secret=SECRET, jwt_issuer="athena-demo"),
                         n1_settings=N1Settings(),
                         n3_settings=N3Settings(kicad_cli=kicad, ngspice=ngspice, jobs_inline=True,
                                                workroot=f"{tmp}/jobs"))
        d = Demo(TestClient(app))
        print("ATHENA N3 demo: SYNTHETIC data only\n  " + "\n  ".join(notes))

        for sid, uses, extra in (("SRC-RFQ", ["RFQ_ANALYSIS", "PRODUCT_DEFINITION"], {}),
                                 ("SRC-PRODUCT", ["PRODUCT_DEFINITION"], {}),
                                 ("SRC-DESIGN", ["DESIGN_ENGINEERING", "VERIFICATION", "SOURCING"], {})):
            d.call("post", "/v1/sources/register", "demo.steward",
                   json={"source_id": sid, "tenant_id": "demo", "publisher": "demo", "license": "proprietary",
                         "permitted_use": uses, "data_origin": "SYNTHETIC", **extra})
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
        prd_req = json.loads((HERE / "synthetic_n3_prd.json").read_text())
        prd = d.call("post", "/v1/n2/prds", "demo.pm", json=prd_req).json()
        d.call("post", f"/v1/n2/prds/{prd['prd_id']}/revisions/A/release", "demo.pa",
               json={"reason": "PRD approved for design"})
        print(f"\nN2  PRD {prd['n0_artifact_id']}@{prd['revision']} admitted by N0 ({prd['n0_decision']}) "
              "and released to N3")

        b = d.call("post", "/v1/n3/baselines", "demo.engineer", json={
            "prd_id": prd["prd_id"], "project_id": prd_req["project_id"], "design_source_id": "SRC-DESIGN"}).json()
        print(f"N3.1 baseline {b['baseline_id']} locked to {b['prd_artifact']} "
              f"(payload {b['prd_payload_sha256'][:12]}…, approval {b['prd_approval_hash'][:12]}…)")
        wps = {w["kind"]: w for w in d.call("post", f"/v1/n3/baselines/{b['baseline_id']}/plan", "demo.engineer",
                                            json={"budget_per_package": {"amount": "20000", "currency": "USD"}}).json()}
        print(f"N3.2 planned {len(wps)} immutable work packages: {', '.join(wps)}")

        print("\nN3.4–N3.5 first PCB revision B carries a DRC clearance violation:")
        bad = {**files, "psu.kicad_pcb": gen.board(marker="ATHENA_TEST_DRC clearance:error").encode()}
        a = run_package(d, wps["PCB_LAYOUT"], bad)
        print(f"      assessment {a['decision']}: {sorted({f['code'] for f in a['findings'] if f['outcome'] != 'INFO'})}")
        actions = json.loads((HERE / "synthetic_n3_actions.json").read_text())
        actions["assessment_id"] = a["assessment_id"]
        act = d.call("post", f"/v1/n3/assessments/{a['assessment_id']}/actions", "demo.engineer", json=actions).json()
        print(f"N3.6 proposals (never executed): recommended {[x['action_id'] for x in act['ranked'] if x['recommended']]}"
              f", refused {[(x['action_id'], x['codes'][0]) for x in act['blocked']]}; decision still "
              f"{act['assessment_decision']}")
        r = d.call("post", "/v1/n3/reviews", "demo.authority", ok=None,
                   json={"assessment_id": a["assessment_id"], "decision": "APPROVED", "reason": "try to approve"})
        print(f"      design authority approval attempt → {r.status_code} {r.json()['detail']['code']}")

        print("\nN3.4–N3.6 reworked revision C, every package, design-authority review:")
        rev_c = {**files, "psu.kicad_sch": gen.schematic(rev="C").encode(), "psu.kicad_pcb": gen.board(rev="C").encode()}
        for kind, wp in wps.items():
            first = kind == "PCB_LAYOUT"
            a = run_package(d, wp, rev_c, revision="C", route=not first)
            d.call("post", "/v1/n3/reviews", "demo.authority", json={
                "assessment_id": a["assessment_id"], "decision": "APPROVED", "reason": "evidence checked"})
            print(f"    {kind:<21} {a['decision']} rev {a['design_revision']} → approved by demo.authority")

        print("\nN3.7 releases through N0:")
        for scope in ("schematic_pcb", "circuit_simulation", "mechanical_thermal"):
            rel = d.call("post", f"/v1/n3/baselines/{b['baseline_id']}/releases", "demo.authority",
                         ok=(201, 207), json={"scope": scope, "targets": ["N4", "N5"]}).json()
            for p in rel["packages"]:
                print(f"    {scope:<19} → {p['target']} {p['artifact_type']:<15} {p['status']:<15} "
                      f"{p['manifest_id'] or p['reason'][:60]}")

        print("\nN2 revises the PRD (revision B) → N3 invalidates the old baseline:")
        d.call("post", "/v1/n2/prds", "demo.pm", json=prd_req)
        d.call("post", f"/v1/n2/prds/{prd['prd_id']}/revisions/B/release", "demo.pa",
               json={"reason": "revised output tolerance"})
        st = d.call("get", f"/v1/n3/subjects/baseline/{b['baseline_id']}/status", "demo.auditor").json()
        print(f"    baseline {b['baseline_id']}: {st['status']} ({st['history'][-1]['reason']})")
        verify = d.call("get", "/v1/audit/verify", "demo.auditor").json()
        print(f"    audit and approval chains verify: {verify['audit']['ok']}")
        print(f"\n{b['approval_scope']}")
        app.state.n3.worker.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
