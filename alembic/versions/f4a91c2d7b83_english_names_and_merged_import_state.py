"""Nomes em ingles, staging fora do schema e estado de import fundido

Tres mudancas que andam juntas porque as tres sao sobre a mesma coisa -- o
schema tinha nome em dois idiomas, tabela que nunca era removida e estado de
import espalhado em quatro tabelas:

1. Tudo em ingles. Metade do schema estava em portugues (`municipios`,
   `socios`, `correios_cep`, `situacao_cadastral`) e metade em ingles
   (`establishments`, `company_name`, `opened_at`), sem regra nenhuma
   separando os dois. Ficou ingles em todo lugar. Nome proprio do dominio
   continua como e: `cnpj`, `cnae`, `cep`, `mei`, `simples`, `ibge`, `uf`,
   `receita_code`.

2. As tabelas de staging saem do schema. Elas sao scratch UNLOGGED de ~63M
   linhas que so existe entre o COPY e o swap do build; nasciam aqui na
   migration e ninguem nunca as removia, entao ficavam pra tras ocupando
   disco depois de todo import. Agora o `import-all` as dropa quando o swap
   consumiu tudo e o pipeline as recria no inicio do proximo
   (`ensure_staging_tables`) -- ver app/importer/pipeline.py.

   De passagem, esta migration tambem dropa (com IF EXISTS) o resto do que o
   import cria e deveria ter removido sozinho: as tabelas-sombra `*_new`/`*_old`
   do swap atomico e o scratch do e-DNE. Ver TRANSIENT_TABLES.

3. `import_run` + `import_all_run` viram `import_runs`. Eram duas tabelas de
   UMA linha cada, escritas pela mesma thread, descrevendo a mesma execucao --
   e a coluna `cnpj` de `import_all_run` era exatamente o que
   `import_run.status` guardava (o status da fase de CNPJ). O que sobrava do
   ex-`import_run` (periodo, mensagem, relogio) virou as colunas `cnpj_*`.
   `import_progress` (uma linha por estagio, escrita em paralelo) e
   `import_log` (uma linha por arquivo) continuam separadas -- o grao e outro
   -- e so foram renomeadas pra `import_steps` e `import_files`.

Renomeia tabela, coluna, indice, constraint e sequence: `ALTER TABLE ...
RENAME` no Postgres troca o nome da tabela e deixa o resto com o nome antigo,
e um schema onde a tabela e `partners` mas o indice e `ix_socios_*` e pior que
nao ter renomeado nada. Nenhum dado e reescrito -- e tudo DDL de catalogo,
instantaneo mesmo nas tabelas de 70M+ linhas.

Revision ID: f4a91c2d7b83
Revises: dd6590827abd
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f4a91c2d7b83'
down_revision: Union[str, Sequence[str], None] = 'dd6590827abd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (antes, depois) -- a ordem nao importa, nenhuma tabela referencia outra pelo
# nome aqui (FK segue o OID, nao o nome).
TABLES = [
    ("municipios", "municipalities"),
    ("naturezas_juridicas", "legal_natures"),
    ("qualificacoes", "qualifications"),
    ("paises", "countries"),
    ("motivos_situacao_cadastral", "registration_status_reasons"),
    ("correios_cep", "postal_codes"),
    ("socios", "partners"),
    ("import_progress", "import_steps"),
    ("import_log", "import_files"),
    ("import_all_run", "import_runs"),
]

COLUMNS = [
    ("establishments", "municipio_id", "municipality_id"),
    ("establishments", "situacao_cadastral", "registration_status"),
    ("establishments", "natureza_juridica", "legal_nature"),
    ("establishments", "motivo_situacao_cadastral", "registration_status_reason"),
    ("establishments_stats", "situacao_cadastral", "registration_status"),
    ("establishments_cnae_stats", "situacao_cadastral", "registration_status"),
    ("postal_codes", "municipio_cod_ibge", "municipality_ibge_code"),
    ("postal_codes", "logradouro", "street"),
    ("postal_codes", "complemento", "complement"),
    ("postal_codes", "bairro", "district"),
    ("postal_codes", "municipio", "municipality"),
    ("postal_codes", "nome", "name"),
    ("partners", "cnpj_basico", "cnpj_root"),
    ("partners", "cpf_cnpj_socio", "partner_tax_id"),
    ("partners", "representante_legal", "legal_rep"),
    ("partners", "data_entrada_sociedade", "partnership_start_date"),
    ("partners", "identificador_socio", "partner_type"),
    ("partners", "qualificacao_socio", "partner_qualification"),
    ("partners", "qualificacao_representante_legal", "legal_rep_qualification"),
    ("partners", "pais", "country"),
    ("partners", "faixa_etaria", "age_range"),
    ("partners", "nome_socio", "partner_name"),
    ("partners", "nome_representante", "legal_rep_name"),
    # As seis fases do import-all, agora em `import_runs`.
    ("import_runs", "municipios", "municipalities"),
    ("import_runs", "municipios_geo", "municipalities_geo"),
]

INDEXES = [
    ("ix_municipios_ibge_code", "ix_municipalities_ibge_code"),
    ("ix_municipios_receita_code", "ix_municipalities_receita_code"),
    ("ix_naturezas_juridicas_code", "ix_legal_natures_code"),
    ("ix_qualificacoes_code", "ix_qualifications_code"),
    ("ix_paises_code", "ix_countries_code"),
    ("ix_motivos_situacao_cadastral_code", "ix_registration_status_reasons_code"),
    ("ix_correios_cep_logradouro_trgm", "ix_postal_codes_street_trgm"),
    ("ix_correios_cep_bairro_trgm", "ix_postal_codes_district_trgm"),
    ("ix_correios_cep_municipio_trgm", "ix_postal_codes_municipality_trgm"),
    ("ix_socios_cnpj_basico", "ix_partners_cnpj_root"),
    ("ix_socios_cpf_cnpj_socio", "ix_partners_partner_tax_id"),
    ("ix_socios_nome_socio_trgm", "ix_partners_partner_name_trgm"),
    ("ix_establishments_situacao_cadastral", "ix_establishments_registration_status"),
]

# PRIMARY KEY e UNIQUE viram um indice no catalogo, entao `ALTER INDEX ...
# RENAME` renomeia a constraint junto -- nao precisa (nem da) pra usar
# `ALTER TABLE ... RENAME CONSTRAINT` aqui.
CONSTRAINT_INDEXES = [
    ("municipios_pkey", "municipalities_pkey"),
    ("naturezas_juridicas_pkey", "legal_natures_pkey"),
    ("qualificacoes_pkey", "qualifications_pkey"),
    ("paises_pkey", "countries_pkey"),
    ("motivos_situacao_cadastral_pkey", "registration_status_reasons_pkey"),
    ("correios_cep_pkey", "postal_codes_pkey"),
    # O swap de `partners` no pipeline renomeia `partners_new_pkey` pra
    # `partners_pkey` -- com o nome antigo aqui, o primeiro import depois desta
    # migration falharia com "relation partners_pkey already exists".
    ("socios_pkey", "partners_pkey"),
    ("import_progress_pkey", "import_steps_pkey"),
    ("import_log_pkey", "import_files_pkey"),
    ("uq_import_log_period_filename", "uq_import_files_period_filename"),
    ("import_all_run_pkey", "import_runs_pkey"),
]

# Mesma historia do pkey: o pipeline renomeia `partners_new_id_seq` pra
# `partners_id_seq` no swap.
SEQUENCES = [
    ("municipios_id_seq", "municipalities_id_seq"),
    ("naturezas_juridicas_id_seq", "legal_natures_id_seq"),
    ("qualificacoes_id_seq", "qualifications_id_seq"),
    ("paises_id_seq", "countries_id_seq"),
    ("motivos_situacao_cadastral_id_seq", "registration_status_reasons_id_seq"),
    ("socios_id_seq", "partners_id_seq"),
    ("import_log_id_seq", "import_files_id_seq"),
    ("import_all_run_id_seq", "import_runs_id_seq"),
]

# Renomeadas SO SE existirem. `establishments_municipio_id_fkey` some no
# primeiro build: o build monta `establishments_new` do zero e devolve apenas
# `establishments_cep_fkey` no swap, entao num banco que ja importou uma vez
# essa constraint nao existe -- e um RENAME CONSTRAINT direto abortaria a
# migration inteira.
FOREIGN_KEYS = [
    ("postal_codes", "correios_cep_municipio_cod_ibge_fkey",
     "postal_codes_municipality_ibge_code_fkey"),
    ("establishments", "establishments_municipio_id_fkey",
     "establishments_municipality_id_fkey"),
]


def _rename_constraint_if_exists(table: str, old: str, new: str) -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{old}' AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}";
            END IF;
        END $$
    """)

