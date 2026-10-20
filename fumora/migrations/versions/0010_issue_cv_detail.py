"""issue: cached ComicVine issue record (full tags for ComicVine series)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-20 14:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('issue', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cv_detail', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('issue', schema=None) as batch_op:
        batch_op.drop_column('cv_detail')
