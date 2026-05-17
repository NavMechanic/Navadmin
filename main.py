"""CLI entry point for the Navadmin RAG pipeline."""

import argparse
import sys


def cmd_ingest(args) -> None:
    from rag.pipeline import RAGPipeline

    pipeline = RAGPipeline()
    count = pipeline.ingest(args.path)
    print(f"Ingested {count} chunk(s) from: {args.path}")


def cmd_ingest_telemetry(args) -> None:
    """Parse NexTest tester log files and index as structured failure events."""
    from rag.pipeline import RAGPipeline

    pipeline = RAGPipeline()
    count = pipeline.ingest_telemetry(args.path)
    print(f"Ingested {count} telemetry chunk(s) from: {args.path}")


def cmd_ingest_fea(args) -> None:
    """Parse FEA mesh JSON/CSV and index stress voxels."""
    from rag.pipeline import RAGPipeline

    pipeline = RAGPipeline()
    count = pipeline.ingest_fea(args.path)
    print(f"Ingested {count} FEA voxel chunk(s) from: {args.path}")


def cmd_ingest_cad(args) -> None:
    """Parse STEP file or CAD JSON manifest and index components."""
    from rag.pipeline import RAGPipeline

    pipeline = RAGPipeline()
    count = pipeline.ingest_cad(args.path)
    print(f"Ingested {count} CAD component chunk(s) from: {args.path}")


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


def cmd_ingest_drive(args) -> None:
    """Ingest documents from the Aviator Google Drive folders."""
    from rag.ingestion.drive_loader import AviatorDriveLoader
    from rag.pipeline import RAGPipeline
    from rag.chunking.strategies import ChunkingStrategy

    pipeline = RAGPipeline()
    loader = AviatorDriveLoader(credentials_path=args.credentials)

    if args.local_mirror:
        docs = loader.load_local_aviator_mirror(args.local_mirror)
    else:
        docs = loader.load_all_aviator()

    print(f"Loaded {len(docs)} document(s) from Drive.")
    if not docs:
        print("No documents found — nothing to ingest.")
        return

    embedder = pipeline._embedder if pipeline.config.chunking.strategy == "semantic" else None
    chunker = ChunkingStrategy(pipeline.config.chunking)
    chunks = chunker.chunk(docs, embedder=embedder)
    print(f"Created {len(chunks)} chunk(s). Embedding and storing …")
    pipeline._vector_store.add_chunks(chunks)
    pipeline._retriever.index_for_bm25(chunks)
    print(f"Done. {len(chunks)} chunk(s) indexed from Aviator Drive data.")


def cmd_reset(args) -> None:
    from rag.pipeline import RAGPipeline

    pipeline = RAGPipeline()
    pipeline.reset_index()
    print("Vector index has been reset.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="navadmin-rag",
        description="Navadmin RAG pipeline CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ingest
    ingest_parser = subparsers.add_parser(
        "ingest", help="Load documents into the vector index"
    )
    ingest_parser.add_argument("path", help="Path to a file or directory to ingest")
    ingest_parser.set_defaults(func=cmd_ingest)

    # ingest-telemetry
    ingest_tel_parser = subparsers.add_parser(
        "ingest-telemetry",
        help="Parse NexTest tester log files and index as structured failure events",
    )
    ingest_tel_parser.add_argument(
        "path", help="Path to a NexTest .log/.txt file or directory of log files"
    )
    ingest_tel_parser.set_defaults(func=cmd_ingest_telemetry)

    # ingest-fea
    ingest_fea_parser = subparsers.add_parser(
        "ingest-fea",
        help="Parse FEA mesh JSON/CSV and index stress voxels",
    )
    ingest_fea_parser.add_argument(
        "path", help="Path to FEA mesh file (.json or .csv)"
    )
    ingest_fea_parser.set_defaults(func=cmd_ingest_fea)

    # ingest-cad
    ingest_cad_parser = subparsers.add_parser(
        "ingest-cad",
        help="Parse STEP file or CAD JSON manifest and index components",
    )
    ingest_cad_parser.add_argument(
        "path", help="Path to STEP file (.stp/.step) or CAD JSON manifest (.json)"
    )
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
    drive_parser.set_defaults(func=cmd_ingest_drive)

    # reset
    reset_parser = subparsers.add_parser(
        "reset", help="Clear the vector index"
    )
    reset_parser.set_defaults(func=cmd_reset)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
