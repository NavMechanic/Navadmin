"""initial N0 schema: all tables plus database-enforced append-only triggers

Revision ID: 0001
Revises: 
Create Date: 2026-11-24 18:39:23.177102
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy of the append-only policy at this revision (migrations must not import moving app code).
APPEND_ONLY_TABLES = ("audit_events", "approval_events", "decisions", "release_manifests", "model_runs", "feedback",
                      "lineage_edges")
PG_FUNCTION = """
CREATE OR REPLACE FUNCTION n0_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (ATHENA N0 audit invariant)', TG_TABLE_NAME;
END; $$ LANGUAGE plpgsql;
"""


def _append_only_ddl(table: str, dialect: str) -> list[str]:
    if dialect == "postgresql":
        return [f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION n0_append_only();"]
    if dialect == "sqlite":
        return [f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;",
                f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"]
    return []


def upgrade() -> None:
    op.create_table('approval_events',
    sa.Column('approval_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('artifact_id', sa.String(length=160), nullable=False),
    sa.Column('exact_revision', sa.String(length=32), nullable=False),
    sa.Column('scope', sa.String(length=24), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=False),
    sa.Column('approver_id', sa.String(length=128), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('prev_hash', sa.String(length=64), nullable=False),
    sa.Column('event_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('approval_id')
    )
    with op.batch_alter_table('approval_events', schema=None) as batch_op:
        batch_op.create_index('ix_approval_artifact', ['tenant_id', 'artifact_id', 'exact_revision'], unique=False)

    op.create_table('artifacts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('artifact_id', sa.String(length=128), nullable=False),
    sa.Column('revision', sa.String(length=16), nullable=False),
    sa.Column('revision_order', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.String(length=128), nullable=False),
    sa.Column('artifact_type', sa.String(length=32), nullable=False),
    sa.Column('release_stage', sa.String(length=16), nullable=False),
    sa.Column('synthetic', sa.Boolean(), nullable=False),
    sa.Column('source_id', sa.String(length=128), nullable=False),
    sa.Column('source_sha256', sa.String(length=64), nullable=False),
    sa.Column('payload_sha256', sa.String(length=64), nullable=False),
    sa.Column('submitted', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('canonical', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('submitted_by', sa.String(length=128), nullable=False),
    sa.Column('origin_node', sa.String(length=4), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'artifact_id', 'revision', name='uq_artifact_revision')
    )
    with op.batch_alter_table('artifacts', schema=None) as batch_op:
        batch_op.create_index('ix_artifact_sha', ['tenant_id', 'source_sha256'], unique=False)

    op.create_table('audit_events',
    sa.Column('event_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('event_type', sa.String(length=48), nullable=False),
    sa.Column('subject', sa.String(length=256), nullable=False),
    sa.Column('actor_id', sa.String(length=128), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('prev_hash', sa.String(length=64), nullable=False),
    sa.Column('event_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('event_id')
    )
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_events_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('blobs',
    sa.Column('blob_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('source_id', sa.String(length=128), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(length=512), nullable=False),
    sa.Column('declared_mime', sa.String(length=128), nullable=True),
    sa.Column('sniffed_mime', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('scan', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('bucket', sa.String(length=32), nullable=False),
    sa.Column('object_key', sa.String(length=512), nullable=False),
    sa.Column('created_by', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('blob_id'),
    sa.UniqueConstraint('tenant_id', 'sha256', 'kind', name='uq_blob_tenant_sha_kind')
    )
    with op.batch_alter_table('blobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_blobs_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('chain_heads',
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('chain', sa.String(length=16), nullable=False),
    sa.Column('head_hash', sa.String(length=64), nullable=False),
    sa.Column('length', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('tenant_id', 'chain')
    )
    op.create_table('decisions',
    sa.Column('decision_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('artifact_id', sa.String(length=128), nullable=False),
    sa.Column('revision', sa.String(length=16), nullable=False),
    sa.Column('decision', sa.String(length=8), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('validator_version', sa.String(length=64), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('decision_id')
    )
    with op.batch_alter_table('decisions', schema=None) as batch_op:
        batch_op.create_index('ix_decision_artifact', ['tenant_id', 'artifact_id', 'revision', 'seq'], unique=False)

    op.create_table('feedback',
    sa.Column('feedback_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('artifact_id', sa.String(length=128), nullable=False),
    sa.Column('revision', sa.String(length=16), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('training_eligible', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('feedback_id')
    )
    op.create_table('lineage_edges',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('source', sa.String(length=256), nullable=False),
    sa.Column('target', sa.String(length=256), nullable=False),
    sa.Column('relation', sa.String(length=48), nullable=False),
    sa.Column('attrs', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('lineage_edges', schema=None) as batch_op:
        batch_op.create_index('ix_lineage_source', ['tenant_id', 'source'], unique=False)
        batch_op.create_index('ix_lineage_target', ['tenant_id', 'target'], unique=False)

    op.create_table('model_runs',
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('run_id')
    )
    with op.batch_alter_table('model_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_model_runs_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('release_manifests',
    sa.Column('manifest_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('artifact_id', sa.String(length=128), nullable=False),
    sa.Column('revision', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('manifest_sha256', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('manifest_id')
    )
    with op.batch_alter_table('release_manifests', schema=None) as batch_op:
        batch_op.create_index('ix_release_artifact', ['tenant_id', 'artifact_id', 'revision'], unique=False)

    op.create_table('sources',
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('source_id', sa.String(length=128), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('approval_status', sa.String(length=16), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('registered_by', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('tenant_id', 'source_id')
    )
    op.create_table('vendor_results',
    sa.Column('result_id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('work_package_id', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('result_id')
    )
    op.create_table('vendor_work_packages',
    sa.Column('tenant_id', sa.String(length=128), nullable=False),
    sa.Column('work_package_id', sa.String(length=128), nullable=False),
    sa.Column('vendor_id', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('decision', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_by', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('tenant_id', 'work_package_id')
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(PG_FUNCTION)
    for table in APPEND_ONLY_TABLES:
        for stmt in _append_only_ddl(table, dialect):
            op.execute(stmt)


def downgrade() -> None:
    op.drop_table('vendor_work_packages')
    op.drop_table('vendor_results')
    op.drop_table('sources')
    with op.batch_alter_table('release_manifests', schema=None) as batch_op:
        batch_op.drop_index('ix_release_artifact')

    op.drop_table('release_manifests')
    with op.batch_alter_table('model_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_model_runs_tenant_id'))

    op.drop_table('model_runs')
    with op.batch_alter_table('lineage_edges', schema=None) as batch_op:
        batch_op.drop_index('ix_lineage_target')
        batch_op.drop_index('ix_lineage_source')

    op.drop_table('lineage_edges')
    op.drop_table('feedback')
    with op.batch_alter_table('decisions', schema=None) as batch_op:
        batch_op.drop_index('ix_decision_artifact')

    op.drop_table('decisions')
    op.drop_table('chain_heads')
    with op.batch_alter_table('blobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_blobs_tenant_id'))

    op.drop_table('blobs')
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_events_tenant_id'))

    op.drop_table('audit_events')
    with op.batch_alter_table('artifacts', schema=None) as batch_op:
        batch_op.drop_index('ix_artifact_sha')

    op.drop_table('artifacts')
    with op.batch_alter_table('approval_events', schema=None) as batch_op:
        batch_op.drop_index('ix_approval_artifact')

    op.drop_table('approval_events')
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS n0_append_only()")
