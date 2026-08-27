from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.importer.pipeline import run_import
from app.models import ImportProgress
from app.schemas import ImportStatusOut

router = APIRouter(prefix="/import", tags=["import"])


@router.get("/status", response_model=ImportStatusOut)
def status(db: Session = Depends(get_db)):
    progress = db.get(ImportProgress, 1)
    if not progress:
        return ImportStatusOut(
            period=None, status="idle", group=None, current_file=None, step=None,
            processed_rows=0, message=None, started_at=None, updated_at=None,
        )

    return ImportStatusOut(
        period=progress.period,
        status=progress.status,
        group=progress.group,
        current_file=progress.current_file,
        step=progress.step,
        processed_rows=progress.processed_rows,
        message=progress.message,
        started_at=progress.started_at.isoformat() if progress.started_at else None,
        updated_at=progress.updated_at.isoformat() if progress.updated_at else None,
    )


@router.post("/trigger")
def trigger(background_tasks: BackgroundTasks, period: str | None = None):
    """Dispara a importação em background -- útil pra rodar via chamada HTTP
    em vez de exec no container. Prefira o CLI (`python -m app.cli
    import-cnpj`) via cron/scheduler pra uma importação de produção."""
    background_tasks.add_task(run_import, period=period, only=None)
    return {"started": True}
