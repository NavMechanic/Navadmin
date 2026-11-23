"""CLI entry point for the Navadmin RAG pipeline.

Every ingest command passes the ATHENA DQ Gateway before anything is indexed: supply the
artifact's Canonical I-O Manifest with ``--submission`` (``dq-manifest`` generates one over the
exact bytes the command reads), the tenant with ``--tenant`` and a persistent registry with
``--dq-registry`` (rights grants, approvers, released revisions).
"""

import argparse
import json
import os
import sys

DEFAULT_DQ_REGISTRY = "data/.dq_registry.json"


# ---------------------------------------------------------------------------
# DQ gateway helpers
# ---------------------------------------------------------------------------

def _dq_gateway(args):
    from rag.dq_gateway import ArtifactRegistry, DQGateway

    return DQGateway(registry=ArtifactRegistry(getattr(args, "dq_registry", None) or DEFAULT_DQ_REGISTRY))


def _make_pipeline(args):
    """RAGPipeline wired to a persistent DQ registry, tenant and enforce/sandbox mode."""
    from rag.pipeline import RAGPipeline

    tenant = getattr(args, "tenant", None) or os.environ.get("ATHENA_TENANT_ID")
    return RAGPipeline(dq_gateway=_dq_gateway(args), tenant_id=tenant,
                       dq_mode="sandbox" if getattr(args, "dq_sandbox", False) else "enforce",
                       actor_id=getattr(args, "actor", None) or "rag-pipeline")


def _load_submission(path):
    """Read a submission JSON: {"manifest": {...}, "required_critical"?, "use_for_training"?}.

    The payload and bytes are never taken from this file: the pipeline builds them from what
    it actually reads. A file of the form {"by_file": {"<name>": {...}}} maps PDFs in a directory.
    """
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if "by_file" in data:
        return {name: _load_submission_obj(v) for name, v in data["by_file"].items()}
    return _load_submission_obj(data)


def _load_submission_obj(data):
    from rag.dq_gateway import ArtifactSubmission

    unknown = set(data) - {"manifest", "required_critical", "use_for_training", "ai_provenance"}
    if unknown:
        raise SystemExit(f"submission file has unsupported keys {sorted(unknown)} "
                         "(payload/content are derived from the ingested bytes)")
    return ArtifactSubmission(manifest=data["manifest"], required_critical=data.get("required_critical", []),
                              use_for_training=data.get("use_for_training", False),
                              ai_provenance=data.get("ai_provenance"))


def _print_decision(decision) -> None:
    print(f"DQ gate: {decision.quality_gate}  artifact={decision.artifact_ref}  "
          f"score={decision.weighted_score}  event={decision.event_id}")
    for r in decision.failed_rules:
        print(f"  ✗ {r.rule_id} [{r.severity}/{r.action_on_fail}] {r.error_code}: {r.message}")
    for reason in decision.reasons:
        print(f"  · {reason}")


def _gated(args, pipeline, run):
    """Run an ingest call; on HOLD with --approve-as, record the sign-off and re-run once."""
    from rag.dq_gateway import DQGateRejected, DQGatewayError

    try:
        return run()
    except DQGateRejected as exc:
        d = exc.decision
        if d is None:
            raise SystemExit(str(exc))
        if d.quality_gate == "HOLD" and getattr(args, "approve_as", None):
            try:
                approved = pipeline.dq_gateway.approve(d.event_id, args.approve_as, pipeline.tenant_id,
                                                       notes="CLI sign-off")
            except DQGatewayError as err:
                _print_decision(d)
                raise SystemExit(f"approval refused: {err}")
            if approved.quality_gate == "RELEASED":
                print(f"Approved by {args.approve_as}: {approved.artifact_ref} RELEASED")
                return run()
            d = approved
        _print_decision(d)
        raise SystemExit(2)


def cmd_ingest(args) -> None:
    pipeline = _make_pipeline(args)
    sub = _load_submission(args.submission)
    count = _gated(args, pipeline, lambda: pipeline.ingest(args.path, sub))
    print(f"Ingested {count} chunk(s) from: {args.path}")


def cmd_ingest_telemetry(args) -> None:
    """Parse NexTest tester log files and index as structured failure events."""
    pipeline = _make_pipeline(args)
    sub = _load_submission(args.submission)
    count = _gated(args, pipeline, lambda: pipeline.ingest_telemetry(args.path, sub))
    print(f"Ingested {count} telemetry chunk(s) from: {args.path}")


def cmd_ingest_fea(args) -> None:
    """Parse FEA mesh JSON/CSV and index stress voxels."""
    pipeline = _make_pipeline(args)
    sub = _load_submission(args.submission)
    count = _gated(args, pipeline, lambda: pipeline.ingest_fea(args.path, sub))
    print(f"Ingested {count} FEA voxel chunk(s) from: {args.path}")


