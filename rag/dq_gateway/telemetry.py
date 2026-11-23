"""Station telemetry gate: PLC/MQTT messages → lifecycle_event.v1 → DQ01/03/04/09/10/25 → Digital Twin.

The twin only sees readings that passed. A missing reading is a DQ03 failure — never replaced
by a nominal value — and simulated data can never pass as REAL (DQ10). Rights are declared
once for the stream by the operator (``rights``) and re-checked per message against G0 (DQ01).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .gateway import ArtifactSubmission, DQGateway
from .manifest import LicenseRights, lineage_hash, sha256_bytes
from .rules import RuleResult

# PLC node → (critical readings with their units)
STATION_SPECS: dict[str, dict[str, str]] = {
    "A_FASTENING": {"torque_nm": "N·m", "depth_mm": "mm", "cycle_time_s": "s"},
    "B_DISPENSING": {"mass_tmq_mg": "mg", "mass_lm_mg": "mg", "nozzle_pressure_bar": "bar", "humidity_pct": "%RH"},
    "C_PRESS": {"inlay_force_n": "N", "display_force_n": "N", "dwell_time_s": "s", "frame_offset_mm": "mm"},
}
_NS = uuid.UUID("6f1c2a52-2b8e-4b1b-9a40-0c2f0b6d7e11")


@dataclass
class TelemetryVerdict:
    admitted: bool
    node: str
    station_id: str
    event_id: Optional[str]
    origin_type: Optional[str]
    readings: dict[str, Optional[float]]
    results: list[RuleResult] = field(default_factory=list)

    @property
    def failed_rules(self) -> list[str]:
        return [r.rule_id for r in self.results if not r.passed]

    @property
    def messages(self) -> list[str]:
        return [f"{r.rule_id} {r.error_code}: {r.message}" for r in self.results if not r.passed]


def _parse_utc(ts: Any) -> Optional[datetime]:
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0 else None


class TelemetryGate:
    def __init__(self, gateway: DQGateway, tenant_id: str, project_id: str, rights: LicenseRights,
                 simulator: bool, work_package_id: str = "WP-LINE-TELEMETRY") -> None:
        self.gateway, self.tenant_id, self.project_id = gateway, tenant_id, project_id
        self.rights, self.simulator, self.work_package_id = rights, simulator, work_package_id
        self.verdicts: list[TelemetryVerdict] = []

    def _reject(self, node, station, origin, readings, rule_id, code, message) -> TelemetryVerdict:
        r = RuleResult(rule_id, "CRITICAL", False, "BLOCK", code, message)
        v = TelemetryVerdict(False, node, station, None, origin, readings, [r])
        self.gateway.telemetry_stats["rejected"] += 1
        self.gateway.telemetry_stats[f"rule:{rule_id}"] += 1
        self.gateway.telemetry_rejections.append({"at": self.gateway.clock().isoformat(), "station": station,
                                                  "rules": [rule_id], "messages": [message]})
        self.verdicts.append(v)
        return v

    def check(self, msg: Any) -> TelemetryVerdict:
        node, station = getattr(msg, "node", "?"), getattr(msg, "station_id", "?")
        fields = STATION_SPECS.get(node)
        readings = {f: getattr(msg, f, None) for f in (fields or {})}
        origin = getattr(msg, "origin_type", None)
        if fields is None:
            return self._reject(node, station, origin, readings, "DQ02", "SCHEMA_INVALID", f"unknown PLC node {node!r}")
        if origin not in ("REAL", "SYNTHETIC", "DERIVED"):
            return self._reject(node, station, origin, readings, "DQ10", "SENSOR_DATA_UNTRUSTED",
                                "origin_type is not declared (REAL|SYNTHETIC|DERIVED)")
        event_time = _parse_utc(getattr(msg, "timestamp", None))
        if event_time is None:
            return self._reject(node, station, origin, readings, "DQ25", "TELEMETRY_INVALID",
                                f"timestamp {getattr(msg, 'timestamp', None)!r} is not timezone-aware UTC")
        now = self.gateway.clock()
        unit_id, lot_id = getattr(msg, "unit_id", None), getattr(msg, "lot_id", None)
        sensor_id = getattr(msg, "sensor_id", None) or f"{station}-PLC"
        raw = json.dumps({k: v for k, v in vars(msg).items()}, sort_keys=True, default=str).encode()
        payload = {"event_kind": "station", "station_id": station, "event_time": event_time.isoformat(),
                   "readings": readings,
                   "sensor": {"sensor_id": sensor_id, "status": getattr(msg, "sensor_status", None) or "UNCALIBRATED",
                              "calibration_id": getattr(msg, "calibration_id", None)}}
        if unit_id is not None:
            payload["unit_id"] = unit_id
        if lot_id is not None:
            payload["lot_id"] = lot_id
        if getattr(msg, "fixture_id", None) is not None:
            payload["fixture_id"] = msg.fixture_id
        grant = self.gateway.registry.rights_grant(self.project_id)
        stamp = event_time.strftime("%Y%m%dT%H%M%S%fZ")
        manifest = {
            "event_id": str(uuid.uuid5(_NS, f"{self.tenant_id}/{station}/{sensor_id}/{stamp}/{unit_id}")),
            "project_id": self.project_id, "work_package_id": self.work_package_id, "node_id": "N8",
            "model_or_tool": f"sensor:{station}", "connector_mode": "SDK",
            "input_schema": "plc_message.v1", "output_schema": "lifecycle_event.v1",
            "source_artifact_ids": [grant.ref] if grant else [f"G0-{self.project_id}@1"],
            "source_revision": grant.revision if grant else "1",
            "source_hash": lineage_hash([grant.output_sha256]) if grant else "0" * 64,
            "output_artifact_uri": f"artifact://{self.project_id}/TEL-{station}@{stamp}",
            "output_file_sha256": sha256_bytes(raw),
            "evidence_ids": [msg.calibration_id] if getattr(msg, "calibration_id", None) else [],
            "source_license_rights": self.rights, "data_classification": "CONFIDENTIAL", "origin_type": origin,
            "retention_and_deletion": {"retention_days": 365}, "captured_at": event_time.isoformat(),
            "processed_at": max(now, event_time).isoformat(),
            "measurement_units": {f: u for f, u in fields.items() if readings.get(f) is not None},
            "risk_method": "HEURISTIC", "risk_score": 0.0, "hard_policy_results": {}, "candidate_actions": [],
        }
        sub = ArtifactSubmission(manifest=manifest, payload=payload, content=raw, transport="MQTT",
                                 required_critical=[f"readings.{f}" for f in fields] + ["unit_id", "lot_id"]
                                 + (["fixture_id"] if node == "C_PRESS" else []),
                                 simulator_source=self.simulator)
        admitted, results, _ = self.gateway.evaluate_telemetry(sub, self.tenant_id)
        v = TelemetryVerdict(admitted, node, station, manifest["event_id"], origin, readings, results)
        self.verdicts.append(v)
        return v
