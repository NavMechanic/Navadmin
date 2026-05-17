"""Pipeline A: NexTest 2010 tester log parser for structured failure events."""

import csv
import os
import re
import warnings
from dataclasses import dataclass, field
from typing import Optional

from rag.ingestion.loaders import Document


@dataclass
class TelemetryEvent:
    track_id: str           # e.g. "N4AI420127"
    timestamp: str
    process: str            # e.g. "L2AR", "DEPTHVAL", "UCT"
    station: str            # e.g. "BE12-L2_AR_075"
    test_code: str          # e.g. "QC_LTE_BC01_TX_POWER_MCH_18300_ANT_CW"
    test_code_short: str    # e.g. "QL01T0M1"
    result: str             # "P" or "F"
    test_value: Optional[float]
    lower_limit: Optional[float]
    upper_limit: Optional[float]
    delta: Optional[float]  # computed: test_value - nearest limit (negative = deficit)
    fail_desc: str
    ntf: bool               # No-Trouble-Found flag
    communication_state: Optional[str]  # "TOTAL_COMMUNICATION_DISCONNECT" if hex=00000000
    metadata: dict = field(default_factory=dict)


def _try_float(val: str) -> Optional[float]:
    """Parse a string to float, returning None if not possible."""
    if val is None:
        return None
    val = val.strip()
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


