"""Aplica as migrations pendentes (Alembic) no boot do container -- roda
antes do uvicorn/scheduler subir (ver docker-entrypoint.sh). Usa um
advisory lock do Postgres pra `app` e `scheduler` não tentarem migrar ao
mesmo tempo quando sobem juntos."""

import logging
import time

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.config import settings

logger = logging.getLogger("migrate")
logging.basicConfig(level=logging.INFO)

# Qualquer int fixo serve -- só precisa ser o mesmo em todo processo que
# roda essa migração, pra todos disputarem o mesmo lock.
ADVISORY_LOCK_ID = 875_401_223


def _connect_with_retry(engine):
    # Sem depends_on/healthcheck pro Postgres (pode ser um servidor
    # externo, que o compose não controla) -- espera ele aceitar conexão
    # em vez de falhar de cara se o app subir primeiro.
    for attempt in range(30):
        try:
            return engine.connect()
        except OperationalError:
            if attempt == 29:
                raise
            logger.info("Postgres ainda não aceita conexões, tentando de novo em 2s...")
            time.sleep(2)


def run_migrations() -> None:
    lock_engine = create_engine(settings.database_url)

    # Conexão só pro lock, separada da que o Alembic usa pra rodar a
    # migração de verdade. Reusar a mesma conexão pro lock e pro upgrade()
    # gerava uma transação implícita (pelo próprio `pg_advisory_lock`) que
    # o `command.upgrade()` nunca commitava -- a migração "rodava" (log
    # dizia "Running upgrade..."), mas nada era persistido de fato.
    with _connect_with_retry(lock_engine) as lock_conn:
        logger.info("Aguardando lock de migração...")
        lock_conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": ADVISORY_LOCK_ID})
        try:
            logger.info("Lock obtido, aplicando migrations...")
            command.upgrade(Config("alembic.ini"), "head")
            logger.info("Migrations em dia.")
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": ADVISORY_LOCK_ID})


if __name__ == "__main__":
    run_migrations()
