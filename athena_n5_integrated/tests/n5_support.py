"""Helpers for the N5 tests: a two-line PSU BOM released by N3 to N5, qualified suppliers, quotations,
FX assumptions, the sourcing report, review and the N6 / N7 handoffs.

All data is fictional. Records not marked synthetic stand for real evidence *inside the tests*.
The two lines are the demo's components (10 finished products):

    line 1  GRM32ER72A475KA88  Murata  × 2  →  Q_required 20   20 × ₹6.50 + ₹12 handling = ₹142
    line 2  SRP1270-220M       Bourns  × 1  →  Q_required 10   10 × ₹9.00 + ₹12 handling = ₹102
                                                                    indicative material quote ₹244
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from n3_support import KIND_TOOL, PACKAGE_FILES, PACKAGE_JOBS, design_files, gen, sha
from n4_support import N4Flow

NOW = datetime.now(timezone.utc).replace(microsecond=0)
PARTS = [p for p in gen.PARTS if p[0] in ("C1", "C2", "L1")]
REFS = [p[0] for p in PARTS]
MURATA, BOURNS = "GRM32ER72A475KA88", "SRP1270-220M"
OS_ID = "IN2026123ABCDEF"          # fictional Open Supply Hub facility id
EMS_KEY = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
EMS_PUB = EMS_KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def days(n: float) -> datetime:
    return NOW + timedelta(days=n)


def two_line_files(rev: str = "B", parts=PARTS) -> dict[str, bytes]:
    refs = [p[0] for p in parts]
    return design_files(**{"psu.kicad_sch": gen.schematic(rev=rev, parts=parts),
                           "psu.kicad_pcb": gen.board(rev=rev, refs=refs), "psu_bom.csv": gen.bom_csv(parts=parts)})


def offer(line: Optional[int], mpn: str, price: str, *, moq: int = 1, pack: int = 1, stock: Optional[int] = 5000,
          lead: int = 5, ccy: str = "INR", charge: Optional[str] = "12", breaks: Optional[list] = None,
          availability: str = "STOCK", **kw: Any) -> dict:
    o = {"line": line, "quoted_mpn": mpn, "currency": ccy, "moq": moq, "pack_multiple": pack,
         "availability": availability, "stock_qty": stock, "lead_time_days": lead,
         "price_breaks": breaks or [{"min_qty": 1, "unit_price": price}],
         "line_charges": [{"kind": "HANDLING", "amount": charge}] if charge else []}
    o.update(kw)
    return o


def sign(data: bytes) -> str:
    return base64.b64encode(EMS_KEY.sign(data)).decode()


class N5Flow(N4Flow):
    # ------------------------------------------------------------------ N0 sources
    def n5_sources(self) -> None:
        self.c.source("SRC-QUOTES", permitted_use=("SOURCING",), publisher="supplier quotations (e-mail / portal)")
        self.c.source("SRC-EMS", permitted_use=("SOURCING",), publisher="EMS / ODM signed drop zone")
        self.c.source("SRC-PRC", permitted_use=("COMMERCIAL", "FINANCE"), publisher="ATHENA N5 sourcing handoffs")
        for sid, pub in (("SRC-DIGIKEY", "DigiKey API"), ("SRC-MOUSER", "Mouser API"), ("SRC-NEXAR", "Nexar API")):
            self.c.source(sid, permitted_use=("SOURCING",), publisher=pub, license="LicenseRef-Distributor-API-Terms")

    # ------------------------------------------------------------------ N2 → N3 → BOM release to N5
    def bom_released(self, files: Optional[dict[str, bytes]] = None, rev: str = "B", targets=("N5",),
                     hazards=None) -> dict:
        self.sources()
        self.n4_sources()
        self.n5_sources()
        self.rfq_to_n2(self.rfq_art())
        body = self.prd_body(scopes=["schematic_pcb"])
        body["hazards"] = hazards or []
        r = self.c.post("/v1/n2/prds", "pm", json=body)
        assert r.status_code == 200 and r.json()["n0_decision"] == "PASS", r.text
        rel = self.c.post(f"/v1/n2/prds/{r.json()['prd_id']}/revisions/A/release", "pa",
                          json={"reason": "PRD approved for design"})
        assert rel.status_code == 200, rel.text
        b = self.baseline()
        self.scope(b["baseline_id"], "schematic_pcb", files or two_line_files(rev))
        out = self.release(b["baseline_id"], "schematic_pcb", targets=targets)
        assert out.status_code == 201, out.text
        self.baseline_id = b["baseline_id"]
        if "N5" in targets:
            self.load_lines()
        return out.json()

    def load_lines(self) -> dict[str, int]:
        """MPN → BOM line number, as N3 released it (``self.L``)."""
        art = self.c.get("/v1/artifacts/BOM-PSU-120W-schematic_pcb", "aud").json()["canonical_artifact"]
        self.L = {i["mpn"]: i["line"] for i in art["payload"]["items"]}
        return self.L

    def rerelease(self, rev: str, files: Optional[dict[str, bytes]] = None, targets=("N5",)):
        """N3 delivers and releases a new design revision of schematic_pcb on the same baseline."""
        f = files or two_line_files(rev)
        wps = self.plan(self.baseline_id, ["schematic_pcb"])
        for kind, wp in wps.items():
            pkg = {n: f[n] for n in PACKAGE_FILES[kind] if n in f}
            assert self.route_tool(wp["work_package_id"], KIND_TOOL[kind]).status_code == 201
            assert self.deliver(wp["work_package_id"], pkg, revision=rev).status_code == 201
            for tool, op, names in PACKAGE_JOBS[kind]:
                assert self.job(wp["work_package_id"], tool, op, [sha(pkg[n]) for n in names]).status_code == 201
            a = self.assess(wp["work_package_id"])
            assert a["decision"] == "PASS", a
            assert self.review(a["assessment_id"]).status_code == 201
        return self.release(self.baseline_id, "schematic_pcb", targets=targets)

    # ------------------------------------------------------------------ N5.1
    def n5_intake(self, build: int = 10, target: int = 30, who: str = "pan", purpose: str = "PROTOTYPE_BUILD",
                  critical=None, expect: int = 201, **kw: Any) -> dict:
        body = {"prd_id": "PSU-120W", "project_id": "PRJ-PSU-120W", "build_quantity": build, "purpose": purpose,
                "target_lead_time_days": target, "release_source_id": "SRC-PRC",
                "critical_lines": critical if critical is not None else [{"line": getattr(self, "L", {}).get(BOURNS, 1),
                                                                                     "reason": "power inductor, single "
                                                                                               "footprint"}],
                **kw}
        r = self.c.post("/v1/n5/bom-intakes", who, json=body)
        assert r.status_code == expect, r.text
        return r.json()

    # ------------------------------------------------------------------ N5.2
    def supplier(self, supplier_id: str, kind: str = "DISTRIBUTOR", proofs=("Murata", "Bourns"),
                 approve: bool = True, verified=None, who: str = "pan", reviewer: str = "sq", **kw: Any) -> dict:
        body = {"supplier_id": supplier_id, "kind": kind, "legal_name": kw.pop("legal_name", f"{supplier_id} Pvt Ltd"),
                "country": "IN", "registration_ids": {"GSTIN": "29ABCDE1234F1Z5"},
                "channel_proofs": [{"manufacturer": m, "proof_type": "MANUFACTURER_AUTHORIZED_LIST",
                                    "reference": f"https://example.test/{m.lower()}/distributors",
                                    "valid_until": days(365).date().isoformat()} for m in proofs]
                if kind == "DISTRIBUTOR" else [], **kw}
        r = self.c.post("/v1/n5/suppliers", who, json=body)
        assert r.status_code == 201, r.text
        if approve and kind in ("EMS", "ODM") and body.get("sites"):
            for site in body["sites"]:
                v = self.verify_site(supplier_id, site["site_id"], site.get("os_id", OS_ID))
                assert v.status_code == 200, v.text
        if approve:
            a = self.review_supplier(supplier_id, verified=list(proofs) if verified is None else verified,
                                     who=reviewer)
            assert a.status_code == 200, a.text
            return a.json()
        return r.json()

    def review_supplier(self, supplier_id: str, decision: str = "APPROVED", verified=(), who: str = "sq",
                        until: Optional[str] = None):
        return self.c.post(f"/v1/n5/suppliers/{supplier_id}/review", who, json={
            "decision": decision, "attestation": "checked GSTIN, sites and the manufacturer's distributor list",
            "approved_until": until or days(365).date().isoformat(), "verified_channel_manufacturers": list(verified)})

    def verify_site(self, supplier_id: str, site_id: str, os_id: str = OS_ID, country: str = "IN", who: str = "sq"):
        csv = f"os_id,name,address,country_code\n{os_id},EMS Plant 1,Plot 7 Example Industrial Area,{country}\n"
        return self.c.post(f"/v1/n5/suppliers/{supplier_id}/sites/{site_id}/verify", who,
                           files=[("osh_csv", ("osh.csv", csv.encode(), "text/csv"))])

    def ems_supplier(self, supplier_id: str = "SUP-EMS-1", approve: bool = True) -> dict:
        return self.supplier(supplier_id, kind="EMS", proofs=(), approve=approve, verified=[],
                             sites=[{"site_id": "SITE-1", "name": "EMS Plant 1", "country": "IN", "os_id": OS_ID,
                                     "capabilities": ["SMT", "THT", "box build"]}],
                             signing_keys=[{"key_id": "EMS-KEY-1", "algorithm": "ED25519", "public_key_pem": EMS_PUB}])

    def standard_suppliers(self) -> None:
        self.supplier("SUP-DIST-A")                                     # authorized for Murata and Bourns
        self.supplier("SUP-DIST-B")                                     # a second authorized distributor
        self.supplier("SUP-BROKER", proofs=("Yageo",))                  # approved, but not a Murata / Bourns channel

    def fx(self, fx_id: str = "FX-USD-INR", rate: str = "83.50", who: str = "pan", **kw: Any):
        body = {"fx_id": fx_id, "from_currency": "USD", "to_currency": "INR", "rate": rate,
                "as_of": (NOW - timedelta(days=1)).date().isoformat(), "source": "treasury reference rate (test)", **kw}
        return self.c.post("/v1/n5/fx-assumptions", who, json=body)

    # ------------------------------------------------------------------ N5.3
    def quote(self, intake: dict, supplier_id: str, offers: list[dict], *, valid: float = 30, rev: str = "B",
              source: str = "SRC-QUOTES", who: str = "pan", ref: Optional[str] = None, **kw: Any):
        body = {"supplier_id": supplier_id, "bom_intake_id": intake["intake_id"], "design_revision": rev,
                "quote_ref": ref or f"Q-{supplier_id}-{len(offers)}", "issued_at": iso(days(-1)),
                "valid_until": iso(days(valid)), "source_id": source, "offers": offers, **kw}
        return self.c.post("/v1/n5/quotes", who, json=body)

    def demo_quotes(self, intake: dict) -> list[dict]:
        """The ₹244 case plus a dearer authorized alternative on each line."""
        out = []
        m, b = self.L[MURATA], self.L[BOURNS]
        for sup, offers in (("SUP-DIST-A", [offer(m, MURATA, "6.50", moq=10, pack=10), offer(b, BOURNS, "9")]),
                            ("SUP-DIST-B", [offer(m, MURATA, "6.80"), offer(b, BOURNS, "9.40")])):
            r = self.quote(intake, sup, offers)
            assert r.status_code == 201, r.text
            out.append(r.json())
        return out

    def ems_quote(self, intake: dict, *, tamper: bool = False, key_id: str = "EMS-KEY-1", capacity: int = 50,
                  offers: Optional[list[dict]] = None, edi: Optional[bytes] = None, who: str = "pan"):
        doc = edi or b"%PDF-1.4\n% EMS quotation EMS-Q-1 (fictional)\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"
        cap_doc = b"%PDF-1.4\n% capacity allocation 50 units SMT line 2 (fictional)\ntrailer << >>\n%%EOF\n"
        name = "quote.edi" if edi else "quote.pdf"
        manifest = {"format": "athena-ems-quote/1", "supplier_id": "SUP-EMS-1", "bom_intake_id": intake["intake_id"],
                    "design_revision": "B", "quote_ref": "EMS-Q-1", "issued_at": iso(days(-1)),
                    "valid_until": iso(days(20)), "quote_document": name, "quote_document_sha256": sha(doc),
                    "signature": {"key_id": key_id, "algorithm": "ED25519", "value": sign(doc)},
                    "offers": offers if offers is not None else
                    [offer(self.L[MURATA], MURATA, "7.10", availability="FACTORY_ORDER", stock=None, lead=12),
                     offer(self.L[BOURNS], BOURNS, "9.90", availability="FACTORY_ORDER", stock=None, lead=12)],
                    "capacity": {"allocated_qty": capacity, "period_start": days(-1).date().isoformat(),
                                 "period_end": days(60).date().isoformat(), "site_id": "SITE-1",
                                 "document_sha256": sha(cap_doc)},
                    "nre": [{"kind": "NRE", "amount": "15000", "currency": "INR", "note": "stencil + fixture"}],
                    "service_terms": "assembly ₹85 / unit, 12-month workmanship warranty",
                    "attachments": [{"path": "capacity.pdf", "sha256": sha(cap_doc), "role": "CAPACITY_EVIDENCE"}]}
        sent = (doc + b"tampered") if tamper else doc
        return self.c.post("/v1/n5/quotes/ems", who, data={"manifest": json.dumps(manifest), "source_id": "SRC-EMS"},
                           files=[("files", (name, sent, "application/pdf" if not edi else "text/plain")),
                                  ("files", ("capacity.pdf", cap_doc, "application/pdf"))])

    # ------------------------------------------------------------------ N5.4–N5.6
    def n5_report(self, intake: dict, who: str = "pan", **kw: Any) -> dict:
        r = self.c.post("/v1/n5/reports", who, json={"bom_intake_id": intake["intake_id"], **kw})
        assert r.status_code == 201, r.text
        return r.json()

    def n5_review(self, report: dict, who: str = "pap", decision: str = "APPROVED", **over: Any):
        body = {"report_id": report["report_id"], "report_sha256": report["report_sha256"],
                "quote_hashes": report["quote_hashes"], "decision": decision,
                "reason": "report and quote hashes checked against the vault", **over}
        return self.c.post("/v1/n5/reviews", who, json=body)

    def n5_handoff(self, report: dict, targets=("N6", "N7"), who: str = "pap"):
        return self.c.post(f"/v1/n5/reports/{report['report_id']}/handoffs", who, json={"targets": list(targets)})

    def sourced(self, build: int = 10) -> tuple[dict, dict]:
        """BOM released to N5, suppliers qualified, quotes in, report PASS."""
        self.bom_released()
        self.standard_suppliers()
        intake = self.n5_intake(build=build)
        self.demo_quotes(intake)
        rep = self.n5_report(intake)
        assert rep["decision"] == "PASS", [(f["code"], f["message"]) for f in rep["findings"] if f["outcome"] != "INFO"]
        return intake, rep

    def handed_off(self) -> tuple[dict, dict, dict]:
        intake, rep = self.sourced()
        assert self.n5_review(rep).status_code == 201
        h = self.n5_handoff(rep)
        assert h.status_code == 201, h.text
        return intake, rep, h.json()


def line(report: dict, n: int) -> dict:
    return next(x for x in report["lines"] if x["line"] == n)


def by_mpn(report: dict, mpn: str) -> dict:
    return next(x for x in report["lines"] if x["mpn"] == mpn)


def codes(report: dict, outcome: Optional[str] = None, level: Optional[str] = None) -> set[str]:
    return {f["code"] for f in report["findings"] if (outcome is None or f["outcome"] == outcome)
            and (level is None or f["level"] == level)}