# Tudo que o import cria e deveria ter removido sozinho, mas pode ter deixado
# pra tras. Nenhuma destas e parte do schema: ou e staging (agora criada e
# dropada pelo import, ver ensure_staging_tables), ou e tabela-SOMBRA do swap
# atomico, ou e scratch de outro import.
#
# As `*_new`/`*_old` sao o swap do build: ele monta `<tabela>_new`, renomeia a
# viva pra `<tabela>_old` e dropa. Um crash entre esses passos (ou um Ctrl-C no
# meio de um build de horas) deixa a sombra ocupando disco -- do tamanho da
# tabela final, que passa de 70M linhas -- sem ninguem nunca mais olhar pra
# ela: o proximo build comeca com `DROP TABLE IF EXISTS` e recria do zero.
#
# `correios_cep_import` E `postal_codes_import` porque o nome mudou nesta mesma
# migration: um banco vindo do codigo antigo pode ter a primeira parada ali.
TRANSIENT_TABLES = [
    # staging (nomes antigos -- esta migration roda antes do rename delas)
    "estabelecimentos_staging",
    "empresas_staging",
    "simples_staging",
    # sombras do swap do build
    "establishments_new",
    "establishments_old",
    "establishment_cnaes_new",
    "establishment_cnaes_old",
    "establishments_stats_new",
    "establishments_stats_old",
    "establishments_cnae_stats_new",
    "establishments_cnae_stats_old",
    "socios_new",
    "socios_old",
    "partners_new",
    "partners_old",
    # scratch do import-ceps (e-DNE), nos dois nomes
    "correios_cep_import",
    "postal_codes_import",
]


