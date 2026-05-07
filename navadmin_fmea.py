#!/usr/bin/env python3
"""
navadmin_fmea.py — Navadmin FMEA & Mermaid Fishbone Tool

Usage:
    python navadmin_fmea.py list
    python navadmin_fmea.py run <template>  [--out report.md]
    python navadmin_fmea.py fishbone <template>
    python navadmin_fmea.py fmea <template>

Examples:
    python navadmin_fmea.py list
    python navadmin_fmea.py run solder_joint --out solder_report.md
    python navadmin_fmea.py fishbone connector_contact
"""

import argparse
import sys
from pathlib import Path

from generators import fishbone_mermaid, fmea_markdown, full_report
from templates import TEMPLATES


def cmd_list(_args: argparse.Namespace) -> None:
    print("\nAvailable FMEA templates:\n")
    for name, analysis in TEMPLATES.items():
        top_rpn = max(e.rpn for e in analysis.fmea_entries)
        print(f"  {name:<22} — {analysis.title}")
        print(f"  {'':22}   System : {analysis.system}")
        print(f"  {'':22}   Entries: {len(analysis.fmea_entries)}  |  "
              f"Causes: {len(analysis.causes)}  |  Highest RPN: {top_rpn}")
        print()


def _get_template(name: str):
    if name not in TEMPLATES:
        print(f"Error: unknown template '{name}'. Run 'list' to see available templates.",
              file=sys.stderr)
        sys.exit(1)
    return TEMPLATES[name]


def cmd_run(args: argparse.Namespace) -> None:
    analysis = _get_template(args.template)
    report = full_report(analysis)
    if args.out:
        path = Path(args.out)
        path.write_text(report, encoding="utf-8")
        print(f"Report written to {path}")
    else:
        print(report)


def cmd_fishbone(args: argparse.Namespace) -> None:
    analysis = _get_template(args.template)
    diagram = fishbone_mermaid(analysis)
    print("```mermaid")
    print(diagram)
    print("```")


def cmd_fmea(args: argparse.Namespace) -> None:
    analysis = _get_template(args.template)
    print(fmea_markdown(analysis))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Navadmin FMEA & Mermaid Fishbone Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all built-in failure-mode templates")

    run_p = sub.add_parser("run", help="Generate full report (FMEA + fishbone)")
    run_p.add_argument("template", help="Template name (see 'list')")
    run_p.add_argument("--out", metavar="FILE", help="Write output to FILE instead of stdout")

    fish_p = sub.add_parser("fishbone", help="Output only the Mermaid fishbone diagram")
    fish_p.add_argument("template", help="Template name (see 'list')")

    fmea_p = sub.add_parser("fmea", help="Output only the FMEA Markdown table")
    fmea_p.add_argument("template", help="Template name (see 'list')")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "list": cmd_list,
        "run": cmd_run,
        "fishbone": cmd_fishbone,
        "fmea": cmd_fmea,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
