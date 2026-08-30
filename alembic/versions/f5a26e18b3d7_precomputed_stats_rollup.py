"""precomputed aggregate table for /establishments/stats

Revision ID: f5a26e18b3d7
Revises: e7b12c9d4a83
Create Date: 2026-08-30

`/establishments/stats` precisava agregar `establishments` a cada request. As
duas agregações que sobraram depois da limpeza -- por porte e por CNAE
principal -- não têm como ficar baratas assim: um `GROUP BY` sobre milhões de
linhas tem que ler todas elas, e o `LIMIT` do top CNAE só corta depois de
agrupar.

Mas essa tabela é reconstruída inteira pelo import e nunca escrita enquanto
está em uso, então não há nada a invalidar: dá pra pré-calcular o agregado uma
vez e responder somando ~1-3M linhas em vez de contar 72M. Ver
models.EstablishmentStats para o grão e o que ele não cobre.

Daqui pra frente é o próprio import que monta e troca o agregado, no mesmo
RENAME atômico da tabela principal (ver _build_final_table). Esta migration
popula uma vez a partir do que já está no banco, pra não ser preciso reimportar
só por causa disso -- é um único GROUP BY sobre a tabela inteira, alguns
minutos, uma vez.
"""
from typing import Sequence, Union

from alembic import op

from app import stats_rollup

# revision identifiers, used by Alembic.
revision: str = 'f5a26e18b3d7'
down_revision: Union[str, Sequence[str], None] = 'e7b12c9d4a83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {stats_rollup.TABLE}")
    op.execute(stats_rollup.create_sql(stats_rollup.TABLE))

    # Popula antes dos índices: num INSERT em massa, criar o índice depois é
    # ordens de magnitude mais rápido do que mantê-lo linha a linha -- mesmo
    # motivo dos DEFERRED_INDEXES do import.
    op.execute(stats_rollup.build_sql(stats_rollup.TABLE, "establishments"))

    for statement in stats_rollup.index_sql(stats_rollup.TABLE):
        op.execute(statement)

    op.execute(f"ANALYZE {stats_rollup.TABLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {stats_rollup.TABLE}")
