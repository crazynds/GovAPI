from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Municipio
from app.regions import UF_TO_REGIAO, ufs_for_regiao

router = APIRouter(prefix="/municipios", tags=["municipios"])


# Largura dos codigos de municipio -- as colunas sao Integer no banco (pro
# JOIN do build casar tipo sem CAST e pra FK de correios_cep funcionar), mas a
# API sempre falou nessa forma de string com zero a esquerda.
RECEITA_CODE_WIDTH = 4
IBGE_CODE_WIDTH = 7


def _serialize(m: Municipio) -> dict:
    return {
        # Nullable agora: uma linha pode existir so pelo lado do IBGE
        # (bootstrap via `import-municipios`) antes do Municipios.zip da
        # Receita ter rodado, ou pra sempre no raro caso de nome sem
        # correspondencia exata entre as duas fontes -- ver
        # app.importer.pipeline._merge_municipios_receita.
        "receita_code": f"{m.receita_code:0{RECEITA_CODE_WIDTH}d}" if m.receita_code is not None else None,
        "name": m.name,
        "uf": m.uf,
        "regiao": UF_TO_REGIAO.get(m.uf) if m.uf else None,
        "ibge_code": f"{m.ibge_code:0{IBGE_CODE_WIDTH}d}" if m.ibge_code is not None else None,
        "population": m.population,
        "area_km2": float(m.area_km2) if m.area_km2 is not None else None,
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
    if not receita_code.strip().isdigit():
        raise HTTPException(422, f"Código de município inválido: {receita_code!r}")
    m = db.query(Municipio).filter(Municipio.receita_code == int(receita_code)).first()
    if not m:
        raise HTTPException(404, "Município não encontrado")
    return _serialize(m)
