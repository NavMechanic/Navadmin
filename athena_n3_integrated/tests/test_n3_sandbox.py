"""N3.4 sandbox properties of the tool runner: minimal environment, private network namespace, process-
group timeouts, crash handling, version gates, safe output collection, the background worker, and
live-optional runs against real ngspice / KiCad 8+ when installed."""
from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

from app.models import DesignConstraints
from app.n3 import engine, parsers
from app.n3.tool_runner import JobWorker, RunResult, ToolError, ToolRunner
from n3_support import CONSTRAINTS, DESIGNS, FAKE_BIN, design_files, gen, n3_settings

CORNERS, _ = engine.default_corners(DesignConstraints(**CONSTRAINTS))


@pytest.fixture
def runner(tmp_path) -> ToolRunner:
    return ToolRunner(n3_settings(tmp_path))


def _kicad_inputs(*names):
    f = design_files()
    return [(n, f[n]) for n in names]


def test_sandbox_environment_is_minimal_and_network_isolated(runner):
    net = gen.NETLIST.replace("* ATHENA_FAKE_MEAS vout_min", "* ATHENA_FAKE_PROBE\n* ATHENA_FAKE_MEAS vout_min")
    res = runner.run("spice_corners", [("psu_buck.cir", net.encode())], CORNERS[:1])
    log = res.outputs[0].data.decode()
    env = log.split("probe_env ", 1)[1].split("\n", 1)[0].split(",")
    assert set(env) <= {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "PWD",
                        "SHLVL", "_", "LC_CTYPE"}
    assert not any(k.endswith("_PROXY") or k.endswith("_proxy") or "TOKEN" in k for k in env)
    cwd = log.split("probe_cwd ", 1)[1].split("\n", 1)[0]
    assert os.path.basename(cwd).startswith("n3job-") and not os.path.exists(cwd)     # removed after the job
    if runner.limits()["network_namespace"]:
        assert "probe_net blocked" in log


def test_timeout_kills_the_whole_process_group(tmp_path):
    r = ToolRunner(n3_settings(tmp_path, job_timeout_s=2))
    sch = gen.schematic(marker="ATHENA_FAKE_SLEEP 30").encode()
    t0 = time.monotonic()
    res = r.run("sch_erc", [("psu.kicad_sch", sch)])
    assert res.status == "TIMEOUT" and time.monotonic() - t0 < 15 and res.outputs == []


def test_crashing_tool_fails_the_job_with_its_stderr(runner):
    res = runner.run("sch_erc", [("psu.kicad_sch", gen.schematic(marker="ATHENA_FAKE_CRASH").encode())])
    assert res.status == "FAILED" and "exit 3" in res.error and "Segmentation fault" in res.error


def test_version_gates_kicad_7_and_odb(tmp_path):
    v7 = ToolRunner(n3_settings(tmp_path, kicad_cli=str(FAKE_BIN / "kicad-cli-v7")))
    res = v7.run("sch_erc", _kicad_inputs("psu.kicad_sch"))
    assert res.status == "FAILED" and "version 8+ is required" in res.error and res.steps == []
    odb = ToolRunner(n3_settings(tmp_path)).run("pcb_export_odb", _kicad_inputs("psu.kicad_pcb"))
    assert odb.status == "FAILED" and "version 9+" in odb.error


def test_missing_tool_and_required_isolation_refuse(tmp_path):
    with pytest.raises(ToolError) as e:
        ToolRunner(n3_settings(tmp_path, kicad_cli="/nonexistent/kicad-cli")).plan("sch_erc", ["a.kicad_sch"], [])
    assert e.value.code == "TOOL_UNAVAILABLE"
    strict = ToolRunner(n3_settings(tmp_path, network_isolation="required"))
    strict._unshare = None
    with pytest.raises(ToolError) as e:
        strict.plan("sch_erc", ["a.kicad_sch"], [])
    assert e.value.code == "SANDBOX_UNAVAILABLE"


def test_output_collection_skips_symlinks_unexpected_files_and_caps(tmp_path, runner):
    work = tmp_path / "w"
    (work / "gerbers").mkdir(parents=True)
    (work / "gerbers" / "a-F_Cu.gtl").write_text("G04*")
    (work / "gerbers" / "b-B_Cu.gbl").write_text("G04*")
    (work / "gerbers" / "evil.sh").write_text("#!/bin/sh")
    os.symlink("/etc/passwd", work / "gerbers" / "leak.gbr")
    res = RunResult("SUCCEEDED", "kicad-cli", "8.0.4")
    assert sorted(o.path for o in runner._collect(work, "pcb_export_gerbers", set(), res)) == \
        ["gerbers/a-F_Cu.gtl", "gerbers/b-B_Cu.gbl"]
    capped = ToolRunner(n3_settings(tmp_path, max_output_files=1))
    assert len(capped._collect(work, "pcb_export_gerbers", set(), res)) == 1 and "cap reached" in res.error


def test_job_worker_runs_in_the_background():
    w = JobWorker(max_workers=1)
    done = []
    w.submit("j1", lambda: (time.sleep(0.3), done.append(1)))
    assert done == []
    w.wait("j1", timeout=5)
    assert done == [1]
    w.shutdown()


