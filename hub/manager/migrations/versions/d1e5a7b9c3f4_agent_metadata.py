"""agent metadata

Revision ID: d1e5a7b9c3f4
Revises: a7d3e5c8f1b2
Create Date: 2026-10-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd1e5a7b9c3f4'
down_revision: Union[str, Sequence[str], None] = 'a7d3e5c8f1b2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('description', sa.Text(),
                                      nullable=False, server_default=''))
    op.add_column('agents', sa.Column('tools', sa.Text(),
                                      nullable=False, server_default='[]'))
    op.add_column('agents', sa.Column('environment', sa.Text(),
                                      nullable=False, server_default='{}'))


def downgrade() -> None:
    op.drop_column('agents', 'environment')
    op.drop_column('agents', 'tools')
    op.drop_column('agents', 'description')
