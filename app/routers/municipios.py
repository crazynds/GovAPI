from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Municipio

router = APIRouter(prefix="/municipios", tags=["municipios"])


@router.get("/search")
def search(name: str = Query(...), limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db)):
    results = db.query(Municipio).filter(Municipio.name.ilike(f"%{name}%")).limit(limit).all()
    return [{"receita_code": m.receita_code, "name": m.name, "uf": m.uf} for m in results]
