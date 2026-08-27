from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Cnae
from app.schemas import CnaeOut

router = APIRouter(prefix="/cnaes", tags=["cnaes"])


@router.get("/search-by-description", response_model=list[CnaeOut])
def search_by_description(
    words: list[str] = Query(...),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    conditions = [Cnae.description.ilike(f"%{word}%") for word in words]
    results = db.query(Cnae).filter(or_(*conditions)).limit(limit).all()
    return results


@router.get("/codes", response_model=list[str])
def all_codes(db: Session = Depends(get_db)):
    return [row[0] for row in db.query(Cnae.code).all()]
