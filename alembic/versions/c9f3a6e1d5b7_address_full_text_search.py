"""Full-text search de endereco em postal_codes

Revision ID: c9f3a6e1d5b7
Revises: b2d5f8a91c04
Create Date: 2026-09-26

`/addresses/search-text` (busca livre tipo "santa maria camobi rua 17 de
maio") comecou usando um ILIKE '%token%' por palavra, todos em AND -- cada
token vira um bitmap scan separado que o planner tem que combinar, e ILIKE e
comparacao de substring, entao nao entende que "camobi" e "rua 17 de maio"
sao unidades diferentes do mesmo endereco a nao ser pela posicao literal na
string.

Full-text search (tsvector/to_tsquery) resolve isso de forma nativa: a busca
vira um `@@` contra os lexemas de UM indice GIN so, nao N ILIKEs combinados,
e "fora de ordem" e o comportamento padrao (e um conjunto de lexemas, nao uma
substring). De quebra, `to_tsvector('portuguese', ...)` ja descarta
preposicoes ("de", "da", "e") sozinho, sem precisar de uma lista de
stopwords mantida a mao no codigo Python.

O texto indexado inclui `uf` (sigla) E o nome completo do estado (via
`uf_full_name`, abaixo) pra "rs" e "rio grande do sul" acharem a mesma coisa
sem o cliente saber qual das duas formas a busca entende.

`uf_full_name` precisa ser IMMUTABLE pra entrar numa coluna GENERATED: o
Postgres exige que a expressao de uma coluna gerada nao dependa de nada alem
da propria linha, e so aceita funcoes que ele consegue provar deterministicas
-- daí `LANGUAGE sql IMMUTABLE`, um CASE fechado (as 27 UF nunca mudam) e nao
uma consulta a `municipalities`.

`address_tsv` e STORED (calculada uma vez, na escrita) e nao um indice de
expressao sobre uma view: STORED e o unico jeito de uma coluna gerada
funcionar como fonte de um indice GIN aqui -- o Postgres nao indexa uma
expressao arbitraria em cima de outra coluna nullable sem materializa-la
primeiro do jeito que o GIN de tsvector precisa (lexema -> lista de linhas).

Como `postal_codes` ja tem dado (import do e-DNE), o ALTER TABLE ... ADD
COLUMN GENERATED reescreve a tabela inteira -- e um passo demorado, mas
acontece uma vez so; e a mesma categoria de custo que os indices trigram
existentes ja pagaram no import inicial.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9f3a6e1d5b7'
down_revision: Union[str, None] = 'b2d5f8a91c04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION uf_full_name(uf text) RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
            SELECT CASE uf
                WHEN 'AC' THEN 'Acre'
                WHEN 'AL' THEN 'Alagoas'
                WHEN 'AP' THEN 'Amapa'
                WHEN 'AM' THEN 'Amazonas'
                WHEN 'BA' THEN 'Bahia'
                WHEN 'CE' THEN 'Ceara'
                WHEN 'DF' THEN 'Distrito Federal'
                WHEN 'ES' THEN 'Espirito Santo'
                WHEN 'GO' THEN 'Goias'
                WHEN 'MA' THEN 'Maranhao'
                WHEN 'MT' THEN 'Mato Grosso'
                WHEN 'MS' THEN 'Mato Grosso do Sul'
                WHEN 'MG' THEN 'Minas Gerais'
                WHEN 'PA' THEN 'Para'
                WHEN 'PB' THEN 'Paraiba'
                WHEN 'PR' THEN 'Parana'
                WHEN 'PE' THEN 'Pernambuco'
                WHEN 'PI' THEN 'Piaui'
                WHEN 'RJ' THEN 'Rio de Janeiro'
                WHEN 'RN' THEN 'Rio Grande do Norte'
                WHEN 'RS' THEN 'Rio Grande do Sul'
                WHEN 'RO' THEN 'Rondonia'
                WHEN 'RR' THEN 'Roraima'
                WHEN 'SC' THEN 'Santa Catarina'
                WHEN 'SP' THEN 'Sao Paulo'
                WHEN 'SE' THEN 'Sergipe'
                WHEN 'TO' THEN 'Tocantins'
                ELSE NULL
            END
        $$
    """)

    op.execute("""
        ALTER TABLE postal_codes ADD COLUMN address_tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('portuguese',
                coalesce(street, '') || ' ' ||
                coalesce(district, '') || ' ' ||
                coalesce(municipality, '') || ' ' ||
                coalesce(uf, '') || ' ' ||
                coalesce(uf_full_name(uf), '')
            )
        ) STORED
    """)

    op.create_index(
        "ix_postal_codes_address_tsv", "postal_codes", ["address_tsv"],
        unique=False, postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_postal_codes_address_tsv", table_name="postal_codes", postgresql_using="gin")
    op.execute("ALTER TABLE postal_codes DROP COLUMN address_tsv")
    op.execute("DROP FUNCTION IF EXISTS uf_full_name(text)")
