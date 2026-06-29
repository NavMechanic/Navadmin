"""
Phase 6.3 — MQTT Telemetry Listener for Field Device Monitoring.

Listen on device/+/telemetry topic for battery health, temperature, errors.
Apply rule engine for predictive replacement triggers (RMA forecasting).
"""

from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Callable, Any
from datetime import datetime, timedelta
from enum import Enum
from collections import deque
import sqlite3


class TelemetryEventType(str, Enum):
    BATTERY_HEALTH = "battery_health"
    OPERATING_TEMP = "operating_temp"
    ERROR_CODE = "error_code"
    CONNECTIVITY = "connectivity"
    FIRMWARE_VERSION = "firmware_version"
    HEARTBEAT = "heartbeat"


@dataclass
class TelemetryEvent:
    """Single telemetry event from field device."""
    device_id: str
    event_type: TelemetryEventType
    value: Any
    timestamp: datetime
    raw_payload: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "event_type": self.event_type.value,
            "value": self.value,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class DeviceHealthStatus:
    """Aggregated device health metrics."""
    device_id: str
    last_heartbeat: datetime
    battery_health_pct: float
    operating_temp_c: float
    error_count: int
    latest_errors: List[str] = field(default_factory=list)
    firmware_version: str = ""
    days_in_field: int = 0
    rma_risk_score: float = 0.0
    recommended_replacement: bool = False
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "battery_health_pct": self.battery_health_pct,
            "operating_temp_c": self.operating_temp_c,
            "error_count": self.error_count,
            "rma_risk_score": self.rma_risk_score,
            "recommended_replacement": self.recommended_replacement,
            "firmware_version": self.firmware_version,
            "days_in_field": self.days_in_field,
            "updated_at": self.updated_at.isoformat(),
        }


class ReplacementRuleEngine:
    """
    Predictive RMA rule engine.
    Determines which devices should be replaced based on telemetry trends.
    """

    # Rule thresholds
    BATTERY_HEALTH_CRITICAL = 40  # % SOH
    TEMP_MAX_SAFE = 75  # °C
    ERROR_RATE_CRITICAL = 10  # errors per day
    RMA_SCORE_THRESHOLD = 0.75

    @staticmethod
    def evaluate_device(health: DeviceHealthStatus) -> None:
        """Apply rules and update RMA recommendation."""
        score = 0.0

        # Battery health rule (0.0-0.4 contribution)
        if health.battery_health_pct < ReplacementRuleEngine.BATTERY_HEALTH_CRITICAL:
            score += 0.4 * (1 - health.battery_health_pct / 100)

        # Temperature rule (0.0-0.3 contribution)
        if health.operating_temp_c > ReplacementRuleEngine.TEMP_MAX_SAFE:
            temp_excess = health.operating_temp_c - ReplacementRuleEngine.TEMP_MAX_SAFE
            score += 0.3 * min(1.0, temp_excess / 25)  # Normalize to 25°C excess

        # Error rate rule (0.0-0.3 contribution)
        if health.error_count > 0:
            daily_error_rate = health.error_count / max(1, health.days_in_field)
            if daily_error_rate > ReplacementRuleEngine.ERROR_RATE_CRITICAL:
                score += 0.3 * min(1.0, daily_error_rate / (2 * ReplacementRuleEngine.ERROR_RATE_CRITICAL))

        health.rma_risk_score = min(1.0, score)
        health.recommended_replacement = (
            health.rma_risk_score >= ReplacementRuleEngine.RMA_SCORE_THRESHOLD
            or health.battery_health_pct < 25
            or health.operating_temp_c > 90
        )


