"""Operator CLI over the N3 sandboxed tool runner (the same allowlist and limits the API uses).

    python scripts/run_tools.py describe
    python scripts/run_tools.py run sch_erc psu.kicad_sch psu.kicad_pro --out out/
    python scripts/run_tools.py run pcb_drc psu.kicad_pcb psu.kicad_sch psu.kicad_pro --out out/
    python scripts/run_tools.py corners psu_buck.cir --vin-min 43.2 --vin-max 52.8 --ambient-max 85 \\
        --load-max 5 --out out/

Tool paths come from ``N3_KICAD_CLI`` / ``N3_NGSPICE`` (default: ``kicad-cli`` / ``ngspice`` on PATH),
limits from the other ``N3_*`` variables. Outputs are written under ``--out``; a JSON summary with the
exact tool version, argv, exit codes and parsed results goes to stdout. Nothing here touches the
database. Use the API to record evidence against a work package.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import DesignConstraints  # noqa: E402
from app.n3 import engine, parsers  # noqa: E402
from app.n3.config import N3Settings  # noqa: E402
from app.n3.tool_runner import OPERATIONS, ToolError, ToolRunner  # noqa: E402


def _summary(res, out: Path) -> dict:
    files = []
    for o in res.outputs:
        dest = out / o.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(o.data)
        det = parsers.detect(o.path, o.data)
        item = {"path": str(dest), "role": det.role, "exit_code": o.exit_code, "bytes": len(o.data)}
        if det.role in ("ERC_REPORT", "DRC_REPORT"):
            rep = parsers.parse_kicad_report(o.data)
            item["violations"] = {"error": sum(v.severity == "error" and not v.excluded for v in rep.violations),
                                  "warning": sum(v.severity == "warning" for v in rep.violations),
                                  "unconnected": rep.unconnected, "parity": rep.parity}
        elif det.role == "SIM_LOG":
            sl = parsers.parse_ngspice_log(o.data, o.exit_code)
            item.update(converged=sl.converged, temp_c=sl.temp_c, measures=sl.measures, errors=sl.errors)
        files.append(item)
    return {"status": res.status, "tool": res.tool, "tool_version": res.tool_version, "error": res.error,
            "steps": [{"argv": s.argv, "exit_code": s.exit_code, "timed_out": s.timed_out, "duration_s": s.duration_s}
                      for s in res.steps], "outputs": files, "sandbox": res.limits}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("describe", help="tool paths, versions and sandbox limits")
    run = sub.add_parser("run", help="run one allowlisted KiCad operation")
    run.add_argument("operation", choices=sorted(k for k, v in OPERATIONS.items() if v["tool"] == "kicad-cli"))
    run.add_argument("inputs", nargs="+", type=Path)
    run.add_argument("--out", type=Path, required=True)
    cor = sub.add_parser("corners", help="ngspice worst-case corners vin_min / vin_max / temp_max / load_max")
    cor.add_argument("netlist", type=Path)
    cor.add_argument("includes", nargs="*", type=Path)
    for name in ("vin-min", "vin-max", "ambient-max", "load-max"):
        cor.add_argument(f"--{name}", type=float, required=True)
    cor.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    runner = ToolRunner(N3Settings.from_env())
    if args.cmd == "describe":
        print(json.dumps(runner.describe(), indent=2))
        return 0
    try:
        if args.cmd == "run":
            res = runner.run(args.operation, [(p.name, p.read_bytes()) for p in args.inputs])
        else:
            corners, missing = engine.default_corners(DesignConstraints(
                input_voltage_min_v=args.vin_min, input_voltage_max_v=args.vin_max, ambient_max_c=args.ambient_max,
                load_max_a=args.load_max))
            res = runner.run("spice_corners", [(p.name, p.read_bytes()) for p in [args.netlist, *args.includes]],
                             corners)
    except ToolError as exc:
        print(json.dumps({"status": "REFUSED", "code": exc.code, "error": exc.message}, indent=2))
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    print(json.dumps(_summary(res, args.out), indent=2))
    return 0 if res.status == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
