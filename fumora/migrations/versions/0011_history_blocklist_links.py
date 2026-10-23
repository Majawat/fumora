"""blocklist: series/issue links; event: indexes for History, series filled in from the issue

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-23 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('blocklist', schema=None) as batch_op:
        batch_op.add_column(sa.Column('series_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('issue_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_blocklist_series_id_series', 'series', ['series_id'], ['id'], ondelete='SET NULL')
        batch_op.create_foreign_key('fk_blocklist_issue_id_issue', 'issue', ['issue_id'], ['id'], ondelete='SET NULL')
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.create_index('ix_event_series_id', ['series_id'], unique=False)
        batch_op.create_index('ix_event_issue_id', ['issue_id'], unique=False)
    # Events logged with only an issue: give them its series, for the series' History.
    op.execute(
        "UPDATE event SET series_id = (SELECT series_id FROM issue WHERE issue.id = event.issue_id) "
        "WHERE series_id IS NULL AND issue_id IS NOT NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.drop_index('ix_event_issue_id')
        batch_op.drop_index('ix_event_series_id')
    with op.batch_alter_table('blocklist', schema=None) as batch_op:
        batch_op.drop_constraint('fk_blocklist_issue_id_issue', type_='foreignkey')
        batch_op.drop_constraint('fk_blocklist_series_id_series', type_='foreignkey')
        batch_op.drop_column('issue_id')
        batch_op.drop_column('series_id')
