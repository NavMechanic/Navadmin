"""Helpers for the N4 tests: the fictional PSU-120W programme from RFQ through N3 to N4 verification.

All data is fictional. Records not marked synthetic stand for real evidence *inside the tests*. The
SCPI "bench instrument" is a local TCP server speaking SCPI (``FakeScpiServer``), lab reports are
generated PDFs, and AOI images are tiny PNGs.
"""
from __future__ import annotations

import copy
import json
import socketserver
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from n3_support import Flow, _RFQ_PAYLOAD, sha

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def days(n: float) -> datetime:
    return NOW + timedelta(days=n)


# ----------------------------------------------------------------------------------------------
# the PSU-120W requirements, hazards and the 14-entry verification plan
# ----------------------------------------------------------------------------------------------
EXTRA_REQS = [
    {"req_id": "R-005", "parameter": "efficiency_pct", "value": 92, "unit": "%", "value_origin": "CUSTOMER"},
    {"req_id": "R-008", "parameter": "isolation_voltage_v", "value": 3000, "unit": "V", "value_origin": "CUSTOMER"},
    {"req_id": "R-009", "parameter": "insulation_resistance_ohm", "value": 100, "unit": "Mohm",
     "value_origin": "CUSTOMER"},
    {"req_id": "R-010", "parameter": "leakage_current_a", "value": 0.25, "unit": "mA", "value_origin": "CUSTOMER"},
    {"req_id": "R-011", "parameter": "ripple_voltage_v", "value": 120, "unit": "mV", "value_origin": "CUSTOMER"},
    {"req_id": "R-012", "parameter": "creepage_distance_mm", "value": 6.4, "unit": "mm", "value_origin": "CUSTOMER"},
]
HAZARDS = [
    {"hazard_id": "H-1", "description": "electric shock through primary-to-secondary insulation breakdown",
     "severity": 10, "requirement_ids": ["R-008", "R-009", "R-012"], "standard_ref": "IEC 62368-1 cl. 5.4"},
    {"hazard_id": "H-2", "description": "excessive touch current", "severity": 9, "requirement_ids": ["R-010"],
     "standard_ref": "IEC 62368-1 cl. 5.7"},
    {"hazard_id": "H-3", "description": "overheating and fire at maximum ambient", "severity": 9,
     "requirement_ids": ["R-007"], "standard_ref": "IEC 62368-1 cl. 6"},
]
SAFETY_IDS = {"R-007", "R-008", "R-009", "R-010", "R-012"}


def rfq_payload() -> dict:
    p = copy.deepcopy(_RFQ_PAYLOAD)
    p["requirements"] = [r for r in p["requirements"] if r["req_id"] != "R-002"] + [
        {"req_id": "R-002", "parameter": "output_voltage_v", "value": 24, "unit": "V", "value_origin": "CUSTOMER",
         "tolerance": {"kind": "ABS", "plus": 0.5, "minus": 0.5}}] + EXTRA_REQS
    return p


SOP = {"BENCH_ATE": ("SOP-ATE-001", "C"), "ACCREDITED_LAB": ("SOP-LAB-001", "A"),
       "INSPECTION": ("SOP-INS-001", "B"), "SIM": ("SOP-SIM-001", "A")}


def tc(test_id: str, reqs: list[str], method: str, char: str, unit: str, lo=None, hi=None, **kw) -> dict:
    mid, rev = SOP[method]
    return {"test_id": test_id, "title": kw.pop("title", f"{char} ({method})"), "requirement_ids": reqs,
            "method": method, "acceptance": [{"characteristic": char, "unit": unit,
                                              **({"lower": str(lo)} if lo is not None else {}),
                                              **({"upper": str(hi)} if hi is not None else {})}],
            "method_ref": {"method_id": mid, "revision": rev}, "owner": kw.pop("owner", "te"), **kw}


