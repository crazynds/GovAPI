from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Municipio
from app.regions import UF_TO_REGIAO, ufs_for_regiao

router = APIRouter(prefix="/municipios", tags=["municipios"])


def _serialize(m: Municipio) -> dict:
    return {
        "receita_code": m.receita_code,
        "name": m.name,
        "uf": m.uf,
        "regiao": UF_TO_REGIAO.get(m.uf) if m.uf else None,
    }


@router.get("/search")
def search(
    name: str | None = Query(None),
    uf: list[str] | None = Query(None, description="Uma ou mais UFs, ex: ?uf=SP&uf=RJ"),
    regiao: str | None = Query(None, description="norte/nordeste/centro-oeste/sudeste/sul"),
    limit: int = Query(10, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(Municipio)

    if name:
        query = query.filter(Municipio.name.ilike(f"%{name}%"))

    ufs = set(uf or [])
    if regiao:
        regiao_ufs = ufs_for_regiao(regiao)
        if not regiao_ufs:
            raise HTTPException(422, f"Região desconhecida: {regiao!r} (use norte/nordeste/centro-oeste/sudeste/sul)")
        ufs |= set(regiao_ufs)
    if ufs:
        query = query.filter(Municipio.uf.in_(ufs))

    results = query.order_by(Municipio.name).limit(limit).all()
    return [_serialize(m) for m in results]


@router.get("/by-code/{receita_code}")
def by_code(receita_code: str, db: Session = Depends(get_db)):
    m = db.query(Municipio).filter(Municipio.receita_code == receita_code).first()
    if not m:
        raise HTTPException(404, "Município não encontrado")
    return _serialize(m)
