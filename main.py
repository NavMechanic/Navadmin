"""CLI entry point for the Navadmin RAG pipeline."""

import argparse
import sys


def cmd_ingest(args) -> None:
    from rag.pipeline import RAGPipeline

    pipeline = RAGPipeline()
    count = pipeline.ingest(args.path)
    print(f"Ingested {count} chunk(s) from: {args.path}")


def cmd_query(args) -> None:
    from rag.pipeline import RAGPipeline

    pipeline = RAGPipeline()
    result = pipeline.query(
        question=args.question,
        evaluate=args.evaluate,
        ground_truth=args.ground_truth,
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