class MQTTTelemetryListener:
    """
    Listen to device telemetry via MQTT.
    Track health metrics, detect failures, trigger RMA recommendations.
    """

    def __init__(
        self,
        broker_host: str = "mqtt.local",
        broker_port: int = 1883,
        db_path: str = "telemetry.db",
        history_days: int = 30,
    ):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.db_path = db_path
        self.history_days = history_days
        self._devices: Dict[str, DeviceHealthStatus] = {}
        self._event_buffer: deque = deque(maxlen=10000)  # Recent events
        self._init_db()
        self.client = None

    # ------------------------------------------------------------------
    # Database Setup
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Initialize SQLite for telemetry storage."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS telemetry_events (
                id INTEGER PRIMARY KEY,
                device_id TEXT,
                event_type TEXT,
                value REAL,
                timestamp TEXT,
                payload_json TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS device_health (
                device_id TEXT UNIQUE,
                battery_health_pct REAL,
                operating_temp_c REAL,
                error_count INTEGER,
                firmware_version TEXT,
                rma_risk_score REAL,
                recommended_replacement BOOLEAN,
                last_update TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rma_predictions (
                id INTEGER PRIMARY KEY,
                device_id TEXT,
                prediction_date TEXT,
                battery_health_pct REAL,
                operating_temp_c REAL,
                error_count INTEGER,
                rma_score REAL,
                recommended BOOLEAN
            )
        """)

        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Event Processing
    # ------------------------------------------------------------------

    def handle_telemetry(self, device_id: str, payload: dict) -> None:
        """
        Process incoming MQTT telemetry message.

        Args:
            device_id: Device identifier
            payload: JSON payload from MQTT message
        """
        timestamp = datetime.utcnow()

        # Parse battery health
        if "battery_health_pct" in payload:
            event = TelemetryEvent(
                device_id=device_id,
                event_type=TelemetryEventType.BATTERY_HEALTH,
                value=payload["battery_health_pct"],
                timestamp=timestamp,
                raw_payload=payload,
            )
            self._process_event(event)

        # Parse operating temperature
        if "operating_temp_c" in payload:
            event = TelemetryEvent(
                device_id=device_id,
                event_type=TelemetryEventType.OPERATING_TEMP,
                value=payload["operating_temp_c"],
                timestamp=timestamp,
                raw_payload=payload,
            )
            self._process_event(event)

        # Parse error codes
        if "error_codes" in payload:
            for error_code in payload["error_codes"]:
                event = TelemetryEvent(
                    device_id=device_id,
                    event_type=TelemetryEventType.ERROR_CODE,
                    value=error_code,
                    timestamp=timestamp,
                    raw_payload=payload,
                )
                self._process_event(event)

        # Firmware version
        if "firmware_version" in payload:
            event = TelemetryEvent(
                device_id=device_id,
                event_type=TelemetryEventType.FIRMWARE_VERSION,
                value=payload["firmware_version"],
                timestamp=timestamp,
                raw_payload=payload,
            )
            self._process_event(event)

        # Heartbeat
        event = TelemetryEvent(
            device_id=device_id,
            event_type=TelemetryEventType.HEARTBEAT,
            value=True,
            timestamp=timestamp,
            raw_payload=payload,
        )
        self._process_event(event)

    def _process_event(self, event: TelemetryEvent) -> None:
        """Process single telemetry event."""
        # Add to buffer
        self._event_buffer.append(event)

        # Update device health status
        if event.device_id not in self._devices:
            self._devices[event.device_id] = DeviceHealthStatus(
                device_id=event.device_id,
                last_heartbeat=event.timestamp,
                battery_health_pct=100.0,
                operating_temp_c=25.0,
                error_count=0,
            )

        health = self._devices[event.device_id]

        # Update based on event type
        if event.event_type == TelemetryEventType.BATTERY_HEALTH:
            health.battery_health_pct = float(event.value)
        elif event.event_type == TelemetryEventType.OPERATING_TEMP:
            health.operating_temp_c = float(event.value)
        elif event.event_type == TelemetryEventType.ERROR_CODE:
            health.error_count += 1
            health.latest_errors.append(str(event.value))
            if len(health.latest_errors) > 10:
                health.latest_errors = health.latest_errors[-10:]
        elif event.event_type == TelemetryEventType.FIRMWARE_VERSION:
            health.firmware_version = str(event.value)
        elif event.event_type == TelemetryEventType.HEARTBEAT:
            health.last_heartbeat = event.timestamp

        health.updated_at = event.timestamp

        # Evaluate RMA risk
        ReplacementRuleEngine.evaluate_device(health)

        # Log to database
        self._log_event(event)
        self._log_health_status(health)

    # ------------------------------------------------------------------
    # Database Logging
    # ------------------------------------------------------------------

    def _log_event(self, event: TelemetryEvent) -> None:
        """Log telemetry event to database."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO telemetry_events
                (device_id, event_type, value, timestamp, payload_json)
                VALUES (?, ?, ?, ?, ?)
            """, (
                event.device_id,
                event.event_type.value,
                event.value if isinstance(event.value, (int, float)) else None,
                event.timestamp.isoformat(),
                json.dumps(event.raw_payload) if event.raw_payload else None,
            ))
            conn.commit()
            conn.close()
        except Exception as exc:
            print(f"  ⚠ Failed to log telemetry event: {exc}")

    def _log_health_status(self, health: DeviceHealthStatus) -> None:
        """Log device health snapshot to database."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO device_health
                (device_id, battery_health_pct, operating_temp_c, error_count,
                 firmware_version, rma_risk_score, recommended_replacement, last_update)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                health.device_id,
                health.battery_health_pct,
                health.operating_temp_c,
                health.error_count,
                health.firmware_version,
                health.rma_risk_score,
                health.recommended_replacement,
                health.updated_at.isoformat(),
            ))
            conn.commit()
            conn.close()
        except Exception as exc:
            print(f"  ⚠ Failed to log health status: {exc}")

    # ------------------------------------------------------------------
    # Querying & Analysis
    # ------------------------------------------------------------------

    def get_device_status(self, device_id: str) -> Optional[DeviceHealthStatus]:
        """Get latest health status for a device."""
        return self._devices.get(device_id)

    def get_all_device_statuses(self) -> List[DeviceHealthStatus]:
        """Get health status for all devices."""
        return list(self._devices.values())

    def get_devices_at_risk(self) -> List[DeviceHealthStatus]:
        """Get devices recommended for RMA."""
        return [
            health for health in self._devices.values()
            if health.recommended_replacement
        ]

    def get_recent_events(
        self,
        device_id: Optional[str] = None,
        event_type: Optional[TelemetryEventType] = None,
        limit: int = 100,
    ) -> List[TelemetryEvent]:
        """Get recent telemetry events with optional filtering."""
        events = list(self._event_buffer)

        if device_id:
            events = [e for e in events if e.device_id == device_id]

        if event_type:
            events = [e for e in events if e.event_type == event_type]

        return events[-limit:]

    def get_fleet_statistics(self) -> dict:
        """Get fleet-wide statistics for dashboard."""
        if not self._devices:
            return {
                "total_devices": 0,
                "avg_battery_health_pct": 0.0,
                "avg_temp_c": 0.0,
                "devices_at_risk": 0,
                "error_rate_per_device": 0.0,
            }

        devices = list(self._devices.values())
        at_risk = sum(1 for d in devices if d.recommended_replacement)
        avg_battery = sum(d.battery_health_pct for d in devices) / len(devices)
        avg_temp = sum(d.operating_temp_c for d in devices) / len(devices)
        total_errors = sum(d.error_count for d in devices)

        return {
            "total_devices": len(devices),
            "avg_battery_health_pct": avg_battery,
            "avg_temp_c": avg_temp,
            "devices_at_risk": at_risk,
            "devices_at_risk_pct": (at_risk / len(devices) * 100) if devices else 0,
            "error_rate_per_device": total_errors / len(devices) if devices else 0,
        }

    def query_devices_by_criteria(
        self,
        battery_below_pct: Optional[float] = None,
        temp_above_c: Optional[float] = None,
        error_count_above: Optional[int] = None,
    ) -> List[DeviceHealthStatus]:
        """Query devices matching criteria."""
        results = list(self._devices.values())

        if battery_below_pct is not None:
            results = [d for d in results if d.battery_health_pct < battery_below_pct]

        if temp_above_c is not None:
            results = [d for d in results if d.operating_temp_c > temp_above_c]

        if error_count_above is not None:
            results = [d for d in results if d.error_count > error_count_above]

        return results

    # ------------------------------------------------------------------
    # MQTT Integration (Mock)
    # ------------------------------------------------------------------

    async def connect_and_listen(self, callback: Optional[Callable] = None) -> None:
        """
        Connect to MQTT broker and listen for telemetry.

        In production, would use paho-mqtt library:
            - Subscribe to 'device/+/telemetry'
            - Parse JSON payload
            - Call handle_telemetry()
            - Apply ReplacementRuleEngine

        Mock version just returns success.
        """
        print(f"  📡 Connecting to MQTT broker {self.broker_host}:{self.broker_port}")
        print(f"  📡 Listening on topic: device/+/telemetry")

        if callback:
            callback("Connected to MQTT broker")

        return True

    async def disconnect(self) -> None:
        """Disconnect from MQTT broker."""
        print(f"  📡 Disconnected from MQTT broker")
        return True

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def generate_rma_report(self) -> dict:
        """Generate RMA prediction report for fleet."""
        at_risk_devices = self.get_devices_at_risk()

        report = {
            "generated_at": datetime.utcnow().isoformat(),
            "total_devices": len(self._devices),
            "devices_at_risk": len(at_risk_devices),
            "predicted_replacements": [
                {
                    "device_id": d.device_id,
                    "battery_health_pct": d.battery_health_pct,
                    "operating_temp_c": d.operating_temp_c,
                    "error_count": d.error_count,
                    "rma_risk_score": d.rma_risk_score,
                    "reason": self._get_failure_reason(d),
                }
                for d in at_risk_devices
            ],
            "fleet_metrics": self.get_fleet_statistics(),
        }

        return report

    @staticmethod
    def _get_failure_reason(health: DeviceHealthStatus) -> str:
        """Generate human-readable failure reason."""
        reasons = []

        if health.battery_health_pct < 25:
            reasons.append(f"Battery critically degraded ({health.battery_health_pct:.0f}% SOH)")
        elif health.battery_health_pct < 40:
            reasons.append(f"Battery health declining ({health.battery_health_pct:.0f}% SOH)")

        if health.operating_temp_c > 90:
            reasons.append(f"Overheating ({health.operating_temp_c:.1f}°C)")
        elif health.operating_temp_c > 75:
            reasons.append(f"Operating at elevated temp ({health.operating_temp_c:.1f}°C)")

        if health.error_count > 10:
            reasons.append(f"High error count ({health.error_count} errors)")

        return " + ".join(reasons) if reasons else "Multiple risk factors detected"
