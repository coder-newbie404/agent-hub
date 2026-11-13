"""sync: messages.posted_at + sync_state

Revision ID: f8c2d4a6e9b1
Revises: b3f1a9c2d4e5
Create Date: 2026-09-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8c2d4a6e9b1'
down_revision: Union[str, Sequence[str], None] = 'b3f1a9c2d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('messages',
                  sa.Column('posted_at', sa.DateTime(), nullable=True))
    # every pre-sync row was live-posted into its thread as it happened
    op.execute("UPDATE messages SET posted_at = created_at")
    op.create_table(
        'sync_state',
        sa.Column('agent_id', sa.String(length=64), primary_key=True),
        sa.Column('instance', sa.String(length=64), nullable=False,
                  server_default=''),
        sa.Column('cursor', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('sync_state')
    op.drop_column('messages', 'posted_at')
