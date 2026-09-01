from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Municipality(Base):
    """Nasce da API de localidades do IBGE (`import-municipalities`, sem chave,
    uma request só) -- ibge_code/name/uf exatos, sem fuzzy match. Roda antes
    de tudo (CEPs, CNPJ): é o que dá a `postal_codes` uma FK de verdade pra
    cá, e o que fecha establishments.cep -> postal_codes.municipality_ibge_code
    -> municipalities.ibge_code.

    `receita_code` só chega depois, com o `Municipios.zip` da própria Receita
    (grupo "reference" do import-cnpj) -- esse arquivo não traz UF nem código
    IBGE, só código+nome, então o casamento com as linhas já existentes (via
    IBGE) é por nome normalizado. Nullable: uma linha pode existir só com o
    lado IBGE até esse import rodar (ou pra sempre, no raro caso de nome sem
    correspondência exata -- ver app.importer.pipeline._import_reference).
    """

    __tablename__ = "municipalities"

    id: Mapped[int] = mapped_column(primary_key=True)
    receita_code: Mapped[int | None] = mapped_column(Integer, unique=True, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    uf: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # Integer (nao String) pra poder ser alvo de FOREIGN KEY de
    # postal_codes.municipality_ibge_code, que ja e Integer -- Postgres nao aceita
    # FK entre tipos diferentes. unique=True pela mesma razao: FK exige indice
    # unico do lado referenciado.
    ibge_code: Mapped[int | None] = mapped_column(Integer, unique=True, index=True, nullable=True)
    population: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    area_km2: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    # Centroide do municipio (Nominatim/OSM) -- ver `import-municipalities-geo`.
    # Usado como fallback de baixa precisao (nivel cidade) quando um CEP
    # ainda nao tem coordenada exata cacheada em `postal_codes`.
    latitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)


class Cnae(Base):
    __tablename__ = "cnaes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255))


class LegalNature(Base):
    __tablename__ = "legal_natures"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255))


class Qualification(Base):
    """Qualificação de sócio/responsável (ex: "Administrador",
    "Diretor") -- mesma tabela usada tanto pra sócio quanto pra
    representante legal no arquivo de Sócios."""

    __tablename__ = "qualifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255))


class Country(Base):
    __tablename__ = "countries"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255))


class RegistrationStatusReason(Base):
    """Motivo da situação cadastral (por que a empresa foi baixada,
    incorporada, etc.) -- ver Establishment.registration_status."""

    __tablename__ = "registration_status_reasons"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255))


ACTIVE_STATUS = 2


