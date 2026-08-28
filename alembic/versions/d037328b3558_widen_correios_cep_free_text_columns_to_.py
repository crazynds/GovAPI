"""widen correios_cep free text columns to text

Um import de e-DNE real quebrou com StringDataRightTruncation: um nome de
bairro estourou o VARCHAR(36) que o próprio edne-correios-loader declara pro
schema dele. Não é um caso isolado -- a `logradouro` da tabela unificada é
montada concatenando duas colunas da lib (36 + 100) numa coluna que só tem
100 de largura, então o mesmo estouro pode acontecer de novo, só que mais raro.

O código Python (app/ceps.widen_free_text_columns) já alarga as tabelas
intermediárias da lib e a de scratch antes de cada `.load()`; esta migration
faz o mesmo do lado de cá, senão o INSERT ... SELECT do upsert (que lê da
scratch já alargada) estoura de novo ao gravar em `correios_cep`.

TEXT em vez de VARCHAR(n): sem custo em Postgres (mesma representação em
disco) e é o padrão já usado em Establishment.company_name/trade_name/email
pro mesmo motivo -- texto livre de fonte externa não tem uma largura "certa".

Revision ID: d037328b3558
Revises: f97c2831fe0c
Create Date: 2026-08-28 17:53:25.604122

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd037328b3558'
down_revision: Union[str, Sequence[str], None] = 'f97c2831fe0c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('correios_cep', 'logradouro',
               existing_type=sa.VARCHAR(length=100),
               type_=sa.Text(),
               existing_nullable=True)
    op.alter_column('correios_cep', 'complemento',
               existing_type=sa.VARCHAR(length=100),
               type_=sa.Text(),
               existing_nullable=True)
    op.alter_column('correios_cep', 'bairro',
               existing_type=sa.VARCHAR(length=72),
               type_=sa.Text(),
               existing_nullable=True)
    op.alter_column('correios_cep', 'municipio',
               existing_type=sa.VARCHAR(length=72),
               type_=sa.Text(),
               existing_nullable=True)
    op.alter_column('correios_cep', 'nome',
               existing_type=sa.VARCHAR(length=100),
               type_=sa.Text(),
               existing_nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('correios_cep', 'nome',
               existing_type=sa.Text(),
               type_=sa.VARCHAR(length=100),
               existing_nullable=True)
    op.alter_column('correios_cep', 'municipio',
               existing_type=sa.Text(),
               type_=sa.VARCHAR(length=72),
               existing_nullable=True)
    op.alter_column('correios_cep', 'bairro',
               existing_type=sa.Text(),
               type_=sa.VARCHAR(length=72),
               existing_nullable=True)
    op.alter_column('correios_cep', 'complemento',
               existing_type=sa.Text(),
               type_=sa.VARCHAR(length=100),
               existing_nullable=True)
    op.alter_column('correios_cep', 'logradouro',
               existing_type=sa.Text(),
               type_=sa.VARCHAR(length=100),
               existing_nullable=True)
