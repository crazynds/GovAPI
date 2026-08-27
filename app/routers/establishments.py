from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Establishment, Municipio
from app.schemas import EstablishmentOut, EstablishmentPage

router = APIRouter(prefix="/establishments", tags=["establishments"])


def _serialize(e: Establishment) -> EstablishmentOut:
    return EstablishmentOut(
        cnpj=e.cnpj,
        company_name=e.company_name,
        trade_name=e.trade_name,
        is_headquarters=e.is_headquarters,
        is_mei=e.is_mei,
        is_simples=e.is_simples,
        company_size=e.company_size,
        main_cnae_code=e.main_cnae_code,
        secondary_cnae_codes=e.secondary_cnae_codes or [],
        municipio_name=e.municipio.name if e.municipio else None,
        uf=e.uf,
        email=e.email,
        phone=e.phone,
        cellphone=e.cellphone,
        cellphone_confidence=e.cellphone_confidence,
        opened_at=e.opened_at,
    )


@router.get("", response_model=EstablishmentPage)
def search(
    cnae_codes: list[str] | None = Query(None),
    uf: str | None = Query(None),
    municipio_codes: list[str] | None = Query(None),
    company_size: str | None = Query(None),
    is_mei: bool | None = Query(None),
    is_simples: bool | None = Query(None),
    only_with_cellphone: bool = Query(True),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(Establishment)

    if cnae_codes:
        secondary = Establishment.secondary_cnae_codes.cast(JSONB)
        conditions = [Establishment.main_cnae_code.in_(cnae_codes)]
        conditions += [secondary.contains([code]) for code in cnae_codes]
        query = query.filter(or_(*conditions))

    if uf:
        query = query.filter(Establishment.uf == uf)

    if municipio_codes:
        query = query.filter(Establishment.municipio.has(Municipio.receita_code.in_(municipio_codes)))

    if company_size:
        query = query.filter(Establishment.company_size == company_size)

    if is_mei is not None:
        query = query.filter(Establishment.is_mei == is_mei)

    if is_simples is not None:
        query = query.filter(Establishment.is_simples == is_simples)

    if only_with_cellphone:
        query = query.filter(Establishment.cellphone.isnot(None))

    total = query.count()
    items = (
        query.order_by(Establishment.cellphone_confidence.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return EstablishmentPage(
        data=[_serialize(e) for e in items],
        total=total,
        per_page=per_page,
        current_page=page,
        last_page=max(1, -(-total // per_page)),
    )


@router.get("/by-cnpj", response_model=list[EstablishmentOut])
def by_cnpjs(
    cnpjs: list[str] = Query(...),
    only_with_cellphone: bool = Query(False),
    db: Session = Depends(get_db),
):
    query = db.query(Establishment).filter(Establishment.cnpj.in_(cnpjs))
    if only_with_cellphone:
        query = query.filter(Establishment.cellphone.isnot(None))
    return [_serialize(e) for e in query.all()]
