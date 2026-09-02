from datetime import date

from pydantic import BaseModel


class AddressOut(BaseModel):
    """Endereço do estabelecimento.

    Só CEP, número e complemento ficam guardados por estabelecimento;
    logradouro/bairro/município/UF vêm da base dos Correios pelo CEP, e só
    são gravados quando o CEP não os resolve (CEP de localidade). Ver
    Establishment em app/models.py.
    """

    cep: str | None
    street: str | None
    number: str | None
    complement: str | None
    district: str | None
    municipality: str | None
    uf: str | None
    # De onde vieram logradouro/bairro: "correios" (pelo CEP) ou "receita"
    # (o CEP não resolve, então valeu o dado da Receita).
    source: str | None


class EstablishmentOut(BaseModel):
    cnpj: str
    company_name: str
    trade_name: str | None
    is_headquarters: bool
    is_mei: bool
    is_simples: bool
    company_size: str | None
    company_size_label: str | None
    legal_nature_code: str | None
    legal_nature_description: str | None
    main_cnae_code: str | None
    main_cnae_description: str | None
    secondary_cnae_codes: list[str]
    secondary_cnae_descriptions: list[str]
    municipality_name: str | None
    uf: str | None
    email: str | None
    phone: str | None
    cellphone: str | None
    cellphone_confidence: int
    opened_at: date | None
    registration_status: str | None
    registration_status_label: str | None
    registration_status_reason_code: str | None
    registration_status_reason_description: str | None
    address: AddressOut

    model_config = {"from_attributes": True}


class CodeDescriptionOut(BaseModel):
    code: str
    description: str

    model_config = {"from_attributes": True}


class PartnerOut(BaseModel):
    cnpj_root: str
    partner_type: str | None
    partner_type_label: str | None
    partner_name: str
    partner_tax_id: str | None
    partner_qualification_code: str | None
    partner_qualification_description: str | None
    partnership_start_date: date | None
    country_code: str | None
    country_description: str | None
    legal_rep: str | None
    legal_rep_name: str | None
    legal_rep_qualification_code: str | None
    legal_rep_qualification_description: str | None
    age_range_label: str | None
    company_name: str | None


class CursorPage(BaseModel):
    """Pagina de uma busca paginada por cursor.

    Nao tem `total` nem `last_page` de proposito: produzir esses numeros exige
    um `count()` sobre o resultado inteiro, que numa tabela de dezenas de
    milhoes de linhas custa o mesmo que ler tudo -- era o que derrubava
    /establishments por timeout mesmo com `per_page=1`. Ver app/pagination.py.

    `next_cursor` e None na ultima pagina; enquanto vier preenchido, devolva-o
    em `?cursor=` pra pegar a proxima.
    """

    next_cursor: str | None = None
    limit: int


class PartnerPageOut(CursorPage):
    data: list[PartnerOut]


class OffsetPage(BaseModel):
    """Pagina de uma busca paginada por `offset`/`limit`.

    So /establishments usa esta forma. Ela cabe ali porque a busca exige um
    recorte geografico (cidade ou estado), e isso limita o conjunto a percorrer
    -- o `OFFSET` faz o banco produzir e descartar `offset` linhas, o que e
    barato sobre alguns milhares e nao seria sobre dezenas de milhoes. As
    outras buscas continuam por cursor (`CursorPage`), onde o conjunto nao tem
    esse teto.

    Continua sem `total`/`last_page`, pelo mesmo motivo de sempre: contar exige
    varrer o resultado inteiro. `has_more` responde a unica pergunta que a
    paginacao precisa -- e sai de graca, lendo uma linha a mais que o `limit`.
    """

    offset: int
    limit: int
    has_more: bool


class EstablishmentPage(OffsetPage):
    data: list[EstablishmentOut]


class AddressPageOut(CursorPage):
    # Linhas de `postal_codes` montadas em SQL direto (ver
    # app/routers/addresses.py), com `exact`/`distance_km` a mais quando a
    # busca e por proximidade -- por isso dict e nao um model fixo.
    data: list[dict]


class CnaeCountOut(BaseModel):
    cnae_code: str
    total: int


class EstablishmentStatsOut(BaseModel):
    """Agregações de /establishments/stats.

    `by_uf`, `by_region`, `by_company_size` e `top_cnaes` vêm vazios a menos que
    `include_breakdowns=true`: cada um é um GROUP BY que varre todas as linhas
    do filtro, e num recorte grande isso não termina em tempo de request.
    A exceção é `by_uf`/`by_region` quando se filtra por uma única UF -- aí a
    resposta é o próprio total, sem consultar nada.
    """

    total: int
    with_cellphone: int
    with_email: int
    by_uf: dict[str, int]
    by_region: dict[str, int]
    by_company_size: dict[str, int]
    top_cnaes: list[CnaeCountOut]


class CnaeOut(BaseModel):
    code: str
    description: str

    model_config = {"from_attributes": True}


class AnnexOut(BaseModel):
    annex: str
    description: str


class BracketOut(BaseModel):
    bracket: int
    rbt12_ate: float
    nominal_rate: float
    deduction_amount: float


class AnnexTableOut(BaseModel):
    annex: str
    description: str
    brackets: list[BracketOut]


class SimplesCalculationOut(BaseModel):
    annex: str
    rbt12: float
    bracket: int
    nominal_rate: float
    deduction_amount: float
    effective_rate: float
    monthly_revenue: float
    tax_amount: float


class FactorROut(BaseModel):
    factor_r: float
    limit: float
    elegivel_annex_iii: bool
    annex_sugerido: str


class ImportStepOut(BaseModel):
    """Um estágio do pipeline. Os três (download/extract/import) rodam em
    paralelo, cada um num arquivo diferente -- ver app/importer/pipeline.py."""

    step: str
    status: str
    group: str | None
    current_file: str | None
    processed_rows: int
    total_bytes: int | None
    percent: float | None
    message: str | None
    started_at: str | None
    updated_at: str | None


class ImportStatusOut(BaseModel):
    period: str | None
    status: str
    message: str | None
    started_at: str | None
    updated_at: str | None
    stages: list[ImportStepOut]
