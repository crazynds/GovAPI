"""Agregado pre-calculado de establishments (ver models.EstablishmentStats).

O SQL vive aqui, e nao no pipeline, porque duas coisas precisam dele: o import
(que monta o agregado da tabela shadow e troca junto no swap) e a migration
(que popula uma vez a partir da tabela que ja esta no banco, pra nao ser
preciso reimportar 72M linhas so pra ganhar o agregado).
"""

TABLE = "establishments_stats"

# As colunas de dimensao, na ordem do GROUP BY. Uma lista so, pra o INSERT, o
# GROUP BY e o router nao poderem discordar entre si.
DIMENSIONS = (
    "uf",
    "situacao_cadastral",
    "company_size",
    "main_cnae",
    "is_mei",
    "is_simples",
    "is_headquarters",
)

# `with_cellphone_and_email` e a intersecao, e nao e redundante: sem ela nao da
# pra responder `only_with_cellphone=true`, que restringe a POPULACAO e faz
# `with_email` passar a significar "tem os dois". Ver _measure_columns no router.
MEASURES = ("total", "with_cellphone", "with_email", "with_phone", "with_cellphone_and_email")

_MEASURE_SQL = """
               count(*),
               count(*) FILTER (WHERE cellphone IS NOT NULL),
               count(*) FILTER (WHERE email IS NOT NULL),
               count(*) FILTER (WHERE phone IS NOT NULL),
               count(*) FILTER (WHERE cellphone IS NOT NULL AND email IS NOT NULL)
"""

_MEASURE_DDL = """
            total bigint NOT NULL,
            with_cellphone bigint NOT NULL,
            with_email bigint NOT NULL,
            with_phone bigint NOT NULL,
            with_cellphone_and_email bigint NOT NULL
"""

# --- Agregado por CNAE -------------------------------------------------------
#
# Tabela separada porque o filtro `?cnae_codes=` casa CNAE principal OU
# secundario, e `secondary_cnaes` e um array. Medido em producao: pro CNAE
# 4781400 em PR, o secundario acrescenta 39,4% das empresas -- responder so
# pelo principal subnotificaria quase metade do alvo.
#
# O preco de desnormalizar o array e que as linhas NAO podem ser somadas entre
# CNAEs diferentes: uma empresa com dois CNAEs aparece em dois baldes e seria
# contada duas vezes. Por isso o router so usa esta tabela pra UM codigo por
# vez -- dentro de um unico `cnae`, cada empresa aparece uma vez so, e ai a
# soma e exata.

CNAE_TABLE = "establishments_cnae_stats"

CNAE_DIMENSIONS = (
    "cnae",
    "uf",
    "situacao_cadastral",
    "company_size",
    "is_mei",
    "is_simples",
    "is_headquarters",
)


def build_sql(target: str, source: str) -> str:
    """INSERT que preenche `target` agregando `source`.

    Uma unica varredura de `source`: o GROUP BY monta as dimensoes e os
    `count(*) FILTER` as medidas na mesma passada.
    """
    dims = ", ".join(DIMENSIONS)
    return f"""
        INSERT INTO {target} ({dims}, {', '.join(MEASURES)})
        SELECT {dims},
{_MEASURE_SQL}
        FROM {source}
        GROUP BY {dims}
    """


def cnae_build_sql(target: str, source: str) -> str:
    """INSERT do agregado por CNAE.

    O LATERAL abre cada empresa em uma linha por CNAE distinto dela --
    principal e secundarios juntos. `DISTINCT` porque o principal as vezes
    tambem aparece na lista de secundarios, e sem ele a empresa seria contada
    duas vezes dentro do MESMO balde. `coalesce` porque `secondary_cnaes` e
    NULL (nao array vazio) quando nao ha nenhum, e `NULL || x` seria NULL.
    `main_cnae` NULL vira um elemento NULL no array, descartado pelo WHERE.
    """
    dims = ", ".join(CNAE_DIMENSIONS)
    return f"""
        INSERT INTO {target} ({dims}, {', '.join(MEASURES)})
        SELECT {dims},
{_MEASURE_SQL}
        FROM {source} e
        CROSS JOIN LATERAL (
            SELECT DISTINCT unnest(
                coalesce(e.secondary_cnaes, '{{}}'::integer[]) || e.main_cnae
            ) AS cnae
        ) codes
        WHERE codes.cnae IS NOT NULL
        GROUP BY {dims}
    """


def create_sql(name: str) -> str:
    """DDL da tabela agregada. Espelha models.EstablishmentStats -- o import
    cria a shadow com este DDL em vez de `LIKE`, porque a tabela final pode nao
    existir ainda no primeiro import."""
    return f"""
        CREATE TABLE {name} (
            id bigserial PRIMARY KEY,
            uf smallint,
            situacao_cadastral smallint,
            company_size smallint,
            main_cnae integer,
            is_mei boolean NOT NULL,
            is_simples boolean NOT NULL,
            is_headquarters boolean NOT NULL,
{_MEASURE_DDL}
        )
    """


def cnae_create_sql(name: str) -> str:
    return f"""
        CREATE TABLE {name} (
            id bigserial PRIMARY KEY,
            cnae integer NOT NULL,
            uf smallint,
            situacao_cadastral smallint,
            company_size smallint,
            is_mei boolean NOT NULL,
            is_simples boolean NOT NULL,
            is_headquarters boolean NOT NULL,
{_MEASURE_DDL}
        )
    """


# (nome do indice, colunas). A tabela e pequena o bastante pra varrer inteira,
# mas UF e o filtro mais comum de longe e corta 27x logo de cara.
INDEXES = (
    ("ix_establishments_stats_uf", "(uf)"),
    ("ix_establishments_stats_main_cnae", "(main_cnae)"),
)


# O CNAE vem primeiro: e sempre igualdade e e o filtro que define a consulta.
CNAE_INDEXES = (
    ("ix_establishments_cnae_stats_cnae_uf", "(cnae, uf)"),
)


def index_sql(table: str, suffix: str = "") -> list[str]:
    return [f"CREATE INDEX {name}{suffix} ON {table} {cols}" for name, cols in INDEXES]


def cnae_index_sql(table: str, suffix: str = "") -> list[str]:
    return [f"CREATE INDEX {name}{suffix} ON {table} {cols}" for name, cols in CNAE_INDEXES]
