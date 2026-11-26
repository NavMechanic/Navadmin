"""N4 vision datasets → YOLOv8 detection layout with ``data.yaml``.

Supported layouts (auto-detected inside the artifact):

* **DeepPCB** (SRC-036) — ``PCBData/…/<id>_test.jpg`` + ``<group>_not/<id>.txt`` boxes
  ``x1,y1,x2,y2,type`` (types 1–6: open, short, mousebite, spur, copper, pin-hole); uses the
  dataset's own ``trainval.txt`` / ``test.txt`` when present.
* **MVTec AD / AD 2** (SRC-038/039) — ``<cat>/test*/<defect>/*.png`` with ``ground_truth`` masks →
  boxes from connected components. **Evaluation only** (``train: null``), written exclusively to
  ``eval_noncommercial/N4`` and stamped CC BY-NC-SA 4.0 / ``commercial_training_allowed: false``.
* **VisA** (SRC-040) — ``split_csv/1cls.csv`` with mask paths.
* **Pascal VOC** (SRC-037) — ``*.xml`` annotations next to the images.

Only TRAIN-CANDIDATE sources get a ``train`` split; every other source is written eval-only.
"""
from __future__ import annotations

import csv
import hashlib
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
import yaml

from ..policy_gates import NONCOMMERCIAL_HARD_BLOCK, PolicyViolation
from ..provenance import sha256_file
from .common import FormatError, finish_output, prepare_output

DEEPPCB_CLASSES = ["open", "short", "mousebite", "spur", "copper", "pin-hole"]
IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")
Sample = tuple[Path, list[tuple[str, float, float, float, float]], Optional[str]]   # image, [(cls,x1,y1,x2,y2)], split