def cmd_ingest_cad(args) -> None:
    """Parse STEP file or CAD JSON manifest and index components."""
    pipeline = _make_pipeline(args)
    sub = _load_submission(args.submission)
    count = _gated(args, pipeline, lambda: pipeline.ingest_cad(args.path, sub))
    print(f"Ingested {count} CAD component chunk(s) from: {args.path}")


def cmd_dq_manifest(args) -> None:
    """Generate a Canonical I-O Manifest over the exact bytes an ingest command will read."""
    from rag.dq_gateway import build_manifest
    from rag.dq_gateway.sources import read_kind

    gw = _dq_gateway(args)
    content, _ = read_kind(args.path, args.kind)
    sources = [s for s in (args.sources or "").split(",") if s]
    if not sources:
        grant = gw.registry.rights_grant(args.project)
        if grant is None:
            raise SystemExit(f"project {args.project} has no G0 rights grant in {args.dq_registry}; "
                             "pass --sources or record the grant first")
        sources = [grant.ref]
    manifest = build_manifest(
        registry=gw.registry, project_id=args.project, node_id=args.node, artifact_id=args.artifact_id,
        revision=args.rev, output_schema="document_corpus.v1", content=content, sources=sources,
        evidence_ids=[e for e in args.evidence.split(",") if e], rights=args.rights,
        classification=args.classification, origin=args.origin, model_or_tool=args.tool,
        connector_mode=args.connector_mode, input_schema=args.input_schema,
    )
    out = json.dumps({"manifest": manifest}, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
        print(f"Manifest for {args.path} ({args.kind}) written to {args.out}; "
              f"output_file_sha256={manifest['output_file_sha256']}")
    else:
        print(out)


def _parse_spatial_bbox(bbox_str: str) -> dict:
    """Parse 'x_min,x_max,y_min,y_max,z_min,z_max' into bbox dict."""
    parts = [float(p.strip()) for p in bbox_str.split(",")]
    if len(parts) != 6:
        raise ValueError(
            "Spatial bbox must be 6 comma-separated floats: x_min,x_max,y_min,y_max,z_min,z_max"
        )
    return {
        "x_axis_range": [parts[0], parts[1]],
        "y_axis_range": [parts[2], parts[3]],
        "z_axis_range": [parts[4], parts[5]],
    }


def _parse_telemetry_event_arg(te_arg: str):
    """Parse '<log_file>:<track_id>' into a TelemetryEvent by scanning the log file."""
    from rag.ingestion.telemetry_parser import TelemetryParser

    if ":" not in te_arg:
        print(f"Warning: --telemetry-event should be 'log_file:track_id', got: {te_arg}")
        return None

    log_path, track_id = te_arg.rsplit(":", 1)
    parser = TelemetryParser()
    events = parser.parse_log_file(log_path)
    matched = [e for e in events if e.track_id == track_id]

    if not matched:
        print(f"Warning: no failure event found for track_id={track_id} in {log_path}")
        return None

    return matched[0]


def cmd_query(args) -> None:
    from rag.pipeline import RAGPipeline

    pipeline = RAGPipeline()

    spatial_bbox = None
    if getattr(args, "spatial_bbox", None):
        try:
            spatial_bbox = _parse_spatial_bbox(args.spatial_bbox)
        except ValueError as exc:
            print(f"Error parsing --spatial-bbox: {exc}", file=sys.stderr)
            sys.exit(1)

    telemetry_event = None
    if getattr(args, "telemetry_event", None):
        telemetry_event = _parse_telemetry_event_arg(args.telemetry_event)

    result = pipeline.query(
        question=args.question,
        evaluate=args.evaluate,
        ground_truth=args.ground_truth,
        spatial_bbox=spatial_bbox,
        telemetry_event=telemetry_event,
    )

    print("\n" + "=" * 60)
    print("ANSWER")
    print("=" * 60)
    print(result.answer)

    print("\n" + "=" * 60)
    print("SOURCES")
    print("=" * 60)
    for source in result.sources:
        idx = source.get("index", "?")
        src = source.get("source", "unknown")
        score = source.get("score")
        score_str = f"  (score: {score:.4f})" if score is not None else ""
        print(f"  [{idx}] {src}{score_str}")

    if result.evaluation:
        print("\n" + "=" * 60)
        print("EVALUATION METRICS")
        print("=" * 60)
        print(result.evaluation)

    print()


def cmd_dq_seed_demo(args) -> None:
    """Seed the SYNTHETIC demo tenant (project, NDA, G0 grant, approver) into the DQ registry."""
    from rag.dq_gateway.demo import seed_demo

    info = seed_demo(_dq_gateway(args))
    if not info["seeded"]:
        print(f"Demo tenant already present in {args.dq_registry}.")
        return
    print(f"Seeded tenant '{info['tenant']}', project '{info['project']}' into {args.dq_registry}.\n"
          f"Approver: {info['approver']} (gates G1–G5, G7). Evidence: EV-NDA-DEMO. All demo data is SYNTHETIC.")


def cmd_ingest_drive(args) -> None:
    """Ingest documents from the Aviator Google Drive folders (through the DQ gateway)."""
    from rag.ingestion.drive_loader import AviatorDriveLoader
    from rag.dq_gateway.sources import aviator_mirror_bundle

    pipeline = _make_pipeline(args)
    sub = _load_submission(args.submission)
    loader = AviatorDriveLoader(credentials_path=args.credentials)

    if args.local_mirror:
        docs = loader.load_local_aviator_mirror(args.local_mirror)
        content, files = aviator_mirror_bundle(args.local_mirror)
    else:
        # The live Drive loader yields parsed text, not the downloaded bytes, so nothing can be
        # checked against output_file_sha256: DQ05 quarantines it. Mirror locally to ingest.
        docs = loader.load_all_aviator()
        content, files = None, {}

    print(f"Loaded {len(docs)} document(s) from Drive.")
    if not docs:
        print("No documents found — nothing to ingest.")
        return

    count = _gated(args, pipeline, lambda: pipeline.ingest_documents(
        docs, sub, source_kind="document", content=content, file_bytes=files))
    print(f"Done. {count} chunk(s) indexed from Aviator Drive data.")


def cmd_reset(args) -> None:
    from rag.pipeline import RAGPipeline

    pipeline = RAGPipeline()
    pipeline.reset_index()
    print("Vector index has been reset.")


def cmd_twin_simulate(args) -> None:
    """Run a batch simulation of the Digital Twin and print results."""
    from rag.digital_twin.twin import DigitalTwin

    rag_pipeline = None
    if getattr(args, "with_rag", False):
        from rag.pipeline import RAGPipeline
        rag_pipeline = RAGPipeline()

    twin = DigitalTwin(rag_pipeline=rag_pipeline)
    print(f"Running batch simulation ({args.n} cycles)…")
    twin.run_batch_simulation(n=args.n)

    print(f"\nPredicted yield:  {twin.yield_rate() * 100:.1f}%")
    fm = twin.top_failure_modes()
    if fm:
        print("\nPredicted failure mode Pareto:")
        for code, count in fm.items():
            print(f"  {count:3d}x  {code}")
    else:
        print("No predicted failures.")


def cmd_twin_dashboard(args) -> None:
    """Launch the Streamlit digital twin dashboard."""
    import subprocess, sys

    cmd = [sys.executable, "-m", "streamlit", "run", "dashboard/app.py"]
    if getattr(args, "with_rag", False):
        cmd += ["--", "--with-rag"]
    subprocess.run(cmd)


def cmd_fmea_assess(args) -> None:
    """Assess PFMEA and DFMEA checklists and print high-risk items."""
    from rag.fmea.fmea import load_pfmea_csv, load_dfmea_csv, FMEAAssessment
    from pathlib import Path

    pfmea_path = args.pfmea or "data/PFMEA_Aviator_Assembly.csv"
    dfmea_path = args.dfmea or "data/DFMEA_Aviator_Design.csv"

    if not Path(pfmea_path).exists() or not Path(dfmea_path).exists():
        print(f"Error: FMEA files not found at {pfmea_path} and {dfmea_path}")
        return

    pfmea = load_pfmea_csv(pfmea_path)
    dfmea = load_dfmea_csv(dfmea_path)
    assessment = FMEAAssessment(pfmea, dfmea)

    print(assessment.to_report())

    high_rpn = assessment.high_rpn_items(threshold=args.rpn_threshold)
    print(f"\nTotal items with RPN >= {args.rpn_threshold}: {len(high_rpn)}")


def cmd_fmea_check_twin(args) -> None:
    """Run digital twin batch sim and show FMEA items triggered by predicted failures."""
    from rag.fmea.fmea import load_pfmea_csv, load_dfmea_csv, FMEAAssessment
    from rag.digital_twin.twin import DigitalTwin
    from pathlib import Path

    pfmea_path = args.pfmea or "data/PFMEA_Aviator_Assembly.csv"
    dfmea_path = args.dfmea or "data/DFMEA_Aviator_Design.csv"

    if not Path(pfmea_path).exists() or not Path(dfmea_path).exists():
        print(f"Error: FMEA files not found")
        return

    pfmea = load_pfmea_csv(pfmea_path)
    dfmea = load_dfmea_csv(dfmea_path)
    assessment = FMEAAssessment(pfmea, dfmea)
    twin = DigitalTwin(fmea_assessment=assessment)

    print(f"Running twin batch simulation ({args.n} cycles) with FMEA assessment…")
    twin.run_batch_simulation(n=args.n)

    print(f"\nPredicted yield: {twin.yield_rate() * 100:.1f}%")
    print(f"\nFMEA Assessment:")
    print(twin.fmea_report())


def cmd_ipc_check(args) -> None:
    """Run IPC-A-610G inspection against a single nominal or batch prediction."""
    from rag.ipc.ipc_a610g import IPCInspector, IPCClass
    from rag.digital_twin.twin import DigitalTwin
    from rag.fmea.fmea import load_pfmea_csv, load_dfmea_csv, FMEAAssessment
    from pathlib import Path

    inspector = IPCInspector(ipc_class=IPCClass.CLASS_2)

    pfmea_path = "data/PFMEA_Aviator_Assembly.csv"
    dfmea_path = "data/DFMEA_Aviator_Design.csv"
    fmea = None
    if Path(pfmea_path).exists() and Path(dfmea_path).exists():
        fmea = FMEAAssessment(load_pfmea_csv(pfmea_path), load_dfmea_csv(dfmea_path))

    twin = DigitalTwin(fmea_assessment=fmea, ipc_inspector=inspector)
    n = args.n
    print(f"Running IPC-A-610G inspection across {n} simulated cycles…\n")
    twin.run_batch_simulation(n=n)

    # Show last snapshot IPC report
    last = twin._history[-1]
    if last.ipc_results:
        print(inspector.format_report(last.ipc_results))
    else:
        print("No IPC results — check that IPCInspector is wired up.")

    # Aggregate variability summary
    all_ipc = twin._ipc_history
    if all_ipc:
        print(f"\n=== Process Variability Summary ({n} cycles) ===")
        from collections import Counter
        all_conditions = [r.condition.value for cycle in all_ipc for r in cycle]
        counts = Counter(all_conditions)
        for cond in ("Target", "Acceptable", "Process Indicator", "Defect"):
            pct = counts.get(cond, 0) / len(all_conditions) * 100 if all_conditions else 0
            print(f"  {cond:20s}: {counts.get(cond,0):4d}  ({pct:.1f}%)")

        print("\nMost variable IPC criteria (highest Defect+PI rate):")
        from collections import defaultdict
        section_counts = defaultdict(lambda: {"Defect": 0, "PI": 0, "total": 0})
        for cycle in all_ipc:
            for r in cycle:
                section_counts[r.criteria_section]["total"] += 1
                if r.condition.value == "Defect":
                    section_counts[r.criteria_section]["Defect"] += 1
                elif r.condition.value == "Process Indicator":
                    section_counts[r.criteria_section]["PI"] += 1
        ranked = sorted(section_counts.items(),
                        key=lambda x: -(x[1]["Defect"] + x[1]["PI"]))
        for sec, c in ranked[:5]:
            bad_rate = (c["Defect"] + c["PI"]) / c["total"] * 100
            print(f"  §{sec}: {bad_rate:.1f}% Defect+PI rate")


def cmd_ingest_pdf(args) -> None:
    """Ingest a PDF file or directory with section and table detection (through the DQ gateway)."""
    from pathlib import Path
    from rag.ingestion.pdf_pipeline import PDFPipeline

    pipeline = _make_pipeline(args)
    sub = _load_submission(args.submission)
    if isinstance(sub, dict):
        resolver = lambda path: sub.get(Path(path).name)  # noqa: E731
    else:
        resolver = lambda path: sub  # noqa: E731
    pdf_pipeline = PDFPipeline(pipeline, submission_for=resolver)

    if os.path.isdir(args.path):
        results = pdf_pipeline.ingest_local_directory(args.path)
    else:
        results = [pdf_pipeline.ingest_file(args.path)]

    print(PDFPipeline.format_report(results))


def cmd_ingest_pdf_drive(args) -> None:
    """Sync and ingest PDFs from Google Drive Aviator folders (through the DQ gateway)."""
    from pathlib import Path
    from rag.ingestion.drive_sync import DrivePDFSync
    from rag.ingestion.pdf_pipeline import PDFPipeline

    pipeline = _make_pipeline(args)
    sub = _load_submission(args.submission)
    if isinstance(sub, dict):
        pipeline.dq_submission_for = lambda path: sub.get(Path(path).name)
    elif sub is not None:
        raise SystemExit("ingest-pdf-drive needs a {\"by_file\": {...}} submission file (one manifest per PDF)")
    sync = DrivePDFSync(
        rag_pipeline=pipeline,
        credentials_path=args.credentials,
        manifest_path=args.manifest,
    )

    folders = args.folders.split(",") if args.folders else None

    if args.local_mirror:
        results = sync.sync_local_mirror(args.local_mirror)
    elif args.folder_id:
        results = sync.sync_folder(args.folder_id, folder_name=args.folder_id)
    else:
        results = sync.sync_all_aviator(folders=folders)

    print(PDFPipeline.format_report(results))
    print()
    print(sync.manifest.status_report())


def cmd_pdf_drive_status(args) -> None:
    """Show sync status of indexed Drive PDFs."""
    from rag.ingestion.drive_sync import DriveSyncManifest

    manifest = DriveSyncManifest(args.manifest)
    print(manifest.status_report())


def cmd_athena_run(args) -> None:
    """Execute Project Athena orchestration loop with optional RAG integration."""
    from athena.orchestrator import AthenaOrchestrator

    rag = None
    try:
        from rag.pipeline import RAGPipeline
        print("Loading RAG pipeline …")
        rag = RAGPipeline()
        print("  ✓ RAG pipeline ready for RCA context")
    except Exception as exc:
        print(f"⚠  RAG pipeline unavailable ({type(exc).__name__}): running Athena without RCA context")

    print("Starting Project Athena orchestrator …")
    orchestrator = AthenaOrchestrator(rag_pipeline=rag)

    print("Starting end-to-end hardware production loop …\n")
    run = orchestrator.run(deal_id=args.deal_id)

    # Pretty-print the run
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"  PROJECT ATHENA RUN  {run.run_id}")
    print(f"  Status : {run.status.value.upper()}")
    print(f"  Elapsed: {(run.completed_at - run.started_at).total_seconds():.1f}s")
    print(sep)

    if run.demand:
        d = run.demand
        print(f"\n[1] DEMAND  {d.signal.deal_id}  {d.recommended_build_qty}× units")

    if run.sourcing:
        s = run.sourcing
        print(f"[2] SOURCING  {len(s.bom.lines)} parts  {s.shortage_count} shortages  risk={s.sourcing_risk}")

    if run.simulation:
        pv = run.simulation.physical_validation
        print(f"[3] SIMULATION  physical={'PASS' if pv.passed else 'FAIL'}  "
              f"stress={pv.max_stress_mpa}MPa  thermal={pv.thermal_delta_c}°C")

    if run.execution:
        ex = run.execution
        print(f"[4] EXECUTION  ${ex.total_cost_usd:,.0f}  {ex.lead_time_days}d lead")

    if run.quality:
        q = run.quality
        print(f"[5] QUALITY  yield={q.yield_rate*100:.1f}%  "
              f"raas=${q.total_raas_monthly_usd:,.0f}/mo  yass=${q.total_yass_annual_usd:,.0f}/yr")

    if run.errors:
        print(f"\nWarnings ({len(run.errors)}):")
        for e in run.errors:
            print(f"  ⚠ {e}")

    print(f"\n{sep}\n")

    if args.json_out:
        import json
        with open(args.json_out, "w") as f:
            json.dump(run.model_dump(mode="json"), f, indent=2, default=str)
        print(f"Full run data written to {args.json_out}\n")

    sys.exit(0 if run.status.value in ("success", "partial") else 1)


