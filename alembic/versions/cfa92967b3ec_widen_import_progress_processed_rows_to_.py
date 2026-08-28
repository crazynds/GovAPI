"""widen import_progress.processed_rows to bigint

Revision ID: cfa92967b3ec
Revises: d5821abae220
Create Date: 2026-08-28 13:47:33.502200

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cfa92967b3ec'
down_revision: Union[str, Sequence[str], None] = 'd5821abae220'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('import_progress', 'processed_rows',
               existing_type=sa.INTEGER(),
               type_=sa.BigInteger(),
               existing_nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('import_progress', 'processed_rows',
               existing_type=sa.BigInteger(),
               type_=sa.INTEGER(),
               existing_nullable=False)
