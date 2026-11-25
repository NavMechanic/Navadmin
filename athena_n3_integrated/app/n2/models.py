"""N2 contracts: PRD creation request, PRD revision record, PRDRelease (the approval-signed release to N3)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..models import Decision, DesignConstraints, DesignScope, TestLimit

_ID = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{1,127}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PRDCreateRequest(_Strict):
    tenant_id: str = Field(pattern=_ID)
    project_id: str = Field(pattern=_ID)
    source_id: str = Field(pattern=_ID, description="SourceRecord permitting PRODUCT_DEFINITION for N2 output")
    prd_id: str = Field(pattern=_ID)
    product_name: str = Field(min_length=1)
    rfq_artifact_id: str
    rfq_revision: str
    design_scopes: list[DesignScope] = Field(min_length=1)
    design_constraints: DesignConstraints
    test_limits: list[TestLimit] = Field(default_factory=list)


class PRDRecord(_Strict):
    model_config = ConfigDict(extra="forbid", title="PRDRecord.v1")
    prd_id: str
    tenant_id: str
    revision: str
    n0_artifact_id: str
    payload_sha256: str
    n0_decision: Decision
    n0_decision_id: str
    violated_codes: list[str]
    source_rfq: str
    authored_by: str
    created_at: datetime


class PRDReleaseRequest(_Strict):
    reason: str = Field(min_length=5)


class PRDRelease(_Strict):
    model_config = ConfigDict(extra="forbid", title="PRDRelease.v1")
    schema_version: Literal["PRDRelease.v1"] = "PRDRelease.v1"
    release_id: str
    tenant_id: str
    prd_id: str
    prd_revision: str
    n0_artifact_id: str
    payload_sha256: str
    n0_decision_id: str
    manifest_id: str
    manifest_sha256: str
    approval_id: str
    approval_event_hash: str = Field(description="hash-chained ApprovalEvent.v1 — the release signature")
    approved_by: str
    released_at: datetime
    target_nodes: list[str]
    supersedes_release_id: Optional[str] = None


N2_SCHEMAS = {"n2_prd_create_request": PRDCreateRequest, "n2_prd_record": PRDRecord, "n2_prd_release": PRDRelease}
