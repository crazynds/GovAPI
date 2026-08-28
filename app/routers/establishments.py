from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Query as ORMQuery, Session

from app.db import get_db
from app.models import Cnae, Establishment, Municipio
from app.regions import UF_TO_REGIAO, ufs_for_regiao
from app.schemas import EstablishmentOut, EstablishmentPage, EstablishmentStatsOut

router = APIRouter(prefix="/establishments", tags=["establishments"])

# Codigos oficiais de porte (Receita) -- ver layout do arquivo Empresas.
COMPANY_SIZE_LABELS = {
    "00": "Não informado",
    "01": "Micro empresa",
    "03": "Empresa de pequeno porte",
    "05": "Demais (médio/grande porte)",
}


def _cnae_descriptions(db: Session, codes: set[str]) -> dict[str, str]:
    codes = {c for c in codes if c}
    if not codes:
        return {}
    rows = db.query(Cnae.code, Cnae.description).filter(Cnae.code.in_(codes)).all()
    return dict(rows)


def _serialize(e: Establishment, cnae_map: dict[str, str] | None = None) -> EstablishmentOut:
    cnae_map = cnae_map or {}
    secondary_codes = e.secondary_cnae_codes or []
    return EstablishmentOut(
        cnpj=e.cnpj,
        company_name=e.company_name,
        trade_name=e.trade_name,
        is_headquarters=e.is_headquarters,
        is_mei=e.is_mei,
        is_simples=e.is_simples,
        company_size=e.company_size,
        company_size_label=COMPANY_SIZE_LABELS.get(e.company_size or ""),
        main_cnae_code=e.main_cnae_code,
        main_cnae_description=cnae_map.get(e.main_cnae_code or ""),
        secondary_cnae_codes=secondary_codes,
        secondary_cnae_descriptions=[cnae_map[c] for c in secondary_codes if c in cnae_map],
        municipio_name=e.municipio.name if e.municipio else None,
        uf=e.uf,
        email=e.email,
        phone=e.phone,
        cellphone=e.cellphone,
        cellphone_confidence=e.cellphone_confidence,
        opened_at=e.opened_at,
    )


def _serialize_many(db: Session, items: list[Establishment]) -> list[EstablishmentOut]:
    all_codes: set[str] = set()
    for e in items:
        all_codes.add(e.main_cnae_code or "")
        all_codes.update(e.secondary_cnae_codes or [])
    cnae_map = _cnae_descriptions(db, all_codes)
    return [_serialize(e, cnae_map) for e in items]


def _apply_filters(
    query: ORMQuery,
    *,
    cnae_codes: list[str] | None,
    cnae_match: str,
    uf: list[str] | None,
    regiao: str | None,
    municipio_codes: list[str] | None,
    company_size: list[str] | None,
    is_mei: bool | None,
    is_simples: bool | None,
    is_headquarters: bool | None,
    name: str | None,
    only_with_cellphone: bool,
    only_with_email: bool,
    has_phone: bool | None,
    opened_after: date | None,
    opened_before: date | None,
) -> ORMQuery:
    if cnae_codes:
        secondary = Establishment.secondary_cnae_codes.cast(JSONB)
        per_code = [
            or_(Establishment.main_cnae_code == code, secondary.contains([code])) for code in cnae_codes
        ]
        query = query.filter(and_(*per_code) if cnae_match == "all" else or_(*per_code))

    ufs = set(uf or [])
    if regiao:
        regiao_ufs = ufs_for_regiao(regiao)
        if not regiao_ufs:
            raise HTTPException(422, f"Região desconhecida: {regiao!r} (use norte/nordeste/centro-oeste/sudeste/sul)")
        ufs |= set(regiao_ufs)
    if ufs:
        query = query.filter(Establishment.uf.in_(ufs))

    if municipio_codes:
        query = query.filter(Establishment.municipio.has(Municipio.receita_code.in_(municipio_codes)))

    if company_size:
        query = query.filter(Establishment.company_size.in_(company_size))

    if is_mei is not None:
        query = query.filter(Establishment.is_mei == is_mei)

    if is_simples is not None:
        query = query.filter(Establishment.is_simples == is_simples)

    if is_headquarters is not None:
        query = query.filter(Establishment.is_headquarters == is_headquarters)

    if name:
        pattern = f"%{name}%"
        query = query.filter(
            or_(Establishment.company_name.ilike(pattern), Establishment.trade_name.ilike(pattern))
        )

    if only_with_cellphone:
        query = query.filter(Establishment.cellphone.isnot(None))

    if only_with_email:
        query = query.filter(Establishment.email.isnot(None))

    if has_phone is not None:
        query = query.filter(Establishment.phone.isnot(None) if has_phone else Establishment.phone.is_(None))

    if opened_after:
        query = query.filter(Establishment.opened_at >= opened_after)

    if opened_before:
        query = query.filter(Establishment.opened_at <= opened_before)

    return query


