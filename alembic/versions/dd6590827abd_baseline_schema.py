"""schema inteiro, do zero

Revision ID: dd6590827abd
Revises:
Create Date: 2026-08-31

Baseline: substitui as 21 migrations anteriores por uma so. Elas contavam a
historia de um banco que nao existe mais -- este schema e criado num banco
vazio e populado por `import all`, entao nao ha nada pra migrar
incrementalmente. Migrations novas voltam a ser diffs a partir daqui.

O que mudou junto do squash (e foi a razao dele): o par
`establishments.main_cnae` + `secondary_cnaes integer[]` deixou de ser o
caminho de busca do filtro `?cnae_codes=`. O array saiu, e a relacao
empresa-CNAE virou a tabela `establishment_cnaes` -- uma linha por (empresa,
CNAE), com `is_main` marcando o principal. `main_cnae` continua em
`establishments` porque e dimensao do agregado de /stats e sai em toda
resposta. Ver models.EstablishmentCnae pro motivo (o OR entre um btree e um
GIN nao produzia saida ordenada por cnpj, e a busca caia numa varredura das
63M linhas).

Nenhum indice aqui e criado CONCURRENTLY: o banco esta vazio quando esta
migration roda. As tabelas grandes (`establishments`, `establishment_cnaes`)
sao recriadas do zero pelo import, com os indices adiados pro fim do bulk load
(ver DEFERRED_INDEXES/CNAES_DEFERRED_INDEXES em app/importer/pipeline.py) -- o
que esta migration cria delas e so a forma, pra tabela existir antes do
primeiro import.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'dd6590827abd'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Antes de qualquer indice: os de trigrama (`?name=`, busca de CEP por
    # texto, `?nome=` de socios) sao `USING gin (... gin_trgm_ops)`, e esse
    # operator class vem da extensao. Sem ela o CREATE INDEX falha.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table('cnaes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_cnaes_code'), 'cnaes', ['code'], unique=True)
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
    sa.Column('phone', sa.BigInteger(), nullable=True),
    sa.Column('cellphone', sa.BigInteger(), nullable=True),
    sa.Column('cnae_fiscal_principal', sa.Integer(), nullable=True),
    sa.Column('municipio_codigo', sa.Integer(), nullable=True),
    sa.Column('cep', sa.Integer(), nullable=True),
    sa.Column('data_inicio_atividade', sa.Date(), nullable=True),
    sa.Column('uf', sa.SmallInteger(), nullable=True),
    sa.Column('situacao_cadastral', sa.SmallInteger(), nullable=True),
    sa.Column('motivo_situacao_cadastral', sa.SmallInteger(), nullable=True),
    sa.Column('cellphone_confidence', sa.SmallInteger(), nullable=False),
    sa.Column('is_headquarters', sa.Boolean(), nullable=False),
    sa.Column('cnae_fiscal_secundaria', postgresql.ARRAY(sa.Integer()), nullable=True),
    sa.Column('nome_fantasia', sa.Text(), nullable=True),
    sa.Column('correio_eletronico', sa.Text(), nullable=True),
    sa.Column('logradouro', sa.Text(), nullable=True),
    sa.Column('numero', sa.Text(), nullable=True),
    sa.Column('complemento', sa.Text(), nullable=True),
    sa.Column('bairro', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('cnpj'),
    prefixes=['UNLOGGED']
    )
    op.create_table('establishment_cnaes',
    sa.Column('cnpj', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('cnae', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('uf', sa.SmallInteger(), nullable=True),
    sa.Column('is_main', sa.Boolean(), nullable=False),
    sa.Column('has_cellphone', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('cnpj', 'cnae')
    )
    op.create_index('ix_establishment_cnaes_cellphone', 'establishment_cnaes', ['cnae', 'uf', 'cnpj'], unique=False, postgresql_where=sa.text('has_cellphone'))
    op.create_index('ix_establishment_cnaes_cnae_uf_cnpj', 'establishment_cnaes', ['cnae', 'uf', 'cnpj'], unique=False)
    op.create_table('establishments_cnae_stats',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cnae', sa.Integer(), nullable=False),
    sa.Column('uf', sa.SmallInteger(), nullable=True),
    sa.Column('situacao_cadastral', sa.SmallInteger(), nullable=True),
    sa.Column('company_size', sa.SmallInteger(), nullable=True),
    sa.Column('is_mei', sa.Boolean(), nullable=False),
    sa.Column('is_simples', sa.Boolean(), nullable=False),
    sa.Column('is_headquarters', sa.Boolean(), nullable=False),
    sa.Column('total', sa.BigInteger(), nullable=False),
    sa.Column('with_cellphone', sa.BigInteger(), nullable=False),
    sa.Column('with_email', sa.BigInteger(), nullable=False),
    sa.Column('with_phone', sa.BigInteger(), nullable=False),
    sa.Column('with_cellphone_and_email', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_establishments_cnae_stats_cnae_uf', 'establishments_cnae_stats', ['cnae', 'uf'], unique=False)
    op.create_table('establishments_stats',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uf', sa.SmallInteger(), nullable=True),
    sa.Column('situacao_cadastral', sa.SmallInteger(), nullable=True),
    sa.Column('company_size', sa.SmallInteger(), nullable=True),
    sa.Column('main_cnae', sa.Integer(), nullable=True),
    sa.Column('is_mei', sa.Boolean(), nullable=False),
    sa.Column('is_simples', sa.Boolean(), nullable=False),
    sa.Column('is_headquarters', sa.Boolean(), nullable=False),
    sa.Column('total', sa.BigInteger(), nullable=False),
    sa.Column('with_cellphone', sa.BigInteger(), nullable=False),
    sa.Column('with_email', sa.BigInteger(), nullable=False),
    sa.Column('with_phone', sa.BigInteger(), nullable=False),
    sa.Column('with_cellphone_and_email', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_establishments_stats_main_cnae', 'establishments_stats', ['main_cnae'], unique=False)
    op.create_index('ix_establishments_stats_uf', 'establishments_stats', ['uf'], unique=False)
    op.create_table('import_all_run',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('municipios', sa.String(length=20), nullable=False),
    sa.Column('ceps', sa.String(length=20), nullable=False),
    sa.Column('ceps_osm', sa.String(length=20), nullable=False),
    sa.Column('cnpj', sa.String(length=20), nullable=False),
    sa.Column('ibge', sa.String(length=20), nullable=False),
    sa.Column('municipios_geo', sa.String(length=20), nullable=False),
    sa.Column('message', sa.String(length=255), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('import_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('period', sa.String(length=10), nullable=False),
    sa.Column('filename', sa.String(length=60), nullable=False),
    sa.Column('rows_imported', sa.Integer(), nullable=False),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('period', 'filename', name='uq_import_log_period_filename')
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
    op.create_table('motivos_situacao_cadastral',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=8), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_motivos_situacao_cadastral_code'), 'motivos_situacao_cadastral', ['code'], unique=True)
    op.create_table('municipios',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('receita_code', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('uf', sa.String(length=2), nullable=True),
    sa.Column('ibge_code', sa.Integer(), nullable=True),
    sa.Column('population', sa.BigInteger(), nullable=True),
    sa.Column('area_km2', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('latitude', sa.Numeric(precision=10, scale=7), nullable=True),
    sa.Column('longitude', sa.Numeric(precision=10, scale=7), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_municipios_ibge_code'), 'municipios', ['ibge_code'], unique=True)
    op.create_index(op.f('ix_municipios_receita_code'), 'municipios', ['receita_code'], unique=True)
    op.create_table('naturezas_juridicas',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=8), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_naturezas_juridicas_code'), 'naturezas_juridicas', ['code'], unique=True)
    op.create_table('paises',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=8), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_paises_code'), 'paises', ['code'], unique=True)
    op.create_table('qualificacoes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=8), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_qualificacoes_code'), 'qualificacoes', ['code'], unique=True)
    op.create_table('simples_staging',
    sa.Column('cnpj_basico', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('opcao_simples', sa.Boolean(), nullable=False),
    sa.Column('opcao_mei', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('cnpj_basico'),
    prefixes=['UNLOGGED']
    )
    op.create_table('socios',
    sa.Column('id', sa.Integer(), nullable=False),
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
    op.create_index('ix_socios_nome_socio_trgm', 'socios', ['nome_socio'], unique=False, postgresql_using='gin', postgresql_ops={'nome_socio': 'gin_trgm_ops'})
    op.create_table('correios_cep',
    sa.Column('cep', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('municipio_cod_ibge', sa.Integer(), nullable=True),
    sa.Column('latitude', sa.Numeric(precision=10, scale=7), nullable=True),
    sa.Column('longitude', sa.Numeric(precision=10, scale=7), nullable=True),
    sa.Column('coord_updated_at', sa.DateTime(), nullable=True),
    sa.Column('uf', sa.String(length=2), nullable=True),
    sa.Column('logradouro', sa.Text(), nullable=True),
    sa.Column('complemento', sa.Text(), nullable=True),
    sa.Column('bairro', sa.Text(), nullable=True),
    sa.Column('municipio', sa.Text(), nullable=True),
    sa.Column('nome', sa.Text(), nullable=True),
    sa.Column('coord_source', sa.String(length=30), nullable=True),
    sa.ForeignKeyConstraint(['municipio_cod_ibge'], ['municipios.ibge_code'], ),
    sa.PrimaryKeyConstraint('cep')
    )
    op.create_index('ix_correios_cep_bairro_trgm', 'correios_cep', ['bairro'], unique=False, postgresql_using='gin', postgresql_ops={'bairro': 'gin_trgm_ops'})
    op.create_index('ix_correios_cep_logradouro_trgm', 'correios_cep', ['logradouro'], unique=False, postgresql_using='gin', postgresql_ops={'logradouro': 'gin_trgm_ops'})
    op.create_index('ix_correios_cep_municipio_trgm', 'correios_cep', ['municipio'], unique=False, postgresql_using='gin', postgresql_ops={'municipio': 'gin_trgm_ops'})
    op.create_table('establishments',
    sa.Column('cnpj', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('phone', sa.BigInteger(), nullable=True),
    sa.Column('cellphone', sa.BigInteger(), nullable=True),
    sa.Column('main_cnae', sa.Integer(), nullable=True),
    sa.Column('municipio_id', sa.Integer(), nullable=True),
    sa.Column('cep', sa.Integer(), nullable=True),
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
    sa.Column('company_name', sa.Text(), nullable=False),
    sa.Column('trade_name', sa.Text(), nullable=True),
    sa.Column('email', sa.Text(), nullable=True),
    sa.Column('address_number', sa.Text(), nullable=True),
    sa.Column('address_complement', sa.Text(), nullable=True),
    sa.Column('street', sa.Text(), nullable=True),
    sa.Column('district', sa.Text(), nullable=True),
    sa.Column('address', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['cep'], ['correios_cep.cep'], ),
    sa.ForeignKeyConstraint(['municipio_id'], ['municipios.id'], ),
    sa.PrimaryKeyConstraint('cnpj')
    )
    op.create_index('ix_establishments_cellphone', 'establishments', ['cellphone'], unique=False, postgresql_where=sa.text('cellphone IS NOT NULL'))
    op.create_index('ix_establishments_cep', 'establishments', ['cep'], unique=False, postgresql_where=sa.text('cep IS NOT NULL'))
    op.create_index('ix_establishments_company_name_trgm', 'establishments', ['company_name'], unique=False, postgresql_using='gin', postgresql_ops={'company_name': 'gin_trgm_ops'})
    op.create_index('ix_establishments_main_cnae', 'establishments', ['main_cnae'], unique=False)
    op.create_index('ix_establishments_situacao_cadastral', 'establishments', ['situacao_cadastral'], unique=False)
    op.create_index('ix_establishments_trade_name_trgm', 'establishments', ['trade_name'], unique=False, postgresql_using='gin', postgresql_ops={'trade_name': 'gin_trgm_ops'}, postgresql_where=sa.text('trade_name IS NOT NULL'))
    op.create_index('ix_establishments_uf_confidence', 'establishments', ['uf', sa.text('cellphone_confidence DESC'), sa.text('cnpj DESC')], unique=False)


def downgrade() -> None:
    op.drop_index('ix_establishments_uf_confidence', table_name='establishments')
    op.drop_index('ix_establishments_trade_name_trgm', table_name='establishments', postgresql_using='gin', postgresql_ops={'trade_name': 'gin_trgm_ops'}, postgresql_where=sa.text('trade_name IS NOT NULL'))
    op.drop_index('ix_establishments_situacao_cadastral', table_name='establishments')
    op.drop_index('ix_establishments_main_cnae', table_name='establishments')
    op.drop_index('ix_establishments_company_name_trgm', table_name='establishments', postgresql_using='gin', postgresql_ops={'company_name': 'gin_trgm_ops'})
    op.drop_index('ix_establishments_cep', table_name='establishments', postgresql_where=sa.text('cep IS NOT NULL'))
    op.drop_index('ix_establishments_cellphone', table_name='establishments', postgresql_where=sa.text('cellphone IS NOT NULL'))
    op.drop_table('establishments')
    op.drop_index('ix_correios_cep_municipio_trgm', table_name='correios_cep', postgresql_using='gin', postgresql_ops={'municipio': 'gin_trgm_ops'})
    op.drop_index('ix_correios_cep_logradouro_trgm', table_name='correios_cep', postgresql_using='gin', postgresql_ops={'logradouro': 'gin_trgm_ops'})
    op.drop_index('ix_correios_cep_bairro_trgm', table_name='correios_cep', postgresql_using='gin', postgresql_ops={'bairro': 'gin_trgm_ops'})
    op.drop_table('correios_cep')
    op.drop_index('ix_socios_nome_socio_trgm', table_name='socios', postgresql_using='gin', postgresql_ops={'nome_socio': 'gin_trgm_ops'})
    op.drop_index('ix_socios_cpf_cnpj_socio', table_name='socios', postgresql_where=sa.text('cpf_cnpj_socio IS NOT NULL'))
    op.drop_index('ix_socios_cnpj_basico', table_name='socios')
    op.drop_table('socios')
    op.drop_table('simples_staging')
    op.drop_index(op.f('ix_qualificacoes_code'), table_name='qualificacoes')
    op.drop_table('qualificacoes')
    op.drop_index(op.f('ix_paises_code'), table_name='paises')
    op.drop_table('paises')
    op.drop_index(op.f('ix_naturezas_juridicas_code'), table_name='naturezas_juridicas')
    op.drop_table('naturezas_juridicas')
    op.drop_index(op.f('ix_municipios_receita_code'), table_name='municipios')
    op.drop_index(op.f('ix_municipios_ibge_code'), table_name='municipios')
    op.drop_table('municipios')
    op.drop_index(op.f('ix_motivos_situacao_cadastral_code'), table_name='motivos_situacao_cadastral')
    op.drop_table('motivos_situacao_cadastral')
    op.drop_table('import_run')
    op.drop_table('import_progress')
    op.drop_table('import_log')
    op.drop_table('import_all_run')
    op.drop_index('ix_establishments_stats_uf', table_name='establishments_stats')
    op.drop_index('ix_establishments_stats_main_cnae', table_name='establishments_stats')
    op.drop_table('establishments_stats')
    op.drop_index('ix_establishments_cnae_stats_cnae_uf', table_name='establishments_cnae_stats')
    op.drop_table('establishments_cnae_stats')
    op.drop_index('ix_establishment_cnaes_cnae_uf_cnpj', table_name='establishment_cnaes')
    op.drop_index('ix_establishment_cnaes_cellphone', table_name='establishment_cnaes', postgresql_where=sa.text('has_cellphone'))
    op.drop_table('establishment_cnaes')
    op.drop_table('estabelecimentos_staging')
    op.drop_table('empresas_staging')
    op.drop_index(op.f('ix_cnaes_code'), table_name='cnaes')
    op.drop_table('cnaes')
