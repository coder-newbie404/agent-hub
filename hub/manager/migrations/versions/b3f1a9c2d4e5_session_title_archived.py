"""session title archived

Revision ID: b3f1a9c2d4e5
Revises: c97a618b644a
Create Date: 2026-09-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f1a9c2d4e5'
down_revision: Union[str, Sequence[str], None] = 'c97a618b644a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('sessions',
                  sa.Column('title', sa.String(length=128), nullable=True))
    op.add_column('sessions',
                  sa.Column('archived', sa.Boolean(), nullable=False,
                            server_default=sa.false()))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('sessions', 'archived')
    op.drop_column('sessions', 'title')