@router.get("", response_model=EstablishmentPage)
def search(
    cnae_codes: list[str] | None = Query(None, description="Códigos CNAE (principal ou secundário)"),
    cnae_match: str = Query("any", pattern="^(any|all)$", description="'any': tem ao menos um dos CNAEs; 'all': tem todos"),
    uf: list[str] | None = Query(None, description="Uma ou mais UFs, ex: ?uf=SP&uf=RJ"),
    regiao: str | None = Query(None, description="norte/nordeste/centro-oeste/sudeste/sul, combina com uf"),
    municipio_codes: list[str] | None = Query(None),
    company_size: list[str] | None = Query(None, description="Códigos de porte (00/01/03/05), aceita múltiplos"),
    is_mei: bool | None = Query(None),
    is_simples: bool | None = Query(None),
    is_headquarters: bool | None = Query(None),
    name: str | None = Query(None, description="Busca por razão social ou nome fantasia"),
    only_with_cellphone: bool = Query(True),
    only_with_email: bool = Query(False),
    has_phone: bool | None = Query(None, description="Filtra por ter (true) ou não ter (false) telefone fixo"),
    opened_after: date | None = Query(None, description="Data de abertura >= (YYYY-MM-DD)"),
    opened_before: date | None = Query(None, description="Data de abertura <= (YYYY-MM-DD)"),
    sort_by: str = Query("cellphone_confidence", pattern="^(cellphone_confidence|opened_at|company_name)$"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = _apply_filters(
        db.query(Establishment),
        cnae_codes=cnae_codes,
        cnae_match=cnae_match,
        uf=uf,
        regiao=regiao,
        municipio_codes=municipio_codes,
        company_size=company_size,
        is_mei=is_mei,
        is_simples=is_simples,
        is_headquarters=is_headquarters,
        name=name,
        only_with_cellphone=only_with_cellphone,
        only_with_email=only_with_email,
        has_phone=has_phone,
        opened_after=opened_after,
        opened_before=opened_before,
    )

    total = query.count()

    sort_column = getattr(Establishment, sort_by)
    order = sort_column.desc() if sort_dir == "desc" else sort_column.asc()
    items = query.order_by(order).offset((page - 1) * per_page).limit(per_page).all()

    return EstablishmentPage(
        data=_serialize_many(db, items),
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
    return _serialize_many(db, query.all())


@router.get("/stats", response_model=EstablishmentStatsOut)
def stats(
    cnae_codes: list[str] | None = Query(None),
    cnae_match: str = Query("any", pattern="^(any|all)$"),
    uf: list[str] | None = Query(None),
    regiao: str | None = Query(None),
    municipio_codes: list[str] | None = Query(None),
    company_size: list[str] | None = Query(None),
    is_mei: bool | None = Query(None),
    is_simples: bool | None = Query(None),
    is_headquarters: bool | None = Query(None),
    name: str | None = Query(None),
    only_with_cellphone: bool = Query(False),
    only_with_email: bool = Query(False),
    has_phone: bool | None = Query(None),
    opened_after: date | None = Query(None),
    opened_before: date | None = Query(None),
    top_cnaes: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Agregações sobre o mesmo conjunto de filtros de `/establishments`:
    total, distribuição por UF/região, por porte, e os CNAEs principais
    mais frequentes -- útil pra dimensionar uma campanha antes de paginar
    os resultados individuais."""
    base = _apply_filters(
        db.query(Establishment),
        cnae_codes=cnae_codes,
        cnae_match=cnae_match,
        uf=uf,
        regiao=regiao,
        municipio_codes=municipio_codes,
        company_size=company_size,
        is_mei=is_mei,
        is_simples=is_simples,
        is_headquarters=is_headquarters,
        name=name,
        only_with_cellphone=only_with_cellphone,
        only_with_email=only_with_email,
        has_phone=has_phone,
        opened_after=opened_after,
        opened_before=opened_before,
    )

    total = base.count()

    by_uf_rows = (
        base.with_entities(Establishment.uf, func.count().label("total"))
        .group_by(Establishment.uf)
        .order_by(func.count().desc())
        .all()
    )
    by_company_size_rows = (
        base.with_entities(Establishment.company_size, func.count().label("total"))
        .group_by(Establishment.company_size)
        .order_by(func.count().desc())
        .all()
    )
    by_main_cnae_rows = (
        base.with_entities(Establishment.main_cnae_code, func.count().label("total"))
        .filter(Establishment.main_cnae_code.isnot(None))
        .group_by(Establishment.main_cnae_code)
        .order_by(func.count().desc())
        .limit(top_cnaes)
        .all()
    )

    by_regiao: dict[str, int] = {}
    for uf_code, count in by_uf_rows:
        regiao_key = UF_TO_REGIAO.get(uf_code, "desconhecida")
        by_regiao[regiao_key] = by_regiao.get(regiao_key, 0) + count

    return EstablishmentStatsOut(
        total=total,
        with_cellphone=base.filter(Establishment.cellphone.isnot(None)).count(),
        with_email=base.filter(Establishment.email.isnot(None)).count(),
        by_uf={uf_code or "desconhecido": count for uf_code, count in by_uf_rows},
        by_regiao=by_regiao,
        by_company_size={size or "desconhecido": count for size, count in by_company_size_rows},
        top_cnaes=[{"cnae_code": code, "total": count} for code, count in by_main_cnae_rows],
    )
