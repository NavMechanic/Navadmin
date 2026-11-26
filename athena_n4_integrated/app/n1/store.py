"""Typed, tenant-scoped queries over the N1 tables. Every write is an insert (append-only)."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..repository import dump
from .db import (N1AccountRow, N1AttestationRow, N1BatchRow, N1FeedbackRow, N1HandoffRow, N1ObservationRow,
                 N1OptimizationRow, N1PreviewRow, N1ReportRow, N1SlmEvaluationRow)
from .models import (ActionOptimizationResult, BuyerAccount, ConnectorPreview, EvidenceAttestation, FeedbackCase,
                     HandoffResult, MarketReport, SlmEvaluation)


class N1Store:
    def __init__(self, session: Session) -> None:
        self.s = session

    def _add(self, row) -> None:
        self.s.add(row)
        self.s.flush()

    # ------------------------------------------------------------------ batches
    def get_batch(self, tenant_id: str, artifact_id: str, revision: str) -> Optional[N1BatchRow]:
        return self.s.get(N1BatchRow, (tenant_id, artifact_id, revision))

    def add_batch(self, row: N1BatchRow) -> None:
        self._add(row)

    def batches(self, tenant_id: str) -> dict[tuple[str, str], N1BatchRow]:
        rows = self.s.scalars(select(N1BatchRow).where(N1BatchRow.tenant_id == tenant_id))
        return {(r.n0_artifact_id, r.n0_revision): r for r in rows}

    # ------------------------------------------------------------------ observations
    def observation_revisions(self, tenant_id: str, evidence_id: str) -> list[N1ObservationRow]:
        return list(self.s.scalars(select(N1ObservationRow).where(N1ObservationRow.tenant_id == tenant_id,
                                                                  N1ObservationRow.evidence_id == evidence_id)
                                   .order_by(N1ObservationRow.revision)))

    def latest_observation(self, tenant_id: str, evidence_id: str) -> Optional[N1ObservationRow]:
        revs = self.observation_revisions(tenant_id, evidence_id)
        return revs[-1] if revs else None

    def latest_observations(self, tenant_id: str) -> list[N1ObservationRow]:
        latest: dict[str, N1ObservationRow] = {}
        for r in self.s.scalars(select(N1ObservationRow).where(N1ObservationRow.tenant_id == tenant_id)
                                .order_by(N1ObservationRow.evidence_id, N1ObservationRow.revision)):
            latest[r.evidence_id] = r
        return list(latest.values())

    def add_observation(self, row: N1ObservationRow) -> None:
        self._add(row)

    # ------------------------------------------------------------------ accounts
    def account_revisions(self, tenant_id: str, account_id: str) -> list[BuyerAccount]:
        rows = self.s.scalars(select(N1AccountRow).where(N1AccountRow.tenant_id == tenant_id,
                                                         N1AccountRow.account_id == account_id)
                              .order_by(N1AccountRow.revision))
        return [BuyerAccount.model_validate(r.record) for r in rows]

    def latest_accounts(self, tenant_id: str) -> dict[str, BuyerAccount]:
        latest: dict[str, BuyerAccount] = {}
        for r in self.s.scalars(select(N1AccountRow).where(N1AccountRow.tenant_id == tenant_id)
                                .order_by(N1AccountRow.account_id, N1AccountRow.revision)):
            latest[r.account_id] = BuyerAccount.model_validate(r.record)
        return latest

    def add_account(self, acc: BuyerAccount) -> None:
        self._add(N1AccountRow(tenant_id=acc.tenant_id, account_id=acc.account_id, revision=acc.revision,
                               record=dump(acc)))

    # ------------------------------------------------------------------ attestations
    def attestations_for(self, tenant_id: str) -> dict[str, EvidenceAttestation]:
        out: dict[str, EvidenceAttestation] = {}
        for r in self.s.scalars(select(N1AttestationRow).where(N1AttestationRow.tenant_id == tenant_id)
                                .order_by(N1AttestationRow.created_at)):
            out[r.evidence_id] = EvidenceAttestation.model_validate(r.record)
        return out

    def add_attestation(self, a: EvidenceAttestation) -> None:
        self._add(N1AttestationRow(attestation_id=a.attestation_id, tenant_id=a.tenant_id, evidence_id=a.evidence_id,
                                   evidence_sha256=a.evidence_sha256, status=a.status, record=dump(a)))

    # ------------------------------------------------------------------ reports
    def latest_report_row(self, tenant_id: str, scope_key: str) -> Optional[N1ReportRow]:
        return self.s.scalars(select(N1ReportRow).where(N1ReportRow.tenant_id == tenant_id,
                                                        N1ReportRow.scope_key == scope_key)
                              .order_by(N1ReportRow.revision.desc())).first()

    def report_row(self, tenant_id: str, report_id: str) -> Optional[N1ReportRow]:
        row = self.s.get(N1ReportRow, report_id)
        return row if row is not None and row.tenant_id == tenant_id else None

    def get_report(self, tenant_id: str, report_id: str) -> Optional[MarketReport]:
        row = self.report_row(tenant_id, report_id)
        return MarketReport.model_validate(row.record) if row else None

    def report_rows(self, tenant_id: str, limit: int = 50) -> list[N1ReportRow]:
        return list(self.s.scalars(select(N1ReportRow).where(N1ReportRow.tenant_id == tenant_id)
                                   .order_by(N1ReportRow.created_at.desc()).limit(limit)))

    def add_report(self, r: MarketReport) -> None:
        self._add(N1ReportRow(report_id=r.report_id, tenant_id=r.tenant_id, scope_key=r.scope_key, revision=r.revision,
                              supersedes_report_id=r.supersedes_report_id, report_sha256=r.report_sha256,
                              created_by=r.generated_by, record=dump(r)))

    # ------------------------------------------------------------------ feedback
    def add_feedback(self, f: FeedbackCase) -> None:
        self._add(N1FeedbackRow(case_id=f.case_id, tenant_id=f.tenant_id, target_kind=f.target_kind,
                                target_id=f.target_id, label=f.label, training_eligible=f.training_eligible,
                                record=dump(f)))

    def labeled_outcomes(self, tenant_id: str) -> int:
        return len(list(self.s.scalars(select(N1FeedbackRow.case_id).where(
            N1FeedbackRow.tenant_id == tenant_id, N1FeedbackRow.training_eligible.is_(True),
            N1FeedbackRow.label.in_(("OUTCOME_WON", "OUTCOME_LOST"))))))

    # ------------------------------------------------------------------ previews / optimizations / slm / handoffs
    def add_preview(self, p: ConnectorPreview) -> None:
        self._add(N1PreviewRow(preview_id=p.preview_id, tenant_id=p.tenant_id, connector=p.connector,
                               raw_sha256=p.raw_sha256, record=dump(p)))

    def get_preview(self, tenant_id: str, preview_id: str) -> Optional[ConnectorPreview]:
        row = self.s.get(N1PreviewRow, preview_id)
        return ConnectorPreview.model_validate(row.record) if row and row.tenant_id == tenant_id else None

    def add_optimization(self, r: ActionOptimizationResult) -> None:
        self._add(N1OptimizationRow(optimization_id=r.optimization_id, tenant_id=r.tenant_id, report_id=r.report_id,
                                    record=dump(r)))

    def get_optimization(self, tenant_id: str, optimization_id: str) -> Optional[ActionOptimizationResult]:
        row = self.s.get(N1OptimizationRow, optimization_id)
        return ActionOptimizationResult.model_validate(row.record) if row and row.tenant_id == tenant_id else None

    def add_slm_evaluation(self, e: SlmEvaluation) -> None:
        self._add(N1SlmEvaluationRow(evaluation_id=e.evaluation_id, tenant_id=e.tenant_id, provider=e.provider,
                                     model=e.model, prompt_revision=e.prompt_revision,
                                     gold_set_sha256=e.gold_set_sha256, passed=e.passed, record=dump(e)))

    def passing_evaluation(self, tenant_id: str, provider: str, model: str, prompt_revision: str,
                           gold_sha: str) -> Optional[SlmEvaluation]:
        row = self.s.scalars(select(N1SlmEvaluationRow).where(
            N1SlmEvaluationRow.tenant_id == tenant_id, N1SlmEvaluationRow.provider == provider,
            N1SlmEvaluationRow.model == model, N1SlmEvaluationRow.prompt_revision == prompt_revision,
            N1SlmEvaluationRow.gold_set_sha256 == gold_sha)
            .order_by(N1SlmEvaluationRow.created_at.desc())).first()
        return SlmEvaluation.model_validate(row.record) if row is not None and row.passed else None

    def add_handoff(self, h: HandoffResult) -> None:
        self._add(N1HandoffRow(handoff_id=h.handoff_id, tenant_id=h.tenant_id, report_id=h.report_id, record=dump(h)))

    def handoffs_for(self, tenant_id: str, report_id: str) -> list[HandoffResult]:
        rows = self.s.scalars(select(N1HandoffRow).where(N1HandoffRow.tenant_id == tenant_id,
                                                         N1HandoffRow.report_id == report_id)
                              .order_by(N1HandoffRow.created_at))
        return [HandoffResult.model_validate(r.record) for r in rows]
