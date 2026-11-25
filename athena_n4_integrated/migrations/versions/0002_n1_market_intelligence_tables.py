"""N1 market-intelligence tables (append-only, revisioned) with database-enforced triggers

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-25 05:17:27.079182
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy of the N1 append-only policy at this revision (migrations must not import moving app code).
N1_APPEND_ONLY_TABLES = ("n1_batches", "n1_observations", "n1_buyer_accounts", "n1_attestations", "n1_reports",
                         "n1_feedback_cases", "n1_connector_previews", "n1_optimizations", "n1_slm_evaluations",
                         "n1_handoffs")


def _append_only_ddl(table: str, dialect: str) -> list[str]:
    if dialect == "postgresql":   # n0_append_only() is created by revision 0001
        return [f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION n0_append_only();"]
    if dialect == "sqlite":
        return [f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;",
                f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"]
    return []


def upgrade() -> None:
    op.create_table('n1_attestations',
    sa.Column('attestation_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('evidence_id', sa.String(length=128), nullable=False),
    sa.Column('evidence_sha256', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('attestation_id')
    )
    with op.batch_alter_table('n1_attestations', schema=None) as batch_op:
        batch_op.create_index('ix_n1_att_evidence', ['tenant_id', 'evidence_id'], unique=False)

    op.create_table('n1_batches',
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('n0_artifact_id', sa.String(length=128), nullable=False),
    sa.Column('n0_revision', sa.String(length=16), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('batch_id', sa.String(length=128), nullable=False),
    sa.Column('source_id', sa.String(length=128), nullable=False),
    sa.Column('synthetic', sa.Boolean(), nullable=False),
    sa.Column('payload_sha256', sa.String(length=64), nullable=False),
    sa.Column('n0_decision_id', sa.String(length=64), nullable=False),
    sa.Column('submitted_by', sa.String(length=128), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('tenant_id', 'n0_artifact_id', 'n0_revision')
    )
    op.create_table('n1_buyer_accounts',
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('account_id', sa.String(length=64), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('tenant_id', 'account_id', 'revision')
    )
    op.create_table('n1_connector_previews',
    sa.Column('preview_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('connector', sa.String(length=32), nullable=False),
    sa.Column('raw_sha256', sa.String(length=64), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('preview_id')
    )
    op.create_table('n1_feedback_cases',
    sa.Column('case_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('target_kind', sa.String(length=16), nullable=False),
    sa.Column('target_id', sa.String(length=128), nullable=False),
    sa.Column('label', sa.String(length=32), nullable=False),
    sa.Column('training_eligible', sa.Boolean(), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('case_id')
    )
    op.create_table('n1_handoffs',
    sa.Column('handoff_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('report_id', sa.String(length=64), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('handoff_id')
    )
    op.create_table('n1_observations',
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('evidence_id', sa.String(length=128), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('evidence_sha256', sa.String(length=64), nullable=False),
    sa.Column('n0_artifact_id', sa.String(length=128), nullable=False),
    sa.Column('n0_revision', sa.String(length=16), nullable=False),
    sa.Column('account_id', sa.String(length=64), nullable=True),
    sa.Column('synthetic', sa.Boolean(), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('tenant_id', 'evidence_id', 'revision')
    )
    with op.batch_alter_table('n1_observations', schema=None) as batch_op:
        batch_op.create_index('ix_n1_obs_account', ['tenant_id', 'account_id'], unique=False)

    op.create_table('n1_optimizations',
    sa.Column('optimization_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('report_id', sa.String(length=64), nullable=True),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('optimization_id')
    )
    op.create_table('n1_reports',
    sa.Column('report_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('scope_key', sa.String(length=64), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('supersedes_report_id', sa.String(length=64), nullable=True),
    sa.Column('report_sha256', sa.String(length=64), nullable=False),
    sa.Column('created_by', sa.String(length=128), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('report_id'),
    sa.UniqueConstraint('tenant_id', 'scope_key', 'revision', name='uq_n1_report_scope_rev')
    )
    op.create_table('n1_slm_evaluations',
    sa.Column('evaluation_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('prompt_revision', sa.String(length=64), nullable=False),
    sa.Column('gold_set_sha256', sa.String(length=64), nullable=False),
    sa.Column('passed', sa.Boolean(), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('evaluation_id')
    )

    dialect = op.get_bind().dialect.name
    for table in N1_APPEND_ONLY_TABLES:
        for stmt in _append_only_ddl(table, dialect):
            op.execute(stmt)


def downgrade() -> None:
    op.drop_table('n1_slm_evaluations')
    op.drop_table('n1_reports')
    op.drop_table('n1_optimizations')
    with op.batch_alter_table('n1_observations', schema=None) as batch_op:
        batch_op.drop_index('ix_n1_obs_account')

    op.drop_table('n1_observations')
    op.drop_table('n1_handoffs')
    op.drop_table('n1_feedback_cases')
    op.drop_table('n1_connector_previews')
    op.drop_table('n1_buyer_accounts')
    op.drop_table('n1_batches')
    with op.batch_alter_table('n1_attestations', schema=None) as batch_op:
        batch_op.drop_index('ix_n1_att_evidence')

    op.drop_table('n1_attestations')
