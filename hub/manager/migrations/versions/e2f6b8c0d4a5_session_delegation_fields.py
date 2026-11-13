"""session delegation fields

Revision ID: e2f6b8c0d4a5
Revises: d1e5a7b9c3f4
Create Date: 2026-10-04

Downgrade leaves ``sessions.owner_id`` nullable: SQLite cannot re-apply
NOT NULL via ALTER COLUMN, and a table rebuild just to re-tighten the
constraint is not worth it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e2f6b8c0d4a5'
down_revision: Union[str, Sequence[str], None] = 'd1e5a7b9c3f4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite has no ALTER COLUMN; batch mode recreates the table.
    with op.batch_alter_table('sessions') as batch_op:
        batch_op.alter_column('owner_id',
                              existing_type=sa.String(length=64),
                              nullable=True)
    op.add_column('sessions', sa.Column('creator_agent_id', sa.String(64),
                                        nullable=True))
    op.add_column('sessions', sa.Column('description', sa.Text(),
                                        nullable=False, server_default=''))
    op.add_column('sessions', sa.Column('last_used_at', sa.DateTime(),
                                        nullable=True))
    op.create_table(
        'session_grants',
        sa.Column('session_id', sa.String(length=32),
                  sa.ForeignKey('sessions.id'), primary_key=True),
        sa.Column('agent_id', sa.String(length=64), primary_key=True),
        sa.Column('level', sa.String(length=8), nullable=False),
        sa.Column('granted_by', sa.String(length=64), nullable=False),
        sa.Column('granted_at', sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('session_grants')
    op.drop_column('sessions', 'last_used_at')
    op.drop_column('sessions', 'description')
    op.drop_column('sessions', 'creator_agent_id')
