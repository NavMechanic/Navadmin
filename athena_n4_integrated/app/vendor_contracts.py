"""Vendor boundary: what may leave N0 (VendorWorkPackage.v1) and what may come back (VendorResult.v1).

Nothing is transmitted to a vendor connector unless *every* approved input
* is pinned as ``ARTIFACT_ID@REVISION`` in the caller's tenant, is the latest revision and is PASS;
* comes from a source whose SourceRecord.v1 is APPROVED with ``VENDOR_TRANSMISSION`` in
  ``permitted_use``, lists this vendor in ``allowed_vendor_ids`` and is not RESTRICTED;
and the package carries a positive budget cap. Otherwise the package is recorded as BLOCKED with
``N0-VEND-001 VENDOR_TRANSMISSION_UNAPPROVED`` and no connector may pick it up.

Results must reference files that were uploaded through quarantine (``n0://quarantine/<tenant>/<sha>``,
scanned) and carry ``checksum_sha256 = SHA-256("\\n".join(sorted(file SHA-256s)))``.
"""
from __future__ import annotations

from datetime import datetime

from .db import VendorResultRow, WorkPackageRow
from .errors import N0Error
from .models import TransmissionDecision, VendorResult, VendorResultDecision, VendorWorkPackage
from .repository import Repository, dump, new_id
from .rules import Findings, decide, evaluate_rights, normalize_revision, sha256_hex
from .security import Principal
from .storage import StorageError, parse_uri


def result_checksum(file_sha256s: list[str]) -> str:
    return sha256_hex("\n".join(sorted(file_sha256s)).encode())


def authorize_work_package(repo: Repository, principal: Principal, wp: VendorWorkPackage,
                           now: datetime) -> TransmissionDecision:
    if wp.tenant_id != principal.tenant_id:
        raise N0Error(403, "CROSS_TENANT_REFERENCE", "work package belongs to another tenant")
    if repo.get_work_package(wp.tenant_id, wp.work_package_id) is not None:
        raise N0Error(409, "WORK_PACKAGE_EXISTS", f"{wp.work_package_id} already exists; work packages are "
                                                  "immutable")
    f = Findings()
    f.ran("vendor_transmission")
    if wp.budget_cap.amount <= 0:
        f.add("VENDOR_TRANSMISSION_UNAPPROVED", "budget_cap must be positive", "budget_cap")
    authorized: list[str] = []
    for ref in wp.approved_input_ids:
        if "@" not in ref:
            f.add("VENDOR_TRANSMISSION_UNAPPROVED", f"{ref} is not pinned as ARTIFACT_ID@REVISION",
                  "approved_input_ids")
            continue
        aid, raw = ref.split("@", 1)
        rev = normalize_revision(raw) or raw
        row = repo.get_artifact(wp.tenant_id, aid, rev)
        if row is None:
            f.add("VENDOR_TRANSMISSION_UNAPPROVED", f"{aid}@{rev} was not admitted in this tenant",
                  "approved_input_ids")
            continue
        problems = []
        latest = repo.latest_revision(wp.tenant_id, aid)
        if latest is not None and latest.revision_order > row.revision_order:
            problems.append(f"superseded by @{latest.revision}")
        d = repo.latest_decision(wp.tenant_id, aid, rev)
        if d is None or d.decision != "PASS":
            problems.append(f"gateway decision is {d.decision if d else 'missing'}, not PASS")
        source = repo.get_source(wp.tenant_id, row.source_id)
        rights = evaluate_rights(row.canonical["processing_purpose"], row.source_id, source, now, Findings())
        if not rights.vendor_transmission_allowed:
            problems.append(f"source {row.source_id} does not permit VENDOR_TRANSMISSION")
        if source is not None and wp.vendor_id not in source.allowed_vendor_ids:
            problems.append(f"vendor {wp.vendor_id} is not in allowed_vendor_ids of {row.source_id}")
        if source is not None and source.confidentiality == "RESTRICTED":
            problems.append(f"{row.source_id} is RESTRICTED and never leaves N0")
        if problems:
            f.add("VENDOR_TRANSMISSION_UNAPPROVED", f"{aid}@{rev}: " + "; ".join(problems), "approved_input_ids")
        else:
            authorized.append(f"{aid}@{rev}")
    ok = not f.items
    decision = TransmissionDecision(work_package_id=wp.work_package_id, authorized=ok, violated_rules=f.items,
                                    authorized_inputs=authorized if ok else [])
    repo.add_work_package(WorkPackageRow(tenant_id=wp.tenant_id, work_package_id=wp.work_package_id,
                                         vendor_id=wp.vendor_id, status="AUTHORIZED" if ok else "BLOCKED",
                                         record=dump(wp), decision=dump(decision), created_by=principal.principal_id))
    if ok:
        for ref in authorized:
            repo.add_edge(wp.tenant_id, f"artifact:{ref}", f"work_package:{wp.work_package_id}", "INPUT_TO",
                          vendor_id=wp.vendor_id)
    return decision


def ingest_vendor_result(repo: Repository, principal: Principal, vr: VendorResult) -> VendorResultDecision:
    wp = repo.get_work_package(principal.tenant_id, vr.work_package_id)
    if wp is None:
        raise N0Error(404, "WORK_PACKAGE_NOT_FOUND", f"{vr.work_package_id} not found in this tenant")
    if wp.status != "AUTHORIZED":
        raise N0Error(409, "WORK_PACKAGE_NOT_AUTHORIZED", f"{vr.work_package_id} is {wp.status}")
    f = Findings()
    f.ran("vendor_result_integrity")
    hashes, blobs = [], []
    for i, uri in enumerate(vr.native_file_uris):
        where = f"native_file_uris[{i}]"
        try:
            bucket, key = parse_uri(uri)
        except StorageError as exc:
            f.add("BLOB_NOT_CLEARED", str(exc), where)
            continue
        tenant, sha = key.split("/", 1)
        if tenant != principal.tenant_id:
            f.add("CROSS_TENANT_REFERENCE", "vendor file is stored under another tenant", where)
            continue
        blob = repo.find_blob(tenant, sha)
        if blob is None or blob.status not in ("SCAN_PASSED", "RELEASED"):
            f.add("BLOB_NOT_CLEARED", "vendor files must be uploaded through quarantine and scanned first", where)
            continue
        hashes.append(sha)
        blobs.append(blob)
    verified = len(hashes) == len(vr.native_file_uris) and vr.checksum_sha256 == result_checksum(hashes)
    if not verified and len(hashes) == len(vr.native_file_uris):
        f.add("SHA256_MISMATCH", "checksum_sha256 does not match the returned files", "checksum_sha256")
    decision = decide(f.items)
    out = VendorResultDecision(result_id=new_id("VRS"), work_package_id=vr.work_package_id,
                               status="ACCEPTED" if decision == "PASS" else "REJECTED", decision=decision,
                               violated_rules=f.items, checksum_verified=verified,
                               native_file_uris=vr.native_file_uris)
    repo.add_vendor_result(VendorResultRow(result_id=out.result_id, tenant_id=principal.tenant_id,
                                           work_package_id=vr.work_package_id, status=out.status,
                                           record={"result": dump(vr), "decision": dump(out)}))
    t = principal.tenant_id
    repo.add_edge(t, f"work_package:{vr.work_package_id}", f"vendor_result:{out.result_id}", "PRODUCED",
                  status=out.status, tool_version=vr.tool_version)
    for blob in blobs:
        repo.add_edge(t, f"vendor_result:{out.result_id}", f"blob:{blob.blob_id}", "RETURNED", sha256=blob.sha256)
    return out
