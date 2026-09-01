"""Import e leitura da base unificada de CEP dos Correios (e-DNE).

A tabela `postal_codes` e um model nosso (ver app/models.py). O esquema veio do
edne-correios-loader, mas quem cria e popula somos nos: o `import-ceps` manda a
lib montar a base nova numa tabela de scratch e daqui sai um UPSERT. A lib
nunca toca na tabela real -- e isso que permite `establishments.cep` ter uma
FOREIGN KEY pra ca, porque nenhuma linha e apagada debaixo de quem referencia.

O `cep` e INTEGER (4 bytes em vez dos 8 digitos como texto), e a formatacao com
zero a esquerda acontece na leitura -- ver `SELECT_COLUMNS`.
"""

from sqlalchemy import String, Text, text
from sqlalchemy.orm import Session

TABLE = "postal_codes"

# Tabela onde o edne-correios-loader monta a base nova. Ele a cria com o
# esquema dele (cep como VARCHAR(8)), e o cast pra INTEGER acontece no upsert.
SCRATCH_TABLE = "postal_codes_import"

# So as colunas de endereco (e-DNE). As de coordenada (latitude/longitude/
# coord_source/coord_updated_at) vivem na mesma tabela desde a fusao com
# `cep_coordenadas`, e ficam DE FORA daqui de proposito: o upsert do import
# lista essas colunas no ON CONFLICT DO UPDATE, entao deixa-las de fora e o que
# faz a coordenada sobreviver a um `import-ceps`.
COLUMNS = (
    "cep",
    "street",
    "complement",
    "district",
    "municipality",
    "municipality_ibge_code",
    "uf",
    "name",
)

# O MESMO conteudo, com o nome que a coluna tem na tabela de scratch: o esquema
# dela e do edne-correios-loader, e o da lib esta em portugues. Nossas colunas
# foram traduzidas; as dela nao podem ser -- e ela quem cria essa tabela.
# Sem esta traducao, o SELECT do upsert pediria `s.street` numa tabela que so
# tem `s.logradouro`.
SOURCE_COLUMNS = {
    "cep": "cep",
    "street": "logradouro",
    "complement": "complemento",
    "district": "bairro",
    "municipality": "municipio",
    "municipality_ibge_code": "municipio_cod_ibge",
    "uf": "uf",
    "name": "nome",
}

CEP_WIDTH = 8


# Colunas cujo tamanho e um codigo de largura fixa (sigla de UF/pais, flag de
# 1 caractere, o proprio CEP) -- essas ficam do jeito que a lib declarou.
# Tudo com largura declarada acima disso e texto livre (nome de logradouro,
# bairro, abreviacao) e vai pra `widen_free_text_columns`.
_FIXED_WIDTH_MAX = 8


def reset_source_tables(db: Session, metadata) -> None:
    """Dropa todas as tabelas que o edne-correios-loader vai (re)criar,
    inclusive a que sobrou de um run anterior que falhou no meio.

    `create_tables` (chamado dentro de `.load()`) faz
    `metadata.create_all(self.engine, ...)` -- numa conexao da ENGINE, nao na
    `self.connection` usada pros INSERTs -- e commita na hora. Se um run
    anterior criou `log_bairro` e morreu numa StringDataRightTruncation antes
    do cleanup no fim, a tabela fica pra tras com o schema de ENTAO
    (committed, independente da transacao das inserts que fez rollback). Como
    `create_all` so cria tabela que nao existe, o proximo run reusa essa
    tabela estreita e quebra de novo -- mesmo depois de
    `widen_free_text_columns` alargar o metadata em memoria, porque a tabela
    real no banco nunca foi recriada com o tipo novo.

    Drop explicito antes de cada run resolve isso de vez: `create_all` sempre
    cria do zero, com o metadata (ja alargado) que valer naquele momento.
    Seguro porque nenhuma tabela aqui e `postal_codes` -- a real fica de fora
    do metadata da lib, so a de scratch (renomeada via `table_names`) entra.
    """
    for table in reversed(metadata.sorted_tables):
        db.execute(text(f'DROP TABLE IF EXISTS "{table.name}" CASCADE'))
    db.commit()