class Establishment(Base):
    """Tabela final consultada pela API de busca. Reconstruida do zero a
    cada import (RENAME atomico), nunca atualizada linha a linha.

    Tudo que e numero por natureza e guardado como numero, e o CNPJ como
    inteiro em base 36 (ver app/cnpj.py) -- sao ~63M linhas, cada byte por
    linha vale ~63MB. A formatacao (zero a esquerda, +55, pontuacao) e
    aplicada na saida, em app/routers/establishments.py.

    A ordem das colunas nao e cosmetica: as de largura fixa vem primeiro, em
    ordem decrescente de tamanho, pro Postgres nao gastar padding de
    alinhamento entre elas (47 bytes de parte fixa em vez de ~104).
    """

    __tablename__ = "establishments"
    __table_args__ = (
        # Parcial so onde o predicado esta SEMPRE no WHERE da query. `uf` e
        # `main_cnae` ja foram parciais em `registration_status = 2` e isso
        # quebrou a busca: o filtro de situacao e opcional na API, e sem ele no
        # WHERE o Postgres descarta o indice e cai em seq scan sobre 72M linhas
        # (ver DEFERRED_INDEXES em app/importer/pipeline.py).
        Index("ix_establishments_cellphone", "cellphone", postgresql_where=text("cellphone IS NOT NULL")),
        # O que serve o filtro por UF. O sufixo de ordenacao e vestigial (a API
        # so ordena pela PK), mas o prefixo `(uf)` e o que importa e refazer o
        # indice so pra encurtar nao paga.
        Index("ix_establishments_uf_confidence", "uf", text("cellphone_confidence DESC"), text("cnpj DESC")),
        Index("ix_establishments_main_cnae", "main_cnae"),
        Index("ix_establishments_registration_status", "registration_status"),
        Index("ix_establishments_cep", "cep", postgresql_where=text("cep IS NOT NULL")),
        # `?name=` e ILIKE '%x%', que btree nenhum avalia -- so um GIN de
        # trigramas serve. Sem isso a busca por nome varre a tabela inteira.
        Index("ix_establishments_company_name_trgm", "company_name",
              postgresql_using="gin", postgresql_ops={"company_name": "gin_trgm_ops"}),
        Index("ix_establishments_trade_name_trgm", "trade_name",
              postgresql_using="gin", postgresql_ops={"trade_name": "gin_trgm_ops"},
              postgresql_where=text("trade_name IS NOT NULL")),
    )
    # fillfactor = 100 (sem UPDATE depois do bulk load, nao faz sentido
    # reservar espaco livre por pagina pra HOT update) e aplicado por SQL na
    # migration e no _build_final_table -- SQLAlchemy nao expoe storage
    # parameters de tabela via __table_args__.

    # Base 36 das 12 posicoes alfanumericas; o DV nao e guardado (e derivado).
    cnpj: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # Nacional, sem o +55: 11987654321.
    phone: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cellphone: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    main_cnae: Mapped[int | None] = mapped_column(Integer, nullable=True)
    municipality_id: Mapped[int | None] = mapped_column(ForeignKey("municipalities.id"), nullable=True)
    # Liga o estabelecimento a `postal_codes` (base e-DNE dos Correios), de onde
    # saem logradouro/bairro/municipio/UF na leitura -- por isso essas colunas
    # nao sao duplicadas aqui. INTEGER (4 bytes) e nao os 8 digitos como texto.
    #
    # NULL quando o CEP da Receita nao existe na base dos Correios (digitacao
    # errada, extinto, endereco no exterior). Nesses casos o endereco bruto vai
    # inteiro pra coluna `address` -- ver _build_final_table. E justamente por
    # virar NULL nesses casos que a FOREIGN KEY abaixo e possivel.
    cep: Mapped[int | None] = mapped_column(ForeignKey("postal_codes.cep"), nullable=True)
    opened_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    uf: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # ver app/regions.py
    company_size: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # 1 nula, 2 ativa, 3 suspensa, 4 inapta, 8 baixada -- todas as empresas
    # ficam aqui, nao so as ativas (ver STATUS_LABELS no router).
    registration_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    legal_nature: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    registration_status_reason: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    cellphone_confidence: Mapped[int] = mapped_column(SmallInteger, default=0)
    is_headquarters: Mapped[bool] = mapped_column(Boolean, default=False)
    is_mei: Mapped[bool] = mapped_column(Boolean, default=False)
    is_simples: Mapped[bool] = mapped_column(Boolean, default=False)
    # NULL quando nao ha nenhum, nao array vazio -- um '{}' custa 24 bytes por
    # linha, e a maioria das empresas nao tem CNAE secundario.
    company_name: Mapped[str] = mapped_column(Text)
    trade_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Especificos do estabelecimento, nao existem em `postal_codes`.
    address_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    address_complement: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Preenchidos SO quando o CEP existe mas nao resolve o logradouro: CEP de
    # localidade (cidade pequena com um CEP so) nao tem rua em `postal_codes`,
    # e ai o dado da Receita e a unica fonte. Quando o CEP resolve, ficam NULL
    # e a leitura pega do join -- ver _build_final_table.
    street: Mapped[str | None] = mapped_column(Text, nullable=True)
    district: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Endereco bruto da Receita, so pras linhas SEM vinculo de CEP (CEP ausente
    # ou fora da base dos Correios). NULL em todo o resto, que e a grande
    # maioria -- por isso um JSON aqui nao pesa: nao ha um blob por linha, ha um
    # por excecao. Guarda o registro inteiro (logradouro, numero, complemento,
    # bairro, cep como veio) pra nao perder o endereco de quem nao casou.
    address: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    municipality: Mapped[Municipality | None] = relationship()


class EstablishmentCnae(Base):
    """Relacao N:N entre estabelecimento e CNAE -- uma linha por (empresa, CNAE).

    Substitui o par `main_cnae` + `secondary_cnaes` (array) como CAMINHO DE
    BUSCA. `establishments.main_cnae` continua existindo, porque e dimensao do
    agregado de /stats e sai em toda resposta; o array de secundarios saiu, e o
    conteudo dele agora vive aqui, uma linha por codigo, com `is_main`
    distinguindo o principal.

    RegistrationStatusReason: o filtro `?cnae_codes=` era `main_cnae = X OR secondary_cnaes && [X]`,
    um OR entre um btree e um GIN que nao produz saida ordenada por `cnpj`.
    Com `ORDER BY cnpj LIMIT n` o planner ou ordenava o conjunto filtrado
    inteiro, ou (o que ele escolhia) varria a PK linha a linha filtrando --
    a tabela toda, ~63M linhas, e timeout. Aqui o mesmo filtro e igualdade
    numa coluna so, e um indice terminado em `cnpj` entrega a pagina em ordem
    sem ordenar nada.

    `uf`, `municipality_id` e `has_cellphone` sao COPIAS de `establishments` --
    de proposito. Sem elas o banco acha os candidatos por CNAE aqui e precisa
    sondar a PK da tabela grande um por um pra descobrir quem e do RS e tem
    celular; num recorte seletivo isso e o gargalo de volta. Pior no caso da
    cidade: com `municipality_id` so na tabela grande, CNAE + municipio vira um
    BitmapAnd entre dois indices, que monta os dois bitmaps inteiros antes da
    primeira linha e perde a ordem por `cnpj`. Com as copias aqui, filtro e
    ordem saem de um indice unico, sem tocar em `establishments`.

    Duplicar coluna normalmente e divida de manutencao, mas aqui nao ha update
    possivel: as duas tabelas sao reconstruidas do zero a cada import e
    trocadas no MESMO RENAME atomico (ver _build_final_table). Sao esses tres
    filtros copiados porque sao os que aparecem em praticamente toda busca --
    os outros (porte, situacao, MEI, data) continuam sendo resolvidos no join.

    Sem FOREIGN KEY pra `establishments`: as duas trocam de nome no mesmo swap,
    e uma FK entre elas so criaria ordem obrigatoria no RENAME em troca de
    garantia nenhuma (ambas saem do mesmo INSERT, do mesmo snapshot).

    Ordem das colunas pelo mesmo motivo de `Establishment`: largura fixa
    decrescente, 20 bytes de parte fixa sem padding.
    """

    __tablename__ = "establishment_cnaes"
    __table_args__ = (
        # O indice da busca. `cnae` primeiro porque e sempre igualdade e e o
        # que define a consulta; `uf` em seguida porque e o filtro mais comum
        # depois dele; `cnpj` no fim pra saida ja sair na ordem do ORDER BY --
        # e isso que faz o LIMIT parar cedo em vez de ordenar o conjunto todo.
        Index("ix_establishment_cnaes_cnae_uf_cnpj", "cnae", "uf", "cnpj"),
        # O mesmo, mas pro recorte por cidade. Existe pra que o filtro
        # CNAE + municipio saia de UM indice so: com indices separados
        # (`cnae` aqui, `municipality_id` em `establishments`) o Postgres
        # intersecta os dois num BitmapAnd, que precisa montar os dois bitmaps
        # INTEIROS antes de emitir a primeira linha e devolve o resultado em
        # ordem de pagina fisica -- ou seja, o LIMIT deixa de cortar cedo e o
        # Sort volta. Com as duas colunas no mesmo indice e igualdade nas duas,
        # `cnpj` ja sai ordenado e a pagina para na 25a linha.
        Index("ix_establishment_cnaes_cnae_municipality_cnpj", "cnae", "municipality_id", "cnpj"),
        # Mesma coisa, podado pra quem tem celular -- o default da API e
        # `only_with_cellphone=true`. Parcial e seguro aqui (ao contrario do
        # que aconteceu com `registration_status = 2`, ver DEFERRED_INDEXES):
        # quando o cliente manda `only_with_cellphone=false` o predicado sai do
        # WHERE, o Postgres descarta este indice e usa o de cima, que cobre
        # todas as linhas.
        Index("ix_establishment_cnaes_cellphone", "cnae", "uf", "cnpj",
              postgresql_where=text("has_cellphone")),
    )

    # (cnpj, cnae) e a chave natural e serve as duas coisas: garante que nao ha
    # linha repetida (o principal as vezes repete na lista de secundarios) e
    # responde a leitura por pagina -- os CNAEs das ~25 empresas da resposta,
    # que e como a serializacao remonta o que era o array.
    cnpj: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    cnae: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    # Copia de `establishments.municipality_id`, pelo mesmo motivo que `uf`
    # (ver o docstring). Vem logo depois de `cnae` porque as duas sao de 4
    # bytes: a parte fixa vai de 16 pra 20 bytes, sem padding novo.
    municipality_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uf: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    is_main: Mapped[bool] = mapped_column(Boolean)
    has_cellphone: Mapped[bool] = mapped_column(Boolean)


# UNLOGGED em todas as tabelas de staging: elas sao inteiramente
# reconstruiveis (o pipeline refaz o arquivo em caso de falha, ver ImportLog) e
# truncadas no fim de todo import, entao pagar WAL por ~63M linhas so gasta
# disco e tempo. Custo: um crash do Postgres em si zera a tabela -- que e
# exatamente o que o retry do pipeline ja faria.
def _staging() -> dict:
    return {"prefixes": ["UNLOGGED"]}


class EstablishmentStats(Base):
    """Agregado pre-calculado de `establishments`, pra /establishments/stats.

    Existe por uma propriedade especifica desta base: `establishments` e
    reconstruida inteira a cada import e NUNCA escrita enquanto esta em uso.
    Nao ha invalidacao a fazer, nem risco de o agregado divergir dos dados no
    meio do dia -- ele e montado do mesmo snapshot, no mesmo build, e trocado
    no mesmo RENAME atomico (ver _build_final_table).

    O grao sao as dimensoes de baixa cardinalidade que /stats filtra e agrupa;
    as medidas sao contagens, entao a resposta e `sum()` sobre este agregado em
    vez de `count()` sobre 72M linhas. Reduz de ~72M pra ~1-3M linhas.

    NAO cobre todo filtro do endpoint: `name` (ILIKE), `opened_at`,
    `municipality_codes` e CNAE secundario ficam de fora -- os tres primeiros por
    cardinalidade, o ultimo porque uma empresa tem varios CNAEs e desnormalizar
    contaria ela mais de uma vez (e o que `EstablishmentCnaeStats` resolve, pra
    um codigo por consulta). Pedido que use qualquer um deles cai na tabela
    grande; quem decide e `uncovered` no router.
    """

    __tablename__ = "establishments_stats"
    __table_args__ = (
        # A tabela e pequena o bastante pra varrer inteira, mas UF e o filtro
        # mais comum de longe e corta 27x logo de cara.
        Index("ix_establishments_stats_uf", "uf"),
        Index("ix_establishments_stats_main_cnae", "main_cnae"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Dimensoes -- mesmos codigos numericos de `establishments`, todas
    # nullable pelos mesmos motivos que la.
    uf: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    registration_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    company_size: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    main_cnae: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_mei: Mapped[bool] = mapped_column(Boolean)
    is_simples: Mapped[bool] = mapped_column(Boolean)
    is_headquarters: Mapped[bool] = mapped_column(Boolean)

    # Medidas.
    total: Mapped[int] = mapped_column(BigInteger)
    with_cellphone: Mapped[int] = mapped_column(BigInteger)
    with_email: Mapped[int] = mapped_column(BigInteger)
    with_phone: Mapped[int] = mapped_column(BigInteger)
    # A intersecao, e nao e derivavel das outras duas. Sem ela,
    # `only_with_cellphone=true` nao tem resposta: restringir a populacao faz
    # `with_email` significar "tem os dois".
    with_cellphone_and_email: Mapped[int] = mapped_column(BigInteger)


class EstablishmentCnaeStats(Base):
    """Agregado por CNAE, contando o codigo como principal OU secundario.

    Existe separado de `EstablishmentStats` porque o filtro `?cnae_codes=` casa
    os dois, e uma empresa tem varios CNAEs: nao da pra ter principal e
    secundario como uma dimensao so sem desnormalizar. Medido em producao --
    CNAE 4781400 em PR: 240.771 como principal, 397.369 contando secundarios.
    Ignorar o secundario subnotificaria 39,4%.

    O grao e uma linha por (CNAE, dimensoes), com a empresa aparecendo em um
    balde por CNAE distinto que ela tem. CONSEQUENCIA IMPORTANTE: os baldes NAO
    podem ser somados entre CNAEs diferentes -- uma empresa com dois CNAEs esta
    em dois baldes e seria contada duas vezes. Dentro de um unico `cnae` a soma
    e exata, e e so assim que o router usa esta tabela (um codigo por consulta;
    varios caem na tabela grande).

    `main_cnae` nao e dimensao aqui de proposito: cruzar CNAE com CNAE
    principal multiplicaria a cardinalidade por ~1300 sem uso claro.
    """

    __tablename__ = "establishments_cnae_stats"
    __table_args__ = (
        # CNAE na frente: e sempre igualdade, e e o que define a consulta.
        Index("ix_establishments_cnae_stats_cnae_uf", "cnae", "uf"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    cnae: Mapped[int] = mapped_column(Integer)
    uf: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    registration_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    company_size: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    is_mei: Mapped[bool] = mapped_column(Boolean)
    is_simples: Mapped[bool] = mapped_column(Boolean)
    is_headquarters: Mapped[bool] = mapped_column(Boolean)

    total: Mapped[int] = mapped_column(BigInteger)
    with_cellphone: Mapped[int] = mapped_column(BigInteger)
    with_email: Mapped[int] = mapped_column(BigInteger)
    with_phone: Mapped[int] = mapped_column(BigInteger)
    with_cellphone_and_email: Mapped[int] = mapped_column(BigInteger)


class CompanyStaging(Base):
    """CNPJ basico alfanumerico a partir de 2026 (Receita), guardado em base 36
    -- ver app/cnpj.py."""

    __tablename__ = "companies_staging"
    __table_args__ = _staging()

    cnpj_root: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    company_size: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    legal_nature: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    company_name: Mapped[str | None] = mapped_column(Text, nullable=True)


class SimplesStaging(Base):
    __tablename__ = "simples_staging"
    __table_args__ = _staging()

    cnpj_root: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    simples_option: Mapped[bool] = mapped_column(Boolean, default=False)
    mei_option: Mapped[bool] = mapped_column(Boolean, default=False)


class EstablishmentStaging(Base):
    """Espelho do arquivo de Estabelecimentos, ja nos tipos finais -- a
    conversao (base 36, int, telefone parseado) acontece no COPY, entao o build
    da tabela final e um INSERT ... SELECT sem nenhum CAST.

    Sem indice secundario nenhum de proposito: e varrida inteira uma vez no
    build, e um full scan sai mais barato que manter um btree atualizado a cada
    um dos ~63M UPSERTs que populam a tabela.
    """

    __tablename__ = "establishments_staging"
    __table_args__ = _staging()

    # Corpo do CNPJ (raiz + ordem) em base 36. O DV do CSV nao e guardado: e
    # derivado do corpo, e o import so o usa pra conferir a fonte.
    # Sem `cnpj_root`: ele e o proprio cnpj sem as 4 ultimas posicoes, o que
    # em base 36 e uma divisao inteira por 36^4. Guardar 8 bytes por linha pra
    # repetir o que ja esta no cnpj custaria ~500MB nas ~63M linhas; o JOIN do
    # build calcula na hora (ver BRANCH_SPAN em app/cnpj.py).
    cnpj: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    phone: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cellphone: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    main_cnae: Mapped[int | None] = mapped_column(Integer, nullable=True)
    municipality_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cep: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activity_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    uf: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    registration_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    registration_status_reason: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    cellphone_confidence: Mapped[int] = mapped_column(SmallInteger, default=0)
    is_headquarters: Mapped[bool] = mapped_column(Boolean, default=False)
    secondary_cnaes: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    trade_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Logradouro/bairro entram aqui crus e podem virar NULL no build, se o CEP
    # ja resolver o endereco em `postal_codes`.
    street: Mapped[str | None] = mapped_column(Text, nullable=True)
    number: Mapped[str | None] = mapped_column(Text, nullable=True)
    complement: Mapped[str | None] = mapped_column(Text, nullable=True)
    district: Mapped[str | None] = mapped_column(Text, nullable=True)


class Partner(Base):
    """Quadro societario -- um socio (PF/PJ/estrangeiro) por linha, uma
    empresa (cnpj_root) pode ter varias. Tabela final direta (sem staging
    + swap): cada Socios<N>.zip cobre uma faixa disjunta de cnpj_root
    (mesmo particionamento de Empresas/Estabelecimentos), entao nao tem o
    que fazer merge entre arquivos -- so carregar. Zerada no inicio do
    grupo "partners" a cada import completo (ver run_import), pra nao
    acumular duplicado mes a mes.

    ~24M linhas, entao vale a mesma compactacao da tabela de estabelecimentos.
    """

    __tablename__ = "partners"
    __table_args__ = (
        Index("ix_partners_cnpj_root", "cnpj_root"),
        Index("ix_partners_partner_tax_id", "partner_tax_id", postgresql_where=text("partner_tax_id IS NOT NULL")),
        # `?name=` e ILIKE '%x%' -- ver o comentario em Establishment.
        Index("ix_partners_partner_name_trgm", "partner_name",
              postgresql_using="gin", postgresql_ops={"partner_name": "gin_trgm_ops"}),
    )

    # Integer e nao BigInteger: sao ~24M linhas, e o TRUNCATE do inicio do
    # grupo reinicia a sequence (RESTART IDENTITY), entao o contador nao
    # acumula import a import ate estourar os 2,1 bilhoes.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cnpj_root: Mapped[int] = mapped_column(BigInteger)
    # PF: a Receita ja entrega o CPF mascarado (LGPD, "***123456**") -- so os 6
    # digitos do meio variam, e e isso que fica guardado aqui. PJ/estrangeiro:
    # o CNPJ completo em base 36. `partner_type` diz qual dos dois e.
    partner_tax_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    legal_rep: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    partnership_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    partner_type: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # 1=PJ 2=PF 3=Estrangeiro
    partner_qualification: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    legal_rep_qualification: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    country: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    age_range: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    partner_name: Mapped[str] = mapped_column(Text)
    legal_rep_name: Mapped[str | None] = mapped_column(Text, nullable=True)


class PostalCode(Base):
    """Tudo que sabemos sobre um CEP: o endereco (base e-DNE dos Correios) e a
    coordenada (extrato do OSM em massa, ou BrasilAPI sob demanda).

    Eram duas tabelas, `postal_codes` e `cep_coordenadas`, com a mesma chave.
    Ficaram separadas enquanto o edne-correios-loader era dono do esquema e
    reconstruia a tabela a cada import, o que teria destruido uma coluna de
    coordenada colada nela. Hoje a lib so popula uma tabela de scratch e o
    merge e um upsert nosso que toca so as colunas de endereco -- entao a
    coordenada sobrevive ao import e as duas metades cabem numa tabela so.

    As duas metades sao independentes e ambas opcionais: ha CEP com endereco e
    sem coordenada (a maioria, ate o `import-ceps-osm` rodar) e CEP que so o
    OSM conhece, sem endereco nenhum. Por isso as colunas de endereco sao
    nullable -- antes eram NOT NULL porque a tabela so tinha uma metade.

    `establishments.cep` tem FOREIGN KEY pra ca. Funciona porque nada aqui e
    apagado: o import de CEP e upsert, nao substituicao.
    """

    __tablename__ = "postal_codes"
    __table_args__ = tuple(
        # Os tres filtros de texto de /addresses/search sao ILIKE '%x%'.
        Index(f"ix_postal_codes_{col}_trgm", col,
              postgresql_using="gin", postgresql_ops={col: "gin_trgm_ops"})
        for col in ("street", "district", "municipality")
    )

    cep: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    # NULL quando o e-DNE traz um codigo IBGE que nao existe na lista atual
    # (municipio historico/fundido/extinto) -- mesmo tratamento de CEP orfao
    # ja usado em establishments.cep: guardar um codigo que nao bate com nada
    # nao serviria pra nada e impediria a FK. Ver app.ceps.upsert_from.
    municipality_ibge_code: Mapped[int | None] = mapped_column(ForeignKey("municipalities.ibge_code"), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)
    coord_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    uf: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # Text (nao VARCHAR(n)) de proposito: o e-DNE real nao respeita as
    # larguras que o proprio schema do e-DNE declara (visto na pratica --
    # nome de logradouro/bairro estourando VARCHAR(36)/(100) e derrubando o
    # import com StringDataRightTruncation). Sem custo em Postgres: TEXT e
    # VARCHAR(n) tem a mesma representacao em disco, e e o mesmo padrao ja
    # usado em Establishment.company_name/trade_name/email.
    street: Mapped[str | None] = mapped_column(Text, nullable=True)
    complement: Mapped[str | None] = mapped_column(Text, nullable=True)
    district: Mapped[str | None] = mapped_column(Text, nullable=True)
    municipality: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # De onde veio a coordenada: 'osm_extract', 'brasilapi', ou
    # 'brasilapi_sem_coordenada' pra marcar "ja perguntei e nao tem" e nao
    # bater na API de novo pelo mesmo CEP. NULL = nunca foi buscada.
    coord_source: Mapped[str | None] = mapped_column(String(30), nullable=True)


class ImportFile(Base):
    """Marca "arquivo X do periodo Y ja foi baixado e carregado com
    sucesso" -- usado pra retomar de onde parou em vez de refazer tudo.

    Fica separada de `ImportRun` porque o grao e outro: uma linha por ARQUIVO
    por periodo (dezenas por import, apagadas no fim de cada periodo), nao o
    estado unico da execucao.
    """

    __tablename__ = "import_files"
    __table_args__ = (UniqueConstraint("period", "filename", name="uq_import_files_period_filename"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(10))
    filename: Mapped[str] = mapped_column(String(60))
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[datetime] = mapped_column(DateTime)


class ImportStep(Base):
    """Uma linha por estagio do pipeline de CNPJ (download/extract/import/
    build) -- exposta via GET /import/status pra monitoramento externo.

    Uma linha por estagio, e nao uma linha global, porque os estagios rodam em
    paralelo (ver app/importer/pipeline.py): num dado instante ha tres arquivos
    diferentes em tres estagios diferentes, e um `current_file` escalar so nao
    representa isso. Cada thread escreve so a sua linha, o que tambem evita
    contencao entre elas -- e e por isso que esta tabela NAO foi fundida em
    `ImportRun`, que e uma linha unica escrita pela thread principal.
    """

    __tablename__ = "import_steps"

    step: Mapped[str] = mapped_column(String(20), primary_key=True)  # download|extract|import|build
    period: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="idle")  # idle|running|success|failed
    group: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_file: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # BigInteger -- nos passos "download"/"extract" isso conta bytes, nao
    # linhas, e um arquivo de >2GB (ex. Estabelecimentos) ja passa do
    # limite de um Integer de 32 bits (visto na pratica: NumericValueOutOfRange).
    processed_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ImportRun(Base):
    """Estado da execucao do import, numa linha so (id=1).

    Funde o que eram `import_run` (estado global do pipeline de CNPJ) e
    `import_runs` (as 6 fases do comando `import-all`). Eram duas tabelas
    de UMA linha cada, escritas pela mesma thread principal, descrevendo a
    mesma execucao -- e a coluna `cnpj` daqui e exatamente o que
    `import_run.status` guardava: o status da fase de CNPJ. Manter as duas
    custava um JOIN (ou duas leituras) pra responder "como vai o import" sem
    ganhar isolamento nenhum.

    `status` e o geral (do `import-all`); as seis colunas de fase levam
    pending|running|success|failed|skipped, na ordem em que `import-all` as
    executa. A fase `cnpj` tem colunas `cnpj_*` proprias porque e a unica com
    um sub-pipeline atras dela (periodo, mensagem e relogio proprios, alem dos
    estagios em `ImportStep`).

    Existe pra `import-all` retomar de onde parou se for cancelado no meio:
    uma nova chamada pula toda fase ja 'success' -- MAS so enquanto a
    tentativa anterior nao tiver terminado com sucesso. Se `status` (o geral)
    for 'success', a proxima chamada e um refresh periodico de verdade (mes
    que vem, novo periodo de CNPJ, e-DNE atualizado) e reprocessa as 6 fases
    do zero -- ver app.cli.import_all, que decide isso comparando o status
    geral antes de começar.
    """

    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- geral (import-all) ---
    status: Mapped[str] = mapped_column(String(20), default="idle")  # idle|running|success|failed
    message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)

    # --- uma coluna por fase, na ordem de execucao ---
    municipalities: Mapped[str] = mapped_column(String(20), default="pending")
    ceps: Mapped[str] = mapped_column(String(20), default="pending")
    ceps_osm: Mapped[str] = mapped_column(String(20), default="pending")
    cnpj: Mapped[str] = mapped_column(String(20), default="pending")
    ibge: Mapped[str] = mapped_column(String(20), default="pending")
    municipalities_geo: Mapped[str] = mapped_column(String(20), default="pending")

    # --- detalhe da fase de CNPJ (o ex-`import_run`) ---
    cnpj_period: Mapped[str | None] = mapped_column(String(10), nullable=True)
    cnpj_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cnpj_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cnpj_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
