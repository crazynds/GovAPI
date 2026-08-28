"""Import e leitura da base unificada de CEP dos Correios (e-DNE).

A tabela `correios_cep` e um model nosso (ver app/models.py). O esquema veio do
edne-correios-loader, mas quem cria e popula somos nos: o `import-ceps` manda a
lib montar a base nova numa tabela de scratch e daqui sai um UPSERT. A lib
nunca toca na tabela real -- e isso que permite `establishments.cep` ter uma
FOREIGN KEY pra ca, porque nenhuma linha e apagada debaixo de quem referencia.

O `cep` e INTEGER (4 bytes em vez dos 8 digitos como texto), e a formatacao com
zero a esquerda acontece na leitura -- ver `SELECT_COLUMNS`.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

TABLE = "correios_cep"

# Tabela onde o edne-correios-loader monta a base nova. Ele a cria com o
# esquema dele (cep como VARCHAR(8)), e o cast pra INTEGER acontece no upsert.
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

CEP_WIDTH = 8


def to_int(raw: str | int | None) -> int | None:
    """CEP em qualquer forma ("01310-100", "01310100", 1310100) -> int.

    None quando nao sao 8 digitos -- CEP malformado nao vira consulta.
    """
    if raw is None:
        return None
    digits = "".join(c for c in str(raw) if c.isdigit())
    return int(digits) if len(digits) == CEP_WIDTH else None


def to_str(value: int | None) -> str | None:
    """Inverso: o CEP de 8 posicoes com zero a esquerda, como a API expoe."""
    return f"{value:0{CEP_WIDTH}d}" if value is not None else None


def select_columns(prefix: str = "") -> str:
    """Colunas pra um SELECT, com o `cep` ja formatado como texto.

    A coluna e INTEGER no banco, mas a API sempre falou em CEP de 8 posicoes com
    zero a esquerda -- e "01310100" nao sobrevive a um int sem o lpad.
    """
    p = f"{prefix}." if prefix else ""
    return ", ".join(
        f"lpad({p}cep::text, {CEP_WIDTH}, '0') AS cep" if c == "cep" else f"{p}{c}"
        for c in COLUMNS
    )


def upsert_from(db: Session, source_table: str) -> tuple[int, int, int]:
    """Mescla `source_table` em `correios_cep`. Devolve (novos, atualizados, stale).

    UPSERT e nao DELETE + INSERT: nada some debaixo de quem referencia a tabela
    (a FK de establishments.cep depende disso), e a base nunca fica vazia no
    meio de um import.
    """
    cols = ", ".join(COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c != "cep")
    # A scratch vem da lib com `cep` VARCHAR -- cast aqui, filtrando a 8 digitos
    # pro cast nunca estourar numa linha estranha.
    incoming_cols = ", ".join(f"{c}::integer" if c == "cep" else c for c in COLUMNS)
    incoming = f"SELECT {incoming_cols} FROM {source_table} WHERE cep ~ '^[0-9]{{8}}$'"

    before = db.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar() or 0
    total_in = db.execute(text(f"SELECT count(*) FROM ({incoming}) s")).scalar() or 0

    db.execute(text(f"""
        INSERT INTO {TABLE} ({cols})
        {incoming}
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
        WHERE NOT EXISTS (SELECT 1 FROM ({incoming}) s WHERE s.cep = c.cep)
    """)).scalar() or 0

    inserted = after - before
    return inserted, total_in - inserted, stale
