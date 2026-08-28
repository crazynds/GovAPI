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


class Municipio(Base):
    __tablename__ = "municipios"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Integer (nao texto) pro JOIN do build casar tipo com
    # estabelecimentos_staging.municipio_codigo sem CAST. A API continua
    # falando em string -- ver app/routers/municipios.py.
    receita_code: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    uf: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # Preenchidos por `python -m app.cli import-ibge` (IBGE/SIDRA) -- ver
    # app/importer/ibge.py. Nulos até essa importação rodar pela primeira vez.
    ibge_code: Mapped[str | None] = mapped_column(String(7), nullable=True, index=True)
    population: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    area_km2: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    # Centroide do municipio (Nominatim/OSM) -- ver `import-municipios-geo`.
    # Usado como fallback de baixa precisao (nivel cidade) quando um CEP
    # ainda nao tem coordenada exata cacheada em cep_coordenadas.
    latitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)


class Cnae(Base):
    __tablename__ = "cnaes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255))


class NaturezaJuridica(Base):
    __tablename__ = "naturezas_juridicas"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255))


class Qualificacao(Base):
    """Qualificação de sócio/responsável (ex: "Administrador",
    "Diretor") -- mesma tabela usada tanto pra sócio quanto pra
    representante legal no arquivo de Sócios."""

    __tablename__ = "qualificacoes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255))


class Pais(Base):
    __tablename__ = "paises"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255))


class Motivo(Base):
    """Motivo da situação cadastral (por que a empresa foi baixada,
    incorporada, etc.) -- ver Establishment.situacao_cadastral."""

    __tablename__ = "motivos_situacao_cadastral"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255))


SITUACAO_ATIVA = 2


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
        # Parciais de proposito: as consultas da API sao quase sempre sobre
        # empresas ativas e/ou com contato, e um indice cheio sobre 63M linhas
        # custa varios GB pra indexar linhas que nunca sao lidas.
        Index("ix_establishments_cellphone", "cellphone", postgresql_where=text("cellphone IS NOT NULL")),
        Index("ix_establishments_uf", "uf", postgresql_where=text("situacao_cadastral = 2")),
        Index("ix_establishments_main_cnae", "main_cnae", postgresql_where=text("situacao_cadastral = 2")),
        Index(
            "ix_establishments_secondary_cnaes",
            "secondary_cnaes",
            postgresql_using="gin",
            postgresql_where=text("secondary_cnaes IS NOT NULL"),
        ),
        Index("ix_establishments_situacao_cadastral", "situacao_cadastral"),
        Index("ix_establishments_cep", "cep", postgresql_where=text("cep IS NOT NULL")),
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
    municipio_id: Mapped[int | None] = mapped_column(ForeignKey("municipios.id"), nullable=True)
    # Liga o estabelecimento a `correios_cep` (base e-DNE dos Correios), de onde
    # saem logradouro/bairro/municipio/UF na leitura -- por isso essas colunas
    # nao sao duplicadas aqui. INTEGER (4 bytes) e nao os 8 digitos como texto.
    #
    # NULL quando o CEP da Receita nao existe na base dos Correios (digitacao
    # errada, extinto, endereco no exterior). Nesses casos o endereco bruto vai
    # inteiro pra coluna `address` -- ver _build_final_table. E justamente por
    # virar NULL nesses casos que a FOREIGN KEY abaixo e possivel.
    cep: Mapped[int | None] = mapped_column(ForeignKey("correios_cep.cep"), nullable=True)
    opened_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    uf: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # ver app/regions.py
    company_size: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # 1 nula, 2 ativa, 3 suspensa, 4 inapta, 8 baixada -- todas as empresas
    # ficam aqui, nao so as ativas (ver SITUACAO_LABELS no router).
    situacao_cadastral: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    natureza_juridica: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    motivo_situacao_cadastral: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    cellphone_confidence: Mapped[int] = mapped_column(SmallInteger, default=0)
    is_headquarters: Mapped[bool] = mapped_column(Boolean, default=False)
    is_mei: Mapped[bool] = mapped_column(Boolean, default=False)
    is_simples: Mapped[bool] = mapped_column(Boolean, default=False)
    # NULL quando nao ha nenhum, nao array vazio -- um '{}' custa 24 bytes por
    # linha, e a maioria das empresas nao tem CNAE secundario.
    secondary_cnaes: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    company_name: Mapped[str] = mapped_column(Text)
    trade_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Especificos do estabelecimento, nao existem em `correios_cep`.
    address_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    address_complement: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Preenchidos SO quando o CEP existe mas nao resolve o logradouro: CEP de
    # localidade (cidade pequena com um CEP so) nao tem rua em `correios_cep`,
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

    municipio: Mapped[Municipio | None] = relationship()


