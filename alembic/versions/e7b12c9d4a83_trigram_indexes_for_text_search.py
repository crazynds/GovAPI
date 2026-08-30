"""trigram (pg_trgm) indexes for the ILIKE text searches

Revision ID: e7b12c9d4a83
Revises: d4e93a17f5c2
Create Date: 2026-08-30

Toda busca textual da API é `ILIKE '%termo%'`: `?name=` em /establishments,
`?nome=` em /socios/buscar, `?logradouro=`/`?bairro=`/`?municipio=` em
/enderecos/buscar. Um btree não avalia isso -- o padrão não está ancorado no
início, então não existe faixa contígua pra percorrer -- e o resultado era
varredura completa toda vez que o termo fosse raro. Um GIN de trigramas é a
estrutura que serve: ele indexa os trechos de 3 caracteres, e é isso que torna
`%silva%` uma consulta e não uma varredura.

ORDEM IMPORTA. Os índices de `correios_cep` e `socios` vêm primeiro: são os
baratos (~1M e ~24M linhas) e resolvem os endpoints que estavam sofrendo mais.
Os dois de `establishments` são de longe os mais caros -- GIN de trigramas
sobre 72M razões sociais leva horas pra construir e ocupa dezenas de GB. Se o
disco não comportar, dá pra parar depois do primeiro bloco: cada CREATE é
independente e `IF NOT EXISTS`, então retomar depois é só rodar de novo.

Trigrama precisa de pelo menos 3 caracteres pra funcionar. Busca por 1 ou 2
letras continua caindo em varredura -- vale limitar isso na API se virar
problema.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e7b12c9d4a83'
down_revision: Union[str, Sequence[str], None] = 'd4e93a17f5c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (nome, tabela, coluna, WHERE parcial) -- do mais barato pro mais caro.
TRIGRAM_INDEXES = [
    ("ix_correios_cep_logradouro_trgm", "correios_cep", "logradouro", None),
    ("ix_correios_cep_bairro_trgm", "correios_cep", "bairro", None),
    ("ix_correios_cep_municipio_trgm", "correios_cep", "municipio", None),
    ("ix_socios_nome_socio_trgm", "socios", "nome_socio", None),
    ("ix_establishments_company_name_trgm", "establishments", "company_name", None),
    # Parcial: `trade_name` é nullable e a maioria das empresas não tem nome
    # fantasia, então indexar as linhas nulas seria só disco.
    ("ix_establishments_trade_name_trgm", "establishments", "trade_name", "trade_name IS NOT NULL"),
]


def upgrade() -> None:
    # Fora do autocommit_block: é DDL comum e barata, e se falhar aqui é melhor
    # falhar antes de gastar horas construindo índice.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    with op.get_context().autocommit_block():
        for name, table, column, where in TRIGRAM_INDEXES:
            where_sql = f" WHERE {where}" if where else ""
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                f"ON {table} USING gin ({column} gin_trgm_ops){where_sql}"
            )

    op.execute("ANALYZE correios_cep")
    op.execute("ANALYZE socios")
    op.execute("ANALYZE establishments")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table, _column, _where in TRIGRAM_INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    # pg_trgm fica: dropar uma extensão que outra coisa pode ter passado a usar
    # é mais arriscado do que deixar.
