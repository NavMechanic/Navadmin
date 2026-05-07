# Navadmin — FMEA & Mermaid Fishbone Tool

A Python CLI that generates **Mermaid.js Ishikawa (fishbone) diagrams** and
**FMEA tables with RPN scoring** for naval electronics failure analysis.
Physics-of-Failure (PoF) mechanics are embedded in every template so engineers
get root-cause language (IMC kinetics, Coffin-Manson fatigue, Holm contact
model, etc.) rather than generic advice.

---

## Quick Start

```bash
# List available templates
python navadmin_fmea.py list

# Generate a full report (FMEA table + Mermaid fishbone) to stdout
python navadmin_fmea.py run solder_joint

# Write the report to a Markdown file
python navadmin_fmea.py run solder_joint --out solder_report.md

# Print only the Mermaid fishbone block
python navadmin_fmea.py fishbone connector_contact

# Print only the FMEA table
python navadmin_fmea.py fmea solder_joint
```

No external dependencies — standard library only.

---

## Built-in Templates

| Template | System | Failure Mode |
|----------|--------|--------------|
| `solder_joint` | PCB Assembly (SMT) | Solder joint reliability — BGA, QFP, 0402, CAF, tin whisker |
| `connector_contact` | MIL-DTL-38999 circular connector | Contact resistance, crimp pull-out, moisture ingress |

---

## Output Structure

### 1 — Mermaid Fishbone Diagram

Rendered inline in any Markdown editor (GitHub, VS Code, Claude.ai):

```mermaid
%%{init: {"theme": "base"}}%%
fishbone
    title Solder Joint Failure
    section Material
        Wrong solder alloy
            SAC305 substituted with Sn63Pb37 in lead-free design
        Expired or mis-stored paste
    section Method
        Incorrect reflow profile
            Time-above-liquidus (TAL) < 45 s
    section Machine
        Oven temperature non-uniformity
    section Man
        ESD / mechanical damage during handling
    section Measurement
        Inadequate AOI programming
    section Environment
        High relative humidity in production area
        Ship-board vibration during operations
```

### 2 — FMEA Table

Sorted by RPN (highest risk first):

| # | Item | Failure Mode | S | O | D | RPN | Risk |
|---|------|--------------|---|---|---|-----|------|
| 1 | BGA Solder Ball | Open circuit — thermal fatigue crack | 9 | 5 | 4 | **180** | **HIGH** |
| 2 | PCB Pad (BGA corner) | Pad cratering — laminate fracture | 10 | 3 | 6 | **180** | **HIGH** |

### 3 — Physics-of-Failure Notes

Each entry includes a PoF explanation grounded in failure mechanics:

> **BGA Solder Ball — thermal fatigue crack**
> Nf ∝ (ΔεT / 2)^−c per Coffin-Manson. Each 10 °C reduction in ΔT roughly
> doubles fatigue life. Underfill reduces solder strain energy density by
> transferring load to the epoxy.

---

## RPN Risk Bands

| RPN | Risk | Response |
|-----|------|----------|
| 200–1000 | CRITICAL | Immediate corrective action |
| 125–199 | HIGH | Corrective action required |
| 50–124 | MEDIUM | Monitor and schedule action |
| 1–49 | LOW | Acceptable; document and review |

---

## File Layout

```
navadmin_fmea.py   — CLI entry point (list / run / fishbone / fmea)
models.py          — Cause, FMEAEntry, FMEAAnalysis dataclasses
generators.py      — Mermaid and Markdown output generators
templates.py       — Built-in PoF-annotated failure-mode templates
```

---

## Adding Custom Templates

Import the dataclasses from `models.py`, build an `FMEAAnalysis`, and register
it in the `TEMPLATES` dict in `templates.py`:

```python
from models import Cause, FMEAAnalysis, FMEAEntry, IshikawaCategory

MY_ANALYSIS = FMEAAnalysis(
    title="My Failure",
    system="My System",
    problem_statement="Root Problem",
    causes=[
        Cause(
            description="Root cause",
            category=IshikawaCategory.MATERIAL,
            sub_causes=["Sub-cause A"],
            physics_of_failure="Mechanism description...",
        ),
    ],
    fmea_entries=[
        FMEAEntry(
            item="Component",
            failure_mode="Failure mode",
            effect="Effect on system",
            severity=8,
            cause="Root cause",
            occurrence=4,
            controls="Existing controls",
            detection=4,
            recommended_action="Corrective action",
            physics_of_failure="PoF details...",
        ),
    ],
)
```
