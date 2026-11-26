"""AOI / vision inspection intake (e.g. YOLOv8 / edge-AI inference output).

The model only *triages*:

* confidence ≥ ``aoi_flag_threshold``       → ``MODEL_FLAGGED`` (presumed defect, needs confirmation)
* ``aoi_ambiguous_threshold`` ≤ c < flag    → ``AMBIGUOUS`` (independent human adjudication required)
* below                                     → ``BELOW_THRESHOLD`` (logged, not acted on)

A unit passes only on an independent human adjudication (``quality_reviewer``, not the importer)
that labels every flagged or ambiguous call and accepts the unit. Model labels are never training
data: only human adjudications are exported, and only double-adjudicated, agreeing labels are
marked ``double_labeled``. Vision models stay out of automated release decisions until a licensed,
double-labeled dataset and an independent holdout calibration pass the dataset policy gates.
"""
from __future__ import annotations

import hashlib

from ..models import AoiDetection, AoiInference, N4DesignIntake

VISION_RELEASE_DECISIONS = "DEFERRED_GATED"


def triage(inf: AoiInference, flag: float, ambiguous: float) -> list[AoiDetection]:
    out = []
    for img in inf.images:
        for i, d in enumerate(img.detections):
            tri = "MODEL_FLAGGED" if d.confidence >= flag else "AMBIGUOUS" if d.confidence >= ambiguous \
                else "BELOW_THRESHOLD"
            did = "AOI-" + hashlib.sha256(f"{img.sha256}:{i}:{d.cls}".encode()).hexdigest()[:16]
            out.append(AoiDetection(detection_id=did, image_sha256=img.sha256, cls=d.cls, confidence=d.confidence,
                                    bbox_xyxy=d.bbox_xyxy, triage=tri))
    return out


def check(inf: AoiInference, images: dict[str, bytes], intake: N4DesignIntake) -> list[tuple[str, str]]:
    problems = []
    for img in inf.images:
        data = images.get(img.path)
        if data is None:
            problems.append(("MISSING_FILE", f"image {img.path} is in the inference output but was not uploaded"))
        elif hashlib.sha256(data).hexdigest() != img.sha256:
            problems.append(("SHA256_MISMATCH", f"image {img.path} does not match its stated SHA-256"))
    for name in sorted(set(images) - {i.path for i in inf.images}):
        problems.append(("UNDECLARED_FILE", f"{name} is not referenced by the inference output"))
    want = (intake.pcb_revision or intake.design_revision).upper()
    if inf.pcb_revision.upper() != want:
        problems.append(("WRONG_PCB_REVISION", f"AOI ran against PCB revision {inf.pcb_revision}; the intake is "
                                               f"locked to {want}"))
    return problems
