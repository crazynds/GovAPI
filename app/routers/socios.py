from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Establishment, Pais, Qualificacao, Socio
from app.schemas import SocioOut, SocioPageOut

router = APIRouter(prefix="/socios", tags=["socios"])

IDENTIFICADOR_SOCIO_LABELS = {
    "1": "Pessoa Jurídica",
    "2": "Pessoa Física",
    "3": "Estrangeiro",
}

# Faixa etária -- layout oficial do arquivo de Sócios (Receita Federal).
FAIXA_ETARIA_LABELS = {
    "0": "Não se aplica",
    "1": "0 a 12 anos",
    "2": "13 a 20 anos",
    "3": "21 a 30 anos",
    "4": "31 a 40 anos",
    "5": "41 a 50 anos",
    "6": "51 a 60 anos",
    "7": "61 a 70 anos",
    "8": "71 a 80 anos",
    "9": "Maior que 80 anos",
}


def _serialize_many(db: Session, socios: list[Socio]) -> list[SocioOut]:
    qualificacao_codes = {s.qualificacao_socio for s in socios if s.qualificacao_socio}
    qualificacao_codes |= {s.qualificacao_representante_legal for s in socios if s.qualificacao_representante_legal}
    qualificacoes = dict(
        db.query(Qualificacao.code, Qualificacao.description).filter(Qualificacao.code.in_(qualificacao_codes)).all()
    ) if qualificacao_codes else {}

    pais_codes = {s.pais for s in socios if s.pais}
    paises = dict(db.query(Pais.code, Pais.description).filter(Pais.code.in_(pais_codes)).all()) if pais_codes else {}

    cnpj_basicos = {s.cnpj_basico for s in socios}
    companies = {}

    # Join por prefixo (matriz é o registro mais útil pra exibir o nome da
    # empresa) -- feito em uma query só, não uma por sócio.
    if cnpj_basicos:
        rows = db.query(Establishment.cnpj, Establishment.company_name).filter(
            Establishment.is_headquarters.is_(True),
            or_(*[Establishment.cnpj.like(f"{b}%") for b in cnpj_basicos]),
        ).all()
        companies = {cnpj[:8]: name for cnpj, name in rows}

    out = []
    for s in socios:
        out.append(
            SocioOut(
                cnpj_basico=s.cnpj_basico,
                identificador_socio=s.identificador_socio,
                identificador_socio_label=IDENTIFICADOR_SOCIO_LABELS.get(s.identificador_socio or ""),
                nome_socio=s.nome_socio,
                cpf_cnpj_socio=s.cpf_cnpj_socio,
                qualificacao_socio_code=s.qualificacao_socio,
                qualificacao_socio_description=qualificacoes.get(s.qualificacao_socio or ""),
                data_entrada_sociedade=s.data_entrada_sociedade,
                pais_code=s.pais,
                pais_description=paises.get(s.pais or ""),
                representante_legal=s.representante_legal,
                nome_representante=s.nome_representante or None,
                qualificacao_representante_code=s.qualificacao_representante_legal,
                qualificacao_representante_description=qualificacoes.get(s.qualificacao_representante_legal or ""),
                faixa_etaria_label=FAIXA_ETARIA_LABELS.get(s.faixa_etaria or ""),
                company_name=companies.get(s.cnpj_basico),
            )
        )
    return out


@router.get("/por-empresa/{cnpj}", response_model=list[SocioOut])
def por_empresa(cnpj: str, db: Session = Depends(get_db)):
    """Lista os sócios de uma empresa. Aceita CNPJ completo (14 dígitos) ou
    só a raiz (8 dígitos, ex: os 8 primeiros do CNPJ) -- sócios são
    registrados por raiz, não por estabelecimento."""
    cnpj_basico = "".join(c for c in cnpj if c.isdigit())[:8]
    socios = db.query(Socio).filter(Socio.cnpj_basico == cnpj_basico).all()
    return _serialize_many(db, socios)


@router.get("/buscar", response_model=SocioPageOut)
def buscar(
    nome: str | None = Query(None, description="Busca por nome do sócio (ILIKE)"),
    documento: str | None = Query(
        None, description="CPF/CNPJ do sócio -- lembre que o CPF vem mascarado pela Receita (ex: ***123456**)"
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Busca sócios por nome e/ou documento -- útil pra achar em quais
    empresas uma pessoa/empresa aparece como sócia."""
    query = db.query(Socio)
    if nome:
        query = query.filter(Socio.nome_socio.ilike(f"%{nome}%"))
    if documento:
        query = query.filter(Socio.cpf_cnpj_socio.ilike(f"%{documento}%"))

    total = query.count()
    items = query.order_by(Socio.nome_socio).offset((page - 1) * per_page).limit(per_page).all()

    return SocioPageOut(
        data=_serialize_many(db, items),
        total=total,
        per_page=per_page,
        current_page=page,
        last_page=max(1, -(-total // per_page)),
    )
