"""aggregate by CNAE, and the cellphone-and-email intersection measure

Revision ID: a91f4c7e2b56
Revises: f5a26e18b3d7
Create Date: 2026-08-30

Fecha os dois buracos que sobraram em /establishments/stats -- justamente os da
consulta central de prospecção, `?uf=..&cnae_codes=..&only_with_cellphone=true`,
que era o único recorte que ainda estourava.

1. `with_cellphone_and_email` no agregado existente. Sem a interseção,
   `only_with_cellphone=true` não tem resposta: ele restringe a população, e aí
   `with_email` passa a significar "tem os dois". Somar `with_email` mesmo assim
   daria um número plausível e errado, então esse filtro caía na tabela grande.

2. `establishments_cnae_stats`, uma linha por (CNAE, dimensões), com o código
   contando como principal OU secundário. O agregado antigo tem `main_cnae`,
   mas o filtro do endpoint casa os dois, e medido em produção isso não é
   detalhe: CNAE 4781400 em PR dá 240.771 como principal e 397.369 contando
   secundários -- 39,4% a mais.

Uma empresa aparece num balde por CNAE distinto dela, então os baldes NÃO podem
ser somados entre CNAEs. Dentro de um único `cnae` a soma é exata, e é só assim
que o router usa a tabela: um código por consulta, vários caem na tabela grande.

Daqui pra frente o import monta e troca as duas no mesmo RENAME atômico de
`establishments` (ver _build_final_table). Esta migration popula uma vez do que
já está no banco. O agregado por CNAE é o mais caro dos dois: o LATERAL abre
cada empresa em uma linha por CNAE, então é bem mais que uma varredura simples.
"""
from typing import Sequence, Union

from alembic import op

from app import stats_rollup

# revision identifiers, used by Alembic.
revision: str = 'a91f4c7e2b56'
down_revision: Union[str, Sequence[str], None] = 'f5a26e18b3d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Recriar sai mais barato que ALTER + UPDATE: a coluna nova é NOT NULL e
    # precisaria ser calculada linha a linha de qualquer forma, e a tabela toda
    # sai de um único GROUP BY.
    op.execute(f"DROP TABLE IF EXISTS {stats_rollup.TABLE}")
    op.execute(stats_rollup.create_sql(stats_rollup.TABLE))
    op.execute(stats_rollup.build_sql(stats_rollup.TABLE, "establishments"))
    for statement in stats_rollup.index_sql(stats_rollup.TABLE):
        op.execute(statement)
    op.execute(f"ANALYZE {stats_rollup.TABLE}")

    op.execute(f"DROP TABLE IF EXISTS {stats_rollup.CNAE_TABLE}")
    op.execute(stats_rollup.cnae_create_sql(stats_rollup.CNAE_TABLE))
    op.execute(stats_rollup.cnae_build_sql(stats_rollup.CNAE_TABLE, "establishments"))
    for statement in stats_rollup.cnae_index_sql(stats_rollup.CNAE_TABLE):
        op.execute(statement)
    op.execute(f"ANALYZE {stats_rollup.CNAE_TABLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {stats_rollup.CNAE_TABLE}")
    # Volta o agregado principal sem a coluna de interseção seria pior que
    # inútil (o router atual espera ela), então só recria como está.
    op.execute(f"ALTER TABLE {stats_rollup.TABLE} DROP COLUMN IF EXISTS with_cellphone_and_email")
