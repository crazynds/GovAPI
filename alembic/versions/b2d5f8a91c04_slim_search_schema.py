"""busca enxuta: um indice por tabela, UF sai de municipalities

Revision ID: b2d5f8a91c04
Revises: a1c4e7f20b13
Create Date: 2026-09-02

A busca passa a ter uma forma so, e o schema passa a carregar so o que ela usa:

    SELECT establishments.* FROM establishment_cnaes
    JOIN establishments  ON establishment_cnaes.cnpj = establishments.cnpj
    JOIN municipalities  ON municipalities.id = establishment_cnaes.municipality_id
    WHERE cnae IN (?) AND municipalities.ibge_code = ?  -- ou municipalities.uf = ?
      AND has_cellphone = ?
    ORDER BY cnpj LIMIT ?

O que muda, e por que:

1. `establishment_cnaes.uf` SAI. Era copia de `establishments.uf` pra evitar o
   join; agora a UF sai de `municipalities`, que a busca ja precisa joinar de
   qualquer forma (a tabela tem ~5.570 linhas -- o join custa um hash).
   `is_main` sai junto: nao entra em filtro nenhum, e o CNAE secundario e
   deduzivel (`cnae <> establishments.main_cnae`).

2. Os TRES indices de `establishment_cnaes` viram UM: `(municipality_id, cnae,
   cnpj)`. `municipality_id` na frente porque toda busca agora entra por
   cidade ou por estado -- e estado vira `municipality_id IN (as cidades da
   UF)`, que tambem e igualdade na coluna lider. `cnpj` no fim mantem a saida
   ordenada dentro de cada cidade, que e o que o `ORDER BY cnpj LIMIT` usa.

   CONSEQUENCIA CONHECIDA: numa busca por estado o Postgres varre uma faixa do
   indice por municipio e ordena o resultado antes do LIMIT (nao ha mais um
   indice que entregue a UF inteira ja ordenada). O custo passa a ser
   proporcional ao numero de linhas que casam, nao mais a tabela toda -- que
   era o problema real: com o filtro de cidade fora do indice, a pagina saia
   de uma varredura da PK de `establishments` em ordem de `cnpj` nacional.

3. `establishments` fica so com PK, unique e os indices de FK. Todos os outros
   existiam pra filtros que a rota nao aceita mais (nome, situacao, porte,
   data, CNAE principal, UF). Indice que ninguem le e escrita cara: sao ~72M
   linhas reconstruidas a cada import.

4. `municipalities.uf` vira SMALLINT com o mesmo codigo de `establishments.uf`
   (app/regions.py), ganha indice `(uf, ibge_code)` -- os dois caminhos de
   entrada da busca -- e ganha `country_id` apontando pra `countries`.

Nada aqui preenche dado: `establishment_cnaes` e `establishments` sao
reconstruidas do zero pelo proximo `import-all` (ver _build_final_table).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b2d5f8a91c04'
down_revision: Union[str, None] = 'a1c4e7f20b13'
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


# Congelado aqui de proposito, e nao importado de app.regions: uma migration
# precisa continuar significando a mesma coisa se o mapa mudar no codigo.
UF_TO_CODE = [
    ('AC', 1), ('AL', 2), ('AM', 3), ('AP', 4), ('BA', 5), ('CE', 6), ('DF', 7),
    ('ES', 8), ('GO', 9), ('MA', 10), ('MG', 11), ('MS', 12), ('MT', 13),
    ('PA', 14), ('PB', 15), ('PE', 16), ('PI', 17), ('PR', 18), ('RJ', 19),
    ('RN', 20), ('RO', 21), ('RR', 22), ('RS', 23), ('SC', 24), ('SE', 25),
    ('SP', 26), ('TO', 27), ('EX', 28),
]

# Indices de `establishments` que somem: nenhum e PK, unique ou coluna de FK.
# `ix_establishments_cep` fica de fora da lista -- `cep` e FK pra postal_codes.
ESTABLISHMENTS_DROPPED_INDEXES = [
    ('ix_establishments_cellphone', ['cellphone'], 'cellphone IS NOT NULL', None),
    ('ix_establishments_uf_confidence', None, None, None),
    ('ix_establishments_main_cnae', ['main_cnae'], None, None),
    ('ix_establishments_registration_status', ['registration_status'], None, None),
    ('ix_establishments_company_name_trgm', ['company_name'], None, 'gin'),
    ('ix_establishments_trade_name_trgm', ['trade_name'], 'trade_name IS NOT NULL', 'gin'),
]


def upgrade() -> None:
    # -- establishment_cnaes: tres indices e duas colunas viram um indice ------
    op.drop_index('ix_establishment_cnaes_cnae_uf_cnpj', table_name='establishment_cnaes')
    op.drop_index('ix_establishment_cnaes_cnae_municipality_cnpj', table_name='establishment_cnaes')
    op.drop_index('ix_establishment_cnaes_cellphone', table_name='establishment_cnaes')

    op.drop_column('establishment_cnaes', 'uf')
    op.drop_column('establishment_cnaes', 'is_main')

    op.create_index(
        'ix_establishment_cnaes_municipality_cnae_cnpj',
        'establishment_cnaes', ['municipality_id', 'cnae', 'cnpj'],
    )

    # -- establishments: so PK, unique e FK -----------------------------------
    for name, _cols, _where, _using in ESTABLISHMENTS_DROPPED_INDEXES:
        op.drop_index(name, table_name='establishments')

    # -- municipalities: UF numerica, indice de entrada, vinculo com pais ------
    cases = " ".join(f"WHEN '{uf}' THEN {code}" for uf, code in UF_TO_CODE)
    op.execute(
        "ALTER TABLE municipalities "
        "ALTER COLUMN uf TYPE smallint "
        f"USING CASE upper(btrim(uf)) {cases} ELSE NULL END"
    )

    op.add_column('municipalities', sa.Column('country_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'municipalities_country_id_fkey', 'municipalities', 'countries', ['country_id'], ['id'],
    )
    # Base so tem municipio brasileiro; o pais e o mesmo pra todos. Best-effort:
    # se a tabela de referencia da Receita ainda nao foi importada, fica NULL e
    # o proximo import-all preenche (ver _import_reference).
    op.execute(
        "UPDATE municipalities SET country_id = c.id FROM countries c "
        "WHERE btrim(c.code) = '105' AND municipalities.country_id IS NULL"
    )

    op.create_index('ix_municipalities_uf_ibge_code', 'municipalities', ['uf', 'ibge_code'])


def downgrade() -> None:
    op.drop_index('ix_municipalities_uf_ibge_code', table_name='municipalities')
    op.drop_constraint('municipalities_country_id_fkey', 'municipalities', type_='foreignkey')
    op.drop_column('municipalities', 'country_id')

    cases = " ".join(f"WHEN {code} THEN '{uf}'" for uf, code in UF_TO_CODE)
    op.execute(
        "ALTER TABLE municipalities "
        "ALTER COLUMN uf TYPE varchar(2) "
        f"USING CASE uf {cases} ELSE NULL END"
    )

    for name, cols, where, using in ESTABLISHMENTS_DROPPED_INDEXES:
        if name == 'ix_establishments_uf_confidence':
            # Expressao com DESC nas duas ultimas colunas -- op.create_index nao
            # a expressa, entao volta em SQL.
            op.execute(
                "CREATE INDEX ix_establishments_uf_confidence ON establishments "
                "(uf, cellphone_confidence DESC, cnpj DESC)"
            )
            continue
        kwargs = {}
        if where:
            kwargs['postgresql_where'] = sa.text(where)
        if using:
            kwargs['postgresql_using'] = using
            kwargs['postgresql_ops'] = {cols[0]: f'{using}_trgm_ops'}
        op.create_index(name, 'establishments', cols, **kwargs)

    op.drop_index('ix_establishment_cnaes_municipality_cnae_cnpj', table_name='establishment_cnaes')

    op.add_column('establishment_cnaes', sa.Column('is_main', sa.Boolean(), nullable=True))
    op.add_column('establishment_cnaes', sa.Column('uf', sa.SmallInteger(), nullable=True))

    op.create_index('ix_establishment_cnaes_cnae_uf_cnpj', 'establishment_cnaes', ['cnae', 'uf', 'cnpj'])
    op.create_index(
        'ix_establishment_cnaes_cnae_municipality_cnpj',
        'establishment_cnaes', ['cnae', 'municipality_id', 'cnpj'],
    )
    op.create_index(
        'ix_establishment_cnaes_cellphone', 'establishment_cnaes', ['cnae', 'uf', 'cnpj'],
        postgresql_where=sa.text('has_cellphone'),
    )
