"""Parsers for native EDA / CAE files and tool reports. Pure functions over bytes, no execution.

* KiCad 6–9 S-expression files: ``(kicad_sch …)`` / ``(kicad_pcb …)`` with title-block revision,
  ``generator_version``, footprint reference designators (``(property "Reference" …)`` in v8+,
  ``(fp_text reference …)`` in v6/7) and copper layer count.
* KiCad ERC / DRC JSON reports (``kicad-cli sch erc --format json`` / ``pcb drc --format json``).
* BOM CSV as exported by ``kicad-cli sch export bom`` (column names matched case-insensitively).
* ngspice batch logs (``ngspice -b -o``): errors, convergence, ``.meas`` results, analysis temperature.
* STEP (ISO 10303-21) headers, FreeCAD archives, thermal-analysis JSON reports.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import BomItem
from ..scanning import sniff_mime

GERBER_EXT = {".gbr", ".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".gm1", ".gko", ".g1", ".g2"}
NETLIST_EXT = {".cir", ".sp", ".spice", ".net", ".lib"}
GERBER_RE = re.compile(r"\.(gbrjob|g[tb][lospa]|g\d{1,2}|gm\d{1,2}|gko)")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".tif", ".tiff", ".webp"}


@dataclass
class Detected:
    role: str
    native: bool
    fmt: str
    tool: Optional[str] = None
    tool_version: Optional[str] = None
    revision: Optional[str] = None
    problems: list[str] = field(default_factory=list)


def _ext(name: str) -> str:
    n = name.lower()
    i = n.rfind(".")
    return n[i:] if i >= 0 else ""


def _text(data: bytes, limit: int = 2_000_000) -> str:
    return data[:limit].decode("utf-8", errors="replace")


def kicad_meta(text: str) -> dict[str, Optional[str]]:
    gv = re.search(r'\(generator_version\s+"([^"]+)"\)', text)
    fv = re.search(r'^\s*\(kicad_(?:sch|pcb)\s+\(version\s+(\d+)\)', text)
    rev = re.search(r'\(title_block\b.*?\(rev\s+"([^"]*)"\)', text, re.S)
    return {"tool_version": gv.group(1) if gv else (f"file-format {fv.group(1)}" if fv else None),
            "revision": rev.group(1).strip() if rev and rev.group(1).strip() else None}


def pcb_refdes(text: str) -> set[str]:
    refs = set(re.findall(r'\(property\s+"Reference"\s+"([^"]+)"', text))
    refs |= set(re.findall(r'\(fp_text\s+reference\s+"([^"]+)"', text))
    return {r for r in refs if re.fullmatch(r"[A-Za-z]+[0-9]+[A-Za-z0-9_]*", r)}


def pcb_copper_layers(text: str) -> int:
    block = re.search(r"\(layers\b(.*?)\n\s*\)", text, re.S)
    return len(set(re.findall(r'\(\d+\s+"([^"]+\.Cu)"', block.group(1) if block else text)))


def _balanced(text: str, start: int) -> str:
    depth, in_str, i = 0, False, start
    while i < len(text):
        c = text[i]
        if c == '"' and text[i - 1] != "\\":
            in_str = not in_str
        elif not in_str:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return text[start:]


def schematic_symbols(text: str) -> list[dict[str, str]]:
    """Placed symbol instances with their properties. Library definitions (``lib_symbols``) carry no
    ``lib_id`` and no numbered reference, so they are skipped."""
    out = []
    for m in re.finditer(r'\(symbol\s+\(lib_id\s+"[^"]+"\)', text):
        props = dict(re.findall(r'\(property\s+"([^"]+)"\s+"([^"]*)"', _balanced(text, m.start())))
        if re.fullmatch(r"[A-Za-z]+[0-9]+", props.get("Reference", "")):
            out.append(props)
    return out


def step_meta(text: str) -> dict[str, Optional[str]]:
    m = re.search(r"FILE_NAME\s*\((.*?)\)\s*;", text, re.S)
    if not m:
        return {"tool": None, "tool_version": None}
    strings = re.findall(r"'([^']*)'", m.group(1))
    origin = strings[-2] if len(strings) >= 2 else ""
    v = re.search(r"(\d+(?:\.\d+)+)", origin)
    tool = origin.split(" ")[0] if origin else None
    return {"tool": tool or None, "tool_version": v.group(1) if v else None}


def detect(path: str, data: bytes) -> Detected:
    ext, mime = _ext(path), sniff_mime(data)
    text = _text(data, 200_000)
    if ext == ".kicad_sch":
        if mime != "application/x-kicad-schematic":
            return Detected("OTHER", False, "invalid", problems=[f"{path} is not a KiCad schematic"])
        meta = kicad_meta(text)
        return Detected("SCHEMATIC", True, "kicad_sch", "kicad", meta["tool_version"], meta["revision"])
    if ext == ".kicad_pcb":
        if mime != "application/x-kicad-pcb":
            return Detected("OTHER", False, "invalid", problems=[f"{path} is not a KiCad board"])
        meta = kicad_meta(text)
        return Detected("PCB", True, "kicad_pcb", "kicad", meta["tool_version"], meta["revision"])
    if ext == ".kicad_pro":
        return Detected("PROJECT", True, "kicad_pro", "kicad")
    if ext in (".step", ".stp"):
        if not text.lstrip().startswith("ISO-10303-21"):
            return Detected("OTHER", False, "invalid", problems=[f"{path} is not an ISO 10303-21 STEP file"])
        meta = step_meta(text)
        return Detected("GEOMETRY", True, "step", meta["tool"], meta["tool_version"])
    if ext == ".fcstd":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                doc = z.read("Document.xml").decode("utf-8", "replace")
            v = re.search(r'ProgramVersion="([^"]+)"', doc)
            return Detected("GEOMETRY", True, "freecad", "FreeCAD", v.group(1).split(" ")[0] if v else None)
        except (zipfile.BadZipFile, KeyError):
            return Detected("OTHER", False, "invalid", problems=[f"{path} is not a FreeCAD document"])
    if ext in NETLIST_EXT:
        if mime != "text/x-spice":
            return Detected("OTHER", False, "invalid", problems=[f"{path} is not a SPICE netlist"])
        return Detected("NETLIST", True, "spice")
    if ext in GERBER_EXT or GERBER_RE.fullmatch(ext):
        return Detected("GERBER", False, "gerber")
    if ext in (".drl", ".xln"):
        return Detected("DRILL", False, "excellon")
    if ext == ".zip" and "odb" in path.lower():
        return Detected("ODB", False, "odb++")
    if ext == ".json":
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return Detected("OTHER", False, "json-invalid", problems=[f"{path} is not valid JSON"])
        schema = str(obj.get("$schema", "")) if isinstance(obj, dict) else ""
        if "erc" in schema:
            return Detected("ERC_REPORT", False, "kicad-erc-json", "kicad-cli", obj.get("kicad_version"))
        if "drc" in schema:
            return Detected("DRC_REPORT", False, "kicad-drc-json", "kicad-cli", obj.get("kicad_version"))
        if isinstance(obj, dict) and "boundary_conditions" in obj:
            return Detected("THERMAL_REPORT", False, "thermal-json", obj.get("solver"), obj.get("solver_version"))
        return Detected("OTHER", False, "json")
    if ext == ".csv":
        head = text.splitlines()[0].lower() if text else ""
        if re.search(r"\bref", head) and re.search(r"\bq(?:ty|uantity)\b", head):
            return Detected("BOM", False, "bom-csv")
        return Detected("OTHER", False, "csv")
    if ext == ".log":
        v = re.search(r"ngspice-(\d+)", text)
        if v or "Circuit:" in text:
            return Detected("SIM_LOG", False, "ngspice-log", "ngspice", v.group(1) if v else None)
        return Detected("OTHER", False, "log")
    if ext == ".raw":
        return Detected("SIM_RAW", False, "spice-raw", "ngspice")
    if ext == ".pdf":
        return Detected("DOCUMENT", False, "pdf")
    if ext in IMAGE_EXT:
        return Detected("IMAGE", False, "image")
    return Detected("OTHER", False, ext.lstrip(".") or "unknown")


# ----------------------------------------------------------------------------------------------
# KiCad ERC / DRC reports
# ----------------------------------------------------------------------------------------------
@dataclass
class Violation:
    type: str
    severity: str
    description: str
    excluded: bool = False


@dataclass
class KicadReport:
    kind: str
    tool_version: Optional[str]
    violations: list[Violation]
    unconnected: int = 0
    parity: int = 0


def parse_kicad_report(data: bytes) -> KicadReport:
    obj = json.loads(data)
    schema = str(obj.get("$schema", ""))
    kind = "erc" if "erc" in schema else "drc"
    raw = [v for sheet in obj.get("sheets", []) for v in sheet.get("violations", [])] if kind == "erc" \
        else list(obj.get("violations", []))
    vs = [Violation(str(v.get("type", "unknown")), str(v.get("severity", "error")).lower(),
                    str(v.get("description", "")), bool(v.get("excluded", False)) or
                    str(v.get("severity", "")).lower() == "exclusion") for v in raw]
    return KicadReport(kind, obj.get("kicad_version"), vs, len(obj.get("unconnected_items", []) or []),
                       len(obj.get("schematic_parity", []) or []))


# ----------------------------------------------------------------------------------------------
# BOM
# ----------------------------------------------------------------------------------------------
_COLS = {"refdes": ("refs", "reference", "references", "designator", "designators"), "qty": ("qty", "quantity"),
         "mpn": ("mpn", "manufacturer part number", "mfr part number", "mfr. part #", "part number"),
         "manufacturer": ("manufacturer", "mfr", "mfg"), "value": ("value",), "footprint": ("footprint",),
         "description": ("description",), "dnp": ("dnp", "do not populate", "${dnp}"),
         "v_rating": ("v_rating", "voltage rating"), "i_rating": ("i_rating", "current rating"),
         "p_rating": ("p_rating", "power rating")}


def _expand_refs(cell: str) -> list[str]:
    refs = []
    for tok in re.split(r"[,;\s]+", cell.strip()):
        m = re.fullmatch(r"([A-Za-z]+)(\d+)-\1?(\d+)", tok)
        if m:
            refs += [f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)]
        elif tok:
            refs.append(tok)
    return refs


@dataclass
class ParsedBom:
    items: list[BomItem]
    ratings: dict[str, dict[str, float]]
    problems: list[str]


def parse_bom_csv(data: bytes) -> ParsedBom:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    header = {h: h for h in (reader.fieldnames or [])}
    col = {}
    for key, names in _COLS.items():
        col[key] = next((h for h in header if h.strip().lower() in names), None)
    problems, items, ratings = [], [], {}
    if not col["refdes"] or not col["qty"]:
        return ParsedBom([], {}, ["BOM CSV needs reference and quantity columns"])
    for i, row in enumerate(reader, 1):
        refs = _expand_refs(row.get(col["refdes"]) or "")
        if not refs:
            problems.append(f"BOM row {i} has no reference designators")
            continue
        try:
            qty = int(float(row.get(col["qty"]) or 0))
        except ValueError:
            problems.append(f"BOM row {i}: quantity {row.get(col['qty'])!r} is not a number")
            qty = -1
        get = lambda k: (row.get(col[k]) or "").strip() or None if col[k] else None  # noqa: E731
        dnp = (get("dnp") or "").lower() in ("1", "true", "yes", "dnp", "x")
        items.append(BomItem(line=i, mpn=get("mpn"), manufacturer=get("manufacturer"), refdes=refs, qty=qty,
                             value=get("value"), footprint=get("footprint"), description=get("description"), dnp=dnp))
        for k, p in (("v_rating", "v"), ("i_rating", "i"), ("p_rating", "p")):
            v = get(k)
            if v:
                try:
                    for r in refs:
                        ratings.setdefault(r.lower(), {})[p] = float(v)
                except ValueError:
                    problems.append(f"BOM row {i}: {k} {v!r} is not a number")
    return ParsedBom(items, ratings, problems)


# ----------------------------------------------------------------------------------------------
# ngspice batch logs
# ----------------------------------------------------------------------------------------------
_SPICE_ERRORS = re.compile(r"(^\s*error\b.*$|timestep too small|singular matrix|no convergence|"
                           r"simulation interrupted|iteration limit reached|could not find a valid modelname|"
                           r"^\s*\w+\s*=\s*failed\b.*$)", re.I | re.M)


@dataclass
class SpiceLog:
    converged: bool
    errors: list[str]
    measures: dict[str, float]
    temp_c: Optional[float]
    version: Optional[str]


def parse_ngspice_log(data: bytes, exit_code: Optional[int] = None) -> SpiceLog:
    text = _text(data)
    errors = sorted({m.group(0).strip()[:200] for m in _SPICE_ERRORS.finditer(text)})
    measures = {}
    for name, val in re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([-+]?\d[\d.]*(?:[eE][-+]?\d+)?)\b", text, re.M):
        if name.lower() not in ("at", "trig", "targ", "from", "to", "stack"):
            try:
                f = float(val)
                if math.isfinite(f):
                    measures[name.lower()] = f
            except ValueError:
                pass
    t = re.search(r"Doing analysis at TEMP\s*=\s*([-+]?\d[\d.]*)", text)
    v = re.search(r"ngspice-(\d+)", text)
    converged = not errors and (exit_code in (None, 0)) and "Circuit:" in text
    return SpiceLog(converged, errors, measures, float(t.group(1)) if t else None, v.group(1) if v else None)


def parse_thermal_report(data: bytes) -> dict[str, Any]:
    obj = json.loads(data)
    if not isinstance(obj, dict):
        raise ValueError("thermal report must be a JSON object")
    return obj