def upgrade() -> None:
    for old, new in TABLES:
        op.execute(f'ALTER TABLE "{old}" RENAME TO "{new}"')
    for table, old, new in COLUMNS:
        op.alter_column(table, old, new_column_name=new)
    for old, new in INDEXES + CONSTRAINT_INDEXES:
        op.execute(f'ALTER INDEX "{old}" RENAME TO "{new}"')
    for old, new in SEQUENCES:
        op.execute(f'ALTER SEQUENCE "{old}" RENAME TO "{new}"')
    for table, old, new in FOREIGN_KEYS:
        _rename_constraint_if_exists(table, old, new)

    # --- chaves do JSONB `establishments.address` ---------------------------
    # Essa coluna guarda o endereco cru da Receita, e so pras linhas SEM
    # vinculo de CEP -- uma minoria, mas o build passou a gravar as chaves em
    # ingles (ver `jsonb_build_object` em _build_final_table) e o router passou
    # a le-las em ingles. Sem remapear as que ja estao no banco, todo endereco
    # de excecao sairia nulo na API ate o proximo import completo -- que
    # reconstroi a tabela inteira, mas pode ser semanas depois.
    #
    # `?|` filtra as linhas que ainda tem chave antiga, entao isto e re-executavel
    # e nao toca no que ja esta em ingles.
    op.execute("""
        UPDATE establishments SET address =
            (address - 'logradouro' - 'numero' - 'complemento' - 'bairro')
            || jsonb_strip_nulls(jsonb_build_object(
                'street', address -> 'logradouro',
                'number', address -> 'numero',
                'complement', address -> 'complemento',
                'district', address -> 'bairro'
            ))
        WHERE address IS NOT NULL
          AND address ?| array['logradouro', 'numero', 'complemento', 'bairro']
    """)

    # --- fusao do estado de import ------------------------------------------
    op.execute("ALTER TABLE import_runs ADD COLUMN cnpj_period VARCHAR(10)")
    op.execute("ALTER TABLE import_runs ADD COLUMN cnpj_message VARCHAR(255)")
    op.execute("ALTER TABLE import_runs ADD COLUMN cnpj_started_at TIMESTAMP WITHOUT TIME ZONE")
    op.execute("ALTER TABLE import_runs ADD COLUMN cnpj_updated_at TIMESTAMP WITHOUT TIME ZONE")

    # Traz a linha unica do ex-`import_run`. Os dois lados sao id=1, e cada um
    # pode nao existir: `import-cnpj` avulso criava `import_run` sem nunca
    # tocar em `import_all_run`, e o contrario tambem acontecia. Por isso o
    # UPDATE (quando as duas linhas existem) e o INSERT ... WHERE NOT EXISTS
    # (quando so a de import_run existe) -- e por isso `updated_at`, que e NOT
    # NULL, vem do lado que existe.
    op.execute("""
        UPDATE import_runs r
        SET cnpj = o.status,
            cnpj_period = o.period,
            cnpj_message = o.message,
            cnpj_started_at = o.started_at,
            cnpj_updated_at = o.updated_at
        FROM import_run o
        WHERE r.id = o.id
    """)
    op.execute("""
        INSERT INTO import_runs (
            id, status, message, started_at, updated_at,
            municipalities, ceps, ceps_osm, cnpj, ibge, municipalities_geo,
            cnpj_period, cnpj_message, cnpj_started_at, cnpj_updated_at
        )
        SELECT o.id, 'idle', NULL, NULL, o.updated_at,
               'pending', 'pending', 'pending', o.status, 'pending', 'pending',
               o.period, o.message, o.started_at, o.updated_at
        FROM import_run o
        WHERE NOT EXISTS (SELECT 1 FROM import_runs r WHERE r.id = o.id)
    """)
    op.execute("DROP TABLE import_run")

    # --- fora do schema tudo que e transitorio -------------------------------
    # `IF EXISTS` em todas porque nenhuma tem presenca garantida: depende de o
    # banco ter importado alguma vez, e de como o ultimo import terminou.
    #
    # Sem recriar no downgrade a partir de dados: o conteudo e scratch de um
    # import, reconstruido pelo proximo.
    for table in TRANSIENT_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def downgrade() -> None:
    # Staging de volta ao schema, vazias -- ver a nota no upgrade.
    op.execute("""
        CREATE UNLOGGED TABLE empresas_staging (
            cnpj_basico BIGINT NOT NULL,
            porte_empresa SMALLINT,
            natureza_juridica SMALLINT,
            razao_social TEXT,
            CONSTRAINT empresas_staging_pkey PRIMARY KEY (cnpj_basico)
        )
    """)
    op.execute("""
        CREATE UNLOGGED TABLE simples_staging (
            cnpj_basico BIGINT NOT NULL,
            opcao_simples BOOLEAN NOT NULL,
            opcao_mei BOOLEAN NOT NULL,
            CONSTRAINT simples_staging_pkey PRIMARY KEY (cnpj_basico)
        )
    """)
    op.execute("""
        CREATE UNLOGGED TABLE estabelecimentos_staging (
            cnpj BIGINT NOT NULL,
            phone BIGINT,
            cellphone BIGINT,
            cnae_fiscal_principal INTEGER,
            municipio_codigo INTEGER,
            cep INTEGER,
            data_inicio_atividade DATE,
            uf SMALLINT,
            situacao_cadastral SMALLINT,
            motivo_situacao_cadastral SMALLINT,
            cellphone_confidence SMALLINT NOT NULL,
            is_headquarters BOOLEAN NOT NULL,
            cnae_fiscal_secundaria INTEGER[],
            nome_fantasia TEXT,
            correio_eletronico TEXT,
            logradouro TEXT,
            numero TEXT,
            complemento TEXT,
            bairro TEXT,
            CONSTRAINT estabelecimentos_staging_pkey PRIMARY KEY (cnpj)
        )
    """)

    # Desfaz a fusao: `import_run` volta a existir com a linha que estava nas
    # colunas `cnpj_*`.
    op.execute("""
        CREATE TABLE import_run (
            id SERIAL NOT NULL,
            period VARCHAR(10),
            status VARCHAR(20) NOT NULL,
            message VARCHAR(255),
            started_at TIMESTAMP WITHOUT TIME ZONE,
            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            CONSTRAINT import_run_pkey PRIMARY KEY (id)
        )
    """)
    op.execute("""
        INSERT INTO import_run (id, period, status, message, started_at, updated_at)
        SELECT id, cnpj_period, cnpj, cnpj_message, cnpj_started_at,
               COALESCE(cnpj_updated_at, updated_at)
        FROM import_runs
    """)
    for column in ("cnpj_updated_at", "cnpj_started_at", "cnpj_message", "cnpj_period"):
        op.execute(f"ALTER TABLE import_runs DROP COLUMN {column}")

    op.execute("""
        UPDATE establishments SET address =
            (address - 'street' - 'number' - 'complement' - 'district')
            || jsonb_strip_nulls(jsonb_build_object(
                'logradouro', address -> 'street',
                'numero', address -> 'number',
                'complemento', address -> 'complement',
                'bairro', address -> 'district'
            ))
        WHERE address IS NOT NULL
          AND address ?| array['street', 'number', 'complement', 'district']
    """)

    for table, original, renamed in FOREIGN_KEYS:
        _rename_constraint_if_exists(table, renamed, original)
    for old, new in SEQUENCES:
        op.execute(f'ALTER SEQUENCE "{new}" RENAME TO "{old}"')
    for old, new in INDEXES + CONSTRAINT_INDEXES:
        op.execute(f'ALTER INDEX "{new}" RENAME TO "{old}"')
    for table, old, new in COLUMNS:
        op.alter_column(table, new, new_column_name=old)
    for old, new in TABLES:
        op.execute(f'ALTER TABLE "{new}" RENAME TO "{old}"')
