"""compact storage: colunas numéricas, CNPJ em base 36, staging UNLOGGED

Reconstrói `establishments`, as três tabelas de staging, `socios` e
`import_progress` em vez de converter in-place. Motivos:

  * O CNPJ passa a ser um inteiro em base 36 (ver app/cnpj.py). Não existe
    conversão varchar->bigint em SQL puro que faça isso -- exigiria uma função
    plpgsql só pra migrar dados que são recarregados do zero de qualquer jeito.
  * Um ALTER TYPE sobre ~63M linhas reescreve a tabela inteira e precisa de
    espaço pra duas cópias: o mesmo custo do DROP + reimport, com mais risco.
  * Todas essas tabelas são inteiramente reconstruídas a cada `import-cnpj`
    (staging é truncado no fim, e `establishments` é montada por RENAME
    atômico), então não há dado original a preservar.

ATENÇÃO: `establishments` fica VAZIA até o próximo `import-cnpj` completo
rodar. Nesse intervalo a API responde 200 com `total: 0`.

`municipios` é convertida in-place (~5,5k linhas): só o `receita_code`, que
passa a Integer pro JOIN do build casar tipo sem CAST.

Revision ID: c076b6b91515
Revises: ae61be96739d
Create Date: 2026-08-28 14:53:55.615300

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c076b6b91515'
down_revision: Union[str, Sequence[str], None] = 'ae61be96739d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tabelas reconstruídas do zero (ver docstring do módulo).
REBUILT = (
    "establishments",
    "estabelecimentos_staging",
    "empresas_staging",
    "simples_staging",
    "socios",
    "import_progress",
)

# Colunas de texto livre onde o lz4 paga (nomes de empresa/sócio, e-mail):
# descomprime muito mais rápido que o pglz padrão com razão semelhante, e são
# as colunas que dominam o espaço agora que o resto é numérico.
LZ4_COLUMNS = (
    ("establishments", "company_name"),
    ("establishments", "trade_name"),
    ("establishments", "email"),
    ("estabelecimentos_staging", "nome_fantasia"),
    ("estabelecimentos_staging", "correio_eletronico"),
    ("empresas_staging", "razao_social"),
    ("socios", "nome_socio"),
    ("socios", "nome_representante"),
)


def _set_lz4_compression() -> None:
    """lz4 onde o servidor tiver suporte.

    O postgres:16-alpine do docker-compose pode não ter sido compilado
    `--with-lz4`; sem ele o pglz padrão continua valendo (comprime um pouco
    pior) e não há motivo pra falhar a migration por isso. SAVEPOINT porque um
    erro aqui abortaria a transação inteira da migration.
    """
    conn = op.get_bind()
    for table, column in LZ4_COLUMNS:
        savepoint = conn.begin_nested()
        try:
            conn.execute(sa.text(f'ALTER TABLE {table} ALTER COLUMN "{column}" SET COMPRESSION lz4'))
            savepoint.commit()
        except sa.exc.DatabaseError:
            savepoint.rollback()
            print("lz4 indisponível neste servidor -- mantendo a compressão padrão (pglz).")
            return


def upgrade() -> None:
    """Upgrade schema."""
    for table in REBUILT:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    # Sobra de um build interrompido; senão o CREATE ... LIKE do próximo build
    # herdaria a forma antiga.
    op.execute("DROP TABLE IF EXISTS establishments_new CASCADE")

    # ~5,5k linhas, conversão direta (os códigos da Receita são numéricos).
    op.execute("ALTER TABLE municipios ALTER COLUMN receita_code TYPE integer USING receita_code::integer")

    op.create_table('empresas_staging',
    sa.Column('cnpj_basico', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('porte_empresa', sa.SmallInteger(), nullable=True),
    sa.Column('natureza_juridica', sa.SmallInteger(), nullable=True),
    sa.Column('razao_social', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('cnpj_basico'),
    prefixes=['UNLOGGED']
    )
    op.create_table('estabelecimentos_staging',
    sa.Column('cnpj', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('cnpj_basico', sa.BigInteger(), nullable=False),
    sa.Column('phone', sa.BigInteger(), nullable=True),
    sa.Column('cellphone', sa.BigInteger(), nullable=True),
    sa.Column('cnae_fiscal_principal', sa.Integer(), nullable=True),
    sa.Column('municipio_codigo', sa.Integer(), nullable=True),
    sa.Column('data_inicio_atividade', sa.Date(), nullable=True),
    sa.Column('uf', sa.SmallInteger(), nullable=True),
    sa.Column('situacao_cadastral', sa.SmallInteger(), nullable=True),
    sa.Column('motivo_situacao_cadastral', sa.SmallInteger(), nullable=True),
    sa.Column('cellphone_confidence', sa.SmallInteger(), nullable=False),
    sa.Column('is_headquarters', sa.Boolean(), nullable=False),
    sa.Column('cnae_fiscal_secundaria', postgresql.ARRAY(sa.Integer()), nullable=True),
    sa.Column('nome_fantasia', sa.Text(), nullable=True),
    sa.Column('correio_eletronico', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('cnpj'),
    prefixes=['UNLOGGED']
    )
    op.create_table('import_progress',
    sa.Column('step', sa.String(length=20), nullable=False),
    sa.Column('period', sa.String(length=10), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('group', sa.String(length=20), nullable=True),
    sa.Column('current_file', sa.String(length=60), nullable=True),
    sa.Column('processed_rows', sa.BigInteger(), nullable=False),
    sa.Column('total_bytes', sa.BigInteger(), nullable=True),
    sa.Column('message', sa.String(length=255), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('step')
    )
    op.create_table('import_run',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('period', sa.String(length=10), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('message', sa.String(length=255), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('simples_staging',
    sa.Column('cnpj_basico', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('opcao_simples', sa.Boolean(), nullable=False),
    sa.Column('opcao_mei', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('cnpj_basico'),
    prefixes=['UNLOGGED']
    )
    op.create_table('socios',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('cnpj_basico', sa.BigInteger(), nullable=False),
    sa.Column('cpf_cnpj_socio', sa.BigInteger(), nullable=True),
    sa.Column('representante_legal', sa.BigInteger(), nullable=True),
    sa.Column('data_entrada_sociedade', sa.Date(), nullable=True),
    sa.Column('identificador_socio', sa.SmallInteger(), nullable=True),
    sa.Column('qualificacao_socio', sa.SmallInteger(), nullable=True),
    sa.Column('qualificacao_representante_legal', sa.SmallInteger(), nullable=True),
    sa.Column('pais', sa.SmallInteger(), nullable=True),
    sa.Column('faixa_etaria', sa.SmallInteger(), nullable=True),
    sa.Column('nome_socio', sa.Text(), nullable=False),
    sa.Column('nome_representante', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_socios_cnpj_basico', 'socios', ['cnpj_basico'], unique=False)
    op.create_index('ix_socios_cpf_cnpj_socio', 'socios', ['cpf_cnpj_socio'], unique=False, postgresql_where=sa.text('cpf_cnpj_socio IS NOT NULL'))
    op.create_table('establishments',
    sa.Column('cnpj', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('phone', sa.BigInteger(), nullable=True),
    sa.Column('cellphone', sa.BigInteger(), nullable=True),
    sa.Column('main_cnae', sa.Integer(), nullable=True),
    sa.Column('municipio_id', sa.Integer(), nullable=True),
    sa.Column('opened_at', sa.Date(), nullable=True),
    sa.Column('uf', sa.SmallInteger(), nullable=True),
    sa.Column('company_size', sa.SmallInteger(), nullable=True),
    sa.Column('situacao_cadastral', sa.SmallInteger(), nullable=True),
    sa.Column('natureza_juridica', sa.SmallInteger(), nullable=True),
    sa.Column('motivo_situacao_cadastral', sa.SmallInteger(), nullable=True),
    sa.Column('cellphone_confidence', sa.SmallInteger(), nullable=False),
    sa.Column('is_headquarters', sa.Boolean(), nullable=False),
    sa.Column('is_mei', sa.Boolean(), nullable=False),
    sa.Column('is_simples', sa.Boolean(), nullable=False),
    sa.Column('secondary_cnaes', postgresql.ARRAY(sa.Integer()), nullable=True),
    sa.Column('company_name', sa.Text(), nullable=False),
    sa.Column('trade_name', sa.Text(), nullable=True),
    sa.Column('email', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['municipio_id'], ['municipios.id'], ),
    sa.PrimaryKeyConstraint('cnpj')
    )
    op.create_index('ix_establishments_cellphone', 'establishments', ['cellphone'], unique=False, postgresql_where=sa.text('cellphone IS NOT NULL'))
    op.create_index('ix_establishments_main_cnae', 'establishments', ['main_cnae'], unique=False, postgresql_where=sa.text('situacao_cadastral = 2'))
    op.create_index('ix_establishments_secondary_cnaes', 'establishments', ['secondary_cnaes'], unique=False, postgresql_using='gin', postgresql_where=sa.text('secondary_cnaes IS NOT NULL'))
    op.create_index('ix_establishments_situacao_cadastral', 'establishments', ['situacao_cadastral'], unique=False)
    op.create_index('ix_establishments_uf', 'establishments', ['uf'], unique=False, postgresql_where=sa.text('situacao_cadastral = 2'))

    # Nenhuma dessas tabelas sofre UPDATE depois da carga (establishments é
    # montada por INSERT + swap; staging é COPY + UPSERT e depois truncado),
    # então reservar espaço livre por página pra HOT update só desperdiça.
    for table in ("establishments", "socios"):
        op.execute(f"ALTER TABLE {table} SET (fillfactor = 100)")

    _set_lz4_compression()


def downgrade() -> None:
    """Volta ao schema em texto -- também vazio, pelos mesmos motivos do
    upgrade (não há como reverter a codificação sem os dados originais, que
    vêm de um reimport de qualquer forma)."""
    op.drop_index('ix_establishments_uf', table_name='establishments', postgresql_where=sa.text('situacao_cadastral = 2'))
    op.drop_index('ix_establishments_situacao_cadastral', table_name='establishments')
    op.drop_index('ix_establishments_secondary_cnaes', table_name='establishments', postgresql_using='gin', postgresql_where=sa.text('secondary_cnaes IS NOT NULL'))
    op.drop_index('ix_establishments_main_cnae', table_name='establishments', postgresql_where=sa.text('situacao_cadastral = 2'))
    op.drop_index('ix_establishments_cellphone', table_name='establishments', postgresql_where=sa.text('cellphone IS NOT NULL'))
    op.drop_table('establishments')
    op.drop_index('ix_socios_cpf_cnpj_socio', table_name='socios', postgresql_where=sa.text('cpf_cnpj_socio IS NOT NULL'))
    op.drop_index('ix_socios_cnpj_basico', table_name='socios')
    op.drop_table('socios')
    op.drop_table('simples_staging')
    op.drop_table('import_run')
    op.drop_table('import_progress')
    op.drop_table('estabelecimentos_staging')
    op.drop_table('empresas_staging')

    op.execute("ALTER TABLE municipios ALTER COLUMN receita_code TYPE varchar(16) USING receita_code::text")

    op.create_table(
        "empresas_staging",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cnpj_basico", sa.String(length=8), nullable=False),
        sa.Column("razao_social", sa.String(length=255), nullable=True),
        sa.Column("porte_empresa", sa.String(length=2), nullable=True),
        sa.Column("natureza_juridica", sa.String(length=8), nullable=True),
        sa.Column("source_file", sa.String(length=60), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cnpj_basico", name="uq_empresas_staging_cnpj_basico"),
    )
    op.create_table(
        "simples_staging",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cnpj_basico", sa.String(length=8), nullable=False),
        sa.Column("opcao_simples", sa.String(length=1), nullable=True),
        sa.Column("opcao_mei", sa.String(length=1), nullable=True),
        sa.Column("source_file", sa.String(length=60), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cnpj_basico", name="uq_simples_staging_cnpj_basico"),
    )
    op.create_table(
        "estabelecimentos_staging",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cnpj_basico", sa.String(length=8), nullable=False),
        sa.Column("cnpj_ordem", sa.String(length=4), nullable=False),
        sa.Column("cnpj_dv", sa.String(length=2), nullable=False),
        sa.Column("identificador_matriz_filial", sa.String(length=1), nullable=True),
        sa.Column("nome_fantasia", sa.String(length=255), nullable=True),
        sa.Column("situacao_cadastral", sa.String(length=2), nullable=True),
        sa.Column("motivo_situacao_cadastral", sa.String(length=8), nullable=True),
        sa.Column("data_inicio_atividade", sa.Date(), nullable=True),
        sa.Column("cnae_fiscal_principal", sa.String(length=16), nullable=True),
        sa.Column("cnae_fiscal_secundaria", sa.String(length=2048), nullable=True),
        sa.Column("uf", sa.String(length=2), nullable=True),
        sa.Column("municipio_codigo", sa.String(length=16), nullable=True),
        sa.Column("ddd_1", sa.String(length=4), nullable=True),
        sa.Column("telefone_1", sa.String(length=32), nullable=True),
        sa.Column("correio_eletronico", sa.String(length=120), nullable=True),
        sa.Column("source_file", sa.String(length=60), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cnpj_basico", "cnpj_ordem", "cnpj_dv", name="uq_estabelecimentos_staging_cnpj"),
    )
    op.create_table(
        "socios",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cnpj_basico", sa.String(length=8), nullable=False),
        sa.Column("identificador_socio", sa.String(length=1), nullable=True),
        sa.Column("nome_socio", sa.String(length=255), nullable=False),
        sa.Column("cpf_cnpj_socio", sa.String(length=14), nullable=True),
        sa.Column("qualificacao_socio", sa.String(length=8), nullable=True),
        sa.Column("data_entrada_sociedade", sa.Date(), nullable=True),
        sa.Column("pais", sa.String(length=8), nullable=True),
        sa.Column("representante_legal", sa.String(length=14), nullable=True),
        sa.Column("nome_representante", sa.String(length=255), nullable=True),
        sa.Column("qualificacao_representante_legal", sa.String(length=8), nullable=True),
        sa.Column("faixa_etaria", sa.String(length=1), nullable=True),
        sa.Column("source_file", sa.String(length=60), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_socios_cnpj_basico", "socios", ["cnpj_basico"])
    op.create_index("ix_socios_cpf_cnpj_socio", "socios", ["cpf_cnpj_socio"])
    op.create_table(
        "establishments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cnpj", sa.String(length=14), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("trade_name", sa.String(length=255), nullable=True),
        sa.Column("is_headquarters", sa.Boolean(), nullable=False),
        sa.Column("is_mei", sa.Boolean(), nullable=False),
        sa.Column("is_simples", sa.Boolean(), nullable=False),
        sa.Column("company_size", sa.String(length=2), nullable=True),
        sa.Column("natureza_juridica_code", sa.String(length=8), nullable=True),
        sa.Column("main_cnae_code", sa.String(length=16), nullable=True),
        sa.Column("secondary_cnae_codes", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("municipio_id", sa.Integer(), nullable=True),
        sa.Column("uf", sa.String(length=2), nullable=True),
        sa.Column("email", sa.String(length=120), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("cellphone", sa.String(length=32), nullable=True),
        sa.Column("cellphone_confidence", sa.SmallInteger(), nullable=False),
        sa.Column("opened_at", sa.Date(), nullable=True),
        sa.Column("situacao_cadastral", sa.String(length=2), nullable=True),
        sa.Column("motivo_situacao_cadastral_code", sa.String(length=8), nullable=True),
        sa.ForeignKeyConstraint(["municipio_id"], ["municipios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_establishments_cnpj", "establishments", ["cnpj"], unique=True)
    op.create_index("ix_establishments_main_cnae_code", "establishments", ["main_cnae_code"])
    op.create_index("ix_establishments_uf", "establishments", ["uf"])
    op.create_index("ix_establishments_cellphone", "establishments", ["cellphone"])
    op.create_index("ix_establishments_situacao_cadastral", "establishments", ["situacao_cadastral"])
    op.create_table(
        "import_progress",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(length=10), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("group", sa.String(length=20), nullable=True),
        sa.Column("current_file", sa.String(length=60), nullable=True),
        sa.Column("step", sa.String(length=20), nullable=True),
        sa.Column("processed_rows", sa.BigInteger(), nullable=False),
        sa.Column("message", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
