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

MEASURES = ("total", "with_cellphone", "with_email", "with_phone")


def build_sql(target: str, source: str) -> str:
    """INSERT que preenche `target` agregando `source`.

    Uma unica varredura de `source`: o GROUP BY monta as dimensoes e os
    `count(*) FILTER` as medidas na mesma passada.
    """
    dims = ", ".join(DIMENSIONS)
    return f"""
        INSERT INTO {target} ({dims}, {', '.join(MEASURES)})
        SELECT {dims},
               count(*),
               count(*) FILTER (WHERE cellphone IS NOT NULL),
               count(*) FILTER (WHERE email IS NOT NULL),
               count(*) FILTER (WHERE phone IS NOT NULL)
        FROM {source}
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
            total bigint NOT NULL,
            with_cellphone bigint NOT NULL,
            with_email bigint NOT NULL,
            with_phone bigint NOT NULL
        )
    """


# (nome do indice, colunas). A tabela e pequena o bastante pra varrer inteira,
# mas UF e o filtro mais comum de longe e corta 27x logo de cara.
INDEXES = (
    ("ix_establishments_stats_uf", "(uf)"),
    ("ix_establishments_stats_main_cnae", "(main_cnae)"),
)


def index_sql(table: str, suffix: str = "") -> list[str]:
    return [f"CREATE INDEX {name}{suffix} ON {table} {cols}" for name, cols in INDEXES]
