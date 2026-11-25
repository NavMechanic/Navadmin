"""Upload screening: size limit, MIME sniffing / allow-list, and the malware-scanning hook.

``validate_upload`` sniffs the real type from the bytes (never trusts the client's Content-Type
or extension alone) and rejects executables, scripts, HTML, macro-enabled Office files and
archives with traversal paths. ``MalwareScanner`` is a hook:

* ``ClamdScanner`` speaks the clamd INSTREAM protocol when ``N0_CLAMD_HOST`` is set;
* ``SignatureScanner`` (default) detects the EICAR test signature and a configurable SHA-256
  denylist. It is a development fallback, reported as such by ``/health``; set
  ``N0_REQUIRE_AV=true`` to refuse uploads unless a real engine is configured.
"""
from __future__ import annotations

import hashlib
import io
import socket
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
ALLOWED_MIME = {
    "application/pdf": (".pdf",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (".docx",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (".xlsx",),
    "application/json": (".json", ".kicad_pro", ".gbrjob"),
    "text/csv": (".csv",),
    "text/plain": (".txt", ".log", ".md"),
    "application/zip": (".zip",),
    "model/step": (".step", ".stp"),
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    # native EDA / CAE formats (text-based; sniffed from content, never from the extension alone)
    "application/x-kicad-schematic": (".kicad_sch",),
    "application/x-kicad-pcb": (".kicad_pcb",),
    "text/x-spice": (".cir", ".sp", ".spice", ".net", ".lib"),
    "application/x-spice-raw": (".raw",),
    "application/vnd.gerber": (".gbr", ".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".gm1",
                               ".gko", ".g1", ".g2"),
    "application/x-excellon": (".drl", ".xln"),
    # verification evidence (N4): columnar test logs
    "application/vnd.apache.parquet": (".parquet",),
}
_TEXTUAL = {"text/csv", "application/json", "text/x-spice", "application/x-kicad-schematic", "application/x-kicad-pcb",
            "application/vnd.gerber", "application/x-excellon", "application/x-spice-raw", "model/step"}


@dataclass
class ScreeningResult:
    sniffed_mime: str
    ok: bool
    problems: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    clean: bool
    engine: str
    signature: Optional[str] = None
    real_engine: bool = False


def sniff_mime(data: bytes) -> str:
    head = data[:8192]
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"MZ"):
        return "application/x-msdownload"
    if head.startswith(b"\x7fELF"):
        return "application/x-elf"
    if head.startswith(b"\xca\xfe\xba\xbe") or head.startswith(b"\xcf\xfa\xed\xfe"):
        return "application/x-mach-binary"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            return "application/octet-stream"
        if "[Content_Types].xml" in names:
            if any(n.startswith("word/") for n in names):
                return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if any(n.startswith("xl/") for n in names):
                return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            return "application/vnd.openxmlformats-officedocument"
        return "application/zip"
    if head.startswith(b"PAR1") and data[-4:] == b"PAR1":
        return "application/vnd.apache.parquet"
    if head.lstrip().startswith(b"ISO-10303-21"):
        return "model/step"
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < len(head) - 4:
            return "application/octet-stream"
        text = head[:exc.start].decode("utf-8")
    t = text.lstrip().lower()
    if t.startswith("#!"):
        return "text/x-shellscript"
    if t.startswith("<!doctype html") or t.startswith("<html") or "<script" in t[:2048]:
        return "text/html"
    if t.startswith("(kicad_sch"):
        return "application/x-kicad-schematic"
    if t.startswith("(kicad_pcb"):
        return "application/x-kicad-pcb"
    if t.startswith(("g04", "%fs", "%tf", "%mo")):
        return "application/vnd.gerber"
    if t.startswith("m48"):
        return "application/x-excellon"
    if t.startswith("title:") and "plotname:" in t:
        return "application/x-spice-raw"
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if lines and lines[0].startswith("*") and any(
            ln.startswith((".param", ".include", ".lib", ".tran", ".op", ".dc", ".ac", ".subckt", ".model", ".meas",
                           ".temp", ".end")) for ln in lines[1:]):
        return "text/x-spice"
    if t.startswith("{") or t.startswith("["):
        return "application/json"
    first = text.split("\n", 1)[0]
    if "," in first and "\n" in text:
        return "text/csv"
    return "text/plain"


def validate_upload(data: bytes, filename: str, declared_mime: Optional[str], max_bytes: int) -> ScreeningResult:
    problems: list[str] = []
    mime = sniff_mime(data)
    if len(data) == 0:
        problems.append("empty file")
    if len(data) > max_bytes:
        problems.append(f"file exceeds {max_bytes} bytes")
    if mime not in ALLOWED_MIME:
        problems.append(f"content type {mime} is not allowed")
    else:
        ext = PurePosixPath(filename.lower()).suffix
        if ext not in ALLOWED_MIME[mime]:
            problems.append(f"extension {ext or '(none)'} does not match detected type {mime}")
    declared = (declared_mime or "").split(";")[0].strip().lower()
    if declared and declared not in ("application/octet-stream", mime) and not (
            declared == "text/plain" and mime in _TEXTUAL):
        problems.append(f"declared Content-Type {declared} does not match detected type {mime}")
    if mime.startswith("application/vnd.openxmlformats") or mime == "application/zip":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    p = PurePosixPath(info.filename.replace("\\", "/"))
                    if p.is_absolute() or ".." in p.parts:
                        problems.append(f"archive member {info.filename!r} escapes the archive")
                    if p.name.lower() == "vbaproject.bin":
                        problems.append("macro-enabled Office content (vbaProject.bin) is not allowed")
                    if p.suffix.lower() in (".exe", ".dll", ".bat", ".cmd", ".ps1", ".sh", ".js", ".vbs"):
                        problems.append(f"archive contains executable/script {info.filename!r}")
                total = sum(i.file_size for i in zf.infolist())
                if total > 20 * max_bytes:
                    problems.append("archive expands beyond the safe ratio (zip bomb guard)")
        except zipfile.BadZipFile:
            problems.append("corrupt archive")
    return ScreeningResult(mime, not problems, problems)


class MalwareScanner:
    name = "abstract"
    real_engine = False

    def scan(self, data: bytes) -> ScanResult:  # pragma: no cover - interface
        raise NotImplementedError

    def health(self) -> dict:
        return {"engine": self.name, "real_engine": self.real_engine, "ok": True}


class SignatureScanner(MalwareScanner):
    name = "builtin-signatures (EICAR + SHA-256 denylist; development fallback)"

    def __init__(self, denylist_sha256: Optional[set[str]] = None) -> None:
        self.denylist = set(denylist_sha256 or ())

    def scan(self, data: bytes) -> ScanResult:
        if EICAR in data:
            return ScanResult(False, self.name, "EICAR-Test-Signature")
        digest = hashlib.sha256(data).hexdigest()
        if digest in self.denylist:
            return ScanResult(False, self.name, f"denylisted-sha256:{digest[:12]}")
        return ScanResult(True, self.name)


class ClamdScanner(MalwareScanner):  # pragma: no cover - needs a clamd daemon
    real_engine = True

    def __init__(self, host: str, port: int = 3310, timeout: float = 30.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.name = f"clamd@{host}:{port}"

    def scan(self, data: bytes) -> ScanResult:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.sendall(b"zINSTREAM\0")
            for i in range(0, len(data), 1 << 16):
                chunk = data[i:i + (1 << 16)]
                s.sendall(struct.pack("!L", len(chunk)) + chunk)
            s.sendall(struct.pack("!L", 0))
            reply = s.recv(4096).decode(errors="replace").strip("\0\n ")
        if reply.endswith("OK"):
            return ScanResult(True, self.name, real_engine=True)
        sig = reply.split(":", 1)[-1].replace("FOUND", "").strip()
        return ScanResult(False, self.name, sig or reply, real_engine=True)

    def health(self) -> dict:
        try:
            with socket.create_connection((self.host, self.port), timeout=3) as s:
                s.sendall(b"zPING\0")
                ok = s.recv(64).startswith(b"PONG")
            return {"engine": self.name, "real_engine": True, "ok": ok}
        except OSError as exc:
            return {"engine": self.name, "real_engine": True, "ok": False, "error": str(exc)}


def make_scanner(settings) -> MalwareScanner:
    if settings.clamd_host:
        return ClamdScanner(settings.clamd_host, settings.clamd_port)
    return SignatureScanner()