# ----------------------------------------------------------------------------------------------
# asynchronous worker through the API
# ----------------------------------------------------------------------------------------------
def test_api_jobs_run_on_the_background_worker(make_n1, tmp_path):
    from n3_support import Flow
    flow = Flow(make_n1(n3_settings=n3_settings(tmp_path / "jobs", jobs_inline=False)))
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["circuit_simulation"])["CIRCUIT_SIMULATION"]["work_package_id"]
    slow = (gen.NETLIST + "* ATHENA_FAKE_SLEEP 1\n").encode()
    flow.route_tool(wp, "ngspice")
    assert flow.deliver(wp, {"psu_buck.cir": slow}).status_code == 201
    r = flow.job(wp, "ngspice", "spice_corners", [__import__("hashlib").sha256(slow).hexdigest()])
    assert r.status_code == 202 and r.json()["status"] in ("QUEUED", "RUNNING")
    pending = flow.c.post(f"/v1/n3/work-packages/{wp}/assess", "de")
    assert pending.status_code == 409 and pending.json()["detail"]["code"] == "JOBS_PENDING"
    flow.c.app.state.n3.worker.wait(r.json()["job_id"], timeout=60)
    done = flow.c.get(f"/v1/n3/jobs/{r.json()['job_id']}", "de").json()
    assert done["status"] == "SUCCEEDED" and len(done["outputs"]) == 4 and done["limits"]["durations_s"]
    assert flow.assess(wp)["decision"] == "PASS"
    flow.c.app.state.n3.worker.shutdown()


# ----------------------------------------------------------------------------------------------
# live tools (optional): real ngspice / KiCad 8+ when installed
# ----------------------------------------------------------------------------------------------
REAL_NGSPICE = shutil.which("ngspice")
REAL_KICAD = shutil.which("kicad-cli")


def _kicad_major() -> int:
    if not REAL_KICAD:
        return 0
    try:
        out = subprocess.run([REAL_KICAD, "version"], capture_output=True, text=True, timeout=30).stdout
        return int(out.strip().split(".")[0])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


@pytest.mark.skipif(REAL_NGSPICE is None, reason="ngspice is not installed")
def test_live_ngspice_runs_the_worst_case_corners(tmp_path):
    r = ToolRunner(n3_settings(tmp_path, ngspice=REAL_NGSPICE))
    res = r.run("spice_corners", [("psu_buck.cir", (DESIGNS / "psu_buck.cir").read_bytes())], CORNERS)
    assert res.status == "SUCCEEDED" and res.tool_version and res.tool_version.isdigit()
    logs = {o.path: parsers.parse_ngspice_log(o.data, o.exit_code) for o in res.outputs}
    assert set(logs) == {f"corner_{c.name}.log" for c in CORNERS}
    for name, sl in logs.items():
        assert sl.converged and 23.76 <= sl.measures["vout_min"] <= sl.measures["vout_max"] <= 24.24, (name, sl)
    assert logs["corner_vin_max.log"].measures["stress_q1_v"] == pytest.approx(52.8)
    assert logs["corner_temp_max.log"].temp_c == 85


@pytest.mark.skipif(REAL_NGSPICE is None, reason="ngspice is not installed")
def test_live_ngspice_reports_a_broken_netlist_as_not_converged(tmp_path):
    r = ToolRunner(n3_settings(tmp_path, ngspice=REAL_NGSPICE))
    broken = b"* broken\nVIN in 0 DC {vin}\nD1 in out DMISSING\nRL out 0 {8/iload}\n.tran 1u 100u\n.end\n"
    out = r.run("spice_corners", [("x.cir", broken)], CORNERS[:1]).outputs[0]
    sl = parsers.parse_ngspice_log(out.data, out.exit_code)
    assert out.exit_code != 0 and not sl.converged and sl.errors


@pytest.mark.skipif(REAL_NGSPICE is None, reason="ngspice is not installed")
def test_live_ngspice_through_the_api(make_n1, tmp_path):
    from n3_support import Flow
    flow = Flow(make_n1(n3_settings=n3_settings(tmp_path / "jobs", ngspice=REAL_NGSPICE)))
    b = flow.ready()
    a = flow.work_package(flow.plan(b["baseline_id"], ["circuit_simulation"])["CIRCUIT_SIMULATION"])
    assert a["decision"] == "PASS", a["findings"]
    sim = a["metrics"]["simulation"]
    assert sim["solver"] == "ngspice" and sim["solver_version"].isdigit() and len(sim["corners"]) == 4


@pytest.mark.skipif(_kicad_major() < 8, reason="KiCad 8+ kicad-cli is not installed (sch erc / pcb drc need 8.0)")
def test_live_kicad_cli_erc_on_an_empty_schematic(tmp_path):    # pragma: no cover - depends on the host
    sch = ('(kicad_sch (version 20231120) (generator "eeschema") (generator_version "8.0")\n'
           '\t(uuid "a1b2c3d4-0000-4000-8000-000000000001")\n\t(paper "A4")\n\t(lib_symbols)\n'
           '\t(sheet_instances (path "/" (page "1")))\n)\n').encode()
    res = ToolRunner(n3_settings(tmp_path, kicad_cli=REAL_KICAD)).run("sch_erc", [("empty.kicad_sch", sch)])
    assert res.status == "SUCCEEDED", res.error
    rep = parsers.parse_kicad_report(res.outputs[0].data)
    assert rep.kind == "erc" and rep.tool_version
