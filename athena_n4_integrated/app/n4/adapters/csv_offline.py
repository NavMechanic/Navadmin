"""Offline CSV importer (manual bench / inspection measurements exported from a spreadsheet or tool).

Columns (header row, case-insensitive): ``test_id, characteristic, unit_serial, value, unit,
expanded_uncertainty, k, instrument_id, calibration_id, captured_at, method_id, method_revision,
operator``. A first line starting ``# ATHENA_MOCK_FIXTURE,`` (keep a comma so N0 still sniffs the
file as CSV) marks a mock fixture: it is imported as ``MOCK_FIXTURE`` and never counts as physical
evidence.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

REQUIRED = ("test_id", "characteristic", "unit_serial", "value", "unit", "expanded_uncertainty", "instrument_id",
            "calibration_id", "captured_at", "method_id", "method_revision")
MOCK_MARKER = "# ATHENA_MOCK_FIXTURE"


def parse(data: bytes) -> tuple[list[dict[str, Any]], bool, list[tuple[str, str]]]:
    text = data.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    mock = bool(lines) and lines[0].strip().upper().startswith(MOCK_MARKER)
    body = "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))
    reader = csv.DictReader(io.StringIO(body))
    cols = {(h or "").strip().lower(): h for h in reader.fieldnames or []}
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        return [], mock, [("CSV_COLUMNS_MISSING", f"missing columns {missing}")]
    rows, problems = [], []
    for i, row in enumerate(reader, 1):
        get = lambda k: (row.get(cols[k]) or "").strip() if k in cols else ""  # noqa: E731
        try:
            value, u = Decimal(get("value")), Decimal(get("expanded_uncertainty"))
            k = float(get("k") or 2)
            at = datetime.fromisoformat(get("captured_at").replace("Z", "+00:00"))
        except (InvalidOperation, ValueError) as exc:
            problems.append(("CSV_ROW_INVALID", f"row {i}: {exc}"))
            continue
        if at.tzinfo is None:
            problems.append(("TIMESTAMP_NOT_UTC", f"row {i}: captured_at has no time zone"))
            continue
        rows.append({"test_id": get("test_id"), "characteristic": get("characteristic"),
                     "unit_serial": get("unit_serial"), "value": value, "unit": get("unit"),
                     "expanded_uncertainty": u, "k": k, "instrument_id": get("instrument_id"),
                     "calibration_id": get("calibration_id"), "captured_at": at,
                     "method_ref": {"method_id": get("method_id"), "revision": get("method_revision")}
                     if get("method_id") else None, "operator": get("operator") or None})
    return rows, mock, problems
