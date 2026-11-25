"""Accredited laboratory report intake: a controlled PDF plus a ``athena-lab-report/1`` manifest.

Checks, before any result is used:

* the PDF's SHA-256 equals ``report_sha256`` in the manifest;
* the lab's accreditation number is in this tenant's accepted-lab register, valid on the issue date,
  and its scope covers the cited standards;
* **T12 revision check**: the manifest's ``pcb_revision`` equals the PCB revision of the N4 design
  intake, and the PDF text itself names that revision (``PCB Rev B``, ``Board revision: B``, …)
  and the certificate number. A report that names a different revision is rejected;
* any ``design_artifact`` named in the manifest is one of the intake's N3 design releases.
"""
from __future__ import annotations

import re
from typing import Optional

from ..models import LabReportManifest, N4AcceptedLab, N4DesignIntake

_REV = r"(?:pcb|board|design|hardware|hw)\s*(?:rev(?:ision)?\.?|version)\s*[:#]?\s*([A-Z0-9][A-Z0-9.\-]{0,15})"


def revisions_named(text: str) -> set[str]:
    return {m.group(1).upper().rstrip(".") for m in re.finditer(_REV, text, re.I)}


def check(manifest: LabReportManifest, pdf_sha256: str, pdf_text: str, intake: N4DesignIntake,
          labs: list[N4AcceptedLab]) -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []
    if pdf_sha256 != manifest.report_sha256:
        problems.append(("SHA256_MISMATCH", "the uploaded PDF does not match report_sha256 in the manifest"))
    lab: Optional[N4AcceptedLab] = next((x for x in labs if x.accreditation_number == manifest.lab.accreditation_number),
                                        None)
    if lab is None:
        problems.append(("LAB_NOT_ACCREDITED", f"{manifest.lab.name} ({manifest.lab.accreditation_number}) is not in "
                                               "the accepted-lab register"))
    elif lab.valid_until < manifest.issued_at.date():
        problems.append(("LAB_NOT_ACCREDITED", f"accreditation {lab.accreditation_number} expired {lab.valid_until}"))
    else:
        clauses = {r.standard_clause.split(" cl")[0].strip() for r in manifest.results if r.standard_clause}
        outside = sorted(c for c in clauses if not any(c.startswith(s) or s.startswith(c) for s in lab.scopes))
        if outside:
            problems.append(("LAB_SCOPE_EXCEEDED", f"{outside} are outside the accreditation scope {lab.scopes}"))
    want = (intake.pcb_revision or intake.design_revision).upper()
    if manifest.pcb_revision.upper() != want:
        problems.append(("WRONG_PCB_REVISION", f"the lab report is for PCB revision {manifest.pcb_revision}; the N4 "
                                               f"intake is locked to revision {want}"))
    named = revisions_named(pdf_text)
    if want not in named:
        problems.append(("WRONG_PCB_REVISION", f"the report document does not state PCB revision {want}"
                                               + (f" (it states {sorted(named)})" if named else "")))
    elif named - {want}:
        problems.append(("WRONG_PCB_REVISION", f"the report document also names revisions {sorted(named - {want})}"))
    if manifest.certificate_number not in pdf_text:
        problems.append(("CERTIFICATE_NOT_IN_REPORT", f"certificate {manifest.certificate_number} does not appear in "
                                                      "the report document"))
    if manifest.design_artifact and manifest.design_artifact not in {d.artifact for d in intake.design_refs}:
        problems.append(("WRONG_PCB_REVISION", f"{manifest.design_artifact} is not a design release of this intake"))
    return problems
