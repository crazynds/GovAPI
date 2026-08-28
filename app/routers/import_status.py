from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.importer.pipeline import run_import
from app.models import ImportProgress, ImportRun
from app.schemas import ImportStatusOut, ImportStepOut

router = APIRouter(prefix="/import", tags=["import"])

# Ordem de exibicao dos estagios -- a mesma do fluxo do pipeline.
STAGE_ORDER = ("download", "extract", "import", "build")


@router.get("/status", response_model=ImportStatusOut)
def status(db: Session = Depends(get_db)):
    """Estado da importação: o global (`status`) mais uma entrada por estágio
    do pipeline. Os estágios rodam em paralelo, então num run ativo há três
    arquivos diferentes em `stages` ao mesmo tempo."""
    run = db.get(ImportRun, 1)
    steps = db.query(ImportProgress).all()
    by_step = {step.step: step for step in steps}

    return ImportStatusOut(
        period=run.period if run else None,
        status=run.status if run else "idle",
        message=run.message if run else None,
        started_at=_iso(run.started_at) if run else None,
        updated_at=_iso(run.updated_at) if run else None,
        stages=[_serialize_step(name, by_step.get(name)) for name in STAGE_ORDER],
    )


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _serialize_step(name: str, step: ImportProgress | None) -> ImportStepOut:
    if step is None:
        return ImportStepOut(
            step=name, status="idle", group=None, current_file=None, processed_rows=0,
            total_bytes=None, percent=None, message=None, started_at=None, updated_at=None,
        )

    percent = None
    if step.total_bytes:
        percent = round(min(100.0, step.processed_rows * 100 / step.total_bytes), 1)

    return ImportStepOut(
        step=step.step,
        status=step.status,
        group=step.group,
        current_file=step.current_file,
        processed_rows=step.processed_rows,
        total_bytes=step.total_bytes,
        percent=percent,
        message=step.message,
        started_at=_iso(step.started_at),
        updated_at=_iso(step.updated_at),
    )


@router.post("/trigger")
def trigger(background_tasks: BackgroundTasks, period: str | None = None):
    """Dispara a importação em background -- útil pra rodar via chamada HTTP
    em vez de exec no container. Prefira o CLI (`python -m app.cli
    import-cnpj`) via cron/scheduler pra uma importação de produção."""
    background_tasks.add_task(run_import, period=period, only=None)
    return {"started": True}