def plan_cases() -> list[dict]:
    """The 14-entry requirements-to-test plan of the PSU-120W."""
    return [
        tc("T-01", ["R-002"], "BENCH_ATE", "output_voltage", "V", 23.5, 24.5, title="output voltage, nominal line/load"),
        tc("T-02", ["R-001", "R-002"], "BENCH_ATE", "output_voltage", "V", 23.5, 24.5, title="line regulation 43.2 V"),
        tc("T-03", ["R-001", "R-002"], "BENCH_ATE", "output_voltage", "V", 23.5, 24.5, title="line regulation 52.8 V"),
        tc("T-04", ["R-002", "R-003"], "BENCH_ATE", "output_voltage", "V", 23.5, 24.5, title="load regulation 0–5 A"),
        tc("T-05", ["R-004"], "BENCH_ATE", "output_power", "W", 120, None, title="continuous output power"),
        tc("T-06", ["R-005"], "BENCH_ATE", "efficiency", "%", 92, None, title="efficiency at full load"),
        tc("T-07", ["R-011"], "BENCH_ATE", "ripple_voltage", "mV", None, 120, title="output ripple pk-pk"),
        tc("T-08", ["R-006", "R-007"], "ACCREDITED_LAB", "output_voltage", "V", 23.5, 24.5,
           title="operation at −40 °C and +85 °C ambient"),
        tc("T-09", ["R-008"], "ACCREDITED_LAB", "hipot_leakage_current", "mA", None, 5, title="hipot 3000 V AC 60 s"),
        tc("T-10", ["R-009"], "ACCREDITED_LAB", "insulation_resistance", "Mohm", 100, None,
           title="insulation resistance 500 V DC"),
        tc("T-11", ["R-010"], "ACCREDITED_LAB", "touch_current", "mA", None, 0.25, title="touch current"),
        tc("T-12", ["R-012"], "INSPECTION", "creepage_distance", "mm", 6.4, None, title="creepage measurement"),
        tc("T-13", ["R-012"], "INSPECTION", "defect_count", "count", None, 0, title="AOI solder / bridge inspection"),
        tc("T-14", ["R-001", "R-002"], "SIM", "vout_min", "V", 23.5, 24.5, title="worst-case corners (N3 SPICE)",
           sim_corner="all", sim_model_uncertainty="0.05"),
    ]


# ----------------------------------------------------------------------------------------------
# instruments, calibrations, SOPs, labs, qualifications
# ----------------------------------------------------------------------------------------------
INSTRUMENTS = [
    ("DMM-1", "DMM", "Keysight Technologies", "34465A", "MY59012345", "A.03.01-02.40"),
    ("PWA-1", "POWER_ANALYZER", "Yokogawa", "WT1806E", "C2PB12345", "F2.11"),
    ("SCOPE-1", "OSCILLOSCOPE", "Rohde&Schwarz", "RTB2004", "102233", "02.300"),
    ("MIC-1", "MEASURING_MICROSCOPE", "Mitutoyo", "TM-505B", "MT-778899", None),
    ("AOI-1", "AOI_STATION", "Example AOI Systems", "EdgeAOI-3", "AOI-0042", "3.1.0"),
]
CAL = {i[0]: f"CAL-{i[0]}" for i in INSTRUMENTS}


class FakeScpiServer:
    """A local SCPI-over-TCP bench instrument (``*IDN?``, ``MEAS:VOLT:DC?``, ``SYST:ERR?``)."""

    def __init__(self, idn: str = "Keysight Technologies,34465A,MY59012345,A.03.01-02.40",
                 readings: Optional[dict[str, str]] = None) -> None:
        self.idn, self.readings, self.seen = idn, readings or {"MEAS:VOLT:DC?": "+2.40012000E+01"}, []
        srv = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    cmd = line.decode().strip()
                    srv.seen.append(cmd)
                    if cmd == "*IDN?":
                        out = srv.idn
                    elif cmd == "SYST:ERR?":
                        out = '+0,"No error"'
                    else:
                        out = srv.readings.get(cmd, "+9.9E37")
                    self.wfile.write((out + "\n").encode())

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def make_pdf(lines: list[str]) -> bytes:
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


