"""Quadro societário.

Como em establishments, o banco guarda numero (raiz do CNPJ em base 36, códigos
de qualificação/país/faixa etária como inteiro, CPF/CNPJ do sócio como inteiro)
e a tradução acontece na borda.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import cnpj as cnpj_codec
from app.db import get_db
from app.pagination import SortKey, make_fingerprint, paginate
from app.models import Establishment, Country, Qualification, Partner
from app.schemas import PartnerOut, PartnerPageOut

router = APIRouter(prefix="/partners", tags=["partners"])

# Largura do `code` nas tabelas de referência (que seguem em texto).
QUALIFICATION_WIDTH = 2
COUNTRY_WIDTH = 3

PARTNER_TYPE_LABELS = {
    1: "Pessoa Jurídica",
    2: "Pessoa Física",
    3: "Estrangeiro",
}

# Faixa etária -- layout oficial do arquivo de Sócios (Receita Federal).
AGE_RANGE_LABELS = {
    0: "Não se aplica",
    1: "0 a 12 anos",
    2: "13 a 20 anos",
    3: "21 a 30 anos",
    4: "31 a 40 anos",
    5: "41 a 50 anos",
    6: "51 a 60 anos",
    7: "61 a 70 anos",
    8: "71 a 80 anos",
    9: "Maior que 80 anos",
}


def _code(value: int | None, width: int) -> str | None:
    return f"{value:0{width}d}" if value is not None else None


def _descriptions(db: Session, model, codes: set[int], width: int) -> dict[int, str]:
    wanted = {c for c in codes if c is not None}
    if not wanted:
        return {}
    as_text = {f"{c:0{width}d}": c for c in wanted}
    rows = db.query(model.code, model.description).filter(model.code.in_(as_text)).all()
    return {as_text[code]: description for code, description in rows if code in as_text}


def _tax_id(partner: Partner) -> str | None:
    """Remonta a forma que a Receita publica.

    PJ: o CNPJ completo de 14 posições. PF/estrangeiro: o CPF já vem mascarado
    da origem por LGPD, e só os 6 dígitos do meio existem de fato -- a máscara
    é reconstruída aqui.
    """
    if partner.partner_tax_id is None:
        return None
    if partner.partner_type == 1:
        try:
            return cnpj_codec.full(partner.partner_tax_id)
        except ValueError:
            # Fora da faixa da base 36: o valor nao foi gravado pelo codec (dado
            # legado do import antigo). Melhor nao devolver nada do que devolver
            # um CNPJ inventado.
            return None
    return f"***{partner.partner_tax_id:06d}**"


def _serialize_many(db: Session, partners: list[Partner]) -> list[PartnerOut]:
    qualification_codes = {s.partner_qualification for s in partners}
    qualification_codes |= {s.legal_rep_qualification for s in partners}
    qualifications = _descriptions(db, Qualification, qualification_codes, QUALIFICATION_WIDTH)
    countries = _descriptions(db, Country, {s.country for s in partners}, COUNTRY_WIDTH)

    cnpj_roots = {s.cnpj_root for s in partners}
    companies = {}

    # Nome da empresa (a matriz é o registro mais útil pra exibir) numa query
    # só. Como a base 36 preserva ordem, "todos os estabelecimentos da raiz X"
    # é uma faixa contígua de inteiros -- então isso vira um OR de BETWEENs que
    # a PK resolve. Antes era um `cnpj LIKE '<raiz>%'` por raiz (até 200 num
    # OR), que nenhum índice atende: seq scan da tabela inteira por página.
    if cnpj_roots:
        spans = [
            Establishment.cnpj.between(b * cnpj_codec.BRANCH_SPAN, (b + 1) * cnpj_codec.BRANCH_SPAN - 1)
            for b in cnpj_roots
        ]
        rows = db.query(Establishment.cnpj, Establishment.company_name).filter(
            Establishment.is_headquarters.is_(True),
            or_(*spans),
        ).all()
        companies = {cnpj_codec.root_of_value(value): name for value, name in rows}

    out = []
    for s in partners:
        out.append(
            PartnerOut(
                cnpj_root=cnpj_codec.root_from_int(s.cnpj_root),
                partner_type=_code(s.partner_type, 1),
                partner_type_label=PARTNER_TYPE_LABELS.get(s.partner_type),
                partner_name=s.partner_name,
                partner_tax_id=_tax_id(s),
                partner_qualification_code=_code(s.partner_qualification, QUALIFICATION_WIDTH),
                partner_qualification_description=qualifications.get(s.partner_qualification),
                partnership_start_date=s.partnership_start_date,
                country_code=_code(s.country, COUNTRY_WIDTH),
                country_description=countries.get(s.country),
                legal_rep=f"***{s.legal_rep:06d}**" if s.legal_rep else None,
                legal_rep_name=s.legal_rep_name or None,
                legal_rep_qualification_code=_code(s.legal_rep_qualification, QUALIFICATION_WIDTH),
                legal_rep_qualification_description=qualifications.get(s.legal_rep_qualification),
                age_range_label=AGE_RANGE_LABELS.get(s.age_range),
                company_name=companies.get(s.cnpj_root),
            )
        )
    return out


@router.get("/by-company/{cnpj}", response_model=list[PartnerOut])
def by_company(cnpj: str, db: Session = Depends(get_db)):
    """Lista os sócios de uma empresa. Aceita CNPJ completo (14 posições, com
    ou sem pontuação) ou só a raiz (8) -- sócios são registrados por raiz, não
    por estabelecimento.

    O CNPJ é alfanumérico desde 2026: filtrar por `isdigit()`, como era antes,
    apagava as letras da raiz e devolvia lista vazia em silêncio.
    """
    try:
        value = cnpj_codec.parse(cnpj)
    except ValueError as exc:
        raise HTTPException(422, f"CNPJ inválido: {cnpj!r}") from exc

    partners = db.query(Partner).filter(Partner.cnpj_root == cnpj_codec.root_of_value(value)).all()
    return _serialize_many(db, partners)


def _parse_tax_id(raw: str) -> int:
    cleaned = "".join(c for c in raw.upper() if c.isalnum())
    if not cleaned:
        raise HTTPException(422, f"Documento inválido: {raw!r}")
    if cleaned.isdigit():
        # CPF mascarado (6 dígitos) ou CPF/CNPJ inteiro em dígitos. A raiz de 8
        # posições também é base 36 aqui: é assim que o import guarda o sócio PJ
        # que vem só com a raiz, e sem isso esses registros eram impossíveis de
        # achar por documento.
        if len(cleaned) in (cnpj_codec.ROOT_LEN, cnpj_codec.BODY_LEN, cnpj_codec.BODY_LEN + 2):
            return cnpj_codec.parse(cleaned)
        return int(cleaned)
    try:
        return cnpj_codec.parse(cleaned)
    except ValueError as exc:
        raise HTTPException(422, f"Documento inválido: {raw!r}") from exc


@router.get("/search", response_model=PartnerPageOut)
def search(
    name: str | None = Query(None, description="Busca por name do sócio (ILIKE)"),
    tax_id: str | None = Query(
        None,
        description=(
            "CPF/CNPJ do sócio. O CPF vem mascarado pela Receita (ex: ***123456**) -- passe só os "
            "dígitos visíveis (123456). CNPJ aceita com ou sem pontuação."
        ),
    ),
    cursor: str | None = Query(
        None,
        description="Cursor da página anterior (`next_cursor`). Sem ele, começa do início. "
                    "Os filtros precisam ser os mesmos que geraram o cursor.",
    ),
    limit: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Busca sócios por nome e/ou documento -- útil pra achar em quais
    empresas uma pessoa/empresa aparece como sócia.

    Paginada por cursor: repasse o `next_cursor` da resposta em `?cursor=` pra
    próxima página. Sem `total` nem número de páginas -- ver app/pagination.py."""
    query = db.query(Partner)
    if name:
        query = query.filter(Partner.partner_name.ilike(f"%{name}%"))
    if tax_id:
        # Coluna numérica agora, então é igualdade -- antes era um
        # `ILIKE '%...%'` que varria as ~24M linhas sem usar índice.
        query = query.filter(Partner.partner_tax_id == _parse_tax_id(tax_id))

    # Ordena SO pela PK. Ordenar por `partner_name` (o que este endpoint fazia
    # antes) e uma armadilha com o filtro de nome: `ILIKE '%x%'` nao e
    # indexavel, entao o planner caminha o indice de nome em ordem alfabetica
    # testando linha a linha ate juntar a pagina -- o custo vira funcao de onde
    # o nome cai no alfabeto. Medido em producao: `?name=abreu` 15,8s,
    # `?name=zuzu` estourou o timeout, mesma consulta. Pela PK a ordenacao sai
    # de graca e o filtro e o unico custo.
    keys = [SortKey(Partner.id, "id", nullable=False)]

    items, next_cursor = paginate(
        query, keys, cursor, limit,
        make_fingerprint(name=name, tax_id=tax_id),
    )

    return PartnerPageOut(data=_serialize_many(db, items), next_cursor=next_cursor, limit=limit)
