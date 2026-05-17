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