def png(seed: int = 0) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + bytes([seed % 256]) * 32


# ----------------------------------------------------------------------------------------------
class N4Flow(Flow):
    """Extends the N3 ``Flow`` with N4 set-up, evidence builders and the full verification path."""

    serials = ["PSU-0001", "PSU-0002"]

    def rfq_art(self) -> dict:
        from n3_support import rfq
        art = rfq()
        art["payload"] = {**rfq_payload(), "rfq_id": art["artifact_id"]}
        from app.rules import canonical_json, sha256_hex
        art["source_sha256"] = sha256_hex(canonical_json(art["payload"]))
        art["synthetic"] = False
        return art

    def designed(self, scopes=("schematic_pcb", "circuit_simulation"), hazards=None, safety_ids=None,
                 rfq_id: Optional[str] = None) -> dict:
        """RFQ → N2 PRD (with hazards) → N3 baseline → released DESIGN_RELEASEs for ``scopes``. With
        ``rfq_id`` the RFQ already exists (e.g. an N1 seed) and the sources are already registered."""
        if rfq_id is None:
            self.sources()
        self.n4_sources()
        if rfq_id is None:
            self.rfq_to_n2(self.rfq_art())
        body = self.prd_body(scopes=list(scopes), **({"rfq_id": rfq_id} if rfq_id else {}))
        body["hazards"] = HAZARDS if hazards is None else hazards
        body["safety_critical_requirement_ids"] = [] if safety_ids is None else safety_ids
        r = self.c.post("/v1/n2/prds", "pm", json=body)
        assert r.status_code == 200 and r.json()["n0_decision"] == "PASS", r.text
        rel = self.c.post(f"/v1/n2/prds/{r.json()['prd_id']}/revisions/{r.json()['revision']}/release", "pa",
                          json={"reason": "PRD approved for design"})
        assert rel.status_code == 200, rel.text
        b = self.baseline()
        for scope in scopes:
            self.scope(b["baseline_id"], scope)
            out = self.release(b["baseline_id"], scope, targets=("N4",))
            assert out.status_code == 201, out.text
        return b

    def n4_sources(self) -> None:
        self.c.source("SRC-LAB", permitted_use=("VERIFICATION",), publisher="ATE, lab and AOI evidence")
        self.c.source("SRC-SIMDATA", permitted_use=("VERIFICATION",), publisher="demo fixtures",
                      data_origin="SYNTHETIC")
        self.c.source("SRC-VER", permitted_use=("LIFECYCLE", "COMMERCIAL", "FINANCE"),
                      publisher="ATHENA N4 verification releases")
        self.c.source("SRC-KB", permitted_use=("VERIFICATION", "LLM_EXTRACTION"), publisher="standards and SOPs")

    def registries(self) -> None:
        for iid, kind, mfr, model, serial, fw in INSTRUMENTS:
            body = {"instrument_id": iid, "tenant_id": "acme", "kind": kind, "manufacturer": mfr, "model": model,
                    "serial": serial, "firmware": fw}
            assert self.c.post("/v1/n4/instruments", "te", json=body).status_code == 201
            self.calibrate(iid, CAL[iid])
        for method, (mid, rev) in SOP.items():
            self.method(mid, rev, method)
        self.c.post("/v1/n4/labs", "ql", json={
            "lab_id": "LAB-EXAMPLE", "name": "Example Test Lab GmbH (fictional)", "accreditation_body": "DAkkS",
            "accreditation_number": "D-PL-99999-01-00", "scopes": ["IEC 62368-1"],
            "valid_until": (NOW + timedelta(days=700)).date().isoformat()})
        for who in ("ta", "ta2", "dual4"):
            r = self.c.post("/v1/n4/qualifications", "ql", json={
                "principal_id": who, "methods": ["SIM", "BENCH_ATE", "ACCREDITED_LAB", "INSPECTION"],
                "valid_until": (NOW + timedelta(days=365)).date().isoformat(), "basis": "competence record QR-7"})
            assert r.status_code == 201, r.text

    def calibrate(self, iid: str, cal_id: str, at: datetime = days(-120), due: datetime = days(240), who="li"):
        body = {"calibration_id": cal_id, "instrument_id": iid, "certificate_number": f"CERT-{cal_id}",
                "calibration_lab": "Example Calibration Services (fictional)", "accreditation_ref": "D-K-99999-01-00",
                "calibrated_at": iso(at), "calibration_due": iso(due)}
        r = self.c.post("/v1/n4/calibrations", who, data={"body": json.dumps(body)},
                        files=[("certificate", ("cert.pdf", make_pdf([f"Calibration certificate CERT-{cal_id}"]),
                                                "application/pdf"))])
        assert r.status_code == 201, r.text
        return r.json()

    def method(self, mid: str, rev: str, method: str, approve: bool = True):
        r = self.c.post("/v1/n4/methods", "te", data={"body": json.dumps({
            "method_id": mid, "revision": rev, "title": f"{method} procedure", "method": method})},
            files=[("document", ("sop.pdf", make_pdf([f"{mid} rev {rev}"]), "application/pdf"))])
        assert r.status_code == 201, r.text
        if approve:
            a = self.c.post(f"/v1/n4/methods/{mid}/revisions/{rev}/approve", "qr")
            assert a.status_code == 200, a.text

    def intake(self, who: str = "te", expect: int = 201) -> dict:
        r = self.c.post("/v1/n4/intakes", who, json={"prd_id": "PSU-120W", "project_id": "PRJ-PSU-120W",
                                                     "release_source_id": "SRC-VER"})
        assert r.status_code == expect, r.text
        return r.json()

    def n4_plan(self, intake_id: str, cases: Optional[list[dict]] = None, who: str = "te") -> dict:
        r = self.c.post(f"/v1/n4/intakes/{intake_id}/plans", who,
                        json={"title": "PSU-120W design verification", "test_cases": cases or plan_cases()})
        assert r.status_code == 201, r.text
        return r.json()

    # ------------------------------------------------------------------ evidence builders
    def scpi_log(self, test_id: str, records: list[tuple], serial: str = "PSU-0001", idn: Optional[str] = None,
                 cal: str = "CAL-DMM-1", at: datetime = days(-1), method=("SOP-ATE-001", "C"),
                 claimed: str = "PHYSICAL", attachments: Optional[dict[str, bytes]] = None) -> tuple[str, bytes]:
        log = {"format": "athena-scpi-log/1", "station_id": "ATE-01", "operator": "op-1", "test_id": test_id,
               "unit_serial": serial,
               "method_ref": {"method_id": method[0], "revision": method[1]} if method else None,
               "instrument": {"resource": "TCPIP0::10.0.0.5::5025::SOCKET",
                              "idn": idn or "Keysight Technologies,34465A,MY59012345,A.03.01-02.40",
                              "calibration_id": cal},
               "records": [{"t": iso(at + timedelta(seconds=i)), "command": cmd, "response": resp,
                            "characteristic": ch, "unit": unit, "expanded_uncertainty": u, "k": 2}
                           for i, (cmd, resp, ch, unit, u) in enumerate(records)],
               "attachments": [{"path": n, "sha256": sha(d), "role": "WAVEFORM"} for n, d in (attachments or {}).items()],
               "claimed_provenance": claimed}
        return f"scpi_{test_id}_{serial}.json", json.dumps(log).encode()

    def post_scpi(self, plan_id: str, name: str, data: bytes, attachments: Optional[dict[str, bytes]] = None,
                  source: str = "SRC-LAB", who: str = "li"):
        files = [("files", (name, data, "application/json"))] + [
            ("files", (n, d, "text/csv")) for n, d in (attachments or {}).items()]
        return self.c.post(f"/v1/n4/plans/{plan_id}/evidence/scpi", who, data={"log_name": name, "source_id": source},
                           files=files)

    def bench(self, plan_id: str, test_id: str, value: str = "24.00", u: str = "0.05", serial: str = "PSU-0001",
              char: str = "output_voltage", unit: str = "V", source: str = "SRC-LAB", who: str = "li", **kw):
        name, data = self.scpi_log(test_id, [("MEAS:VOLT:DC?", value, char, unit, u)], serial=serial, **kw)
        return self.post_scpi(plan_id, name, data, source=source, who=who)

    def lab_pdf(self, cert: str = "LAB-2026-0042", pcb: str = "B", extra: Optional[list[str]] = None) -> bytes:
        return make_pdf([f"Test report / certificate {cert}", "Example Test Lab GmbH (fictional), DAkkS "
                         "D-PL-99999-01-00", "Product: PSU-120W", f"PCB Rev {pcb}",
                         "Standard: IEC 62368-1", *(extra or [])])

    def lab_manifest(self, pdf: bytes, results: list[dict], pcb: str = "B", cert: str = "LAB-2026-0042",
                     claimed: str = "PHYSICAL", **kw) -> dict:
        return {"format": "athena-lab-report/1", "lab": {"name": "Example Test Lab GmbH (fictional)",
                                                         "accreditation_body": "DAkkS",
                                                         "accreditation_number": kw.get("accreditation",
                                                                                        "D-PL-99999-01-00")},
                "certificate_number": cert, "issued_at": iso(days(-1)), "product": "PSU-120W", "pcb_revision": pcb,
                "report_sha256": sha(pdf), "results": results, "claimed_provenance": claimed}

    @staticmethod
    def lab_result(test_id: str, char: str, value: str, unit: str, u: str, serial: str = "PSU-0001",
                   due: datetime = days(200), at: datetime = days(-2)) -> dict:
        return {"test_id": test_id, "characteristic": char, "unit_serial": serial, "value": value, "unit": unit,
                "expanded_uncertainty": u, "k": 2, "instrument_id": "LAB-HIPOT-7", "calibration_id": "LABCAL-7",
                "calibration_due": iso(due), "captured_at": iso(at),
                "method_ref": {"method_id": "SOP-LAB-001", "revision": "A"}, "standard_clause": "IEC 62368-1 cl. 5.4"}

    def post_lab(self, plan_id: str, pdf: bytes, manifest: dict, source: str = "SRC-LAB", who: str = "li"):
        return self.c.post(f"/v1/n4/plans/{plan_id}/evidence/lab", who,
                           data={"manifest": json.dumps(manifest), "source_id": source},
                           files=[("files", ("lab_report.pdf", pdf, "application/pdf"))])

    def aoi(self, plan_id: str, detections: list[dict], serial: str = "PSU-0001", pcb: str = "B",
            claimed: str = "PHYSICAL", who: str = "li", source: str = "SRC-LAB"):
        img = png(len(detections))
        inf = {"format": "athena-aoi/1", "station_id": "AOI-LINE-1", "instrument_id": "AOI-1", "test_id": "T-13",
               "unit_serial": serial, "pcb_revision": pcb, "captured_at": iso(days(-1)),
               "model": {"name": "yolov8n-solder", "version": "1.3.0", "framework": "ultralytics-yolov8",
                         "weights_sha256": "e" * 64},
               "images": [{"path": "top.png", "sha256": sha(img), "detections": detections}],
               "claimed_provenance": claimed}
        return self.c.post(f"/v1/n4/plans/{plan_id}/evidence/aoi", who,
                           data={"inference": json.dumps(inf), "source_id": source},
                           files=[("files", ("top.png", img, "image/png"))])

    def adjudicate(self, evidence_id: str, decisions: list[dict], verdict: str = "ACCEPT", who: str = "qr"):
        return self.c.post("/v1/n4/adjudications", who, json={"evidence_id": evidence_id, "decisions": decisions,
                                                              "unit_verdict": verdict, "notes": "reviewed at 20x"})

    def csv(self, plan_id: str, rows: list[dict], mock: bool = False, source: str = "SRC-LAB", who: str = "li"):
        cols = ["test_id", "characteristic", "unit_serial", "value", "unit", "expanded_uncertainty", "k",
                "instrument_id", "calibration_id", "captured_at", "method_id", "method_revision", "operator"]
        lines = (["# ATHENA_MOCK_FIXTURE, generated fixture, never physical evidence"] if mock else []) + [",".join(cols)]
        lines += [",".join(str(r.get(c, "")) for c in cols) for r in rows]
        return self.c.post(f"/v1/n4/plans/{plan_id}/evidence/csv", who, data={"source_id": source},
                           files=[("files", ("manual.csv", ("\n".join(lines) + "\n").encode(), "text/csv"))])

    @staticmethod
    def creepage_row(value: str = "7.10", serial: str = "PSU-0001", **kw) -> dict:
        return {"test_id": "T-12", "characteristic": "creepage_distance", "unit_serial": serial, "value": value,
                "unit": "mm", "expanded_uncertainty": "0.05", "k": 2, "instrument_id": "MIC-1",
                "calibration_id": "CAL-MIC-1", "captured_at": iso(days(-1)), "method_id": "SOP-INS-001",
                "method_revision": "B", "operator": "insp-1", **kw}

    def pfmea(self, plan_id: str, entries: Optional[list[dict]] = None, who: str = "te"):
        return self.c.post(f"/v1/n4/plans/{plan_id}/pfmea", who, json={"entries": entries or pfmea_entries()})

    # ------------------------------------------------------------------ the whole passing evidence set
    def full_evidence(self, plan_id: str, serials: Optional[list[str]] = None) -> None:
        serials = serials or ["PSU-0001"]
        bench = {"T-01": ("output_voltage", "24.00", "V", "0.05"), "T-02": ("output_voltage", "23.98", "V", "0.05"),
                 "T-03": ("output_voltage", "24.02", "V", "0.05"), "T-04": ("output_voltage", "23.91", "V", "0.05"),
                 "T-05": ("output_power", "121.4", "W", "0.6"), "T-06": ("efficiency", "93.1", "%", "0.3"),
                 "T-07": ("ripple_voltage", "48", "mV", "4")}
        for s in serials:
            for tid, (ch, v, unit, u) in bench.items():
                cmd = {"V": "MEAS:VOLT:DC?", "W": "MEAS:POW:AC?", "%": "MEAS:POW:EFF?", "mV": "MEAS:VOLT:PTP?"}[unit]
                wf = {"ripple_waveform.csv": b"t_s,v\n0,0.01\n1e-6,0.02\n"} if tid == "T-07" else None
                name, data = self.scpi_log(tid, [(cmd, v, ch, unit, u)], serial=s, attachments=wf)
                r = self.post_scpi(plan_id, name, data, attachments=wf)
                assert r.status_code == 201, r.text
            pdf = self.lab_pdf()
            results = [self.lab_result("T-08", "output_voltage", "24.05", "V", "0.08", s),
                       self.lab_result("T-09", "hipot_leakage_current", "1.2", "mA", "0.1", s),
                       self.lab_result("T-10", "insulation_resistance", "850", "Mohm", "40", s),
                       self.lab_result("T-11", "touch_current", "0.08", "mA", "0.01", s)]
            r = self.post_lab(plan_id, pdf, self.lab_manifest(pdf, results))
            assert r.status_code == 201, r.text
            r = self.csv(plan_id, [self.creepage_row(serial=s)])
            assert r.status_code == 201, r.text
            a = self.aoi(plan_id, [{"class": "solder_bridge", "confidence": 0.55, "bbox_xyxy": [10, 10, 20, 20]}],
                         serial=s)
            assert a.status_code == 201, a.text
            det = a.json()["detections"][0]["detection_id"]
            assert self.adjudicate(a.json()["evidence_id"], [{"detection_id": det, "label": "FALSE_CALL"}]).status_code \
                == 201

    def verified(self, scopes=("schematic_pcb", "circuit_simulation")) -> tuple[dict, dict, dict]:
        """Designed, registered, planned, PFMEA'd, fully evidenced and assessed PASS."""
        self.designed(scopes)
        self.registries()
        i = self.intake()
        p = self.n4_plan(i["intake_id"])
        assert self.pfmea(p["plan_id"]).status_code == 201
        self.c.post(f"/v1/n4/plans/{p['plan_id']}/control-plan", "te", json={"items": control_plan_items()})
        self.full_evidence(p["plan_id"])
        a = self.n4_assess(p["plan_id"])
        assert a["decision"] == "PASS", [(f["code"], f["message"]) for f in a["findings"] if f["outcome"] != "INFO"]
        return i, p, a

    def n4_assess(self, plan_id: str, who: str = "te") -> dict:
        r = self.c.post(f"/v1/n4/plans/{plan_id}/assess", who)
        assert r.status_code == 201, r.text
        return r.json()

    def n4_review(self, a: dict, who: str = "ta", decision: str = "APPROVED", **over):
        body = {"assessment_id": a["assessment_id"], "evidence_sha256": a["evidence_sha256"],
                "design_revision": a["design_revision"], "decision": decision,
                "reason": "evidence hash and design revision checked", **over}
        return self.c.post("/v1/n4/reviews", who, json=body)

    def n4_release(self, plan_id: str, targets=("N8", "N6", "N7"), who: str = "ta"):
        return self.c.post(f"/v1/n4/plans/{plan_id}/releases", who, json={"targets": list(targets)})


