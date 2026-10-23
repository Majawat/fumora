"""story arcs followed as reading lists

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-23 14:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'story_arc',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('metron_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('desc', sa.Text(), nullable=True),
        sa.Column('cv_id', sa.Integer(), nullable=True),
        sa.Column('synced', sa.DateTime(timezone=True), nullable=True),
        sa.Column('added', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('metron_id'),
    )
    op.create_table(
        'story_arc_entry',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('arc_id', sa.Integer(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('metron_issue_id', sa.Integer(), nullable=False),
        sa.Column('metron_series_id', sa.Integer(), nullable=True),
        sa.Column('series_name', sa.String(length=255), nullable=False),
        sa.Column('series_year', sa.Integer(), nullable=True),
        sa.Column('volume', sa.Integer(), nullable=True),
        sa.Column('number', sa.String(length=20), nullable=False),
        sa.Column('cover_date', sa.Date(), nullable=True),
        sa.Column('store_date', sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(['arc_id'], ['story_arc.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('arc_id', 'metron_issue_id', name='uq_arc_entry_issue'),
    )
    with op.batch_alter_table('story_arc_entry', schema=None) as batch_op:
        batch_op.create_index('ix_story_arc_entry_arc_id', ['arc_id'], unique=False)
        batch_op.create_index('ix_story_arc_entry_metron_issue_id', ['metron_issue_id'], unique=False)


def downgrade() -> None:
    op.drop_table('story_arc_entry')
    op.drop_table('story_arc')
