"""cep as integer everywhere, with a real fk

`correios_cep` passa a ser gerenciada pelo Alembic. Ela era excluída do
autogenerate porque o edne-correios-loader era dono do esquema e a reconstruía
a cada import; hoje a lib só popula uma tabela de scratch e o merge é um upsert
nosso, então a tabela é nossa e vira um model como qualquer outro.

Com isso, `establishments.cep` ganha FOREIGN KEY de verdade. Faltavam duas
coisas pra ela ser possível, e as duas foram resolvidas antes:

  * CEP que não existe na base dos Correios agora vira NULL (o endereço bruto
    vai pra coluna `address`), então não há linha violando a constraint;
  * o import de CEP virou upsert, então nenhuma linha referenciada é apagada.

Faltava só o tipo: o Postgres exige tipos comparáveis numa FK, e não existe
operador `integer = varchar`. `correios_cep.cep` e `cep_coordenadas.cep` passam
a INTEGER, como `establishments.cep` já era. De quebra economiza 5 bytes por
linha nas duas e o join da busca por proximidade deixa de castar.

Os dados existentes são convertidos com `USING cep::integer` -- são todos 8
dígitos, e a tabela é pequena.

Revision ID: 55106eae8a72
Revises: 0a7f993196e6
Create Date: 2026-08-28 17:25:42.778175

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '55106eae8a72'
down_revision: Union[str, Sequence[str], None] = '0a7f993196e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('correios_cep',
    sa.Column('cep', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('logradouro', sa.String(length=100), nullable=True),
    sa.Column('complemento', sa.String(length=100), nullable=True),
    sa.Column('bairro', sa.String(length=72), nullable=True),
    sa.Column('municipio', sa.String(length=72), nullable=False),
    sa.Column('municipio_cod_ibge', sa.Integer(), nullable=False),
    sa.Column('uf', sa.String(length=2), nullable=False),
    sa.Column('nome', sa.String(length=100), nullable=True),
    sa.PrimaryKeyConstraint('cep')
    )
    # `postgresql_using` explícito: sem ele o Postgres recusa varchar -> integer
    # ("column cannot be cast automatically").
    op.alter_column(
        "cep_coordenadas", "cep",
        existing_type=sa.VARCHAR(length=8), type_=sa.Integer(),
        existing_nullable=False, autoincrement=False,
        postgresql_using="cep::integer",
    )
    op.create_foreign_key(
        "establishments_cep_fkey", "establishments", "correios_cep", ["cep"], ["cep"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("establishments_cep_fkey", "establishments", type_="foreignkey")
    op.alter_column(
        "cep_coordenadas", "cep",
        existing_type=sa.Integer(), type_=sa.VARCHAR(length=8),
        existing_nullable=False, autoincrement=False,
        postgresql_using="lpad(cep::text, 8, '0')",
    )
    op.drop_table('correios_cep')