# UNLOGGED em todas as tabelas de staging: elas sao inteiramente
# reconstruiveis (o pipeline refaz o arquivo em caso de falha, ver ImportLog) e
# truncadas no fim de todo import, entao pagar WAL por ~63M linhas so gasta
# disco e tempo. Custo: um crash do Postgres em si zera a tabela -- que e
# exatamente o que o retry do pipeline ja faria.
def _staging() -> dict:
    return {"prefixes": ["UNLOGGED"]}


class EmpresaStaging(Base):
    """CNPJ basico alfanumerico a partir de 2026 (Receita), guardado em base 36
    -- ver app/cnpj.py."""

    __tablename__ = "empresas_staging"
    __table_args__ = _staging()

    cnpj_basico: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    porte_empresa: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    natureza_juridica: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    razao_social: Mapped[str | None] = mapped_column(Text, nullable=True)


class SimplesStaging(Base):
    __tablename__ = "simples_staging"
    __table_args__ = _staging()

    cnpj_basico: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    opcao_simples: Mapped[bool] = mapped_column(Boolean, default=False)
    opcao_mei: Mapped[bool] = mapped_column(Boolean, default=False)


class EstabelecimentoStaging(Base):
    """Espelho do arquivo de Estabelecimentos, ja nos tipos finais -- a
    conversao (base 36, int, telefone parseado) acontece no COPY, entao o build
    da tabela final e um INSERT ... SELECT sem nenhum CAST.

    Sem indice secundario nenhum de proposito: e varrida inteira uma vez no
    build, e um full scan sai mais barato que manter um btree atualizado a cada
    um dos ~63M UPSERTs que populam a tabela.
    """

    __tablename__ = "estabelecimentos_staging"
    __table_args__ = _staging()

    # Corpo do CNPJ (raiz + ordem) em base 36. O DV do CSV nao e guardado: e
    # derivado do corpo, e o import so o usa pra conferir a fonte.
    # Sem `cnpj_basico`: ele e o proprio cnpj sem as 4 ultimas posicoes, o que
    # em base 36 e uma divisao inteira por 36^4. Guardar 8 bytes por linha pra
    # repetir o que ja esta no cnpj custaria ~500MB nas ~63M linhas; o JOIN do
    # build calcula na hora (ver ORDEM_SPAN em app/cnpj.py).
    cnpj: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    phone: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cellphone: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cnae_fiscal_principal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    municipio_codigo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cep: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data_inicio_atividade: Mapped[date | None] = mapped_column(Date, nullable=True)
    uf: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    situacao_cadastral: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    motivo_situacao_cadastral: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    cellphone_confidence: Mapped[int] = mapped_column(SmallInteger, default=0)
    is_headquarters: Mapped[bool] = mapped_column(Boolean, default=False)
    cnae_fiscal_secundaria: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    nome_fantasia: Mapped[str | None] = mapped_column(Text, nullable=True)
    correio_eletronico: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Logradouro/bairro entram aqui crus e podem virar NULL no build, se o CEP
    # ja resolver o endereco em `correios_cep`.
    logradouro: Mapped[str | None] = mapped_column(Text, nullable=True)
    numero: Mapped[str | None] = mapped_column(Text, nullable=True)
    complemento: Mapped[str | None] = mapped_column(Text, nullable=True)
    bairro: Mapped[str | None] = mapped_column(Text, nullable=True)


class Socio(Base):
    """Quadro societario -- um socio (PF/PJ/estrangeiro) por linha, uma
    empresa (cnpj_basico) pode ter varias. Tabela final direta (sem staging
    + swap): cada Socios<N>.zip cobre uma faixa disjunta de cnpj_basico
    (mesmo particionamento de Empresas/Estabelecimentos), entao nao tem o
    que fazer merge entre arquivos -- so carregar. Zerada no inicio do
    grupo "socios" a cada import completo (ver run_import), pra nao
    acumular duplicado mes a mes.

    ~24M linhas, entao vale a mesma compactacao da tabela de estabelecimentos.
    """

    __tablename__ = "socios"
    __table_args__ = (
        Index("ix_socios_cnpj_basico", "cnpj_basico"),
        Index("ix_socios_cpf_cnpj_socio", "cpf_cnpj_socio", postgresql_where=text("cpf_cnpj_socio IS NOT NULL")),
    )

    # Integer e nao BigInteger: sao ~24M linhas, e o TRUNCATE do inicio do
    # grupo reinicia a sequence (RESTART IDENTITY), entao o contador nao
    # acumula import a import ate estourar os 2,1 bilhoes.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cnpj_basico: Mapped[int] = mapped_column(BigInteger)
    # PF: a Receita ja entrega o CPF mascarado (LGPD, "***123456**") -- so os 6
    # digitos do meio variam, e e isso que fica guardado aqui. PJ/estrangeiro:
    # o CNPJ completo em base 36. `identificador_socio` diz qual dos dois e.
    cpf_cnpj_socio: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    representante_legal: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    data_entrada_sociedade: Mapped[date | None] = mapped_column(Date, nullable=True)
    identificador_socio: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # 1=PJ 2=PF 3=Estrangeiro
    qualificacao_socio: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    qualificacao_representante_legal: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    pais: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    faixa_etaria: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    nome_socio: Mapped[str] = mapped_column(Text)
    nome_representante: Mapped[str | None] = mapped_column(Text, nullable=True)


