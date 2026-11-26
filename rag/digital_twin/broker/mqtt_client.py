"""MQTT data broker that streams PLC readings from the assembly line.

In production: connects to a real MQTT broker (e.g. Mosquitto / AWS IoT Core).
In simulation mode: generates synthetic PLC payloads from realistic distributions.
"""

from __future__ import annotations

import json
import time
import threading
import queue
from collections import deque
from dataclasses import dataclass, field, asdict, fields as dc_fields
from datetime import datetime, timezone
from typing import Callable, Optional
import numpy as np

try:
    import paho.mqtt.client as mqtt_lib
    _PAHO_AVAILABLE = True
except ImportError:
    _PAHO_AVAILABLE = False


@dataclass
class BrokerConfig:
    host: str = "localhost"
    port: int = 1883
    keepalive: int = 60
    topic_prefix: str = "aviator/assembly"
    simulate: bool = True          # True = synthetic PLC stream, no real broker needed
    sim_interval_s: float = 2.0    # seconds between simulated events
    qos: int = 1
    # Live mode only: operator-declared origin for messages that do not state their own
    # (e.g. "REAL"). Simulated messages are always SYNTHETIC. Unset + undeclared = rejected (DQ10).
    origin_type: Optional[str] = None


@dataclass
class PLCMessage:
    """Normalized PLC message from any of the three assembly nodes.

    Identity (unit/lot), sensor calibration and origin fields feed the DQ gateway
    (DQ10/DQ25); a message missing any reading is rejected, never back-filled.
    """
    node: str                      # "A_FASTENING" | "B_DISPENSING" | "C_PRESS"
    station_id: str
    torque_nm: Optional[float] = None
    depth_mm: Optional[float] = None
    cycle_time_s: Optional[float] = None
    mass_tmq_mg: Optional[float] = None
    mass_lm_mg: Optional[float] = None
    nozzle_pressure_bar: Optional[float] = None
    humidity_pct: Optional[float] = None
    inlay_force_n: Optional[float] = None
    display_force_n: Optional[float] = None
    dwell_time_s: Optional[float] = None
    frame_offset_mm: Optional[float] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    unit_id: Optional[str] = None
    lot_id: Optional[str] = None
    fixture_id: Optional[str] = None
    sensor_id: Optional[str] = None
    sensor_status: Optional[str] = None      # CALIBRATED | UNCALIBRATED | SIMULATED
    calibration_id: Optional[str] = None
    origin_type: Optional[str] = None        # REAL | SYNTHETIC | DERIVED

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, payload: str) -> "PLCMessage":
        data = json.loads(payload)
        known = {f.name for f in dc_fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class MQTTBroker:
    """
    Dual-mode MQTT client:
    - simulate=True  → generates synthetic PLC readings at sim_interval_s cadence
    - simulate=False → connects to a real broker and subscribes to PLC topics

    Usage (simulation):
        broker = MQTTBroker(BrokerConfig(simulate=True))
        broker.on_message(lambda msg: print(msg))
        broker.start()
        ...
        broker.stop()

    Usage (live):
        broker = MQTTBroker(BrokerConfig(host="10.0.1.50", simulate=False))
        broker.on_message(callback)
        broker.start()
    """

    _TOPICS = {
        "A_FASTENING": "{prefix}/station22/fastening",
        "B_DISPENSING": "{prefix}/dispensing",
        "C_PRESS": "{prefix}/press",
    }

    def __init__(self, config: BrokerConfig = None) -> None:
        self.config = config or BrokerConfig()
        self._callbacks: list[Callable[[PLCMessage], None]] = []
        self._message_queue: queue.Queue[PLCMessage] = queue.Queue()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._rng = np.random.default_rng(int(time.time()))
        self._sim_unit = 0
        self.parse_errors: deque = deque(maxlen=100)   # malformed live payloads (previously dropped silently)

        if not self.config.simulate and not _PAHO_AVAILABLE:
            raise RuntimeError(
                "paho-mqtt is required for live broker mode. "
                "Install with: pip install paho-mqtt"
            )

    def on_message(self, callback: Callable[[PLCMessage], None]) -> None:
        """Register a callback invoked for every incoming PLCMessage."""
        self._callbacks.append(callback)

    def start(self) -> None:
        """Start streaming in a background thread."""
        self._running = True
        if self.config.simulate:
            self._thread = threading.Thread(target=self._simulate_loop, daemon=True)
        else:
            self._thread = threading.Thread(target=self._live_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def drain(self, timeout: float = 0.1) -> list[PLCMessage]:
        """Pull all buffered messages (non-blocking, for Streamlit polling)."""
        messages = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                messages.append(self._message_queue.get_nowait())
            except queue.Empty:
                break
        return messages

    # ------------------------------------------------------------------
    # Simulation mode
    # ------------------------------------------------------------------

    def _simulate_loop(self) -> None:
        while self._running:
            self._sim_unit += 1
            for node_key in ("A_FASTENING", "B_DISPENSING", "C_PRESS"):
                msg = self._generate_plc_message(node_key)
                self._dispatch(msg)
            time.sleep(self.config.sim_interval_s)

    def _generate_plc_message(self, node: str) -> PLCMessage:
        rng = self._rng
        now = datetime.now(timezone.utc)
        synthetic = dict(
            timestamp=now.isoformat(),
            unit_id=f"SIM-U{self._sim_unit:06d}",
            lot_id=f"SIM-LOT-{now:%Y%m%d}",
            sensor_status="SIMULATED",
            origin_type="SYNTHETIC",
        )

        if node == "A_FASTENING":
            return PLCMessage(
                node=node,
                station_id="S22",
                torque_nm=float(rng.normal(0.45, 0.06)),
                depth_mm=float(rng.normal(3.2, 0.15)),
                cycle_time_s=float(rng.normal(2.1, 0.3)),
                sensor_id="S22-SIM",
                **synthetic,
            )
        if node == "B_DISPENSING":
            return PLCMessage(
                node=node,
                station_id="S31",
                mass_tmq_mg=float(rng.normal(352.0, 20.0)),
                mass_lm_mg=float(rng.normal(45.0, 15.0)),
                nozzle_pressure_bar=float(rng.normal(3.0, 0.5)),
                humidity_pct=float(rng.normal(45.0, 15.0)),
                sensor_id="S31-SIM",
                **synthetic,
            )
        # C_PRESS
        return PLCMessage(
            node=node,
            station_id="S42",
            inlay_force_n=float(rng.normal(1400.0, 140.0)),
            display_force_n=float(rng.normal(1210.0, 60.0)),
            dwell_time_s=float(rng.normal(15.0, 1.0)),
            frame_offset_mm=float(rng.normal(0.0, 0.05)),
            fixture_id="BE-45" if self._sim_unit % 2 == 0 else "PA-49",
            sensor_id="S42-SIM",
            **synthetic,
        )

    # ------------------------------------------------------------------
    # Live MQTT mode (requires paho-mqtt)
    # ------------------------------------------------------------------

    def _live_loop(self) -> None:
        client = mqtt_lib.Client()
        client.on_connect = self._on_connect
        client.on_message = self._on_mqtt_message
        client.connect(self.config.host, self.config.port, self.config.keepalive)
        client.loop_start()
        while self._running:
            time.sleep(0.5)
        client.loop_stop()
        client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        prefix = self.config.topic_prefix
        for topic_tpl in self._TOPICS.values():
            client.subscribe(topic_tpl.format(prefix=prefix), qos=self.config.qos)

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            plc = PLCMessage.from_json(msg.payload.decode())
        except Exception as exc:
            self.parse_errors.append({"topic": getattr(msg, "topic", "?"), "error": f"{type(exc).__name__}: {exc}"})
            return
        if plc.origin_type is None and self.config.origin_type:
            plc.origin_type = self.config.origin_type  # operator-declared stream origin
        self._dispatch(plc)

    # ------------------------------------------------------------------

    def _dispatch(self, msg: PLCMessage) -> None:
        self._message_queue.put(msg)
        for cb in self._callbacks:
            try:
                cb(msg)
            except Exception:
                pass
