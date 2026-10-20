"""series numbering (issues or volumes)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19 20:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('series', schema=None) as batch_op:
        batch_op.add_column(sa.Column('numbering', sa.String(length=10), server_default='auto', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('series', schema=None) as batch_op:
        batch_op.drop_column('numbering')
