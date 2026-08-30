"""drop the sort indexes made dead (and harmful) by always ordering on the PK

Revision ID: d4e93a17f5c2
Revises: c8f2a41b6d09
Create Date: 2026-08-30

/socios/buscar e /enderecos/buscar passaram a ordenar só pela chave primária,
então os índices criados em b3d17c4a9e21 pra sustentar a ordenação antiga não
servem mais pra nada.

`ix_socios_nome_socio` não era só peso morto -- era ativamente nocivo. Com
`ORDER BY nome_socio LIMIT n` e um filtro `nome_socio ILIKE '%x%'` (que um
btree não consegue avaliar), o planner escolhia caminhar esse índice em ordem
alfabética testando linha a linha até juntar a página. O custo virava função de
onde o nome cai no alfabeto: medido em produção, `?nome=abreu` levava 15,8s e
`?nome=zuzu` estourava o timeout -- a mesma consulta, com o mesmo número de
resultados. Antes do índice existir, essa busca respondia em 0,17s.

Nenhum dos dois é usado pelo filtro em si: `ILIKE '%x%'` não é sargável num
btree. Pra busca textual ficar rápida de verdade o caminho é um índice GIN com
pg_trgm, que é outra mudança -- ver o README.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e93a17f5c2'
down_revision: Union[str, Sequence[str], None] = 'c8f2a41b6d09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DROPPED = [
    ("ix_socios_nome_socio", "socios", "(nome_socio, id)"),
    ("ix_correios_cep_municipio_logradouro", "correios_cep", "(municipio, logradouro, cep)"),
    # Existiam só pra sustentar `sort_by`, que não existe mais: nenhuma query
    # da API ordena por essas colunas, e nenhum filtro usa um btree sobre elas.
    ("ix_establishments_confidence", "establishments", "(cellphone_confidence DESC, cnpj DESC)"),
    ("ix_establishments_opened_at", "establishments", "(opened_at DESC NULLS LAST, cnpj DESC)"),
]

# `ix_establishments_uf_confidence` FICA. O sufixo de ordenação virou vestigial,
# mas o prefixo `(uf)` é o que serve o filtro por UF -- de longe o mais usado do
# endpoint. Reconstruí-lo como `(uf)` puro economizaria disco ao custo de outro
# CREATE INDEX de dezenas de minutos sobre 72M linhas; fica pro próximo import,
# que o recria do zero de qualquer forma.


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table, _columns in DROPPED:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, columns in DROPPED:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {columns}")
