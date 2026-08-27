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
    main_cnae_code: str | None
    secondary_cnae_codes: list[str]
    municipio_name: str | None
    uf: str | None
    email: str | None
    phone: str | None
    cellphone: str | None
    cellphone_confidence: int
    opened_at: date | None

    model_config = {"from_attributes": True}


class EstablishmentPage(BaseModel):
    data: list[EstablishmentOut]
    total: int
    per_page: int
    current_page: int
    last_page: int


class CnaeOut(BaseModel):
    code: str
    description: str

    model_config = {"from_attributes": True}


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
