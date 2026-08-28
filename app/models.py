from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Municipio(Base):
    __tablename__ = "municipios"

    id: Mapped[int] = mapped_column(primary_key=True)
    receita_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    uf: Mapped[str | None] = mapped_column(String(2), nullable=True)


class Cnae(Base):
    __tablename__ = "cnaes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255))


class Establishment(Base):
    """Tabela final consultada pela API de busca. Reconstruida do zero a
    cada import (RENAME atomico), nunca atualizada linha a linha."""

    __tablename__ = "establishments"

    id: Mapped[int] = mapped_column(primary_key=True)
    cnpj: Mapped[str] = mapped_column(String(14), unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(255))
    trade_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_headquarters: Mapped[bool] = mapped_column(Boolean, default=False)
    is_mei: Mapped[bool] = mapped_column(Boolean, default=False)
    is_simples: Mapped[bool] = mapped_column(Boolean, default=False)
    company_size: Mapped[str | None] = mapped_column(String(2), nullable=True)
    main_cnae_code: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    secondary_cnae_codes: Mapped[list] = mapped_column(JSON, default=list)
    municipio_id: Mapped[int | None] = mapped_column(ForeignKey("municipios.id"), nullable=True)
    uf: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cellphone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    cellphone_confidence: Mapped[int] = mapped_column(SmallInteger, default=0)
    opened_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    municipio: Mapped[Municipio | None] = relationship()


class EmpresaStaging(Base):
    """CNPJ básico e ordem alfanuméricos a partir de 2026 (Receita) — só o
    dígito verificador (não presente aqui) continua numérico."""

    __tablename__ = "empresas_staging"
    __table_args__ = (UniqueConstraint("cnpj_basico", name="uq_empresas_staging_cnpj_basico"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cnpj_basico: Mapped[str] = mapped_column(String(8))
    razao_social: Mapped[str | None] = mapped_column(String(255), nullable=True)
    porte_empresa: Mapped[str | None] = mapped_column(String(2), nullable=True)
    source_file: Mapped[str] = mapped_column(String(60))


class SimplesStaging(Base):
    __tablename__ = "simples_staging"
    __table_args__ = (UniqueConstraint("cnpj_basico", name="uq_simples_staging_cnpj_basico"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cnpj_basico: Mapped[str] = mapped_column(String(8))
    opcao_simples: Mapped[str | None] = mapped_column(String(1), nullable=True)
    opcao_mei: Mapped[str | None] = mapped_column(String(1), nullable=True)
    source_file: Mapped[str] = mapped_column(String(60))


class EstabelecimentoStaging(Base):
    __tablename__ = "estabelecimentos_staging"
    __table_args__ = (
        UniqueConstraint("cnpj_basico", "cnpj_ordem", "cnpj_dv", name="uq_estabelecimentos_staging_cnpj"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cnpj_basico: Mapped[str] = mapped_column(String(8))
    cnpj_ordem: Mapped[str] = mapped_column(String(4))
    cnpj_dv: Mapped[str] = mapped_column(String(2))
    identificador_matriz_filial: Mapped[str | None] = mapped_column(String(1), nullable=True)
    nome_fantasia: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Sem index=True aqui de propósito: só é filtrado 2x no fim do import
    # (build da tabela final), um full scan nessas 2 vezes é mais barato do
    # que manter um índice btree atualizado a cada um dos milhões de UPSERTs
    # que populam essa tabela durante o import.
    situacao_cadastral: Mapped[str | None] = mapped_column(String(2), nullable=True)
    data_inicio_atividade: Mapped[date | None] = mapped_column(Date, nullable=True)
    cnae_fiscal_principal: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cnae_fiscal_secundaria: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    uf: Mapped[str | None] = mapped_column(String(2), nullable=True)
    municipio_codigo: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ddd_1: Mapped[str | None] = mapped_column(String(4), nullable=True)
    telefone_1: Mapped[str | None] = mapped_column(String(32), nullable=True)
    correio_eletronico: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_file: Mapped[str] = mapped_column(String(60))


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
    """Uma única linha (id=1), sobrescrita a cada passo -- exposta via
    GET /import/status pra monitoramento externo (dashboard, alerta)."""

    __tablename__ = "import_progress"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="idle")  # idle|running|success|failed
    group: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_file: Mapped[str | None] = mapped_column(String(60), nullable=True)
    step: Mapped[str | None] = mapped_column(String(20), nullable=True)  # download|extract|import|build
    # BigInteger -- no passo "download"/"extract" isso conta bytes, não
    # linhas, e um arquivo de >2GB (ex. Estabelecimentos) já passa do
    # limite de um Integer de 32 bits (visto na prática: NumericValueOutOfRange).
    processed_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
