"""Contrato da tabela unificada de CEP dos Correios (e-DNE).

`correios_cep` e a unica tabela do banco que nao e um model nosso: o esquema
dela vem do edne-correios-loader. Este modulo e o dono desse contrato --
nome, colunas e DDL num lugar so -- porque tres lugares diferentes precisam
dele (o import em app/cli.py, a busca de endereco em app/routers/enderecos.py
e o vinculo por CEP no build em app/importer/pipeline.py).

A tabela e nossa, nao da lib: o import monta a base nova numa tabela de
scratch e faz UPSERT daqui (ver `_import_ceps`). Assim ninguem apaga linha
que outra tabela referencia, e a base nunca fica vazia no meio de um import.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

TABLE = "correios_cep"

# Tabela onde o edne-correios-loader monta a base nova; a real nunca e
# entregue a lib.
SCRATCH_TABLE = "correios_cep_import"

COLUMNS = (
    "cep",
    "logradouro",
    "complemento",
    "bairro",
    "municipio",
    "municipio_cod_ibge",
    "uf",
    "nome",
)

# Mesmo esquema que o edne-correios-loader cria, pra continuar compativel
# quando ele rodar (ele faz CREATE TABLE IF NOT EXISTS na de scratch).
_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    cep VARCHAR(8) PRIMARY KEY,
    logradouro VARCHAR(100),
    complemento VARCHAR(100),
    bairro VARCHAR(72),
    municipio VARCHAR(72) NOT NULL,
    municipio_cod_ibge INTEGER NOT NULL,
    uf VARCHAR(2) NOT NULL,
    nome VARCHAR(100)
)
"""


def ensure_table(db: Session) -> None:
    """Cria a tabela se `import-ceps` ainda nao rodou -- assim a busca de
    endereco e o build do CNPJ funcionam (vazios) num banco novo."""
    db.execute(text(_DDL))
    db.commit()


def upsert_from(db: Session, source_table: str) -> tuple[int, int, int]:
    """Mescla `source_table` em `correios_cep`. Devolve (novos, atualizados, stale).

    UPSERT e nao DELETE + INSERT: nada some debaixo de quem referencia a
    tabela, e a base nunca fica vazia no meio do caminho.
    """
    cols = ", ".join(COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c != "cep")

    before = db.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar() or 0
    incoming = db.execute(text(f"SELECT count(*) FROM {source_table}")).scalar() or 0

    db.execute(text(f"""
        INSERT INTO {TABLE} ({cols})
        SELECT {cols} FROM {source_table}
        ON CONFLICT (cep) DO UPDATE SET {updates}
    """))
    db.commit()

    after = db.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar() or 0

    # CEPs que ja estavam aqui e nao vieram na base nova (extintos ou
    # remanejados pelos Correios). O upsert nao os remove -- e justamente isso
    # que permite referenciar a tabela sem levar a linha embaixo do pe -- mas
    # conta-los evita acumular CEP morto sem ninguem perceber.
    stale = db.execute(text(f"""
        SELECT count(*) FROM {TABLE} c
        WHERE NOT EXISTS (SELECT 1 FROM {source_table} s WHERE s.cep = c.cep)
    """)).scalar() or 0

    inserted = after - before
    return inserted, incoming - inserted, stale
