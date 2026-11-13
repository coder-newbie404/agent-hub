"""per-agent auth tokens

Revision ID: a7d3e5c8f1b2
Revises: f8c2d4a6e9b1
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d3e5c8f1b2'
down_revision: Union[str, Sequence[str], None] = 'f8c2d4a6e9b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'agent_tokens',
        sa.Column('token_id', sa.String(length=32), primary_key=True),
        sa.Column('agent_id', sa.String(length=64), nullable=True),
        sa.Column('token_hash', sa.String(length=64), nullable=False,
                  unique=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('agent_tokens')
