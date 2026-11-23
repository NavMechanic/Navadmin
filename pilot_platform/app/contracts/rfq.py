"""RFQ / PRD data contract (activity F): typed fields, units, criticality and label synonyms.

This is the single source of truth used by extraction (H), PRD generation (N2),
test planning (N4) and the gold-corpus evaluation.
"""
from __future__ import annotations

from dataclasses import dataclass

RFQ_SCHEMA_VERSION = "rfq/1.0"
PRD_SCHEMA_VERSION = "prd/1.0"


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str              # int | quantity | temperature | length | list | money | text
    unit: str | None
    required: bool
    critical: bool         # critical requirements must be traceable to at least one test
    labels: tuple[str, ...]
    statement: str         # PRD requirement template


RFQ_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("customer", "text", None, False, False, ("customer", "client", "buyer"), "Customer: {v}"),
    FieldSpec("quantity", "int", None, True, False,
              ("annual volume", "quantity", "volume", "qty", "units"), "Build quantity shall be {v} units"),
    FieldSpec("supply_voltage", "quantity", "V", True, True,
              ("supply voltage", "input voltage", "power supply", "supply", "vin", "voltage"),
              "The product shall operate from a {v} V supply"),
    FieldSpec("max_current", "quantity", "A", False, True,
              ("maximum current", "max current", "peak current", "current draw", "current"),
              "Current draw shall not exceed {v} A"),
    FieldSpec("power_budget", "quantity", "W", False, True,
              ("power budget", "max power", "power consumption", "power"),
              "Power consumption shall not exceed {v} W"),
    FieldSpec("operating_temp_min", "temperature", "degC", True, True,
              ("operating temperature", "operating temp", "temperature range", "ambient temperature",
               "ambient range", "temp range"), "The product shall operate at ambient temperatures down to {v} °C"),
    FieldSpec("operating_temp_max", "temperature", "degC", True, True, (),
              "The product shall operate at ambient temperatures up to {v} °C"),
    FieldSpec("board_length", "length", "m", False, False, ("board size", "pcb size", "dimensions", "board dimensions"),
              "PCB length shall not exceed {v} m"),
    FieldSpec("board_width", "length", "m", False, False, (), "PCB width shall not exceed {v} m"),
    FieldSpec("interfaces", "list", None, False, False, ("interfaces", "interface", "connectivity", "comms"),
              "The product shall provide interfaces: {v}"),
    FieldSpec("certifications", "list", None, False, True,
              ("certifications", "certification", "compliance", "standards"),
              "The product shall comply with: {v}"),
    FieldSpec("target_unit_price", "money", None, False, False,
              ("target unit price", "target price", "unit price", "price target"), "Target unit price: {v}"),
)
FIELDS = {f.name: f for f in RFQ_FIELDS}
REQUIRED = tuple(f.name for f in RFQ_FIELDS if f.required)

# label synonym -> field, longest first so "power supply" wins over "power"
LABEL_INDEX: tuple[tuple[str, str], ...] = tuple(sorted(
    ((label, f.name) for f in RFQ_FIELDS for label in f.labels), key=lambda x: -len(x[0])))

KNOWN_INTERFACES = ("USB", "UART", "SPI", "I2C", "CAN", "BLE", "Bluetooth", "Wi-Fi", "WiFi", "LoRa",
                    "Ethernet", "RS-485", "RS485", "Modbus", "LTE", "NFC", "GPIO")
KNOWN_CERTIFICATIONS = ("CE", "FCC", "UL", "RoHS", "REACH", "IEC 62368-1", "IEC 60601-1", "ISO 13485",
                        "ISO 26262", "BIS", "ATEX", "IP67", "IP65")
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "₹": "INR", "£": "GBP"}
