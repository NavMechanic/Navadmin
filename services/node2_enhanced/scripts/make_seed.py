#!/usr/bin/env python3
"""Build ``app/data/node2_seed.json`` from the Athena NPI source modules.

Run this only when the underlying package changes. The generated JSON ships with
the service, so a clone can seed a database without the NPI modules present.

    python3 scripts/make_seed.py --npi /path/to/athena_npi

Everything here is extracted, not invented: BOM lines and costs come from
``bom_data``, FMEA rows from the FMEA workbook builder, control-plan items from
the 62-case test plan. The three states that make the demo interesting — the
adapter's under-minimum creepage, the unowned High AP item, and the provisional
Hi-Pot values — are all conditions the source package itself records.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "app" / "data" / "node2_seed.json"

SKUS = {
    "NB": ("NB-100-PKD", "Wireless Neckband (packed unit)"),
    "PB": ("PB-10K-PKD", "Powerbank 10,000 mAh (packed unit)"),
    "AD": ("AD-20W-PKD", "USB-C Adapter 20 W (packed unit)"),
}
KIT = ("KIT-NPA-01", "Neckband + Powerbank + Adapter bundled kit")

PRODUCT_TO_SKU = {
    "Neckband": "NB-100-PKD", "Powerbank": "PB-10K-PKD", "Adapter": "AD-20W-PKD",
    "Kit (final)": "KIT-NPA-01", "All": "KIT-NPA-01",
}

SUPPLY = {"SKD (import)": "SKD_IMPORT", "Local kitting": "LOCAL_KITTING"}

# Parts whose change invalidates a certification, per the compliance scope.
SAFETY_CRITICAL = {
    "NB-CEL-190JQ", "PB-CEL-10K3S", "PB-BMS-A05", "AD-PCB-20W", "AD-XFM-01",
    "AD-INS-02", "AD-PIN-IN", "AD-SHL-T/B", "NB-BT-QCC3050",
}
# Component-engineering lifecycle flags carried from the materials work.
LIFECYCLE_RISK = {"PB-IC-PD20": "NRND", "AD-IC-PD20": "NRND", "NB-BT-QCC3050": "NRND"}


def parse_fmea(npi: Path) -> tuple[list[list], list[list]]:
    """Pull the DFMEA and PFMEA row literals out of the workbook builder."""
    src = (npi / "build_fmea_workbook.py").read_text()
    blocks: list[list] = []
    for m in re.finditer(r"^rows = (\[)", src, re.M):
        start = m.start(1)
        depth, i = 0, start
        while i < len(src):
            if src[i] == "[":
                depth += 1
            elif src[i] == "]":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        text = src[start:i + 1]
        # RPN cells are spreadsheet formulas; drop to a literal so ast can read it
        text = re.sub(r'"=[^"]*"', '"=RPN"', text)
        blocks.append(ast.literal_eval(text))
    dfmea = next((b for b in blocks if b and str(b[0][0]).startswith("D-")), [])
    pfmea = next((b for b in blocks if b and str(b[0][0]).startswith("P-")), [])
    return dfmea, pfmea


def build(npi: Path) -> dict:
    sys.path.insert(0, str(npi))
    import bom_data as B          # noqa: E402
    import test_data as T         # noqa: E402

    today = date(2026, 8, 15)
    parts: list[dict] = []
    revisions: list[dict] = []
    bom_lines: list[dict] = []
    seen: set[str] = set()

    def add_part(pn: str, name: str, *, part_type: str, supply: str | None = None,
                 supplier: str | None = None, sourcing: str | None = None,
                 cost: float | None = None, description: str | None = None,
                 sellable: bool = False) -> None:
        if pn in seen:
            return
        seen.add(pn)
        parts.append({
            "part_number": pn, "name": name, "description": description,
            "part_type": part_type, "supply_mode": supply, "supplier": supplier,
            "sourcing_status": sourcing, "is_sellable": sellable,
            "lifecycle_risk": LIFECYCLE_RISK.get(pn, "ACTIVE"),
            "safety_critical": pn in SAFETY_CRITICAL,
        })
        revisions.append({
            "part_number": pn, "rev": "A", "lifecycle_state": "RELEASED",
            "unit_cost_usd": cost, "released_on": (today - timedelta(days=45)).isoformat(),
            "released_by": "vault.import",
            "change_summary": "Initial release imported from the NPI BOM package.",
            "source_system": "athena-npi", "source_ref": "bom_data",
        })

    # ---- the three packed SKUs and their structures --------------------
    for code, rows in (("NB", B.NB_BOM), ("PB", B.PB_BOM), ("AD", B.AD_BOM)):
        sku_pn, sku_name = SKUS[code]
        add_part(sku_pn, sku_name, part_type="SKU", sellable=True,
                 description="Fully packed sellable unit.")
        for find, name, pn, spec, qty, cost, mode, sourcing, supplier in rows:
            ptype = ("PACKAGING" if any(k in name.lower()
                                        for k in ("box", "carton", "pouch", "label"))
                     else "DOCUMENT" if "manual" in name.lower()
                     else "CONSUMABLE" if any(k in name.lower()
                                              for k in ("adhesive", "screw", "insulation",
                                                        "tape", "film"))
                     else "COMPONENT")
            add_part(pn, name, part_type=ptype, supply=SUPPLY.get(mode),
                     supplier=supplier, sourcing=sourcing, cost=float(cost),
                     description=spec)
            bom_lines.append({"parent": sku_pn, "parent_rev": "A", "child": pn,
                              "find_number": int(find), "quantity": float(qty),
                              # Safety-critical children are pinned: a floating
                              # line would let a new revision enter a certified
                              # construction without anyone deciding to accept it.
                              "child_rev": "A" if pn in SAFETY_CRITICAL else None,
                              "notes": None})

    # ---- the kit ---------------------------------------------------------
    kit_pn, kit_name = KIT
    add_part(kit_pn, kit_name, part_type="SKU", sellable=True,
             description="Bundled retail kit; rolls up the three packed units.")
    for find, name, pn, spec, qty, cost, note in B.KIT_BOM:
        if pn in (s[0] for s in SKUS.values()):
            bom_lines.append({"parent": kit_pn, "parent_rev": "A", "child": pn,
                              "find_number": int(find), "quantity": float(qty),
                              "child_rev": "A", "notes": note})
            continue
        add_part(pn, name, part_type="PACKAGING" if "carton" in name.lower()
                 else "DOCUMENT" if "manual" in name.lower() else "COMPONENT",
                 supply="LOCAL_KITTING", cost=float(cost) if isinstance(cost, (int, float)) else None,
                 description=spec)
        bom_lines.append({"parent": kit_pn, "parent_rev": "A", "child": pn,
                          "find_number": int(find), "quantity": float(qty),
                          "child_rev": None, "notes": note})

    # ---- users -----------------------------------------------------------
    users = [
        {"username": "a.mehta", "full_name": "Anjali Mehta", "department": "Design Eng.",
         "roles": ["engineer"], "email": "a.mehta@athena.example"},
        {"username": "r.iyer", "full_name": "Rahul Iyer", "department": "Design Eng.",
         "roles": ["engineer"], "email": "r.iyer@athena.example"},
        {"username": "s.khan", "full_name": "Sana Khan", "department": "Quality Eng.",
         "roles": ["quality", "engineer"], "email": "s.khan@athena.example"},
        {"username": "d.rao", "full_name": "Deepak Rao", "department": "Mfg. Eng.",
         "roles": ["engineer"], "email": "d.rao@athena.example"},
        {"username": "p.nair", "full_name": "Priya Nair", "department": "Programme",
         "roles": ["gatekeeper", "viewer"], "email": "p.nair@athena.example"},
        {"username": "ccb.chair", "full_name": "Change Control Board", "department": "PMO",
         "roles": ["change_board", "viewer"], "email": "ccb@athena.example"},
        {"username": "cad.vault", "full_name": "CAD/EDA Vault service account",
         "department": "IT", "roles": ["integration"], "email": None},
        {"username": "admin", "full_name": "Platform Administrator", "department": "IT",
         "roles": ["admin"], "email": "admin@athena.example"},
        # Node 3 (Supply Chain) reads BOM structure, gate state, open changes and
        # certificate validity from here. Read-only by design: a costing service
        # has no business writing to the engineering system of record.
        {"username": "node3.supply", "full_name": "Athena Node 3 service account",
         "department": "Platform", "roles": ["viewer"], "email": None},
        # Node 4 (Digital Twin) reads design parameters and gate state to compare
        # its computed values against what is recorded. Read-only deliberately: a
        # simulated temperature is a prediction, and this account could not mark
        # a parameter verified even if the twin were asked to.
        {"username": "node4.twin", "full_name": "Athena Node 4 service account",
         "department": "Platform", "roles": ["viewer"], "email": None},
        # Node 5 (Quality / Machine Vision) reads design parameters and gate
        # state before it provisions a station, so it can refuse to write a
        # recipe parameter the design record still carries as provisional.
        # Read-only, and this one matters most: Node 5 is the node that touches
        # a PLC. An account that could both change the design record and write
        # it to a controller could close that loop with nobody in it.
        {"username": "node5.quality", "full_name": "Athena Node 5 service account",
         "department": "Platform", "roles": ["viewer"], "email": None},
        # Node 6 (Fleet Twin / RaaS) opens CAPAs from field-failure clusters and
        # proposes FMEA occurrence re-ratings. `field_quality` lets it do exactly
        # those two things. It deliberately cannot apply a re-rating: occurrence
        # drives Action Priority, Action Priority is what the G2 gate blocks on,
        # and an automated service that could lower an AP could unblock a gate
        # with nobody deciding to.
        {"username": "node6.fleet", "full_name": "Athena Node 6 service account",
         "department": "Platform", "roles": ["field_quality"], "email": None},
        # Gateway token-exchange accounts. A platform user whose role has no
        # named counterpart here (an operator, a supply planner) is exchanged
        # onto one of these. They are read-only on purpose: the platform role
        # decides what a person may do, and anything that changes an
        # engineering record has a named human account above.
        #
        # Attribution does not depend on these — the real username travels in
        # x-athena-user and lands in the audit trail — so a shared read account
        # cannot obscure who did what.
        {"username": "platform.viewer", "full_name": "Platform read-only access",
         "department": "Platform", "roles": ["viewer"], "email": None},
        {"username": "platform.operator", "full_name": "Line operator (platform)",
         "department": "Manufacturing", "roles": ["viewer"], "email": None},
    ]

    # ---- FMEA ------------------------------------------------------------
    dfmea, pfmea = parse_fmea(npi)
    fmea_rows: list[dict] = []
    # Departmental responsibility maps to a named owner where the package names
    # one. D-01 is deliberately left unowned: it is the row the source carries
    # with a function ('Design Eng.') and no individual, which is exactly the
    # AIAG-VDA gap the G2 rule exists to catch.
    OWNER_BY_FUNCTION = {
        "Design Eng.": "a.mehta", "Design Eng. / Quality": "s.khan",
        "Mfg. Eng.": "d.rao", "Quality Eng.": "s.khan",
        "Mfg. Eng. / Quality": "d.rao", "Mfg. Eng. / IT": "d.rao",
    }
    UNOWNED = {"D-01", "P-05"}

    for kind, rows in (("DFMEA", dfmea), ("PFMEA", pfmea)):
        for r in rows:
            (row_ref, product, item, mode, effect, sev, cause, occ, prev, det_ctl,
             det, _rpn, ap, action, resp, target, status) = r
            sku = PRODUCT_TO_SKU.get(product, "KIT-NPA-01")
            linked = None
            for pn in seen:
                if pn.split("-")[0] == sku.split("-")[0] and pn != sku:
                    pass
            fmea_rows.append({
                "fmea_type": kind, "row_ref": row_ref, "sku": sku,
                "item_function": item, "failure_mode": mode, "effect": effect,
                "severity": int(sev), "occurrence": int(occ), "detection": int(det),
                "action_priority": str(ap).upper(),
                "prevention_control": prev, "detection_control": det_ctl,
                "recommended_action": action, "responsibility_function": resp,
                "owner_username": None if row_ref in UNOWNED
                                  else OWNER_BY_FUNCTION.get(resp),
                "target_gate": {"G2 exit": "G2_DESIGN_EVT", "G3 exit": "G3_EVT_EXIT",
                                "G4 exit": "G4_DVT_EXIT",
                                "G5 exit": "G5_PVT_EXIT"}.get(target),
                "status": status,
                "linked_part": {"D-01": "PB-BMS-A05", "D-02": "AD-PCB-20W",
                                "D-03": "NB-CEL-190JQ", "P-03": "PB-CEL-10K3S",
                                "P-05": "AD-PCB-20W", "P-08": "AD-PCB-20W"}.get(row_ref),
            })

    # ---- control plan (from the test plan) -------------------------------
    control_items: list[dict] = []
    for tc in T.TEST_CASES:
        (tc_id, product, level, station, characteristic, lo, hi, unit, method,
         equipment, *rest) = tc
        classification = rest[-1] if rest else "Major"
        sku = PRODUCT_TO_SKU.get(product, "KIT-NPA-01")
        spec = (f"{lo}–{hi} {unit}" if lo not in (None, "-") and hi not in (None, "-")
                else str(unit or ""))
        control_items.append({
            "control_ref": tc_id, "sku": sku,
            "station": f"Stn {station}" if station else None,
            "characteristic": characteristic, "specification": spec,
            "method": method, "classification": classification,
            "linked_fmea_ref": None,
            "linked_part": {"AD-PCB-20W" if product == "Adapter" else None: None}
                            .get(None),
        })
    # Trace the Critical adapter and powerbank controls to the PFMEA rows they
    # come from, so the G4 traceability rule has something real to check.
    trace = {"Hi-Pot": "P-05", "Creepage": "P-08", "Weld": "P-03",
             "Protection": "P-04", "Solder": "P-02"}
    fmea_sku = {r["row_ref"]: r["sku"] for r in fmea_rows}
    for c in control_items:
        for token, ref in trace.items():
            # The trace must be SKU-scoped. A weld control on the neckband line
            # is not evidence for the powerbank's cell-weld PFMEA row, and
            # linking them would make every ECN flag the wrong SKU's controls.
            if (token.lower() in c["characteristic"].lower()
                    and fmea_sku.get(ref) == c["sku"]):
                c["linked_fmea_ref"] = ref
                break

    # ---- artifacts --------------------------------------------------------
    artifacts: list[dict] = []
    released = (today - timedelta(days=20)).isoformat()
    for code, (sku_pn, _n) in SKUS.items():
        artifacts += [
            {"name": f"{sku_pn}_CAD_MASTER_V2.STEP", "artifact_type": "CAD_MASTER_MODEL",
             "revision": "2", "status": "RELEASED", "sku": sku_pn,
             "file_format": "STEP AP242", "is_parametric": True,
             "source_system": "cad-vault", "released_on": released,
             "gate_code": "G2_DESIGN_EVT"},
            {"name": f"{sku_pn}_SCHEMATIC_REV_C.PDF", "artifact_type": "SCHEMATIC",
             "revision": "C", "status": "RELEASED", "sku": sku_pn,
             "file_format": "PDF", "source_system": "eda-vault",
             "released_on": released, "gate_code": "G2_DESIGN_EVT"},
            {"name": f"{sku_pn}_PRELIMINARY_BOM_REV3.CSV", "artifact_type": "BOM_EXPORT",
             "revision": "3", "status": "RELEASED", "sku": sku_pn,
             "file_format": "CSV", "source_system": "eda-vault",
             "released_on": released, "gate_code": "G2_DESIGN_EVT"},
        ]
    artifacts += [
        {"name": "CAD_MASTER_V2.STEP", "artifact_type": "CAD_MASTER_MODEL",
         "revision": "2", "status": "RELEASED", "sku": KIT[0], "file_format": "STEP AP242",
         "is_parametric": True, "source_system": "cad-vault", "released_on": released,
         "gate_code": "G2_DESIGN_EVT"},
        {"name": "PRELIMINARY_BOM_REV3.CSV", "artifact_type": "BOM_EXPORT",
         "revision": "3", "status": "RELEASED", "sku": KIT[0], "file_format": "CSV",
         "source_system": "eda-vault", "released_on": released,
         "gate_code": "G2_DESIGN_EVT"},
        # Present but NOT released — the G3 rule must not accept it.
        {"name": "AD-20W_GERBER_REV_B.zip", "artifact_type": "GERBER", "revision": "B",
         "status": "IN_REVIEW", "sku": "AD-20W-PKD", "file_format": "RS-274X",
         "source_system": "eda-vault", "gate_code": "G3_EVT_EXIT"},
    ]

    # ---- certificates -----------------------------------------------------
    certificates = [
        {"scheme": "UN38_3", "sku": "NB-100-PKD", "certificate_number": "UN383-NB-2026-114",
         "status": "VALID", "issued_on": "2026-03-02", "expires_on": "2029-03-01",
         "lab": "TUV Rheinland (Bangalore)", "part_number": "NB-CEL-190JQ",
         "scope": "190 mAh Li-Po cell, T1-T8 transport tests"},
        {"scheme": "UN38_3", "sku": "PB-10K-PKD", "certificate_number": "UN383-PB-2026-115",
         "status": "VALID", "issued_on": "2026-03-02", "expires_on": "2029-03-01",
         "lab": "TUV Rheinland (Bangalore)", "part_number": "PB-CEL-10K3S",
         "scope": "10,000 mAh Li-polymer pack, T1-T8 transport tests"},
        {"scheme": "IEC_62133_2", "sku": "NB-100-PKD", "certificate_number": "62133-NB-0091",
         "status": "VALID", "issued_on": "2026-02-18", "expires_on": "2029-02-17",
         "lab": "TUV Rheinland (Bangalore)", "part_number": "NB-CEL-190JQ"},
        {"scheme": "IEC_62133_2", "sku": "PB-10K-PKD", "certificate_number": "62133-PB-0092",
         "status": "VALID", "issued_on": "2026-02-18", "expires_on": "2029-02-17",
         "lab": "TUV Rheinland (Bangalore)", "part_number": "PB-CEL-10K3S"},
        # Market access is applied for but not granted — correct for a pre-G2
        # programme, and the reason G4 blocks until it lands.
        {"scheme": "BIS_CRS", "sku": "NB-100-PKD", "status": "APPLIED",
         "certificate_number": None, "lab": "BIS-recognised lab (Noida)",
         "scope": "IS 616 / IS 13252(Part 1) registration"},
        {"scheme": "BIS_CRS", "sku": "PB-10K-PKD", "status": "IN_TESTING",
         "certificate_number": None, "lab": "BIS-recognised lab (Noida)",
         "scope": "IS 13252(Part 1) registration"},
        {"scheme": "BIS_CRS", "sku": "AD-20W-PKD", "status": "IN_TESTING",
         "certificate_number": None, "lab": "BIS-recognised lab (Noida)",
         "scope": "IS 13252(Part 1) registration"},
        {"scheme": "WPC_ETA", "sku": "NB-100-PKD", "status": "APPLIED",
         "certificate_number": None, "scope": "BT 5.3, 2.4 GHz ISM band"},
        {"scheme": "BQB", "sku": "NB-100-PKD", "certificate_number": "D-BQB-2026-77120",
         "status": "VALID", "issued_on": "2026-01-20", "expires_on": "2031-01-19",
         "part_number": "NB-BT-QCC3050"},
    ]

    # ---- design parameters -------------------------------------------------
    design_parameters = [
        # The G2 blocker. A measured layout value, below the minimum.
        {"code": "CREEPAGE_AD_PCB_PRI_SEC", "sku": "AD-20W-PKD",
         "part_number": "AD-PCB-20W", "name": "Adapter isolation PCBA",
         "measured_value": 2.4, "unit": "mm", "min_value": 3.0,
         "standard_ref": "IEC 62368-1", "verified": False, "is_placeholder": False,
         "method": "3D CAD tolerance stack against the released layout (DFMEA D-02)"},
        {"code": "CLEARANCE_AD_PCB_PRI_SEC", "sku": "AD-20W-PKD",
         "part_number": "AD-PCB-20W", "name": "Adapter primary-secondary clearance",
         "measured_value": 2.0, "unit": "mm", "min_value": 2.0,
         "standard_ref": "IEC 62368-1", "verified": False, "is_placeholder": True,
         "method": "Provisional pending the safety assessment"},
        # The advisory finding: the package records these as provisional.
        {"code": "HIPOT_VOLTAGE_AD", "sku": "AD-20W-PKD", "part_number": "AD-PCB-20W",
         "name": "Hi-Pot dielectric withstand voltage", "measured_value": 3000,
         "unit": "Vac", "min_value": 3000, "standard_ref": "IEC 62368-1 §5.4.9",
         "verified": False, "is_placeholder": True,
         "method": "Placeholder — do not run the line on this value"},
        {"code": "HIPOT_DWELL_AD", "sku": "AD-20W-PKD", "part_number": "AD-PCB-20W",
         "name": "Hi-Pot dwell time", "measured_value": 1.0, "unit": "s",
         "min_value": 1.0, "standard_ref": "IEC 62368-1 §5.4.9", "verified": False,
         "is_placeholder": True, "method": "Placeholder pending safety assessment"},
        {"code": "CELL_CYCLE_LIFE_NB", "sku": "NB-100-PKD", "part_number": "NB-CEL-190JQ",
         "name": "Cell cycles to 80% capacity", "measured_value": 500, "unit": "cycles",
         "min_value": 500, "standard_ref": "IEC 62133-2", "verified": True,
         "is_placeholder": False, "method": "Supplier datasheet, to be confirmed at DVT"},
    ]

    gates = []
    for sku_pn in [s[0] for s in SKUS.values()] + [KIT[0]]:
        gates.append({"sku": sku_pn, "gate_code": "G0_CONCEPT", "state": "PASSED"})
        gates.append({"sku": sku_pn, "gate_code": "G1_FEASIBILITY", "state": "PASSED"})
        gates.append({"sku": sku_pn, "gate_code": "G2_DESIGN_EVT", "state": "OPEN"})

    return {
        "generated_on": today.isoformat(),
        "source": "athena_npi (bom_data, test_data, build_fmea_workbook)",
        "users": users, "parts": parts, "revisions": revisions, "bom_lines": bom_lines,
        "artifacts": artifacts, "certificates": certificates,
        "fmea_rows": fmea_rows, "control_items": control_items,
        "design_parameters": design_parameters, "gates": gates,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--npi", type=Path, default=Path("/home/claude/athena_npi"))
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    if not (args.npi / "bom_data.py").exists():
        raise SystemExit(f"NPI modules not found at {args.npi}")

    data = build(args.npi)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"wrote {args.out}")
    for k, v in data.items():
        if isinstance(v, list):
            print(f"  {k:<20} {len(v)}")


if __name__ == "__main__":
    main()