def widen_free_text_columns(metadata) -> None:
    """Troca por Text() toda coluna de texto livre do esquema do e-DNE.

    O e-DNE (dados reais dos Correios) nao respeita as larguras que o proprio
    edne-correios-loader declara pro schema dele -- visto na pratica:
    `log_bairro.bai_no_abrev` e VARCHAR(36) e um bairro real tem abreviacao
    maior, `StringDataRightTruncation` no INSERT. E nao e so essa coluna: a
    `logradouro` da tabela unificada da lib e montada concatenando `tlo_tx`
    (36) + `log_no` (100) numa coluna que so tem 100 de largura -- o mesmo
    estouro pode acontecer ali tambem, so que mais raro (por isso nao apareceu
    antes).

    Chamado sobre `DneLoader.metadata` ANTES de `.load()`: como as tabelas
    (inclusive a de scratch que vira `cep_unificado`) sao criadas dentro do
    `.load()`, mudar o tipo aqui muda a DDL que sai. As colunas afetadas
    pertencem a tabelas temporarias, dropadas no fim do import -- widen sem
    limite nao custa nada alem do import em si.
    """
    for table in metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, String) and (column.type.length or 0) > _FIXED_WIDTH_MAX:
                column.type = Text()


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
    """Mescla `source_table` em `postal_codes`. Devolve (novos, atualizados, stale).

    UPSERT e nao DELETE + INSERT: nada some debaixo de quem referencia a tabela
    (a FK de establishments.cep depende disso), e a base nunca fica vazia no
    meio de um import. As colunas de coordenada nao entram no SET, entao
    sobrevivem intactas.
    """
    cols = ", ".join(COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c != "cep")
    # A scratch vem da lib com `cep` VARCHAR -- cast aqui, filtrando a 8 digitos
    # pro cast nunca estourar numa linha estranha. `municipality_ibge_code` vira
    # NULL quando nao bate com nenhum `municipalities.ibge_code` (municipio
    # extinto/fundido, fora da lista atual do IBGE) -- mesmo tratamento que
    # `establishments.cep` ja da a CEP orfao, e pelo mesmo motivo: um codigo
    # que nao resolve nada so serviria pra travar a FOREIGN KEY do upsert
    # inteiro.
    # `AS {c}`: o INSERT lista as colunas na ordem de COLUMNS, entao o alias so
    # documenta qual coluna nossa cada campo da lib alimenta.
    select_cols = ", ".join(
        "s.cep::integer AS cep" if c == "cep"
        else f"CASE WHEN mu.ibge_code IS NOT NULL THEN s.{SOURCE_COLUMNS[c]} END AS {c}"
        if c == "municipality_ibge_code"
        else f"s.{SOURCE_COLUMNS[c]} AS {c}"
        for c in COLUMNS
    )
    incoming = (
        f"SELECT {select_cols} FROM {source_table} s "
        f"LEFT JOIN municipalities mu ON mu.ibge_code = s.{SOURCE_COLUMNS['municipality_ibge_code']} "
        f"WHERE s.cep ~ '^[0-9]{{8}}$'"
    )

    before = db.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar() or 0
    total_in = db.execute(text(f"SELECT count(*) FROM ({incoming}) AS src")).scalar() or 0

    db.execute(text(f"""
        INSERT INTO {TABLE} ({cols})
        {incoming}
        ON CONFLICT (cep) DO UPDATE SET {updates}
    """))
    db.commit()

    after = db.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar() or 0

    # CEPs que ja tinham endereco aqui e nao vieram na base nova (extintos ou
    # remanejados pelos Correios). O upsert nao os remove -- e justamente isso
    # que permite referenciar a tabela sem levar a linha embaixo do pe -- mas
    # conta-los evita acumular CEP morto sem ninguem perceber.
    #
    # `municipality IS NOT NULL` filtra as linhas que so tem coordenada (vindas do
    # `import-ceps-osm`): elas nunca estiveram no e-DNE, entao nao "sumiram"
    # dele -- conta-las como stale seria alarme falso.
    stale = db.execute(text(f"""
        SELECT count(*) FROM {TABLE} c
        WHERE c.municipality IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM ({incoming}) AS src WHERE src.cep = c.cep)
    """)).scalar() or 0

    inserted = after - before
    return inserted, total_in - inserted, stale
