"""filtro por CNAE + cidade num indice so

Revision ID: a1c4e7f20b13
Revises: dd6590827abd
Create Date: 2026-09-01

Copia `municipality_id` de `establishments` pra `establishment_cnaes` e cria
`(cnae, municipality_id, cnpj)`.

Motivo: o filtro CNAE + municipio nao tinha um indice que servisse aos dois
predicados. `municipality_id` so existia na tabela grande (e sem indice), entao
o melhor plano possivel era um BitmapAnd entre o indice de CNAE aqui e algum
caminho em `establishments` -- e BitmapAnd e bloqueante: monta os dois bitmaps
inteiros antes da primeira linha e devolve o resultado em ordem de pagina
fisica, o que ainda obriga um Sort pro `ORDER BY cnpj` da paginacao. Resultado
pratico: filtrar por cidade saia mais caro que nao filtrar.

Com a coluna copiada, os dois predicados sao igualdade nas duas primeiras
colunas de um indice unico e `cnpj` sai ordenado de graca -- a pagina para na
25a linha. E a mesma decisao que ja valia pra `uf` e `has_cellphone`; ver o
docstring de models.EstablishmentCnae.

A coluna fica NULL nas linhas existentes: quem preenche e o proximo `import
all`, que reconstroi `establishment_cnaes` do zero (ver _build_cnaes_table).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1c4e7f20b13'
down_revision: Union[str, None] = 'f4a91c2d7b83'
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('establishment_cnaes', sa.Column('municipality_id', sa.Integer(), nullable=True))
    op.create_index(
        'ix_establishment_cnaes_cnae_municipality_cnpj',
        'establishment_cnaes', ['cnae', 'municipality_id', 'cnpj'],
    )


def downgrade() -> None:
    op.drop_index('ix_establishment_cnaes_cnae_municipality_cnpj', table_name='establishment_cnaes')
    op.drop_column('establishment_cnaes', 'municipality_id')
