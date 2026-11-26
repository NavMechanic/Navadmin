"""N3.4 CLI adapter: the sandboxed KiCad / ngspice runner's allowlist, fixed argv, worst-case corners,
netlist policy and version capture. Uses the fake tools in ``tests/fixtures/bin`` (mocked execution); isolation and live
tools are in ``test_n3_sandbox.py``, report parsers in ``test_n3_parsers.py``."""
from __future__ import annotations

import pytest

from app.n3 import engine, parsers
from app.n3.models import Corner
from app.n3.tool_runner import ToolError, ToolRunner, check_netlist, corner_wrapper, safe_name
from app.models import DesignConstraints
from n3_support import CONSTRAINTS, design_files, gen, n3_settings

CORNERS, _ = engine.default_corners(DesignConstraints(**CONSTRAINTS))


@pytest.fixture
def runner(tmp_path) -> ToolRunner:
    return ToolRunner(n3_settings(tmp_path))


def _kicad_inputs(*names):
    f = design_files()
    return [(n, f[n]) for n in names]


@pytest.mark.parametrize("op, expected", [
    ("sch_erc", ["sch", "erc", "--format", "json", "--severity-all", "--output", "erc.json", "psu.kicad_sch"]),
    ("pcb_drc", ["pcb", "drc", "--format", "json", "--severity-all", "--schematic-parity", "--output", "drc.json",
                 "psu.kicad_pcb"]),
    ("pcb_export_gerbers", ["pcb", "export", "gerbers", "--output", "gerbers/", "psu.kicad_pcb"]),
    ("pcb_export_drill", ["pcb", "export", "drill", "--output", "drill/", "psu.kicad_pcb"]),
    ("pcb_export_step", ["pcb", "export", "step", "--output", "psu.step", "psu.kicad_pcb"]),
])
def test_kicad_operations_map_to_fixed_argv(runner, op, expected):
    names = ["psu.kicad_sch"] if op == "sch_erc" else ["psu.kicad_pcb", "psu.kicad_sch"]
    steps, _ = runner.plan(op, names, [])
    assert steps == [[runner.paths["kicad-cli"]] + expected]


def test_bom_export_requests_mpn_and_rating_fields(runner):
    steps, _ = runner.plan("sch_export_bom", ["psu.kicad_sch"], [])
    argv = steps[0]
    assert argv[1:5] == ["sch", "export", "bom", "--output"] and "MPN" in argv[argv.index("--fields") + 1]


def test_spice_corners_run_one_batch_deck_per_corner(runner):
    steps, extra = runner.plan("spice_corners", ["psu_buck.cir"], CORNERS)
    assert [s[1:] for s in steps] == [["-b", "-o", f"corner_{c}.log", f"corner_{c}.cir"]
                                     for c in ("vin_min", "vin_max", "temp_max", "load_max")]
    deck = extra["wrappers"]["corner_temp_max.cir"]
    assert ".temp 85" in deck and ".param iload=5 vin=48" in deck and ".include psu_buck.cir" in deck


def test_worst_case_corners_come_from_prd_constraints():
    by = {c.name: c for c in CORNERS}
    assert by["vin_min"].params == {"vin": 43.2, "iload": 5} and by["vin_min"].temp_c == 25
    assert by["vin_max"].params["vin"] == 52.8 and by["temp_max"].temp_c == 85
    assert by["load_max"].params == {"vin": 43.2, "iload": 5} and by["load_max"].temp_c == 85
    _, missing = engine.default_corners(DesignConstraints(input_voltage_min_v=10))
    assert set(missing) == {"input_voltage_max_v", "load_max_a", "ambient_max_c"}


@pytest.mark.parametrize("op, names, code", [
    ("format_c_drive", ["psu.kicad_sch"], "OPERATION_NOT_ALLOWED"),
    ("sch_erc", ["psu.kicad_sch", "evil.sh"], "INPUT_NOT_ALLOWED"),
    ("pcb_drc", ["psu.kicad_sch"], "PRIMARY_INPUT_MISSING"),
    ("spice_corners", ["psu_buck.cir"], "CORNERS_REQUIRED"),
])
def test_allowlist_refuses_anything_else(runner, op, names, code):
    with pytest.raises(ToolError) as e:
        runner.plan(op, names, [])
    assert e.value.code == code


def test_input_names_are_sanitised_into_the_work_dir():
    assert safe_name("../../etc/passwd.kicad_sch") == "passwd.kicad_sch"
    assert safe_name("C:\\boards\\my board.kicad_pcb") == "my_board.kicad_pcb"
    assert safe_name(".spiceinit.cir") == "spiceinit.cir"
    with pytest.raises(ToolError):
        safe_name("..")


@pytest.mark.parametrize("netlist, problem", [
    ("* t\n.control\nshell rm -rf /\n.endc\n.end\n", ".control blocks are not allowed"),
    ("*ng_script\n.tran 1u 1m\n.end\n", "ng_script"),
    ("* t\n.include /etc/passwd\n.end\n", "is not a file of this job"),
    ("* t\n.lib 'models.lib' typ\n.end\n", "is not a file of this job"),
    ("* t\nB1 a 0 V=1\n.model d1 d(file=/etc/shadow)\n.end\n", "absolute, home or parent paths"),
    ("* t\n.param vin=12\n.end\n", "must not define corner parameter 'vin'"),
    ("* t\n.temp 25\n.end\n", "must not set the temperature"),
])
def test_netlist_policy_refuses_untrusted_spice(netlist, problem):
    problems = check_netlist("x.cir", netlist, set(), {"vin", "iload"})
    assert any(problem in p for p in problems), problems


def test_netlist_policy_allows_job_includes_and_arithmetic():
    ok = "* t\n.include parts.lib\nR1 a b {24/iload}\nB1 c 0 V = 24.0/max(V(in),1)\n.lib parts.lib\n.end\n"
    assert check_netlist("x.cir", ok, {"parts.lib"}, {"vin", "iload"}) == []
    assert check_netlist("psu_buck.cir", gen.NETLIST, set(), {"vin", "iload"}) == []
    assert "vin=52.8" in corner_wrapper(Corner(name="vin_max", params={"vin": 52.8}, temp_c=25), "d.cir")


def test_netlist_policy_runs_before_any_process_starts(runner, monkeypatch):
    monkeypatch.setattr(runner, "_exec", lambda *a, **k: pytest.fail("a refused netlist must never execute"))
    with pytest.raises(ToolError) as e:
        runner.run("spice_corners", [("x.cir", b"* t\n.control\nshell id\n.endc\n.end\n")], CORNERS)
    assert e.value.code == "NETLIST_POLICY"


def test_tool_versions_are_captured_exactly(runner):
    assert runner.version("kicad-cli") == "8.0.4" and runner.version("ngspice") == "42"
    res = runner.run("sch_erc", _kicad_inputs("psu.kicad_sch", "psu.kicad_pro"))
    assert res.status == "SUCCEEDED" and res.tool_version == "8.0.4"
    rep = parsers.parse_kicad_report(res.outputs[0].data)
    assert rep.kind == "erc" and rep.tool_version == "8.0.4" and rep.violations == []
