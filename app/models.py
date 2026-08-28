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
from sqlalchemy.dialects.postgresql import ARRAY
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
    cnpj: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    cnpj_basico: Mapped[int] = mapped_column(BigInteger)
    phone: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cellphone: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cnae_fiscal_principal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    municipio_codigo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data_inicio_atividade: Mapped[date | None] = mapped_column(Date, nullable=True)
    uf: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    situacao_cadastral: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    motivo_situacao_cadastral: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    cellphone_confidence: Mapped[int] = mapped_column(SmallInteger, default=0)
    is_headquarters: Mapped[bool] = mapped_column(Boolean, default=False)
    cnae_fiscal_secundaria: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    nome_fantasia: Mapped[str | None] = mapped_column(Text, nullable=True)
    correio_eletronico: Mapped[str | None] = mapped_column(Text, nullable=True)


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

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
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


class CepCoordenada(Base):
    """Latitude/longitude por CEP -- tabela própria, gerenciada por nós
    (não pelo edne-correios-loader, que reconstrói `correios_cep` do zero
    a cada import-ceps e destruiria qualquer coluna extra que a gente
    tentasse colar nela). Preenchida sob demanda quando alguém consulta
    GET /enderecos/cep/{cep} e o CEP ainda não tem coordenada salva."""

    __tablename__ = "cep_coordenadas"

    cep: Mapped[str] = mapped_column(String(8), primary_key=True)
    latitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)
    source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


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
