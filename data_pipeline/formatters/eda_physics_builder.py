"""N3 feature packs: KiCad symbols (``.kicad_sym``), footprints (``.kicad_mod``), STEP models,
CircuitNet feature maps (``.npy``/``.npz``) and PDEBench fields (``.npz``/``.h5``).

Outputs ``formatted/feature_pack.jsonl`` (one structured record per entity) and
``formatted/feature_pack.parquet`` (flattened scalar columns). Geometry is reported in SI metres
alongside the native millimetres; nothing is inferred that the file does not contain.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from ..provenance import record_hash
from .common import FormatError, finish_output, payload_files, prepare_output, provenance_meta

# ----------------------------------------------------------------------------------------------
# S-expressions (KiCad)
# ----------------------------------------------------------------------------------------------
_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse_sexpr(text: str) -> list:
    stack: list[list] = [[]]
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) == 1:
                raise FormatError("unbalanced ')' in S-expression")
            done = stack.pop()
            stack[-1].append(done)
        elif tok.startswith('"'):
            stack[-1].append(_unescape(tok[1:-1]) if "\\" in tok else tok[1:-1])
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise FormatError("unbalanced '(' in S-expression")
    return stack[0]


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def _children(node: list, head: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == head]


def _first(node: list, head: str):
    found = _children(node, head)
    return found[0] if found else None


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return math.nan


def kicad_symbols(text: str, lib: str) -> Iterator[dict]:
    tree = parse_sexpr(text)
    root = next((n for n in tree if isinstance(n, list) and n and n[0] == "kicad_symbol_lib"), None)
    if root is None:
        raise FormatError(f"{lib}: not a kicad_symbol_lib")
    for sym in _children(root, "symbol"):
        name = sym[1] if len(sym) > 1 else "?"
        props = {p[1]: p[2] for p in _children(sym, "property") if len(p) > 2}
        pins = []
        units = 0
        for sub in [sym] + _children(sym, "symbol"):
            sub_pins = _children(sub, "pin")
            units += bool(sub_pins) and sub is not sym
            for pin in sub_pins:
                nm, num = _first(pin, "name"), _first(pin, "number")
                pins.append({"electrical_type": pin[1] if len(pin) > 1 else None,
                             "graphic_style": pin[2] if len(pin) > 2 and isinstance(pin[2], str) else None,
                             "name": nm[1] if nm and len(nm) > 1 else None, "number": num[1] if num and len(num) > 1 else None})
        types = Counter(p["electrical_type"] for p in pins)
        extends = _first(sym, "extends")
        yield {"kind": "kicad_symbol", "library": lib, "name": name, "extends": extends[1] if extends else None,
               "reference": props.get("Reference"), "value": props.get("Value"), "footprint": props.get("Footprint"),
               "datasheet": props.get("Datasheet"), "description": props.get("Description") or props.get("ki_description"),
               "keywords": props.get("ki_keywords"), "pin_count": len(pins), "unit_count": max(units, 1 if pins else 0),
               "power_pin_count": types.get("power_in", 0) + types.get("power_out", 0),
               "electrical_types": dict(types), "pins": pins[:512]}


def kicad_footprint(text: str, name_hint: str) -> dict:
    tree = parse_sexpr(text)
    root = next((n for n in tree if isinstance(n, list) and n and n[0] in ("footprint", "module")), None)
    if root is None:
        raise FormatError(f"{name_hint}: not a KiCad footprint")
    pads, centers = [], []
    for pad in _children(root, "pad"):
        at, size, drill = _first(pad, "at"), _first(pad, "size"), _first(pad, "drill")
        x, y = (_num(at[1]), _num(at[2])) if at and len(at) > 2 else (math.nan, math.nan)
        centers.append((x, y))
        layers = _first(pad, "layers")
        pads.append({"number": pad[1] if len(pad) > 1 else None, "type": pad[2] if len(pad) > 2 else None,
                     "shape": pad[3] if len(pad) > 3 and isinstance(pad[3], str) else None, "x_mm": x, "y_mm": y,
                     "w_mm": _num(size[1]) if size and len(size) > 1 else math.nan,
                     "h_mm": _num(size[2]) if size and len(size) > 2 else math.nan,
                     "drill_mm": _num(next((d for d in drill[1:] if not isinstance(d, list) and d != "oval"), None))
                     if drill else None, "layers": [l for l in layers[1:] if isinstance(l, str)] if layers else []})
    crt = []
    for head in ("fp_line", "fp_rect", "fp_poly", "fp_circle"):
        for el in _children(root, head):
            layer = _first(el, "layer")
            if layer and len(layer) > 1 and "CrtYd" in str(layer[1]):
                for key in ("start", "end", "center"):
                    pt = _first(el, key)
                    if pt and len(pt) > 2:
                        crt.append((_num(pt[1]), _num(pt[2])))
                pts = _first(el, "pts")
                if pts:
                    crt += [(_num(p[1]), _num(p[2])) for p in _children(pts, "xy") if len(p) > 2]
    pts = list(dict.fromkeys(c for c in centers if not math.isnan(c[0])))[:400]
    dists = [math.dist(a, b) for i, a in enumerate(pts) for b in pts[i + 1:]]
    pitch = min((d for d in dists if d > 1e-6), default=math.nan)
    attr = _first(root, "attr")
    bbox = None
    if crt:
        xs, ys = [p[0] for p in crt], [p[1] for p in crt]
        bbox = {"w_mm": max(xs) - min(xs), "h_mm": max(ys) - min(ys)}
    types = Counter(p["type"] for p in pads)
    return {"kind": "kicad_footprint", "name": root[1] if len(root) > 1 else name_hint,
            "attributes": [a for a in attr[1:] if isinstance(a, str)] if attr else [], "pad_count": len(pads),
            "smd_pads": types.get("smd", 0), "tht_pads": types.get("thru_hole", 0), "npth_pads": types.get("np_thru_hole", 0),
            "min_pad_pitch_mm": pitch, "min_pad_pitch_m": pitch / 1000 if not math.isnan(pitch) else math.nan,
            "courtyard_w_mm": bbox["w_mm"] if bbox else None, "courtyard_h_mm": bbox["h_mm"] if bbox else None,
            "courtyard_area_m2": bbox["w_mm"] * bbox["h_mm"] * 1e-6 if bbox else None,
            "models": [m[1] for m in _children(root, "model") if len(m) > 1], "pads": pads[:1024]}


_STEP_ENTITY = re.compile(rb"#\d+\s*=\s*([A-Z_][A-Z0-9_]*)\s*\(")
_STEP_POINT = re.compile(rb"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-0-9.Ee+]+)\s*,\s*([-0-9.Ee+]+)\s*,\s*([-0-9.Ee+]+)\s*\)")


def step_features(data: bytes, name: str) -> dict:
    if not data.lstrip().startswith(b"ISO-10303-21"):
        raise FormatError(f"{name}: not an ISO-10303-21 STEP file")
    header = data[:20000].decode("latin-1", errors="replace")
    schema = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", header)
    fname = re.search(r"FILE_NAME\s*\(\s*'([^']*)'", header)
    counts = Counter(m.group(1).decode() for m in _STEP_ENTITY.finditer(data))
    milli = b"SI_UNIT(.MILLI.,.METRE.)" in data.replace(b" ", b"")
    pts = np.array([[float(a), float(b), float(c)] for a, b, c in _STEP_POINT.findall(data)[:2_000_000]]) \
        if b"CARTESIAN_POINT" in data else np.empty((0, 3))
    scale = 1e-3 if milli else 1.0
    ext = (pts.max(axis=0) - pts.min(axis=0)) * scale if len(pts) else np.array([math.nan] * 3)
    return {"kind": "step_model", "name": name, "schema": schema.group(1) if schema else None,
            "file_name": fname.group(1) if fname else None, "length_unit": "mm" if milli else "m (assumed)",
            "entity_count": sum(counts.values()), "solid_count": counts.get("MANIFOLD_SOLID_BREP", 0),
            "face_count": counts.get("ADVANCED_FACE", 0), "point_count": int(len(pts)),
            "bbox_x_m": float(ext[0]), "bbox_y_m": float(ext[1]), "bbox_z_m": float(ext[2]),
            "top_entities": dict(counts.most_common(8))}


def array_features(arr: np.ndarray, name: str, family: str) -> dict:
    a = np.asarray(arr)
    finite = a[np.isfinite(a)] if np.issubdtype(a.dtype, np.number) else np.array([])
    stats = {"min": float(finite.min()), "max": float(finite.max()), "mean": float(finite.mean()),
             "std": float(finite.std()), "p95": float(np.percentile(finite, 95)),
             "nonzero_fraction": float(np.count_nonzero(finite) / finite.size)} if finite.size else {}
    return {"kind": family, "name": name, "shape": list(a.shape), "dtype": str(a.dtype), "size": int(a.size), **stats}


def _numeric_files(artifact_dir: Path, family: str) -> Iterator[dict]:
    for p in payload_files(artifact_dir, (".npy", ".npz", ".h5", ".hdf5")):
        rel = p.relative_to(Path(artifact_dir) / "artifact").as_posix()
        if p.suffix == ".npy":
            yield array_features(np.load(p, allow_pickle=False, mmap_mode="r"), rel, family)
        elif p.suffix == ".npz":
            with np.load(p, allow_pickle=False) as z:
                for key in z.files:
                    yield array_features(z[key], f"{rel}:{key}", family)
        else:
            try:
                import h5py
            except ImportError:
                yield {"kind": family, "name": rel, "skipped": "h5py not installed"}
                continue
            with h5py.File(p, "r") as h:
                def visit(name, obj):
                    if isinstance(obj, h5py.Dataset) and obj.size <= 50_000_000:
                        rows.append(array_features(obj[()], f"{rel}:{name}", family))
                rows: list[dict] = []
                h.visititems(visit)
                yield from rows


def build(lake, spec, artifact_dir: Path, max_entities: int = 2_000_000) -> dict:
    manifest, out = prepare_output(lake, artifact_dir, spec)
    family = {"SRC-028": "circuitnet_feature_map", "SRC-029": "pdebench_field"}.get(spec.source_id, "numeric_array")
    counts: Counter = Counter()
    hashes: set[str] = set()
    rows: list[dict] = []
    errors: list[str] = []

    def emit(rec: dict) -> None:
        if len(rows) >= max_entities:
            return
        rec["provenance"] = provenance_meta(manifest)
        h = record_hash({k: v for k, v in rec.items() if k != "provenance"})
        rec["record_sha256"] = h
        hashes.add(h)
        counts[rec["kind"]] += 1
        rows.append(rec)

    root = Path(artifact_dir) / "artifact"
    for p in payload_files(artifact_dir, (".kicad_sym",)):
        try:
            for rec in kicad_symbols(p.read_text(encoding="utf-8"), p.relative_to(root).as_posix()):
                emit(rec)
        except FormatError as exc:
            errors.append(str(exc))
    for p in payload_files(artifact_dir, (".kicad_mod",)):
        try:
            emit(kicad_footprint(p.read_text(encoding="utf-8"), p.stem))
        except FormatError as exc:
            errors.append(str(exc))
    for p in payload_files(artifact_dir, (".step", ".stp")):
        try:
            emit(step_features(p.read_bytes(), p.relative_to(root).as_posix()))
        except FormatError as exc:
            errors.append(str(exc))
    for rec in _numeric_files(artifact_dir, family):
        emit(rec)
    if not rows:
        raise FormatError(f"{spec.source_id}: no KiCad/STEP/array content found to build a feature pack"
                          + (f" ({errors[:3]})" if errors else ""))
    with open(out / "feature_pack.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=_jsonable) + "\n")
    _write_parquet(out / "feature_pack.parquet", rows)
    return finish_output(out, manifest, "eda_physics_builder", {**counts, "parse_errors": len(errors)}, hashes,
                         {"parse_errors": errors[:50]})


def _jsonable(x: Any):
    if isinstance(x, float) and math.isnan(x):
        return None
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return float(x)
    return str(x)


def _write_parquet(path: Path, rows: list[dict]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    flat = []
    for r in rows:
        flat.append({k: (json.dumps(v, default=_jsonable) if isinstance(v, (list, dict)) else v)
                     for k, v in r.items() if k != "provenance"})
    keys = sorted({k for r in flat for k in r})
    cols = {}
    for k in keys:
        vals = [r.get(k) for r in flat]
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) or v is None for v in vals):
            cols[k] = pa.array([None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v) for v in vals],
                               pa.float64())
        else:
            cols[k] = pa.array([None if v is None else str(v) for v in vals], pa.string())
    table = pa.table(cols)
    meta = {b"athena": json.dumps(rows[0]["provenance"]).encode()} if rows else {}
    pq.write_table(table.replace_schema_metadata(meta), path)
