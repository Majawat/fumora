"""add ddl to a saved protocol list

Direct downloads (GetComics, Anna's Archive via Shelfmark) are a new protocol.
An install that saved its protocol order before they existed never chose to
leave them out, so 'ddl' is added at the end.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-19 13:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    value = conn.execute(sa.text("SELECT value FROM setting WHERE key = 'quality.protocols'")).scalar()
    if value and "ddl" not in [v.strip().lower() for v in value.split(",")]:
        conn.execute(
            sa.text("UPDATE setting SET value = :v WHERE key = 'quality.protocols'"),
            {"v": f"{value.rstrip(', ')}, ddl"},
        )


def downgrade() -> None:
    pass
