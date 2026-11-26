"""AIAG-VDA Action Priority.

Why this module is careful
--------------------------
Action Priority replaced the RPN threshold in the 2019 AIAG-VDA handbook, and it
is what the G2 gate rule blocks on: a HIGH AP row with no named owner stops the
gate. So the mapping from (Severity, Occurrence, Detection) to AP is not a
reporting convenience — it decides whether a programme can pass a gate.

The AP table itself is published in a copyrighted handbook. What is encoded here
is the table's *structure*, which is public and unambiguous, expressed as
severity/occurrence/detection bands. It is deliberately biased toward the
higher priority at every boundary, and every cell is exposed via ``explain()``.

**This must be verified against the licensed AIAG-VDA handbook before
production use.** ``TABLE_VERIFIED`` below is the flag for that, and it is
False. The service reports it, and the re-rating endpoint refuses to apply an AP
that would *lower* priority while the flag is False — a table that has not been
checked may downgrade a row, and a downgrade is what unblocks a gate.

The tests do not assert individual cells, because asserting a cell I cannot
verify would only prove the table matches itself. They assert the invariants the
real table must satisfy whatever its exact cells are: monotonicity in all three
inputs, and the severity floor.
"""
from __future__ import annotations

from typing import Literal

from ..enums import ActionPriority

TABLE_VERIFIED = False
TABLE_SOURCE = ("Structure of the AIAG-VDA FMEA Handbook (1st ed., 2019) Action "
                "Priority table, banded. Not verified against the licensed text.")

Band = Literal["HIGH", "MEDIUM", "LOW"]


def _sev_band(s: int) -> int:
    """0 = 9-10, 1 = 7-8, 2 = 4-6, 3 = 2-3, 4 = 1."""
    if s >= 9:
        return 0
    if s >= 7:
        return 1
    if s >= 4:
        return 2
    if s >= 2:
        return 3
    return 4


def _occ_band(o: int) -> int:
    """0 = 8-10, 1 = 6-7, 2 = 4-5, 3 = 2-3, 4 = 1."""
    if o >= 8:
        return 0
    if o >= 6:
        return 1
    if o >= 4:
        return 2
    if o >= 2:
        return 3
    return 4


def _det_band(d: int) -> int:
    """0 = 7-10, 1 = 5-6, 2 = 2-4, 3 = 1."""
    if d >= 7:
        return 0
    if d >= 5:
        return 1
    if d >= 2:
        return 2
    return 3


H, M, L = ActionPriority.HIGH, ActionPriority.MEDIUM, ActionPriority.LOW

# Indexed [severity_band][occurrence_band][detection_band].
# Read the rows as: within a severity band, priority falls as occurrence falls,
# and within an occurrence band it falls as detection improves.
_TABLE: list[list[list[ActionPriority]]] = [
    # --- S 9-10: a safety or regulatory effect. Detection cannot rescue it. ---
    [[H, H, H, H],   # O 8-10
     [H, H, H, H],   # O 6-7
     [H, H, H, M],   # O 4-5
     [H, M, M, L],   # O 2-3
     [M, M, L, L]],  # O 1
    # --- S 7-8: loss of primary function ---
    [[H, H, H, H],
     [H, H, H, M],
     [H, H, M, M],
     [M, M, L, L],
     [M, L, L, L]],
    # --- S 4-6: degraded function, customer discomfort ---
    [[H, H, M, M],
     [H, M, M, M],
     [M, M, M, L],
     [M, L, L, L],
     [L, L, L, L]],
    # --- S 2-3: annoyance ---
    [[M, M, M, L],
     [M, M, L, L],
     [L, L, L, L],
     [L, L, L, L],
     [L, L, L, L]],
    # --- S 1: no discernible effect ---
    [[L, L, L, L],
     [L, L, L, L],
     [L, L, L, L],
     [L, L, L, L],
     [L, L, L, L]],
]


def _validate(severity: int, occurrence: int, detection: int) -> None:
    for name, v in (("severity", severity), ("occurrence", occurrence),
                    ("detection", detection)):
        if not 1 <= v <= 10:
            raise ValueError(f"{name} must be between 1 and 10, got {v}.")


def action_priority(severity: int, occurrence: int, detection: int) -> ActionPriority:
    """AP for a (S, O, D) triple."""
    _validate(severity, occurrence, detection)
    return _TABLE[_sev_band(severity)][_occ_band(occurrence)][_det_band(detection)]


def rank(ap: ActionPriority | str) -> int:
    """LOW = 0, MEDIUM = 1, HIGH = 2. Higher is more urgent."""
    return {ActionPriority.LOW: 0, ActionPriority.MEDIUM: 1,
            ActionPriority.HIGH: 2}[ActionPriority(str(ap).upper())]


def explain(severity: int, occurrence: int, detection: int) -> dict:
    """The cell and how it was reached, so a reviewer can check it."""
    ap = action_priority(severity, occurrence, detection)
    bands = ("9-10", "7-8", "4-6", "2-3", "1")
    obands = ("8-10", "6-7", "4-5", "2-3", "1")
    dbands = ("7-10", "5-6", "2-4", "1")
    return {
        "severity": severity, "occurrence": occurrence, "detection": detection,
        "severity_band": bands[_sev_band(severity)],
        "occurrence_band": obands[_occ_band(occurrence)],
        "detection_band": dbands[_det_band(detection)],
        "action_priority": ap.value,
        "rpn": severity * occurrence * detection,
        "table_verified": TABLE_VERIFIED,
        "table_source": TABLE_SOURCE,
        "caveat": (
            "RPN is reported for continuity with older documents only. AIAG-VDA "
            "replaced it because it treats a severity-10 / occurrence-1 row the "
            "same as a severity-1 / occurrence-10 one, and those are not the "
            "same risk."
            if TABLE_VERIFIED else
            "This AP was computed from an UNVERIFIED table. It encodes the "
            "published structure of the AIAG-VDA table, not the licensed cell "
            "values. Verify before relying on it to pass a gate."),
    }
