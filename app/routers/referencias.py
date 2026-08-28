from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Motivo, NaturezaJuridica, Pais, Qualificacao
from app.schemas import CodeDescriptionOut

router = APIRouter()


def _search_router(prefix: str, model, tag_name: str) -> APIRouter:
    sub = APIRouter(prefix=prefix, tags=[tag_name])

    @sub.get("/search", response_model=list[CodeDescriptionOut])
    def search(name: str = Query(...), limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
        return db.query(model).filter(model.description.ilike(f"%{name}%")).limit(limit).all()

    @sub.get("", response_model=list[CodeDescriptionOut])
    def list_all(db: Session = Depends(get_db)):
        return db.query(model).order_by(model.code).all()

    @sub.get("/{code}", response_model=CodeDescriptionOut)
    def by_code(code: str, db: Session = Depends(get_db)):
        row = db.query(model).filter(model.code == code).first()
        if not row:
            raise HTTPException(404, f"{tag_name} não encontrado(a): {code!r}")
        return row

    return sub


router.include_router(_search_router("/naturezas-juridicas", NaturezaJuridica, "Natureza jurídica"))
router.include_router(_search_router("/qualificacoes-societarias", Qualificacao, "Qualificação"))
router.include_router(_search_router("/paises", Pais, "País"))
router.include_router(_search_router("/motivos-situacao-cadastral", Motivo, "Motivo"))
