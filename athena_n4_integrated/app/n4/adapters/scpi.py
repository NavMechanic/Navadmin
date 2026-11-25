"""SCPI / VISA bench-instrument intake.

Two paths produce the same ``ScpiLog`` (``athena-scpi-log/1``):

* **ATE software export**: a JSON log uploaded with its raw waveform / CSV attachments.
* **Live driver** (``capture``): N4 talks to a *registered* instrument itself, over raw SCPI on TCP
  (``TCPIP0::host::5025::SOCKET``, stdlib socket) or, when ``pyvisa`` is installed, any VISA
  resource. Only hosts in ``N4_SCPI_ALLOWED_HOSTS`` are reachable, and only read-only queries
  from ``QUERY_ALLOWLIST`` are sent. N4 never issues ``*RST``, source / output commands or file
  operations to an instrument.

Every record carries the UTC timestamp of its query. The ``*IDN?`` response identifies the
manufacturer, model, serial and firmware, and the serial must match the instrument register.
"""
from __future__ import annotations

import re
import socket
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional, Protocol

from ..models import CaptureStep, N4Instrument, ScpiLog

QUERY_ALLOWLIST = re.compile(
    r"^(\*IDN\?|\*OPC\?|SYST(em)?:ERR(or)?\?|READ\?|FETC(h)?\?|"
    r"MEAS(ure)?(:[A-Z]{2,12}){1,4}\?( [-+0-9.E,A-Z]{1,40})?)$", re.I)
SIMULATED = re.compile(r"\b(simulated|simulator|virtual|mock|fake|dummy|demo|pyvisa-sim|sim)\b", re.I)
OVERLOAD = Decimal("9.9E37")


class ScpiError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def parse_idn(idn: str) -> dict[str, str]:
    parts = [p.strip() for p in idn.strip().split(",")]
    if len(parts) < 3 or not all(parts[:3]):
        raise ScpiError("IDN_INVALID", f"*IDN? response {idn!r} is not manufacturer,model,serial[,firmware]")
    return {"manufacturer": parts[0], "model": parts[1], "serial": parts[2],
            "firmware": parts[3] if len(parts) > 3 else ""}


def looks_simulated(idn: dict[str, str]) -> bool:
    return bool(SIMULATED.search(idn["manufacturer"]) or SIMULATED.search(idn["model"])
                or idn["serial"].upper().startswith(("SIM", "MOCK", "FAKE")))


def parse_number(response: str) -> Decimal:
    text = response.strip().split(",")[0].strip()
    text = re.sub(r"\s*[A-Za-zΩµ]+$", "", text)                   # tolerate a trailing unit
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ScpiError("RESPONSE_NOT_NUMERIC", f"instrument response {response!r} is not a number") from None
    if not value.is_finite() or abs(value) >= OVERLOAD:
        raise ScpiError("INSTRUMENT_OVERLOAD", f"instrument reported overload / invalid ({response.strip()})")
    return value