class Cep(Base):
    """Tudo que sabemos sobre um CEP: o endereco (base e-DNE dos Correios) e a
    coordenada (extrato do OSM em massa, ou BrasilAPI sob demanda).

    Eram duas tabelas, `correios_cep` e `cep_coordenadas`, com a mesma chave.
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

    __tablename__ = "correios_cep"

    cep: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    municipio_cod_ibge: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    logradouro: Mapped[str | None] = mapped_column(Text, nullable=True)
    complemento: Mapped[str | None] = mapped_column(Text, nullable=True)
    bairro: Mapped[str | None] = mapped_column(Text, nullable=True)
    municipio: Mapped[str | None] = mapped_column(Text, nullable=True)
    nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    # De onde veio a coordenada: 'osm_extract', 'brasilapi', ou
    # 'brasilapi_sem_coordenada' pra marcar "ja perguntei e nao tem" e nao
    # bater na API de novo pelo mesmo CEP. NULL = nunca foi buscada.
    coord_source: Mapped[str | None] = mapped_column(String(30), nullable=True)


class ImportLog(Base):
    """Marca "arquivo X do periodo Y ja foi baixado e carregado com
    sucesso" -- usado pra retomar de onde parou em vez de refazer tudo."""

    __tablename__ = "import_log"
    __table_args__ = (UniqueConstraint("period", "filename", name="uq_import_log_period_filename"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(10))
    filename: Mapped[str] = mapped_column(String(60))
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[datetime] = mapped_column(DateTime)


class ImportProgress(Base):
    """Uma linha por estagio do pipeline (download/extract/import/build) --
    exposta via GET /import/status pra monitoramento externo.

    Uma linha por estagio, e nao uma linha global, porque os estagios rodam em
    paralelo (ver app/importer/pipeline.py): num dado instante ha tres arquivos
    diferentes em tres estagios diferentes, e um `current_file` escalar so nao
    representa isso. Cada thread escreve so a sua linha, o que tambem evita
    contencao entre elas.
    """

    __tablename__ = "import_progress"

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
    """Estado global de uma execucao do import -- o que antes era o
    `status`/`period` da linha unica de import_progress. Uma linha so (id=1),
    escrita pela thread principal; os estagios ficam em ImportProgress."""

    __tablename__ = "import_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="idle")  # idle|running|success|failed
    message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ImportAllRun(Base):
    """Estado do comando `import-all` em si -- as 5 fases que ele encadeia
    (CEPs, coordenadas OSM, CNPJ, IBGE, centroide de municipio), nao o
    pipeline do CNPJ (que ja tem o proprio ImportRun/ImportProgress). Uma
    linha so (id=1).

    Existe pra `import-all` retomar de onde parou se for cancelado no meio:
    cada fase tem seu proprio status, e uma nova chamada pula toda fase ja
    'success' -- MAS so enquanto a tentativa anterior nao tiver terminado com
    sucesso. Se `status` (o geral) for 'success', a proxima chamada e um
    refresh periodico de verdade (mes que vem, novo periodo de CNPJ, e-DNE
    atualizado) e reprocessa as 5 fases do zero -- ver app.cli.import_all,
    que decide isso comparando o status geral antes de começar.
    """

    __tablename__ = "import_all_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="idle")  # idle|running|success|failed
    # pending|running|success|failed|skipped, uma coluna por fase (na ordem
    # em que import_all as executa).
    ceps: Mapped[str] = mapped_column(String(20), default="pending")
    ceps_osm: Mapped[str] = mapped_column(String(20), default="pending")
    cnpj: Mapped[str] = mapped_column(String(20), default="pending")
    ibge: Mapped[str] = mapped_column(String(20), default="pending")
    municipios_geo: Mapped[str] = mapped_column(String(20), default="pending")
    message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
