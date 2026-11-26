"""Writes the synthetic PSU-120W design package used by ``run_n3_demo.py`` and the tests.

Everything here is fictional demo data: a 48 V → 24 V / 5 A (120 W) DIN-rail buck converter.
Run ``python examples/designs/gen_designs.py`` to regenerate the files next to this script.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# refdes, lib_id, value, footprint, MPN, manufacturer, V / I / P ratings
PARTS = [
    ("U1", "Regulator_Switching:LM5146", "LM5146", "Package_SO:HTSSOP-20", "LM5146RGYR", "Texas Instruments", "100", "", ""),
    ("Q1", "Device:Q_NMOS_GDS", "BSC093N15NS5", "Package_TO_SOT_SMD:TDSON-8", "BSC093N15NS5ATMA1", "Infineon", "150",
     "87", ""),
    ("Q2", "Device:Q_NMOS_GDS", "BSC093N15NS5", "Package_TO_SOT_SMD:TDSON-8", "BSC093N15NS5ATMA1", "Infineon", "150",
     "87", ""),
    ("L1", "Device:L", "22uH", "Inductor_SMD:L_Bourns_SRP1270", "SRP1270-220M", "Bourns", "", "13", ""),
    ("C1", "Device:C", "4.7uF 100V", "Capacitor_SMD:C_1210", "GRM32ER72A475KA88", "Murata", "100", "", ""),
    ("C2", "Device:C", "4.7uF 100V", "Capacitor_SMD:C_1210", "GRM32ER72A475KA88", "Murata", "100", "", ""),
    ("C3", "Device:C", "100uF 35V", "Capacitor_SMD:CP_Elec_10x10", "EEH-ZA1V101P", "Panasonic", "35", "", ""),
    ("R1", "Device:R", "100k", "Resistor_SMD:R_0603", "RC0603FR-07100KL", "Yageo", "75", "", "0.1"),
    ("R2", "Device:R", "5.36k", "Resistor_SMD:R_0603", "RC0603FR-075K36L", "Yageo", "75", "", "0.1"),
    ("J1", "Connector:Screw_Terminal_01x02", "IN 48V", "TerminalBlock:Phoenix_MKDS-1.5-2", "1935161",
     "Phoenix Contact", "300", "16", ""),
    ("J2", "Connector:Screw_Terminal_01x02", "OUT 24V", "TerminalBlock:Phoenix_MKDS-1.5-2", "1935161",
     "Phoenix Contact", "300", "16", ""),
]


def _symbol(i: int, ref: str, lib: str, value: str, fp: str, mpn: str, mfr: str, v: str, a: str, w: str) -> str:
    props = [("Reference", ref), ("Value", value), ("Footprint", fp), ("MPN", mpn), ("Manufacturer", mfr),
             ("V_Rating", v), ("I_Rating", a), ("P_Rating", w)]
    body = "\n".join(f'\t\t(property "{k}" "{val}" (at {20 + i * 10} 30 0) (effects (font (size 1.27 1.27)) '
                     f'{"" if k in ("Reference", "Value") else "hide"}))' for k, val in props if val)
    return (f'\t(symbol (lib_id "{lib}") (at {20 + i * 10} 40 0) (unit 1) (in_bom yes) (on_board yes) (dnp no)\n'
            f'\t\t(uuid "a1b2c3d4-0000-4000-8000-{i:012d}")\n{body}\n'
            f'\t\t(instances (project "psu" (path "/a1b2c3d4-0000-4000-8000-000000000000" '
            f'(reference "{ref}") (unit 1))))\n\t)')


def schematic(rev: str = "B", marker: str = "", parts=PARTS) -> str:
    syms = "\n".join(_symbol(i, *p) for i, p in enumerate(parts))
    text = f'\t(text "{marker}" (at 10 10 0) (effects (font (size 1.27 1.27))) (uuid "f0000000-0000-4000-8000-0000000000aa"))\n' \
        if marker else ""
    return ("(kicad_sch (version 20231120) (generator \"eeschema\") (generator_version \"8.0\")\n"
            "\t(uuid \"a1b2c3d4-0000-4000-8000-000000000000\")\n\t(paper \"A4\")\n"
            f"\t(title_block (title \"PSU-120W 48V to 24V buck\") (date \"2026-11-20\") (rev \"{rev}\") "
            "(company \"Synthetic demo data\"))\n"
            "\t(lib_symbols\n\t\t(symbol \"Device:R\" (pin_numbers hide) (property \"Reference\" \"R\" (at 0 0 0)))\n\t)\n"
            f"{syms}\n{text}"
            "\t(sheet_instances (path \"/\" (page \"1\")))\n)\n")


def board(rev: str = "B", marker: str = "", refs=None, copper=("F.Cu", "B.Cu")) -> str:
    refs = refs if refs is not None else [p[0] for p in PARTS]
    fps = {p[0]: p[3] for p in PARTS}
    layer_ids = [0] + list(range(1, len(copper) - 1)) + [31]
    layers = "\n".join(f'\t\t({n} "{c}" signal)' for n, c in zip(layer_ids, copper))
    feet = "\n".join(
        f'\t(footprint "{fps.get(r, "Resistor_SMD:R_0603")}" (layer "F.Cu") (uuid "b0000000-0000-4000-8000-{i:012d}")\n'
        f'\t\t(at {30 + i * 6} 40)\n\t\t(property "Reference" "{r}" (at 0 -2 0) (layer "F.SilkS"))\n'
        f'\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))\n\t)'
        for i, r in enumerate(refs))
    text = f'\t(gr_text "{marker}" (at 5 5) (layer "Cmts.User") (uuid "c0000000-0000-4000-8000-0000000000aa"))\n' \
        if marker else ""
    return ("(kicad_pcb (version 20240108) (generator \"pcbnew\") (generator_version \"8.0\")\n"
            "\t(general (thickness 1.6) (legacy_teardrops no))\n\t(paper \"A4\")\n"
            f"\t(title_block (title \"PSU-120W 48V to 24V buck\") (rev \"{rev}\"))\n"
            f"\t(layers\n{layers}\n\t\t(32 \"B.Adhes\" user \"B.Adhesive\")\n\t\t(44 \"Edge.Cuts\" user)\n\t)\n"
            "\t(setup (pad_to_mask_clearance 0))\n\t(net 0 \"\")\n\t(net 1 \"VIN\")\n\t(net 2 \"VOUT\")\n"
            f"{feet}\n{text})\n")


def project(min_clearance_mm: float = 0.2) -> str:
    return json.dumps({"board": {"design_settings": {"rules": {"min_clearance": min_clearance_mm,
                                                               "min_track_width": 0.2, "min_via_diameter": 0.5}}},
                       "meta": {"filename": "psu.kicad_pro", "version": 1}}, indent=2)


def bom_csv(parts=PARTS, overrides=None) -> str:
    overrides = overrides or {}
    groups: dict = {}
    for p in parts:
        groups.setdefault((p[2], p[3], p[4]), []).append(p)
    lines = ['"Refs","Value","Footprint","Qty","MPN","Manufacturer","DNP","V_Rating","I_Rating","P_Rating"']
    for (value, fp, mpn), ps in groups.items():
        refs = ",".join(p[0] for p in ps)
        row = {"Refs": refs, "Value": value, "Footprint": fp, "Qty": str(len(ps)), "MPN": mpn,
               "Manufacturer": ps[0][5], "DNP": "", "V_Rating": ps[0][6], "I_Rating": ps[0][7],
               "P_Rating": ps[0][8], **overrides.get(refs, {})}
        lines.append(",".join(f'"{row[k]}"' for k in ("Refs", "Value", "Footprint", "Qty", "MPN", "Manufacturer",
                                                       "DNP", "V_Rating", "I_Rating", "P_Rating")))
    return "\n".join(lines) + "\n"


NETLIST = """* ATHENA PSU-120W averaged buck model, 48 V in / 24 V out (synthetic demo data)
* Corner parameters vin (V) and iload (A) are set by the N3 corner wrapper, never here.
* ATHENA_FAKE_MEAS vout_min=23.95
* ATHENA_FAKE_MEAS vout_max=24.02
* ATHENA_FAKE_MEAS stress_q1_v=vin
* ATHENA_FAKE_MEAS stress_c3_v=24.02
VIN in 0 DC {vin}
BCTL ctl 0 V = 24.0/max(V(in),1)
BSW sw 0 V = V(in)*V(ctl)
RDC sw mid 0.01
L1 mid out 22u IC={iload}
C3 out 0 100u IC=24
RLOAD out 0 {24/iload}
.tran 1u 2m uic
.meas tran vout_min MIN v(out) from=1m to=2m
.meas tran vout_max MAX v(out) from=1m to=2m
.meas tran stress_q1_v MAX v(in)
.meas tran stress_c3_v MAX v(out)
.end
"""

THERMAL = {"solver": "Elmer", "solver_version": "9.0",
           "boundary_conditions": {"convection_h_w_m2k": 10, "radiation_emissivity": 0.85,
                                   "mounting": "DIN rail, vertical, enclosed cabinet"},
           "ambient_c": 85, "load_w": 120,
           "max_temps_c": {"Q1": 128.0, "Q2": 126.5, "L1": 110.0, "U1": 105.0},
           "limits_c": {"Q1": 150, "Q2": 150, "L1": 155, "U1": 125}}


def step(originating: str = "FreeCAD 0.21.2") -> str:
    return ("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('PSU-120W DIN-rail enclosure'),'2;1');\n"
            f"FILE_NAME('enclosure.step','2026-11-20T09:00:00',('design'),('synthetic'),"
            f"'Open CASCADE STEP processor 7.7','{originating}','Unknown');\n"
            "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\nENDSEC;\nDATA;\n"
            "#1=CARTESIAN_POINT('',(0.,0.,0.));\nENDSEC;\nEND-ISO-10303-21;\n")


PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000") + b"\x00" * 16


def write(target: Path = HERE) -> list[Path]:
    files = {"psu.kicad_sch": schematic(), "psu.kicad_pcb": board(), "psu.kicad_pro": project(),
             "psu_bom.csv": bom_csv(), "psu_buck.cir": NETLIST, "thermal_report.json": json.dumps(THERMAL, indent=2),
             "enclosure.step": step()}
    out = []
    for name, text in files.items():
        (target / name).write_text(text)
        out.append(target / name)
    (target / "psu_datasheet.pdf").write_bytes(PDF)
    (target / "layout_screenshot.png").write_bytes(PNG)
    return out + [target / "psu_datasheet.pdf", target / "layout_screenshot.png"]


if __name__ == "__main__":
    for p in write():
        print(p.relative_to(HERE.parent.parent))