def image_size(path: Path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def mask_boxes(mask: np.ndarray, min_area: int = 4) -> list[tuple[int, int, int, int]]:
    """Bounding boxes ``(x1, y1, x2, y2)`` with exclusive ends (they cover every mask pixel) of the
    8-connected foreground regions (run-length union-find, fast on large masks)."""
    m = np.asarray(mask) > 0
    parent: list[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    boxes: dict[int, list[int]] = {}
    area: dict[int, int] = {}
    prev: list[tuple[int, int, int]] = []
    for y in range(m.shape[0]):
        d = np.diff(np.concatenate(([0], m[y].astype(np.int8), [0])))
        runs = []
        for s, e in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
            lab = None
            for ps, pe, pl in prev:
                if ps <= e and s <= pe:                       # touching incl. diagonals
                    root = find(pl)
                    if lab is None:
                        lab = root
                    elif root != lab:
                        parent[root] = lab
            if lab is None:
                lab = len(parent)
                parent.append(lab)
            runs.append((int(s), int(e), lab))
            b = boxes.setdefault(lab, [int(s), y, int(e) - 1, y])
            b[0], b[2], b[3] = min(b[0], int(s)), max(b[2], int(e) - 1), y
            area[lab] = area.get(lab, 0) + int(e - s)
        prev = runs
    merged: dict[int, list[int]] = {}
    merged_area: dict[int, int] = {}
    for lab, b in boxes.items():
        r = find(lab)
        if r in merged:
            mb = merged[r]
            merged[r] = [min(mb[0], b[0]), min(mb[1], b[1]), max(mb[2], b[2]), max(mb[3], b[3])]
        else:
            merged[r] = list(b)
        merged_area[r] = merged_area.get(r, 0) + area[lab]
    return [(b[0], b[1], b[2] + 1, b[3] + 1) for r, b in sorted(merged.items()) if merged_area[r] >= min_area]


def _hash_split(key: str, train: bool) -> str:
    if not train:
        return "test"
    h = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 100
    return "train" if h < 80 else "val" if h < 90 else "test"


# ----------------------------------------------------------------------------------------------
# layouts
# ----------------------------------------------------------------------------------------------
def deeppcb_samples(root: Path) -> Iterator[Sample]:
    pcb = next((p for p in root.rglob("PCBData") if p.is_dir()), None)
    if pcb is None:
        return
    listed: dict[str, str] = {}
    for split_name, file in (("trainval", "trainval.txt"), ("test", "test.txt")):
        f = pcb / file
        if f.exists():
            for line in f.read_text().split("\n"):
                parts = line.split()
                if len(parts) == 2:
                    listed[parts[1]] = split_name
    for ann in sorted(pcb.rglob("*.txt")):
        if ann.name in ("trainval.txt", "test.txt") or not ann.parent.name.endswith("_not"):
            continue
        img_dir = ann.parent.parent / ann.parent.name[:-4]
        img = next((img_dir / f"{ann.stem}_test{e}" for e in (".jpg", ".png") if (img_dir / f"{ann.stem}_test{e}").exists()), None)
        if img is None:
            continue
        boxes = []
        for line in ann.read_text().splitlines():
            vals = [v for v in line.replace(",", " ").split() if v]
            if len(vals) >= 5:
                x1, y1, x2, y2, t = (float(v) for v in vals[:5])
                if 1 <= int(t) <= 6:
                    boxes.append((DEEPPCB_CLASSES[int(t) - 1], x1, y1, x2, y2))
        rel = ann.relative_to(pcb).as_posix()
        yield img, boxes, listed.get(rel)


def mvtec_samples(root: Path) -> Iterator[Sample]:
    for img in sorted(root.rglob("*")):
        if not img.is_file() or img.suffix.lower() not in IMG_EXT or "_mask" in img.stem:
            continue
        parts = img.relative_to(root).parts                 # never look above the artifact root
        if "ground_truth" in parts:
            continue
        test_idx = next((i for i, p in enumerate(parts) if p.startswith("test")), None)
        if test_idx is None or test_idx == 0:
            continue
        category, defect = parts[test_idx - 1], img.parent.name
        cands = [root.joinpath(*parts[:test_idx]) / "ground_truth" / defect / f"{img.stem}_mask.png",
                 root.joinpath(*parts[:test_idx + 1]) / "ground_truth" / defect / f"{img.stem}_mask.png"]
        mask = next((c for c in cands if c.exists()), None)
        boxes = []
        if defect != "good" and mask is not None:
            from PIL import Image
            with Image.open(mask) as im:
                arr = np.array(im.convert("L"))
            boxes = [(f"{category}/{defect}", *b) for b in mask_boxes(arr)]
        yield img, boxes, "test"


def visa_samples(root: Path) -> Iterator[Sample]:
    csv_path = next(iter(root.rglob("1cls.csv")), None)
    if csv_path is None:
        return
    base = csv_path.parent.parent
    with open(csv_path, newline="") as fh:
        for row in csv.DictReader(fh):
            img = base / row["image"]
            if not img.exists():
                continue
            boxes = []
            if row.get("label") == "anomaly" and row.get("mask"):
                from PIL import Image
                with Image.open(base / row["mask"]) as im:
                    arr = np.array(im.convert("L"))
                boxes = [(f"{row['object']}/anomaly", *b) for b in mask_boxes(arr)]
            yield img, boxes, "train" if row.get("split") == "train" else "test"


def voc_samples(root: Path) -> Iterator[Sample]:
    images = {p.name: p for p in root.rglob("*") if p.suffix.lower() in IMG_EXT}
    for xml in sorted(root.rglob("*.xml")):
        try:
            tree = ET.parse(xml)
        except ET.ParseError:
            continue
        fname = tree.findtext("filename") or ""
        img = images.get(fname) or images.get(Path(fname).name)
        if img is None:
            continue
        boxes = []
        for obj in tree.iter("object"):
            bb = obj.find("bndbox")
            if bb is None:
                continue
            boxes.append((obj.findtext("name") or "defect", *(float(bb.findtext(k)) for k in ("xmin", "ymin", "xmax", "ymax"))))
        yield img, boxes, None


LAYOUTS = {"deeppcb": deeppcb_samples, "mvtec": mvtec_samples, "visa": visa_samples, "voc": voc_samples}


# ----------------------------------------------------------------------------------------------
def build(lake, spec, artifact_dir: Path) -> dict:
    eval_only = spec.intended_use != "TRAIN-CANDIDATE"
    if spec.source_id in NONCOMMERCIAL_HARD_BLOCK and not eval_only:
        raise PolicyViolation("HC-04", f"{spec.source_id} is evaluation-only")
    manifest, out = prepare_output(lake, artifact_dir, spec)
    root = Path(artifact_dir) / "artifact"
    kinds = [spec.record_kind] if spec.record_kind in LAYOUTS else list(LAYOUTS)
    samples: list[Sample] = []
    layout = None
    for k in kinds:
        samples = list(LAYOUTS[k](root))
        if samples:
            layout = k
            break
    if not samples:
        raise FormatError(f"{spec.source_id}: no supported image/annotation layout found ({kinds})")

    names = sorted({b[0] for _, boxes, _ in samples for b in boxes} | (set(DEEPPCB_CLASSES) if layout == "deeppcb" else set()))
    if layout == "deeppcb":
        names = DEEPPCB_CLASSES
    index = {n: i for i, n in enumerate(names)}
    counts = {"train": 0, "val": 0, "test": 0, "boxes": 0, "skipped_boxes": 0}
    hashes: set[str] = set()
    for img, boxes, listed in samples:
        rel = img.relative_to(root).as_posix()
        if eval_only:
            split = "test"
        elif listed == "test":
            split = "test"
        elif listed == "trainval":
            split = "val" if _hash_split(rel, True) == "test" else _hash_split(rel, True)
        else:
            split = listed if listed in ("train", "val", "test") else _hash_split(rel, True)
        w, h = image_size(img)
        stem = hashlib.sha1(rel.encode()).hexdigest()[:10] + "_" + img.stem
        (out / "images" / split).mkdir(parents=True, exist_ok=True)
        (out / "labels" / split).mkdir(parents=True, exist_ok=True)
        shutil.copy2(img, out / "images" / split / f"{stem}{img.suffix.lower()}")
        lines = []
        for cls, x1, y1, x2, y2 in boxes:
            x1, x2 = sorted((max(0.0, x1), min(float(w), x2)))
            y1, y2 = sorted((max(0.0, y1), min(float(h), y2)))
            if x2 - x1 < 1 or y2 - y1 < 1:
                counts["skipped_boxes"] += 1
                continue
            lines.append(f"{index[cls]} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} {(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}")
        (out / "labels" / split / f"{stem}.txt").write_text("\n".join(lines) + ("\n" if lines else ""))
        counts[split] += 1
        counts["boxes"] += len(lines)
        hashes.add(sha256_file(img))
    data = {"path": str(out), "train": None if eval_only else "images/train", "val": "images/test" if eval_only else "images/val",
            "test": "images/test", "nc": len(names), "names": {i: n for i, n in enumerate(names)},
            "athena": {"source_id": spec.source_id, "artifact_sha256": manifest.sha256, "layout": layout,
                       "license_spdx_id": manifest.license_spdx_id, "intended_use": manifest.intended_use,
                       "eval_only": eval_only, "commercial_training_allowed": manifest.commercial_training_allowed,
                       "training_rights_approval": "NOT_REVIEWED (see steward ledger)",
                       "domain_transfer_warning": manifest.domain_transfer_warning}}
    header = ("# EVALUATION ONLY — not for model training.\n" if eval_only else "") + \
             (f"# {manifest.license_spdx_id}: non-commercial; commercial_training_allowed: false\n"
              if spec.source_id in NONCOMMERCIAL_HARD_BLOCK else "")
    (out / "data.yaml").write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return finish_output(out, manifest, "vision_yolo_builder", counts, hashes, {"layout": layout, "eval_only": eval_only,
                                                                                 "classes": names})