def check_log(log: ScpiLog, instrument: Optional[N4Instrument]) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Identity and record checks. Returns the parsed IDN and (code, message) problems."""
    problems: list[tuple[str, str]] = []
    try:
        idn = parse_idn(log.instrument.idn)
    except ScpiError as exc:
        return {}, [(exc.code, exc.message)]
    if instrument is None:
        problems.append(("INSTRUMENT_UNREGISTERED", f"no registered instrument has serial {idn['serial']}"))
    elif instrument.serial != idn["serial"] or instrument.model.lower() not in idn["model"].lower():
        problems.append(("INSTRUMENT_IDENTITY_MISMATCH", f"*IDN? reports {idn['model']} s/n {idn['serial']}; the "
                                                         f"register has {instrument.model} s/n {instrument.serial}"))
    for i, r in enumerate(log.records):
        if r.t.tzinfo is None or r.t.utcoffset().total_seconds() != 0:
            problems.append(("TIMESTAMP_NOT_UTC", f"record {i} timestamp {r.t} is not UTC"))
        if not QUERY_ALLOWLIST.match(r.command.strip()):
            problems.append(("COMMAND_NOT_ALLOWED", f"record {i}: {r.command!r} is not an allowlisted query"))
        try:
            parse_number(r.response)
        except ScpiError as exc:
            problems.append((exc.code, f"record {i}: {exc.message}"))
    return idn, problems


# ----------------------------------------------------------------------------------------------
# live driver
# ----------------------------------------------------------------------------------------------
class Transport(Protocol):
    def query(self, command: str) -> str: ...

    def close(self) -> None: ...


def parse_resource(resource: str) -> tuple[str, str, int]:
    m = re.fullmatch(r"TCPIP\d*::([A-Za-z0-9.\-]+)::(\d+)::SOCKET", resource.strip(), re.I)
    if m:
        return "socket", m.group(1), int(m.group(2))
    if re.fullmatch(r"(USB|GPIB|ASRL|TCPIP)\d*::.+", resource.strip(), re.I):
        return "visa", resource.strip(), 0
    raise ScpiError("RESOURCE_INVALID", f"{resource!r} is not a VISA / TCPIP socket resource")


class TcpSocketTransport:
    """Raw SCPI over TCP (port 5025 on most bench instruments), newline-terminated."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

    def query(self, command: str) -> str:
        self.sock.sendall(command.encode("ascii") + b"\n")
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ScpiError("INSTRUMENT_DISCONNECTED", f"connection closed while waiting for {command!r}")
            self.buf += chunk
            if len(self.buf) > 1 << 20:
                raise ScpiError("RESPONSE_TOO_LARGE", "instrument response exceeds 1 MiB")
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("ascii", "replace").strip()

    def close(self) -> None:
        self.sock.close()


class PyVisaTransport:  # pragma: no cover - needs pyvisa and hardware
    def __init__(self, resource: str, timeout: float) -> None:
        try:
            import pyvisa
        except ImportError:
            raise ScpiError("VISA_UNAVAILABLE", "pyvisa is not installed; use a TCPIP socket resource") from None
        self.inst = pyvisa.ResourceManager().open_resource(resource)
        self.inst.timeout = int(timeout * 1000)

    def query(self, command: str) -> str:
        return str(self.inst.query(command)).strip()

    def close(self) -> None:
        self.inst.close()


def open_transport(instrument: N4Instrument, allowed_hosts: list[str], timeout: float) -> Transport:
    if not instrument.resource:
        raise ScpiError("RESOURCE_MISSING", f"{instrument.instrument_id} has no registered VISA / socket resource")
    kind, host, port = parse_resource(instrument.resource)
    if kind == "socket":
        if host not in allowed_hosts:
            raise ScpiError("HOST_NOT_ALLOWED", f"{host} is not in N4_SCPI_ALLOWED_HOSTS")
        try:
            return TcpSocketTransport(host, port, timeout)
        except OSError as exc:
            raise ScpiError("INSTRUMENT_UNREACHABLE", f"{instrument.resource}: {exc}") from None
    return PyVisaTransport(host, timeout)


def capture(transport: Transport, steps: list[CaptureStep], clock: Callable[[], datetime] = lambda: datetime.now(
        timezone.utc)) -> tuple[str, list[dict]]:
    """Query ``*IDN?`` then each allowlisted step; returns (idn, records with UTC timestamps)."""
    for s in steps:
        if not QUERY_ALLOWLIST.match(s.query.strip()):
            raise ScpiError("COMMAND_NOT_ALLOWED", f"{s.query!r} is not an allowlisted read-only query")
    idn = transport.query("*IDN?")
    records = []
    for s in steps:
        t = clock()
        records.append({"t": t.isoformat().replace("+00:00", "Z"), "command": s.query.strip(),
                        "response": transport.query(s.query.strip()), "characteristic": s.characteristic,
                        "unit": s.unit, "expanded_uncertainty": str(s.expanded_uncertainty), "k": s.k})
    err = transport.query("SYST:ERR?")
    if not err.startswith(("+0", "0")):
        raise ScpiError("INSTRUMENT_ERROR", f"instrument error queue: {err}")
    return idn, records
