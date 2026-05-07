"""
Generators for Mermaid fishbone diagrams and Markdown FMEA tables.
"""

from typing import Dict, List
from models import FMEAAnalysis, FMEAEntry, IshikawaCategory


_RPN_SCALE = [
    ("200–1000", "CRITICAL", "Immediate corrective action required"),
    ("125–199",  "HIGH",     "Corrective action required"),
    ("50–124",   "MEDIUM",   "Monitor and schedule action"),
    ("1–49",     "LOW",      "Acceptable risk; document and re-evaluate"),
]

_SOD_SCALE = {
    "Severity (S)": [
        (10, "Catastrophic — mission failure / safety hazard"),
        (8,  "Major — significant degradation of function"),
        (6,  "Moderate — reduced performance, workaround possible"),
        (4,  "Minor — slight degradation, not noticed by operator"),
        (2,  "Negligible — imperceptible effect"),
        (1,  "None"),
    ],
    "Occurrence (O)": [
        (10, "Almost certain — failure expected in most conditions"),
        (8,  "High — repeated failures"),
        (6,  "Moderate — occasional failures"),
        (4,  "Low — relatively few failures"),
        (2,  "Remote — failure unlikely"),
        (1,  "Almost impossible"),
    ],
    "Detection (D)": [
        (10, "Absolute uncertainty — cannot detect or is not checked"),
        (8,  "Very remote — control is unlikely to detect"),
        (6,  "Low — control may detect"),
        (4,  "Moderate — control is likely to detect"),
        (2,  "High — control almost certain to detect"),
        (1,  "Almost certain — control will detect"),
    ],
}


def fishbone_mermaid(analysis: FMEAAnalysis) -> str:
    """Return a Mermaid fishbone diagram string."""
    by_cat: Dict[str, List[str]] = {c.value: [] for c in IshikawaCategory}
    sub_map: Dict[str, List[str]] = {}

    for cause in analysis.causes:
        key = cause.description
        by_cat[cause.category.value].append(key)
        if cause.sub_causes:
            sub_map[key] = cause.sub_causes

    lines = [
        '%%{init: {"theme": "base"}}%%',
        "fishbone",
        f"    title {analysis.problem_statement}",
    ]

    for category, cause_list in by_cat.items():
        if not cause_list:
            continue
        lines.append(f"    section {category}")
        for cause_desc in cause_list:
            lines.append(f"        {cause_desc}")
            for sub in sub_map.get(cause_desc, []):
                lines.append(f"            {sub}")

    return "\n".join(lines)


def fmea_markdown(analysis: FMEAAnalysis) -> str:
    """Return a full FMEA report in GitHub-flavored Markdown."""
    header = [
        f"# FMEA Report: {analysis.title}",
        "",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| System | {analysis.system} |",
        f"| Problem Statement | {analysis.problem_statement} |",
    ]
    if analysis.date:
        header.append(f"| Date | {analysis.date} |")
    if analysis.engineer:
        header.append(f"| Engineer | {analysis.engineer} |")
    header.append("")

    sorted_entries: List[FMEAEntry] = sorted(
        analysis.fmea_entries, key=lambda e: e.rpn, reverse=True
    )

    table = [
        "## Failure Mode and Effects Analysis",
        "",
        "| # | Item | Failure Mode | Effect | S | Cause | O | Controls | D | RPN | Risk |",
        "|---|------|--------------|--------|---|-------|---|----------|---|-----|------|",
    ]
    for i, e in enumerate(sorted_entries, 1):
        table.append(
            f"| {i} | {e.item} | {e.failure_mode} | {e.effect} | "
            f"{e.severity} | {e.cause} | {e.occurrence} | "
            f"{e.controls} | {e.detection} | **{e.rpn}** | **{e.risk_level}** |"
        )

    actions = [
        "",
        "## Recommended Actions (sorted by RPN)",
        "",
        "| # | Item | Failure Mode | RPN | Recommended Action |",
        "|---|------|--------------|-----|--------------------|",
    ]
    for i, e in enumerate(sorted_entries, 1):
        actions.append(
            f"| {i} | {e.item} | {e.failure_mode} | {e.rpn} | {e.recommended_action} |"
        )

    pof_entries = [e for e in sorted_entries if e.physics_of_failure]
    pof_section: List[str] = []
    if pof_entries:
        pof_section = [
            "",
            "## Physics-of-Failure (PoF) Notes",
            "",
        ]
        for e in pof_entries:
            pof_section.append(f"**{e.item} — {e.failure_mode}**")
            pof_section.append(f"> {e.physics_of_failure}")
            pof_section.append("")

    scales = [
        "",
        "## Scoring Reference",
        "",
        "### RPN Risk Bands",
        "",
        "| RPN Range | Risk Level | Required Response |",
        "|-----------|------------|-------------------|",
    ]
    for rng, lvl, resp in _RPN_SCALE:
        scales.append(f"| {rng} | **{lvl}** | {resp} |")

    for scale_name, ratings in _SOD_SCALE.items():
        scales += [
            "",
            f"### {scale_name}",
            "",
            "| Score | Description |",
            "|-------|-------------|",
        ]
        for score, desc in ratings:
            scales.append(f"| {score} | {desc} |")

    return "\n".join(header + table + actions + pof_section + scales)


def full_report(analysis: FMEAAnalysis) -> str:
    """Return the complete Markdown report with embedded Mermaid diagram."""
    diagram = fishbone_mermaid(analysis)
    fmea = fmea_markdown(analysis)

    cause_pof = [
        f"- **{c.description}** ({c.category.value}): {c.physics_of_failure}"
        for c in analysis.causes
        if c.physics_of_failure
    ]

    sections = [fmea, "", "---", "", "## Ishikawa (Fishbone) Diagram", "",
                "```mermaid", diagram, "```"]

    if cause_pof:
        sections += ["", "## Root-Cause Physics-of-Failure Notes", ""] + cause_pof

    return "\n".join(sections)
