"""indexes for cursor pagination, and drop the situacao_cadastral partials

Revision ID: b3d17c4a9e21
Revises: ce6ff13ae5c4
Create Date: 2026-08-29

Duas correções que andam juntas:

1. `ix_establishments_uf` e `ix_establishments_main_cnae` eram parciais em
   `situacao_cadastral = 2`. Mas o filtro de situação é OPCIONAL na API (o
   default inclui todas as situações), e sem `situacao_cadastral = 2` no WHERE
   o Postgres não consegue provar o predicado e descarta o índice inteiro. Na
   prática: `?uf=RR` (a menor UF do país) ia a seq scan sobre 72M linhas e
   estourava o timeout, enquanto `?uf=RR&situacao=ativa` respondia em 0,35s.

2. A busca passou de OFFSET pra keyset (ver app/pagination.py), que ordena por
   (coluna, PK). Sem índice cobrindo esse par, o keyset não tem como fazer o
   seek que é a razão de existir dele.

Os CREATE são CONCURRENTLY: em produção a tabela já está populada e servindo,
e um CREATE INDEX comum pegaria um lock de escrita por vários minutos. O preço
é que cada um roda fora de transação -- daí o autocommit_block.

Numa base cheia (~72M estabelecimentos) isso leva dezenas de minutos e ocupa
alguns GB. No caminho do import não é essa migration que roda: lá os índices
são criados na tabela de shadow antes do swap, sem CONCURRENTLY (é mais rápido
e não tem concorrência), ver DEFERRED_INDEXES em app/importer/pipeline.py.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b3d17c4a9e21'
down_revision: Union[str, Sequence[str], None] = 'ce6ff13ae5c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (nome, tabela, definicao das colunas)
NEW_INDEXES = [
    # Substitui o antigo ix_establishments_uf: `(uf)` sozinho e prefixo deste,
    # entao um indice so serve os dois usos (filtro por UF e a ordenacao
    # default da busca) por um custo de disco parecido.
    ("ix_establishments_uf_confidence", "establishments", "(uf, cellphone_confidence DESC, cnpj DESC)"),
    # Mesma ordenacao, pras buscas sem filtro de UF.
    ("ix_establishments_confidence", "establishments", "(cellphone_confidence DESC, cnpj DESC)"),
    ("ix_establishments_opened_at", "establishments", "(opened_at DESC, cnpj DESC)"),
    ("ix_socios_nome_socio", "socios", "(nome_socio, id)"),
    ("ix_correios_cep_municipio_logradouro", "correios_cep", "(municipio, logradouro, cep)"),
]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # main_cnae vira cheio: o predicado parcial de situacao nao esta no
        # WHERE das buscas. Recriado antes do drop nao da -- o nome colide --,
        # entao vai com nome novo e o antigo cai depois.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_establishments_main_cnae_full "
            "ON establishments (main_cnae)"
        )
        for name, table, columns in NEW_INDEXES:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {columns}")

        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_establishments_main_cnae")
        op.execute("ALTER INDEX ix_establishments_main_cnae_full RENAME TO ix_establishments_main_cnae")
        # `(uf)` agora e prefixo de ix_establishments_uf_confidence.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_establishments_uf")

    # O planner so usa indice novo depois de ter estatistica que justifique --
    # e o RENAME do swap do import deixa a tabela sem nenhuma (era por isso que
    # ela estava sem estatistica em producao). O import agora faz isso sozinho
    # no fim do build; aqui e pra quem so aplicar a migration.
    op.execute("ANALYZE establishments")
    op.execute("ANALYZE socios")
    op.execute("ANALYZE correios_cep")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_establishments_uf "
            "ON establishments (uf) WHERE situacao_cadastral = 2"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_establishments_main_cnae_partial "
            "ON establishments (main_cnae) WHERE situacao_cadastral = 2"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_establishments_main_cnae")
        op.execute("ALTER INDEX ix_establishments_main_cnae_partial RENAME TO ix_establishments_main_cnae")

        for name, _table, _columns in NEW_INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
