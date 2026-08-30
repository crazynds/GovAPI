"""fix ix_establishments_opened_at nulls ordering

Revision ID: c8f2a41b6d09
Revises: b3d17c4a9e21
Create Date: 2026-08-30

`DESC` sozinho é `NULLS FIRST` no Postgres -- tanto no ORDER BY quanto na
definição do índice. O índice criado em b3d17c4a9e21 era `(opened_at DESC,
cnpj DESC)`, mas `opened_at` é nullable, então o ORDER BY que a API monta pede
`opened_at DESC NULLS LAST` (o keyset depende de NULLS LAST). O planner compara
a ordenação pedida com a do índice incluindo esse flag: como não casava, ele
descartava o índice e ordenava o resultado filtrado inteiro.

Só este índice precisa: `cellphone_confidence` e `cnpj` são NOT NULL, e a API
deixou de emitir NULLS LAST pra coluna não-nula (ver app/pagination.py:
order_by_clause), então os outros passaram a casar sem tocar no banco.

Vale a pena reavaliar se os três índices de ordenação ainda pagam o disco que
custam: desde que /establishments passou a ordenar só pela PK por default, eles
só servem quando o cliente manda `sort_by` explicitamente.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c8f2a41b6d09'
down_revision: Union[str, Sequence[str], None] = 'b3d17c4a9e21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NAME = "ix_establishments_opened_at"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {NAME}_nl "
            "ON establishments (opened_at DESC NULLS LAST, cnpj DESC)"
        )
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}")
        op.execute(f"ALTER INDEX {NAME}_nl RENAME TO {NAME}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {NAME}_nf "
            "ON establishments (opened_at DESC, cnpj DESC)"
        )
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}")
        op.execute(f"ALTER INDEX {NAME}_nf RENAME TO {NAME}")