def cmd_fishbone_diagram(args) -> None:
    """Generate and display Mermaid fishbone diagrams for FMEA items."""
    from rag.fmea.fmea import load_pfmea_csv, load_dfmea_csv, FMEAAssessment
    from rag.fmea.fishbone import FishboneGenerator
    from pathlib import Path

    pfmea_path = args.pfmea or "data/PFMEA_Aviator_Assembly.csv"
    dfmea_path = args.dfmea or "data/DFMEA_Aviator_Design.csv"

    if not Path(pfmea_path).exists() or not Path(dfmea_path).exists():
        print(f"Error: FMEA files not found at {pfmea_path} and {dfmea_path}")
        return

    pfmea = load_pfmea_csv(pfmea_path)
    dfmea = load_dfmea_csv(dfmea_path)
    assessment = FMEAAssessment(pfmea, dfmea)

    # Select items to diagram
    if args.item_id:
        items = [i for i in assessment.all_items if i.item_id == args.item_id]
        if not items:
            print(f"Error: FMEA item '{args.item_id}' not found")
            return
    else:
        items = assessment.high_rpn_items(threshold=args.rpn_threshold)

    if not items:
        print(f"No FMEA items found with RPN >= {args.rpn_threshold}")
        return

    print(f"Generating fishbone diagrams for {len(items)} item(s):\n")
    print("=" * 70)

    for item in items:
        diagram = FishboneGenerator.from_fmea_item(item)
        print(f"\n{diagram.title}")
        print(f"Effect: {item.failure_mode}")
        if hasattr(item, "cause_list"):
            print(f"Causes: {', '.join(item.cause_list)}")
        print("\nMermaid code:")
        print("```mermaid")
        print(diagram.mermaid_code)
        print("```")
        print("=" * 70)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="navadmin-rag",
        description="Navadmin RAG pipeline CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_dq_args(p, submission_help="Submission JSON with the Canonical I-O Manifest (see dq-manifest)"):
        g = p.add_argument_group("ATHENA DQ gateway")
        g.add_argument("--submission", metavar="JSON", default=None, help=submission_help)
        g.add_argument("--tenant", default=None, help="Tenant ID (default: $ATHENA_TENANT_ID)")
        g.add_argument("--dq-registry", metavar="PATH", default=os.environ.get("ATHENA_DQ_REGISTRY", DEFAULT_DQ_REGISTRY),
                       help=f"Persistent DQ registry (default: {DEFAULT_DQ_REGISTRY})")
        g.add_argument("--dq-sandbox", action="store_true",
                       help="Also index HOLD artifacts whose origin is SYNTHETIC (tagged dq_sandbox in Qdrant)")
        g.add_argument("--actor", default=None, help="Submitting actor ID (default: rag-pipeline)")
        g.add_argument("--approve-as", metavar="REVIEWER", default=None,
                       help="Registered approver signing off a HOLD result (four-eyes: not the submitter)")

    # ingest
    ingest_parser = subparsers.add_parser(
        "ingest", help="Load documents into the vector index"
    )
    ingest_parser.add_argument("path", help="Path to a file or directory to ingest")
    add_dq_args(ingest_parser)
    ingest_parser.set_defaults(func=cmd_ingest)

    # ingest-telemetry
    ingest_tel_parser = subparsers.add_parser(
        "ingest-telemetry",
        help="Parse NexTest tester log files and index as structured failure events",
    )
    ingest_tel_parser.add_argument(
        "path", help="Path to a NexTest .log/.txt file or directory of log files"
    )
    add_dq_args(ingest_tel_parser)
    ingest_tel_parser.set_defaults(func=cmd_ingest_telemetry)

    # ingest-fea
    ingest_fea_parser = subparsers.add_parser(
        "ingest-fea",
        help="Parse FEA mesh JSON/CSV and index stress voxels",
    )
    ingest_fea_parser.add_argument(
        "path", help="Path to FEA mesh file (.json or .csv)"
    )
    add_dq_args(ingest_fea_parser)
    ingest_fea_parser.set_defaults(func=cmd_ingest_fea)

    # ingest-cad
    ingest_cad_parser = subparsers.add_parser(
        "ingest-cad",
        help="Parse STEP file or CAD JSON manifest and index components",
    )
    ingest_cad_parser.add_argument(
        "path", help="Path to STEP file (.stp/.step) or CAD JSON manifest (.json)"
    )
    add_dq_args(ingest_cad_parser)
    ingest_cad_parser.set_defaults(func=cmd_ingest_cad)

    # query
    query_parser = subparsers.add_parser(
        "query", help="Ask a question against the indexed documents"
    )
    query_parser.add_argument("question", help="Natural language question")
    query_parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Compute faithfulness, relevance, precision, and recall metrics",
    )
    query_parser.add_argument(
        "--ground-truth",
        metavar="TEXT",
        default=None,
        help="Reference answer for context recall scoring",
    )
    query_parser.add_argument(
        "--spatial-bbox",
        metavar="x_min,x_max,y_min,y_max,z_min,z_max",
        default=None,
        help="Spatial bounding box filter for FEA/CAD queries (6 comma-separated floats)",
    )
    query_parser.add_argument(
        "--telemetry-event",
        metavar="LOG_FILE:TRACK_ID",
        default=None,
        help="Load a specific failure event from a log file for RCA prompt (format: path:track_id)",
    )
    query_parser.set_defaults(func=cmd_query)

    # ingest-drive
    drive_parser = subparsers.add_parser(
        "ingest-drive",
        help="Ingest Aviator documents from Google Drive",
    )
    drive_parser.add_argument(
        "--credentials",
        metavar="PATH",
        default=None,
        help="Path to Google service-account JSON (defaults to GOOGLE_APPLICATION_CREDENTIALS env var)",
    )
    drive_parser.add_argument(
        "--local-mirror",
        metavar="DIR",
        default=None,
        help="Load from a local directory mirror instead of the live Drive API (e.g. data/aviator)",
    )
    add_dq_args(drive_parser)
    drive_parser.set_defaults(func=cmd_ingest_drive)

    # reset
    reset_parser = subparsers.add_parser(
        "reset", help="Clear the vector index"
    )
    reset_parser.set_defaults(func=cmd_reset)

    # twin-simulate
    twin_sim_parser = subparsers.add_parser(
        "twin-simulate",
        help="Run a Digital Twin batch simulation and print predicted yield",
    )
    twin_sim_parser.add_argument(
        "-n", type=int, default=50,
        help="Number of simulated assembly cycles (default: 50)",
    )
    twin_sim_parser.add_argument(
        "--with-rag", action="store_true",
        help="Connect the RAG pipeline for contextual RCA output",
    )
    twin_sim_parser.set_defaults(func=cmd_twin_simulate)

    # twin-dashboard
    twin_dash_parser = subparsers.add_parser(
        "twin-dashboard",
        help="Launch the Streamlit digital twin live dashboard",
    )
    twin_dash_parser.add_argument(
        "--with-rag", action="store_true",
        help="Enable RAG-powered RCA panel in the dashboard",
    )
    twin_dash_parser.set_defaults(func=cmd_twin_dashboard)

    # fmea-assess
    fmea_assess_parser = subparsers.add_parser(
        "fmea-assess",
        help="Assess PFMEA and DFMEA checklists and print high-risk items",
    )
    fmea_assess_parser.add_argument(
        "--pfmea", type=str, default=None,
        help="Path to PFMEA CSV (default: data/PFMEA_Aviator_Assembly.csv)",
    )
    fmea_assess_parser.add_argument(
        "--dfmea", type=str, default=None,
        help="Path to DFMEA CSV (default: data/DFMEA_Aviator_Design.csv)",
    )
    fmea_assess_parser.add_argument(
        "--rpn-threshold", type=int, default=100,
        help="RPN threshold for high-risk items (default: 100)",
    )
    fmea_assess_parser.set_defaults(func=cmd_fmea_assess)

    # fmea-check-twin
    fmea_check_parser = subparsers.add_parser(
        "fmea-check-twin",
        help="Run digital twin batch sim and show FMEA items triggered by predicted failures",
    )
    fmea_check_parser.add_argument(
        "-n", type=int, default=50,
        help="Number of simulated cycles (default: 50)",
    )
    fmea_check_parser.add_argument(
        "--pfmea", type=str, default=None,
        help="Path to PFMEA CSV",
    )
    fmea_check_parser.add_argument(
        "--dfmea", type=str, default=None,
        help="Path to DFMEA CSV",
    )
    fmea_check_parser.set_defaults(func=cmd_fmea_check_twin)

    # ipc-check
    ipc_parser = subparsers.add_parser(
        "ipc-check",
        help="Run IPC-A-610G Class 2 inspection against simulated assembly cycles",
    )
    ipc_parser.add_argument(
        "-n", type=int, default=50,
        help="Number of simulated cycles (default: 50)",
    )
    ipc_parser.set_defaults(func=cmd_ipc_check)

    # ingest-pdf
    ingest_pdf_parser = subparsers.add_parser(
        "ingest-pdf",
        help="Ingest a PDF file or directory with section and table detection",
    )
    ingest_pdf_parser.add_argument(
        "path", help="Path to a .pdf file or directory containing PDFs"
    )
    add_dq_args(ingest_pdf_parser)
    ingest_pdf_parser.set_defaults(func=cmd_ingest_pdf)

    # ingest-pdf-drive
    ingest_pdf_drive_parser = subparsers.add_parser(
        "ingest-pdf-drive",
        help="Sync and ingest PDFs from Google Drive Aviator folders (incremental)",
    )
    ingest_pdf_drive_parser.add_argument(
        "--credentials", metavar="PATH", default=None,
        help="Path to Google service-account JSON",
    )
    ingest_pdf_drive_parser.add_argument(
        "--local-mirror", metavar="DIR", default=None,
        help="Ingest from a local directory mirror instead of live Drive API",
    )
    ingest_pdf_drive_parser.add_argument(
        "--folder-id", metavar="ID", default=None,
        help="Sync a specific Drive folder ID (overrides Aviator defaults)",
    )
    ingest_pdf_drive_parser.add_argument(
        "--folders", metavar="root,smt,drawings", default=None,
        help="Comma-separated list of Aviator subfolder names to sync (default: all)",
    )
    ingest_pdf_drive_parser.add_argument(
        "--manifest", metavar="PATH", default="data/.drive_manifest.json",
        help="Path to the sync manifest JSON (default: data/.drive_manifest.json)",
    )
    add_dq_args(ingest_pdf_drive_parser, '{"by_file": {"<pdf name>": {"manifest": {...}}}} — one manifest per PDF')
    ingest_pdf_drive_parser.set_defaults(func=cmd_ingest_pdf_drive)

    # dq-manifest
    dq_manifest_parser = subparsers.add_parser(
        "dq-manifest",
        help="Generate a Canonical I-O Manifest (document_corpus.v1) over the bytes an ingest command reads",
    )
    dq_manifest_parser.add_argument("path", help="File or directory that will be ingested")
    dq_manifest_parser.add_argument("--kind", required=True,
                                    choices=["document", "telemetry_log", "fea_mesh", "cad", "pdf", "drive_mirror"],
                                    help="Which ingest command will read it (decides the hashed file set)")
    dq_manifest_parser.add_argument("--project", required=True)
    dq_manifest_parser.add_argument("--node", required=True, choices=["N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8", "X"])
    dq_manifest_parser.add_argument("--artifact-id", required=True)
    dq_manifest_parser.add_argument("--rev", required=True, help="Immutable revision label")
    dq_manifest_parser.add_argument("--rights", required=True,
                                    choices=["APPROVED", "APPROVED_NO_TRAIN", "UNVERIFIED", "PROHIBITED"])
    dq_manifest_parser.add_argument("--classification", required=True,
                                    choices=["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"])
    dq_manifest_parser.add_argument("--origin", required=True, choices=["REAL", "SYNTHETIC", "DERIVED"])
    dq_manifest_parser.add_argument("--tool", required=True, help="<kind>:<name>[@version], e.g. tool:nextest@4.2")
    dq_manifest_parser.add_argument("--evidence", required=True, help="Comma-separated evidence IDs (e.g. the NDA)")
    dq_manifest_parser.add_argument("--sources", default=None,
                                    help="Comma-separated upstream refs ID@REV (default: the project's G0 grant)")
    dq_manifest_parser.add_argument("--connector-mode", default="CLI",
                                    choices=["API", "SDK", "MCP", "CLI", "BATCH", "PORTAL", "MANUAL"])
    dq_manifest_parser.add_argument("--input-schema", default="raw_file.v1")
    dq_manifest_parser.add_argument("--dq-registry", metavar="PATH",
                                    default=os.environ.get("ATHENA_DQ_REGISTRY", DEFAULT_DQ_REGISTRY))
    dq_manifest_parser.add_argument("--out", metavar="JSON", default=None, help="Write the submission file here")
    dq_manifest_parser.set_defaults(func=cmd_dq_manifest)

    # dq-seed-demo
    dq_seed_parser = subparsers.add_parser(
        "dq-seed-demo", help="Seed the SYNTHETIC demo tenant into the DQ registry")
    dq_seed_parser.add_argument("--dq-registry", metavar="PATH",
                                default=os.environ.get("ATHENA_DQ_REGISTRY", DEFAULT_DQ_REGISTRY))
    dq_seed_parser.set_defaults(func=cmd_dq_seed_demo)

    # pdf-drive-status
    pdf_status_parser = subparsers.add_parser(
        "pdf-drive-status",
        help="Show sync status of PDFs already indexed from Google Drive",
    )
    pdf_status_parser.add_argument(
        "--manifest", metavar="PATH", default="data/.drive_manifest.json",
        help="Path to the sync manifest JSON (default: data/.drive_manifest.json)",
    )
    pdf_status_parser.set_defaults(func=cmd_pdf_drive_status)

    # fishbone-diagram
    # athena-run
    athena_parser = subparsers.add_parser(
        "athena-run",
        help="Execute Project Athena end-to-end production orchestration with RAG integration",
    )
    athena_parser.add_argument(
        "--deal-id", type=str, default=None,
        help="Pecan AI deal ID to forecast (optional; mock if unset)",
    )
    athena_parser.add_argument(
        "--json-out", type=str, default=None, metavar="PATH",
        help="Write full run JSON to this file",
    )
    athena_parser.set_defaults(func=cmd_athena_run)

    # fishbone-diagram
    fishbone_parser = subparsers.add_parser(
        "fishbone-diagram",
        help="Generate Mermaid fishbone (Ishikawa) diagrams for FMEA root cause analysis",
    )
    fishbone_parser.add_argument(
        "--item-id", type=str, default=None,
        help="Generate diagram for a specific FMEA item ID (e.g. PFMEA-S22-001)",
    )
    fishbone_parser.add_argument(
        "--rpn-threshold", type=int, default=100,
        help="RPN threshold for high-risk items to diagram (default: 100)",
    )
    fishbone_parser.add_argument(
        "--pfmea", type=str, default=None,
        help="Path to PFMEA CSV (default: data/PFMEA_Aviator_Assembly.csv)",
    )
    fishbone_parser.add_argument(
        "--dfmea", type=str, default=None,
        help="Path to DFMEA CSV (default: data/DFMEA_Aviator_Design.csv)",
    )
    fishbone_parser.set_defaults(func=cmd_fishbone_diagram)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
