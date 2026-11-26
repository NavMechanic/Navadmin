"""ATHENA data pipeline orchestrator.

    python -m data_pipeline.cli --nodes N0..N8 --priority P0 --mode all            # fetch + gate + format
    python -m data_pipeline.cli --nodes N3,N4 --priority all --action plan         # what would happen, no network
    python -m data_pipeline.cli --sources SRC-036 --action approve --steward steward.n4-quality \\
        --sha256 <artifact sha> --decision APPROVED --rationale "MIT, reviewed"     # steward ledger entry
    python -m data_pipeline.cli --sources SRC-036 --action export --bundle pcb-v1  # gated training export
    python -m data_pipeline.cli --action report                                    # HC-01..HC-12 over the lake

Actions: plan | run (default) | init | format | fixtures | report | approve | export.
Modes: all | train_candidates | rag_reference | eval_benchmarks | eval_noncommercial | private_ground_truth
(aliases train, rag, eval, noncommercial, private; comma-separated lists allowed).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .fetchers import FetchOutcome, fetcher_for
from .formatters import FormatError, build_for
from .policy_gates import (HARD_CONTROLS, NODES, PARTITIONS, GateReport, PolicyViolation, evaluate_artifact,
                           evaluate_training_export)
from .provenance import cyclonedx_bom, load_manifest, load_record_index
from .registry import DEFAULT_REGISTRY, Registry
from .storage import DataLake, REPO_ROOT

MODE_ALIASES = {"train": "train_candidates", "rag": "rag_reference", "eval": "eval_benchmarks",
                "noncommercial": "eval_noncommercial", "nc": "eval_noncommercial", "private": "private_ground_truth"}


def parse_nodes(value: str) -> list[str]:
    if value.lower() == "all":
        return list(NODES)
    out: list[str] = []
    for part in value.split(","):
        part = part.strip().upper()
        if ".." in part:
            a, b = part.split("..")
            lo, hi = int(a.lstrip("N")), int(b.lstrip("N"))
            out += [f"N{i}" for i in range(lo, hi + 1)]
        elif part:
            out.append(part)
    bad = [n for n in out if n not in NODES]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown nodes {bad}; use N0..N8")
    return sorted(set(out), key=NODES.index)


def parse_priority(value: str) -> Optional[list[str]]:
    if value.lower() == "all":
        return None
    out = []
    for part in value.upper().split(","):
        if ".." in part:
            a, b = (int(x.lstrip("P")) for x in part.split(".."))
            out += [f"P{i}" for i in range(a, b + 1)]
        else:
            out.append(part.strip())
    if any(p not in ("P0", "P1", "P2") for p in out):
        raise argparse.ArgumentTypeError("priority must be P0, P1, P2, ranges like P0..P1, or all")
    return out


def parse_modes(value: str) -> Optional[list[str]]:
    if value.lower() == "all":
        return None
    modes = [MODE_ALIASES.get(m.strip().lower(), m.strip().lower()) for m in value.split(",")]
    bad = [m for m in modes if m not in PARTITIONS]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown modes {bad}; use all or {', '.join(PARTITIONS)}")
    return modes


def env_secrets(registry: Registry) -> list[str]:
    names = {n for s in registry if s.api for n in s.api.auth.env_vars()}
    return [os.environ[n] for n in names if os.environ.get(n)]


def latest_artifact(lake: DataLake, spec, include_synthetic: bool = False) -> Optional[Path]:
    base = lake.source_dir(spec)
    if not base.exists():
        return None
    best = None
    for d in lake.artifact_dirs([spec.partition]):
        if not d.is_relative_to(base) or "live_cache" in d.parts and spec.intended_use != "LIVE-LOOKUP":
            continue
        if "synthetic_fixtures" in d.parts and not include_synthetic:
            continue
        m = load_manifest(d)
        if best is None or m.fetched_at_utc > best[1]:
            best = (d, m.fetched_at_utc)
    return best[0] if best else None


# ----------------------------------------------------------------------------------------------
# actions
# ----------------------------------------------------------------------------------------------
def action_plan(args, registry, lake, specs) -> dict:
    rows = []
    for s in specs:
        f = fetcher_for(s, lake, registry, dry_run=True)
        out = f.fetch()
        f.close()
        rows.append({**out.plan, "status": out.status, "reasons": out.reasons, "priority": s.priority,
                     "catalog_status": s.catalog_status})
    return {"action": "plan", "sources": rows}


def action_run(args, registry, lake, specs) -> dict:
    rows = []
    for s in specs:
        f = fetcher_for(s, lake, registry, dry_run=args.dry_run)
        try:
            out: FetchOutcome = f.fetch()
        finally:
            f.close()
        row = out.to_dict()
        if out.status in ("STAGED", "UNCHANGED") and out.artifact_dir and not args.no_format and not args.dry_run:
            try:
                row["format"] = build_for(lake, s, out.artifact_dir)
            except (FormatError, PolicyViolation) as exc:
                row["format"] = {"error": str(exc)}
        rows.append(row)
    return {"action": "run", "dry_run": args.dry_run, "sources": rows}


def action_init(args, registry, lake, specs) -> dict:
    from .fetchers import DropZoneIntake, VaultStubFetcher
    rows = []
    for s in specs:
        if s.access == "drop_zone":
            rows.append({"source_id": s.source_id, "drop_zone": str(DropZoneIntake(s, lake, registry).ensure_zone())})
        elif s.access == "vault":
            rows.append({"source_id": s.source_id, "vault": str(VaultStubFetcher(s, lake, registry).ensure_stub())})
    return {"action": "init", "sources": rows}


def action_fixtures(args, registry, lake, specs) -> dict:
    from .fetchers import VaultStubFetcher
    rows = []
    for s in specs:
        if s.access == "vault":
            out = VaultStubFetcher(s, lake, registry).generate_synthetic_fixtures(args.n)
            rows.append(out.to_dict())
    return {"action": "fixtures", "sources": rows}


def action_format(args, registry, lake, specs) -> dict:
    rows = []
    for s in specs:
        d = latest_artifact(lake, s)
        if d is None:
            rows.append({"source_id": s.source_id, "format": {"error": "no staged artifact"}})
            continue
        try:
            rows.append({"source_id": s.source_id, "artifact_dir": str(d), "format": build_for(lake, s, d)})
        except (FormatError, PolicyViolation) as exc:
            rows.append({"source_id": s.source_id, "artifact_dir": str(d), "format": {"error": str(exc)}})
    return {"action": "format", "sources": rows}


def action_report(args, registry, lake, specs) -> dict:
    ids = {s.source_id for s in specs}
    secrets = env_secrets(registry)
    reports, by_partition = [], {p: [] for p in PARTITIONS}
    for d in lake.artifact_dirs():
        try:
            m = load_manifest(d)
        except Exception as exc:
            reports.append({"subject": str(d), "ok": False, "violations": [{"code": "HC-01", "message": str(exc)}]})
            continue
        if m.source_id not in ids:
            continue
        rep = evaluate_artifact(lake, d, registry, secrets)
        reports.append(rep.to_dict())
        by_partition[lake.partition_of(d)].append(m)
    out_dir = lake.reports_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for part, manifests in by_partition.items():
        if manifests:
            (out_dir / f"cyclonedx-{part}-{stamp}.json").write_text(json.dumps(cyclonedx_bom(manifests), indent=2))
    return {"action": "report", "controls": HARD_CONTROLS, "artifacts": reports,
            "summary": {"artifacts": len(reports), "failing": sum(not r["ok"] for r in reports)}}


def action_approve(args, registry, lake, specs) -> dict:
    if len(specs) != 1 or not args.sha256 or not args.steward or not args.decision:
        raise SystemExit("approve needs exactly one --sources, plus --sha256, --steward and --decision")
    row = lake.ledger.record(source_id=specs[0].source_id, sha256=args.sha256, decision=args.decision,
                             steward_id=args.steward, registry=registry, rationale=args.rationale or "")
    return {"action": "approve", "ledger_entry": row}


def action_export(args, registry, lake, specs) -> dict:
    bundle = args.bundle or datetime.now(timezone.utc).strftime("bundle-%Y%m%dT%H%M%SZ")
    dest = lake.exports_dir() / bundle
    eval_hashes: set[str] = set()
    for d in lake.artifact_dirs(["eval_benchmarks", "eval_noncommercial"]):
        eval_hashes |= load_record_index(d)
    secrets = env_secrets(registry)
    included, refused = [], []
    for s in specs:
        d = latest_artifact(lake, s, include_synthetic=True)
        if d is None:
            refused.append({"source_id": s.source_id, "reasons": ["no staged artifact"]})
            continue
        rep: GateReport = evaluate_training_export(lake, d, registry, lake.ledger, eval_hashes, secrets)
        if rep.ok and not (d / "formatted" / "format_manifest.json").exists():
            rep.fail("HC-03", "artifact has no formatted outputs to export (run --action format)")
        if not rep.ok:
            refused.append({"source_id": s.source_id, "artifact_dir": str(d), "gate": rep.to_dict()})
            continue
        m = load_manifest(d)
        target = dest / s.source_id / d.name
        shutil.copytree(d / "formatted", target / "formatted")
        for name in ("provenance_manifest.json", "prov.jsonld", "spdx.json", "cyclonedx.json"):
            if (d / name).exists():
                shutil.copy2(d / name, target / name)
        included.append({"source_id": s.source_id, "sha256": m.sha256, "version_tag": m.version_tag,
                         "training_rights_approval": lake.ledger.effective(s.source_id, m.sha256),
                         "license_spdx_id": m.license_spdx_id, "gate": rep.to_dict()})
    doc = {"bundle": bundle, "created_at_utc": datetime.now(timezone.utc).isoformat(), "included": included,
           "refused": refused}
    if included:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "bundle_manifest.json").write_text(json.dumps(doc, indent=2, default=str))
    return {"action": "export", **doc, "bundle_dir": str(dest) if included else None}


ACTIONS = {"plan": action_plan, "run": action_run, "init": action_init, "fixtures": action_fixtures,
           "format": action_format, "report": action_report, "approve": action_approve, "export": action_export}


# ----------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m data_pipeline.cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--nodes", type=parse_nodes, default=list(NODES), help="N0..N8, N1,N3 or all (default all)")
    p.add_argument("--priority", type=parse_priority, default=None, help="P0, P0,P1, P0..P1 or all (default all)")
    p.add_argument("--mode", type=parse_modes, default=None, help="partition(s) or all (default all)")
    p.add_argument("--sources", default=None, help="comma-separated SRC ids (narrows the selection)")
    p.add_argument("--action", choices=sorted(ACTIONS), default="run")
    p.add_argument("--dry-run", action="store_true", help="plan fetches without any network access or writes")
    p.add_argument("--no-format", action="store_true", help="fetch and gate only")
    p.add_argument("--lake", default=str(REPO_ROOT / "data_lake"))
    p.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    p.add_argument("--json", action="store_true", help="print the full JSON report")
    p.add_argument("--strict", action="store_true", help="non-zero exit when any source is rejected")
    p.add_argument("--n", type=int, default=20, help="fixtures: records per vault")
    p.add_argument("--steward"), p.add_argument("--sha256"), p.add_argument("--rationale")
    p.add_argument("--decision", choices=["APPROVED", "REJECTED", "PENDING"])
    p.add_argument("--bundle", help="export: bundle name")
    return p


def _table(report: dict) -> str:
    lines = []
    for r in report.get("sources", []):
        status = r.get("status") or ("FORMATTED" if r.get("format") and "error" not in r.get("format", {}) else "")
        fmt = r.get("format") or {}
        fmt_s = f" format:{'ERROR ' + fmt['error'][:80] if 'error' in fmt else fmt.get('builder') or fmt.get('skipped', '')}" \
            if fmt else ""
        reason = (" — " + "; ".join(r.get("reasons", []))[:140]) if r.get("reasons") else ""
        plan = r if "access" in r else (r.get("plan") or {})
        part = r.get("partition") or plan.get("partition", "")
        note = ""
        if plan.get("credentials_present") is False:
            note = f" needs env {plan.get('env_vars')}"
        elif plan.get("access") == "drop_zone":
            note = f" manual drop zone ({len(plan.get('files_waiting', []))} waiting)"
        elif plan.get("access") == "vault":
            note = " vault (no network)"
        elif plan.get("access"):
            note = f" {plan.get('access')}"
        lines.append(f"{r.get('source_id', '?'):8} {status:13} {part:20}{note}{fmt_s}{reason}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    registry = Registry.load(args.registry)
    lake = DataLake(args.lake)
    ids = [x.strip() for x in args.sources.split(",")] if args.sources else None
    specs = registry.select(nodes=args.nodes, priorities=args.priority, partitions=args.mode, source_ids=ids)
    report = ACTIONS[args.action](args, registry, lake, specs)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.action in ("run", "report", "export") and not args.dry_run:
        (lake.reports_dir() / f"{args.action}-{stamp}.json").write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    elif args.action in ("plan", "run", "fixtures", "format"):
        print(_table(report))
        print(f"\n{len(specs)} source(s) selected; lake: {lake.root}")
    elif args.action == "report":
        s = report["summary"]
        print(f"{s['artifacts']} artifact(s) evaluated, {s['failing']} failing HC-01..HC-12")
        for r in report["artifacts"]:
            if not r["ok"]:
                print(f"  FAIL {r['subject']}: {[v['code'] + ' ' + v['message'] for v in r['violations']][:3]}")
    elif args.action == "export":
        print(f"exported {len(report['included'])} artifact(s) to {report['bundle_dir']}; refused {len(report['refused'])}")
        for r in report["refused"]:
            print(f"  REFUSED {r['source_id']}: {[v['code'] + ' ' + v['message'] for v in r.get('gate', {}).get('violations', [])] or r.get('reasons')}")
    else:
        print(json.dumps(report, indent=2, default=str))
    rows = report.get("sources", [])
    failed = [r for r in rows if r.get("status") == "FAILED" or "error" in (r.get("format") or {})]
    rejected = [r for r in rows if r.get("status") == "REJECTED"]
    if args.action == "export" and report["refused"]:
        return 2
    return 1 if failed or (args.strict and rejected) else 0


if __name__ == "__main__":
    sys.exit(main())
