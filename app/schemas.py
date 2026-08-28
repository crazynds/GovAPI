from datetime import date

from pydantic import BaseModel


class EstablishmentOut(BaseModel):
    cnpj: str
    company_name: str
    trade_name: str | None
    is_headquarters: bool
    is_mei: bool
    is_simples: bool
    company_size: str | None
    company_size_label: str | None
    natureza_juridica_code: str | None
    natureza_juridica_description: str | None
    main_cnae_code: str | None
    main_cnae_description: str | None
    secondary_cnae_codes: list[str]
    secondary_cnae_descriptions: list[str]
    municipio_name: str | None
    uf: str | None
    email: str | None
    phone: str | None
    cellphone: str | None
    cellphone_confidence: int
    opened_at: date | None
    situacao_cadastral: str | None
    situacao_cadastral_label: str | None
    motivo_situacao_cadastral_code: str | None
    motivo_situacao_cadastral_description: str | None

    model_config = {"from_attributes": True}


class CodeDescriptionOut(BaseModel):
    code: str
    description: str

    model_config = {"from_attributes": True}


class SocioOut(BaseModel):
    cnpj_basico: str
    identificador_socio: str | None
    identificador_socio_label: str | None
    nome_socio: str
    cpf_cnpj_socio: str | None
    qualificacao_socio_code: str | None
    qualificacao_socio_description: str | None
    data_entrada_sociedade: date | None
    pais_code: str | None
    pais_description: str | None
    representante_legal: str | None
    nome_representante: str | None
    qualificacao_representante_code: str | None
    qualificacao_representante_description: str | None
    faixa_etaria_label: str | None
    company_name: str | None


class SocioPageOut(BaseModel):
    data: list[SocioOut]
    total: int
    per_page: int
    current_page: int
    last_page: int


class EstablishmentPage(BaseModel):
    data: list[EstablishmentOut]
    total: int
    per_page: int
    current_page: int
    last_page: int


class CnaeCountOut(BaseModel):
    cnae_code: str
    total: int


class EstablishmentStatsOut(BaseModel):
    total: int
    with_cellphone: int
    with_email: int
    by_uf: dict[str, int]
    by_regiao: dict[str, int]
    by_company_size: dict[str, int]
    top_cnaes: list[CnaeCountOut]


class CnaeOut(BaseModel):
    code: str
    description: str

    model_config = {"from_attributes": True}


class AnexoOut(BaseModel):
    anexo: str
    descricao: str


class FaixaOut(BaseModel):
    faixa: int
    rbt12_ate: float
    aliquota_nominal: float
    valor_a_deduzir: float


class AnexoTabelaOut(BaseModel):
    anexo: str
    descricao: str
    faixas: list[FaixaOut]


class CalculoSimplesOut(BaseModel):
    anexo: str
    rbt12: float
    faixa: int
    aliquota_nominal: float
    valor_a_deduzir: float
    aliquota_efetiva: float
    receita_mes: float
    valor_imposto: float


class FatorROut(BaseModel):
    fator_r: float
    limite: float
    elegivel_anexo_iii: bool
    anexo_sugerido: str


class ImportStatusOut(BaseModel):
    period: str | None
    status: str
    group: str | None
    current_file: str | None
    step: str | None
    processed_rows: int
    message: str | None
    started_at: str | None
    updated_at: str | None