def pfmea_entries() -> list[dict]:
    return [
        {"entry_id": "FM-01", "process_step": "transformer winding", "function": "primary-secondary insulation",
         "failure_mode": "insulation breakdown", "effect": "electric shock hazard at the output",
         "cause": "damaged triple-insulated wire", "prevention_controls": ["incoming TIW inspection"],
         "detection_controls": [{"description": "100 % hipot", "test_id": "T-09"}],
         "severity": 10, "occurrence": 2, "detection": 2, "hazard_ids": ["H-1"], "special_characteristic": True},
        {"entry_id": "FM-02", "process_step": "Y-capacitor placement", "function": "limit touch current",
         "failure_mode": "wrong Y-capacitor value", "effect": "touch current above limit",
         "cause": "feeder mix-up", "detection_controls": [{"description": "touch current test", "test_id": "T-11"}],
         "severity": 9, "occurrence": 2, "detection": 3, "hazard_ids": ["H-2"]},
        {"entry_id": "FM-03", "process_step": "thermal pad assembly", "function": "MOSFET heat transfer",
         "failure_mode": "missing thermal pad", "effect": "overheating at 85 °C ambient",
         "cause": "operator omission", "detection_controls": [{"description": "85 °C run", "test_id": "T-08"}],
         "severity": 9, "occurrence": 3, "detection": 4, "hazard_ids": ["H-3"]},
        {"entry_id": "FM-04", "process_step": "reflow", "function": "solder joints", "failure_mode": "solder bridge",
         "effect": "short on the feedback divider", "cause": "stencil wear",
         "detection_controls": [{"description": "AOI + human adjudication", "test_id": "T-13"}],
         "severity": 6, "occurrence": 4, "detection": 3},
    ]


def control_plan_items() -> list[dict]:
    return [{"characteristic": "output_voltage", "test_id": "T-01",
             "specification": {"characteristic": "output_voltage", "unit": "V", "lower": "23.5", "upper": "24.5"},
             "measurement_method": "DMM via ATE", "sample_size": 1, "frequency": "every unit",
             "reaction_plan": "quarantine the unit and open a CAPA", "special_characteristic": False},
            {"characteristic": "hipot_leakage_current", "test_id": "T-09", "measurement_method": "hipot tester",
             "sample_size": 1, "frequency": "every unit", "reaction_plan": "scrap the unit, stop the line",
             "special_characteristic": True}]


def codes(assessment: dict, outcome: Optional[str] = None) -> set[str]:
    return {f["code"] for f in assessment["findings"] if outcome is None or f["outcome"] == outcome}


