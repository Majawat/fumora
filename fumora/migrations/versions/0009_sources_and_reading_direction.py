"""series sources: rejected Metron matches, Metron check date, reading direction

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19 22:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('series', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reading_direction', sa.String(length=3), nullable=True))
        batch_op.add_column(sa.Column('rejected_metron_ids', sa.JSON(), server_default='[]', nullable=False))
        batch_op.add_column(sa.Column('metron_checked', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('series', schema=None) as batch_op:
        batch_op.drop_column('metron_checked')
        batch_op.drop_column('rejected_metron_ids')
        batch_op.drop_column('reading_direction')
