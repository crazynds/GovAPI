"""merge cep_coordenadas into correios_cep

Duas tabelas com a mesma chave viram uma. Elas ficaram separadas enquanto o
edne-correios-loader era dono do esquema e reconstruía `correios_cep` a cada
import -- uma coluna de coordenada colada nela seria destruída. Hoje a lib só
popula uma tabela de scratch e o merge é um upsert nosso que toca apenas as
colunas de endereço, então a coordenada sobrevive e a separação virou
complexidade sem motivo (uma tabela e um índice de PK a mais, e um JOIN em toda
busca por proximidade).

As colunas de endereço passam a ser nullable: existe CEP que só o extrato do
OSM conhece, sem endereço nenhum. Antes elas eram NOT NULL porque a tabela só
tinha uma das metades; agora as duas metades são independentes e ambas
opcionais.

Revision ID: f97c2831fe0c
Revises: 009709135854
Create Date: 2026-08-28 17:32:22.365692

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f97c2831fe0c'
down_revision: Union[str, Sequence[str], None] = '009709135854'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("correios_cep", sa.Column("latitude", sa.Numeric(precision=10, scale=7), nullable=True))
    op.add_column("correios_cep", sa.Column("longitude", sa.Numeric(precision=10, scale=7), nullable=True))
    op.add_column("correios_cep", sa.Column("coord_updated_at", sa.DateTime(), nullable=True))
    op.add_column("correios_cep", sa.Column("coord_source", sa.String(length=30), nullable=True))

    # Antes de trazer os dados: um CEP que só o OSM conhece não tem endereço,
    # e não entraria enquanto essas colunas fossem NOT NULL.
    op.alter_column("correios_cep", "municipio", existing_type=sa.VARCHAR(length=72), nullable=True)
    op.alter_column("correios_cep", "municipio_cod_ibge", existing_type=sa.INTEGER(), nullable=True)
    op.alter_column("correios_cep", "uf", existing_type=sa.VARCHAR(length=2), nullable=True)

    # Coordenada dos CEPs que já têm endereço...
    op.execute("""
        UPDATE correios_cep c SET
            latitude = cc.latitude, longitude = cc.longitude,
            coord_source = cc.source, coord_updated_at = cc.updated_at
        FROM cep_coordenadas cc WHERE cc.cep = c.cep
    """)
    # ...e os que só existiam como coordenada.
    op.execute("""
        INSERT INTO correios_cep (cep, latitude, longitude, coord_source, coord_updated_at)
        SELECT cc.cep, cc.latitude, cc.longitude, cc.source, cc.updated_at
        FROM cep_coordenadas cc
        WHERE NOT EXISTS (SELECT 1 FROM correios_cep c WHERE c.cep = cc.cep)
    """)

    op.drop_table("cep_coordenadas")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        "cep_coordenadas",
        sa.Column("cep", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("latitude", sa.Numeric(precision=10, scale=7), nullable=True),
        sa.Column("longitude", sa.Numeric(precision=10, scale=7), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("cep"),
    )
    op.execute("""
        INSERT INTO cep_coordenadas (cep, latitude, longitude, source, updated_at)
        SELECT cep, latitude, longitude, coord_source,
               coalesce(coord_updated_at, now() AT TIME ZONE 'utc')
        FROM correios_cep WHERE coord_source IS NOT NULL
    """)

    # As linhas sem endereço (só coordenada) não cabem no esquema antigo, que
    # tinha essas colunas NOT NULL -- já foram salvas em cep_coordenadas acima.
    op.execute("DELETE FROM correios_cep WHERE municipio IS NULL OR uf IS NULL OR municipio_cod_ibge IS NULL")

    op.alter_column("correios_cep", "uf", existing_type=sa.VARCHAR(length=2), nullable=False)
    op.alter_column("correios_cep", "municipio_cod_ibge", existing_type=sa.INTEGER(), nullable=False)
    op.alter_column("correios_cep", "municipio", existing_type=sa.VARCHAR(length=72), nullable=False)

    for column in ("coord_source", "coord_updated_at", "longitude", "latitude"):
        op.drop_column("correios_cep", column)
