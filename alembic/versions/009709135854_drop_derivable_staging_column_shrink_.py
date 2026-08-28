"""drop derivable staging column, shrink socios id

Duas economias em tabelas de dezenas de milhões de linhas:

  * `estabelecimentos_staging.cnpj_basico` era o próprio `cnpj` sem as 4
    últimas posições -- em base 36, uma divisão inteira por 36^4. Guardar 8
    bytes por linha pra repetir o que já está no `cnpj` custava ~500MB nas ~63M
    linhas; o JOIN do build calcula na hora.
  * `socios.id` cabe num Integer (~24M linhas). O TRUNCATE que abre o grupo
    passou a usar RESTART IDENTITY, senão a sequence acumularia import a import
    até estourar os 2,1 bilhões.

Ambas as tabelas são recarregadas do zero a cada import, então nenhum dado é
perdido aqui -- a coluna some e volta preenchida no próximo `import-cnpj`.

Revision ID: 009709135854
Revises: 55106eae8a72
Create Date: 2026-08-28 17:29:29.108294

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '009709135854'
down_revision: Union[str, Sequence[str], None] = '55106eae8a72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('estabelecimentos_staging', 'cnpj_basico')
    op.alter_column('socios', 'id',
               existing_type=sa.BIGINT(),
               type_=sa.Integer(),
               existing_nullable=False,
               autoincrement=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('socios', 'id',
               existing_type=sa.Integer(),
               type_=sa.BIGINT(),
               existing_nullable=False,
               autoincrement=True)
    op.add_column('estabelecimentos_staging', sa.Column('cnpj_basico', sa.BIGINT(), autoincrement=False, nullable=False))