class TelemetryParser:
    """Parse NexTest 2010 log files into structured TelemetryEvent objects."""

    HEX_DISCONNECT_PATTERN = re.compile(r'0{6,}')

    # Column indices for the NexTest CSV log format
    # Datetime, Track Id, NTF?, Technology, Family, Model, Process,
    # Station, Fixture, Testcode, Testcode Desc, P/F, Fail Desc,
    # Test Val, LL, UL, [retry columns...]
    COL_DATETIME = 0
    COL_TRACK_ID = 1
    COL_NTF = 2
    COL_TECHNOLOGY = 3
    COL_FAMILY = 4
    COL_MODEL = 5
    COL_PROCESS = 6
    COL_STATION = 7
    COL_FIXTURE = 8
    COL_TESTCODE = 9
    COL_TESTCODE_DESC = 10
    COL_PF = 11
    COL_FAIL_DESC = 12
    COL_TEST_VAL = 13
    COL_LL = 14
    COL_UL = 15

    def parse_log_file(self, path: str) -> list[TelemetryEvent]:
        """Parse a NexTest log file (CSV-style) into TelemetryEvent objects.

        Only FAIL rows (P/F == "F") are returned.

        Args:
            path: Path to the .log or .txt file.

        Returns:
            List of TelemetryEvent objects for each failure row.
        """
        events: list[TelemetryEvent] = []

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError as exc:
            warnings.warn(f"TelemetryParser: cannot open {path}: {exc}")
            return events

        # Try CSV parse first; fall back to line-by-line for summary files
        lines = content.splitlines()

        # Detect if this is a NexTest CSV log or a human-readable summary
        # CSV logs have many comma-separated columns per row
        csv_lines = [l for l in lines if l.count(",") >= 10]

        if csv_lines:
            events = self._parse_csv_lines(csv_lines, path)
        else:
            # Fall back: parse the human-readable build_summary format
            events = self._parse_summary_text(lines, path)

        return events

    def _parse_csv_lines(self, lines: list[str], source_path: str) -> list[TelemetryEvent]:
        """Parse proper CSV log lines."""
        events: list[TelemetryEvent] = []
        reader = csv.reader(lines)

        for row_num, row in enumerate(reader, start=1):
            # Skip header rows (first row, or rows where PF column is "P/F" or "Result")
            if len(row) <= self.COL_UL:
                continue
            pf_val = row[self.COL_PF].strip().upper()
            if pf_val in ("P/F", "RESULT", "STATUS", ""):
                continue
            if pf_val != "F":
                continue  # Only parse failures

            try:
                event = self._row_to_event(row, source_path)
                if event:
                    events.append(event)
            except Exception as exc:
                warnings.warn(f"TelemetryParser: skipping row {row_num} in {source_path}: {exc}")

        return events

    def _row_to_event(self, row: list[str], source_path: str) -> Optional[TelemetryEvent]:
        """Convert a CSV row to a TelemetryEvent."""
        def get(idx: int) -> str:
            return row[idx].strip() if idx < len(row) else ""

        timestamp = get(self.COL_DATETIME)
        track_id = get(self.COL_TRACK_ID)
        ntf_raw = get(self.COL_NTF).upper()
        ntf = ntf_raw in ("Y", "YES", "TRUE", "1", "NTF")
        process = get(self.COL_PROCESS)
        station = get(self.COL_STATION)
        test_code = get(self.COL_TESTCODE)
        test_code_short = get(self.COL_TESTCODE_DESC)
        fail_desc = get(self.COL_FAIL_DESC)

        test_value = _try_float(get(self.COL_TEST_VAL))
        lower_limit = _try_float(get(self.COL_LL))
        upper_limit = _try_float(get(self.COL_UL))

        # Compute delta
        delta = self._compute_delta(test_value, lower_limit, upper_limit)

        # Detect communication disconnect
        communication_state = self._detect_communication_state(fail_desc, get(self.COL_TEST_VAL))

        metadata = {
            "source": source_path,
            "technology": get(self.COL_TECHNOLOGY),
            "family": get(self.COL_FAMILY),
            "model": get(self.COL_MODEL),
            "fixture": get(self.COL_FIXTURE),
            "data_type": "telemetry_failure",
        }

        return TelemetryEvent(
            track_id=track_id,
            timestamp=timestamp,
            process=process,
            station=station,
            test_code=test_code,
            test_code_short=test_code_short,
            result="F",
            test_value=test_value,
            lower_limit=lower_limit,
            upper_limit=upper_limit,
            delta=delta,
            fail_desc=fail_desc,
            ntf=ntf,
            communication_state=communication_state,
            metadata=metadata,
        )

    def _parse_summary_text(self, lines: list[str], source_path: str) -> list[TelemetryEvent]:
        """Parse the human-readable build_summary.txt format."""
        events: list[TelemetryEvent] = []

        # Extract header metadata
        technology = ""
        family = ""
        model = ""
        current_process = ""

        # Regex patterns for summary parsing
        section_re = re.compile(r"^=== (\w+) FAILURES ===")
        failure_re = re.compile(
            r"^\d+\.\s+(\S+)\s*(?:\((\w+)\))?:?\s*(.*)"
        )
        val_re = re.compile(
            r"[Vv]al[ues]?\s*[=:]?\s*([-\d.]+).*?(?:limit|vs)\s*\[?([-\d.]+),\s*([-\d.]+)\]?"
        )
        ntf_re = re.compile(r"\bNTF\b", re.IGNORECASE)

        for line in lines:
            line = line.strip()

            # Pick up header info
            if line.startswith("Technology:"):
                parts = line.split("|")
                for p in parts:
                    p = p.strip()
                    if p.startswith("Technology:"):
                        technology = p.split(":", 1)[1].strip()
                    elif p.startswith("Family:"):
                        family = p.split(":", 1)[1].strip()
            if "MODEL" in line.upper():
                m = re.search(r"MODEL\s+(\S+)", line, re.IGNORECASE)
                if m:
                    model = m.group(1)

            # Detect process section
            sec_m = section_re.match(line)
            if sec_m:
                current_process = sec_m.group(1)
                continue

            # Parse failure entries
            if not current_process:
                continue
            fail_m = failure_re.match(line)
            if not fail_m:
                continue

            test_code = fail_m.group(1)
            test_code_short = fail_m.group(2) or ""
            description = fail_m.group(3)

            # Extract values and limits
            test_value = None
            lower_limit = None
            upper_limit = None

            val_m = val_re.search(description)
            if val_m:
                test_value = _try_float(val_m.group(1))
                lower_limit = _try_float(val_m.group(2))
                upper_limit = _try_float(val_m.group(3))

            # Also try "Val X vs UL Y" pattern
            if test_value is None:
                m2 = re.search(r"Val\s*([-\d.]+)\s*vs\s*UL\s*([-\d.]+)", description)
                if m2:
                    test_value = _try_float(m2.group(1))
                    upper_limit = _try_float(m2.group(2))

            delta = self._compute_delta(test_value, lower_limit, upper_limit)
            ntf = bool(ntf_re.search(description))
            communication_state = self._detect_communication_state(description, "")

            metadata = {
                "source": source_path,
                "technology": technology,
                "family": family,
                "model": model,
                "fixture": "",
                "data_type": "telemetry_failure",
            }

            events.append(TelemetryEvent(
                track_id="UNKNOWN",
                timestamp="",
                process=current_process,
                station="",
                test_code=test_code,
                test_code_short=test_code_short,
                result="F",
                test_value=test_value,
                lower_limit=lower_limit,
                upper_limit=upper_limit,
                delta=delta,
                fail_desc=description,
                ntf=ntf,
                communication_state=communication_state,
                metadata=metadata,
            ))

        return events

    def _compute_delta(
        self,
        test_value: Optional[float],
        lower_limit: Optional[float],
        upper_limit: Optional[float],
    ) -> Optional[float]:
        """Compute delta: negative means deficit below LL, positive means above UL."""
        if test_value is None:
            return None
        if lower_limit is not None and test_value < lower_limit:
            return test_value - lower_limit  # negative
        if upper_limit is not None and test_value > upper_limit:
            return test_value - upper_limit  # positive
        return 0.0

    def _detect_communication_state(self, fail_desc: str, test_val_raw: str) -> Optional[str]:
        """Detect total communication disconnect from hex 00000000 pattern."""
        combined = (fail_desc or "") + " " + (test_val_raw or "")
        if self.HEX_DISCONNECT_PATTERN.search(combined):
            return "TOTAL_COMMUNICATION_DISCONNECT"
        return None

    def parse_directory(self, directory: str) -> list[TelemetryEvent]:
        """Walk directory and parse all .log and .txt files.

        Args:
            directory: Path to directory containing NexTest log files.

        Returns:
            Combined list of TelemetryEvent objects from all files.
        """
        all_events: list[TelemetryEvent] = []

        for root, _dirs, files in os.walk(directory):
            for filename in sorted(files):
                if filename.endswith((".log", ".txt")):
                    filepath = os.path.join(root, filename)
                    events = self.parse_log_file(filepath)
                    all_events.extend(events)

        return all_events

    def events_to_documents(self, events: list[TelemetryEvent]) -> list[Document]:
        """Convert TelemetryEvents into Documents with rich metadata for indexing.

        Args:
            events: List of parsed failure events.

        Returns:
            List of Documents with human-readable content and structured metadata.
        """
        documents: list[Document] = []

        for event in events:
            # Build human-readable content
            lines = [
                f"MANUFACTURING FAILURE RECORD",
                f"Track ID: {event.track_id}",
                f"Timestamp: {event.timestamp}" if event.timestamp else "",
                f"Process: {event.process}",
                f"Station: {event.station}" if event.station else "",
                f"Test Code: {event.test_code}",
                f"Short Code: {event.test_code_short}" if event.test_code_short else "",
                f"Result: FAIL",
            ]

            if event.test_value is not None:
                lines.append(f"Test Value: {event.test_value}")
            if event.lower_limit is not None or event.upper_limit is not None:
                ll_str = str(event.lower_limit) if event.lower_limit is not None else "N/A"
                ul_str = str(event.upper_limit) if event.upper_limit is not None else "N/A"
                lines.append(f"Limits: [{ll_str}, {ul_str}]")
            if event.delta is not None:
                lines.append(f"Delta from limit: {event.delta:.4f}")
            if event.fail_desc:
                lines.append(f"Failure Description: {event.fail_desc}")
            if event.ntf:
                lines.append("NTF: No-Trouble-Found on retest")
            if event.communication_state:
                lines.append(f"Communication State: {event.communication_state}")

            content = "\n".join(l for l in lines if l)

            # Rich metadata for Qdrant filtering
            metadata = {
                **event.metadata,
                "track_id": event.track_id,
                "timestamp": event.timestamp,
                "process": event.process,
                "station": event.station,
                "test_code": event.test_code,
                "test_code_short": event.test_code_short,
                "result": event.result,
                "test_value": event.test_value,
                "lower_limit": event.lower_limit,
                "upper_limit": event.upper_limit,
                "delta": event.delta,
                "fail_desc": event.fail_desc,
                "ntf": event.ntf,
                "communication_state": event.communication_state or "",
                "data_type": "telemetry_failure",
            }

            # Remove None values from metadata (Qdrant requires scalar values)
            metadata = {k: v for k, v in metadata.items() if v is not None}

            documents.append(Document(content=content, metadata=metadata))

        return documents
