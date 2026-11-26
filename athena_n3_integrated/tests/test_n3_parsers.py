"""Parsers for native EDA / CAE files and tool reports (pure functions, no execution)."""
from __future__ import annotations

import json

import pytest

from app.models import DesignConstraints
from app.n3 import engine, parsers
from app.n3.tool_runner import ToolRunner
from n3_support import CONSTRAINTS, design_files, gen, n3_settings

CORNERS, _ = engine.default_corners(DesignConstraints(**CONSTRAINTS))


@pytest.fixture
def runner(tmp_path) -> ToolRunner:
    return ToolRunner(n3_settings(tmp_path))


def test_ngspice_log_parser_reads_convergence_measures_and_temperature(runner):
    good = runner.run("spice_corners", [("psu_buck.cir", gen.NETLIST.encode())], CORNERS[2:3]).outputs[0]
    sl = parsers.parse_ngspice_log(good.data, good.exit_code)
    assert sl.converged and sl.temp_c == 85 and sl.measures["vout_min"] == 23.95 and sl.measures["stress_q1_v"] == 48
    bad_net = gen.NETLIST + "* ATHENA_FAKE_NONCONVERGE temp_max\n"
    bad = runner.run("spice_corners", [("psu_buck.cir", bad_net.encode())], CORNERS[2:3]).outputs[0]
    sl = parsers.parse_ngspice_log(bad.data, bad.exit_code)
    assert bad.exit_code == 1 and not sl.converged and any("timestep too small" in e.lower() for e in sl.errors)


def test_kicad_report_parser_counts_exclusions_parity_and_unconnected():
    rep = parsers.parse_kicad_report(json.dumps({
        "$schema": "https://schemas.kicad.org/drc.v1.json", "kicad_version": "8.0.4",
        "violations": [{"type": "clearance", "severity": "error", "description": "x"},
                       {"type": "silk_overlap", "severity": "warning", "description": "y", "excluded": True}],
        "unconnected_items": [{}, {}], "schematic_parity": [{}]}).encode())
    assert rep.kind == "drc" and [v.excluded for v in rep.violations] == [False, True]
    assert (rep.unconnected, rep.parity) == (2, 1)


def test_bom_parser_expands_ranges_and_reads_ratings():
    bom = parsers.parse_bom_csv(b'"Designator","Quantity","Mfr Part Number","V_Rating"\n"R1-R3","3","RC0603","75"\n'
                                b'"C1,C2","2","GRM","50"\n')
    assert [i.refdes for i in bom.items] == [["R1", "R2", "R3"], ["C1", "C2"]] and bom.items[0].mpn == "RC0603"
    assert bom.ratings["r2"]["v"] == 75 and bom.problems == []


def test_detection_tells_native_sources_from_documents_and_fakes():
    f = design_files()
    assert parsers.detect("psu.kicad_sch", f["psu.kicad_sch"]).native
    assert parsers.detect("psu_datasheet.pdf", f["psu_datasheet.pdf"]).role == "DOCUMENT"
    assert parsers.detect("layout_screenshot.png", f["layout_screenshot.png"]).role == "IMAGE"
    fake = parsers.detect("psu.kicad_pcb", b"this is a PDF export renamed")
    assert fake.fmt == "invalid" and not fake.native
    sch_syms = parsers.schematic_symbols(gen.schematic())
    assert {s["Reference"] for s in sch_syms} == {p[0] for p in gen.PARTS}
