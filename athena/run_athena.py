"""
CLI entry point for Project Athena.

Usage
-----
# Dry-run (simulated — no real API keys required):
    python -m athena.run_athena

# Trigger against a real Pecan AI deal:
    python -m athena.run_athena --deal-id DEAL-ABCD1234

# Start the FastAPI server:
    python -m athena.run_athena --serve
"""

from __future__ import annotations

import argparse
import json
import sys


def _print_run(run) -> None:
    """Pretty-print an AthenaRun to stdout."""
    sep = "=" * 68

    print(f"\n{sep}")
    print(f"  PROJECT ATHENA RUN  {run.run_id}")
    print(f"  Status : {run.status.value.upper()}")
    print(f"  Started: {run.started_at.isoformat()}")
    if run.completed_at:
        elapsed = (run.completed_at - run.started_at).total_seconds()
        print(f"  Elapsed: {elapsed:.1f}s")
    print(sep)

    # Layer 1
    if run.demand:
        d = run.demand
        print(f"\n[1] DEMAND FORECAST  (Pecan AI)")
        print(f"    Deal       : {d.signal.deal_id} — {d.signal.account_name}")
        print(f"    Probability: {d.signal.probability:.0%}")
        print(f"    Forecast   : {d.forecast_units} units  "
              f"[{d.confidence_interval_low} – {d.confidence_interval_high}]")
        print(f"    Build Qty  : {d.recommended_build_qty} units (recommended)")

    # Layer 2
    if run.sourcing:
        s = run.sourcing
        print(f"\n[2] SOURCING LOCK  (Duro PLM + Nexar)")
        print(f"    BOM        : {s.bom.bom_id}  rev {s.bom.revision} — {s.bom.product_name}")
        print(f"    Lines      : {len(s.bom.lines)}")
        print(f"    Shortages  : {s.shortage_count}  |  Alternates mapped: {s.alternates_mapped}")
        print(f"    Risk       : {s.sourcing_risk.upper()}")
        for a in s.availability:
            flag = "✓" if a.status.value == "available" else "⚠"
            alt = f"  → alt: {a.alternate_mpn}" if a.alternate_mpn else ""
            print(f"      {flag} {a.mpn:<30} {a.status.value}{alt}")

    # Layer 3
    if run.simulation:
        sim = run.simulation
        pv = sim.physical_validation
        print(f"\n[3] SIMULATION  (Lokad + Ansys Twin Builder)")
        print(f"    Physical OK: {'YES' if pv.passed else 'NO — ' + ', '.join(pv.failure_modes)}")
        print(f"    Max stress : {pv.max_stress_mpa} MPa  |  "
              f"ΔT: {pv.thermal_delta_c} °C  |  "
              f"Vibration: {pv.vibration_g_rms} g RMS")
        for ss in sim.safety_stocks[:3]:
            print(f"      {ss.mpn:<30} SS={ss.safety_stock_qty}  ROP={ss.reorder_point}")
        if len(sim.safety_stocks) > 3:
            print(f"      … +{len(sim.safety_stocks) - 3} more parts")

    # Layer 4
    if run.execution:
        ex = run.execution
        print(f"\n[4] EXECUTION  (MacroFab + Fictiv)")
        print(f"    PCBA order : {ex.pcba_order.order_id}  ETA {ex.pcba_order.estimated_ship_date}")
        print(f"    Mech order : {ex.mechanical_order.order_id}  ETA {ex.mechanical_order.estimated_ship_date}")
        print(f"    Total cost : ${ex.total_cost_usd:,.2f}  |  Lead time: {ex.lead_time_days}d")

    # Layer 5
    if run.quality:
        q = run.quality
        print(f"\n[5] QUALITY & REVENUE  (Instrumental + TwinMaker + RaaS/YaSS)")
        print(f"    Units inspected : {len(q.inspections)}")
        print(f"    Yield rate      : {q.yield_rate * 100:.1f}%")
        print(f"    TwinMaker pushes: {len(q.twin_updates)}")
        print(f"    RaaS (monthly)  : ${q.total_raas_monthly_usd:,.2f}")
        print(f"    YaSS (annual)   : ${q.total_yass_annual_usd:,.2f}")
        tiers: dict = {}
        for r in q.revenue_shares:
            tiers[r.monetization_tier] = tiers.get(r.monetization_tier, 0) + 1
        for tier, count in sorted(tiers.items()):
            print(f"      {tier:<12}: {count} units")

    # Errors
    if run.errors:
        print(f"\n{'—' * 68}")
        print("WARNINGS / ERRORS:")
        for e in run.errors:
            print(f"  ⚠  {e}")

    print(f"\n{sep}\n")


def cmd_run(args) -> None:
    from athena.orchestrator import AthenaOrchestrator

    rag_pipeline = None
    if args.with_rag:
        try:
            from rag.pipeline import RAGPipeline
            rag_pipeline = RAGPipeline()
            print("RAG pipeline loaded for RCA context.")
        except Exception as exc:
            print(f"Warning: Could not load RAG pipeline: {exc}", file=sys.stderr)

    print("Starting Project Athena orchestration loop …")
    orch = AthenaOrchestrator(rag_pipeline=rag_pipeline)
    run = orch.run(deal_id=args.deal_id)
    _print_run(run)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(run.model_dump(mode="json"), f, indent=2, default=str)
        print(f"Run data written to {args.json_out}")

    sys.exit(0 if run.status.value in ("success", "partial") else 1)


def cmd_serve(args) -> None:
    import uvicorn
    from athena.api import create_app

    rag_pipeline = None
    if args.with_rag:
        try:
            from rag.pipeline import RAGPipeline
            rag_pipeline = RAGPipeline()
            print("RAG pipeline loaded for RCA context.", file=sys.stderr)
        except Exception as exc:
            print(f"Warning: Could not load RAG pipeline: {exc}", file=sys.stderr)

    app_instance = create_app(rag_pipeline=rag_pipeline)
    uvicorn.run(
        app_instance,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="athena",
        description="Project Athena — hardware production orchestration loop",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    run_p = sub.add_parser("run", help="Execute the Athena loop once and print results")
    run_p.add_argument("--deal-id", default=None, help="Pecan AI deal ID to forecast")
    run_p.add_argument("--json-out", default=None, metavar="PATH",
                       help="Write full run JSON to this file")
    run_p.add_argument("--with-rag", action="store_true",
                       help="Enable RAG pipeline for RCA context on defects")
    run_p.set_defaults(func=cmd_run)

    serve_p = sub.add_parser("serve", help="Start the FastAPI REST server")
    serve_p.add_argument("--host", default="0.0.0.0")
    serve_p.add_argument("--port", type=int, default=8080)
    serve_p.add_argument("--reload", action="store_true")
    serve_p.add_argument("--with-rag", action="store_true",
                         help="Enable RAG pipeline for RCA context on defects")
    serve_p.set_defaults(func=cmd_serve)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not hasattr(args, "func"):
        # Default to 'run' when called with no subcommand.
        args.func = cmd_run
        args.deal_id = None
        args.json_out = None

    args.func(args)


if __name__ == "__main__":
    main()
