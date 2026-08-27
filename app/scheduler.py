"""Processo separado (container `scheduler` no docker-compose) que dispara
`run_import` uma vez por mês -- mesmo horário usado antes no scheduler do
Laravel (dia 20, 03:00), dando folga pro mirror publicar o período novo."""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.importer.pipeline import run_import

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")


def scheduled_import():
    logger.info("Iniciando importação agendada da base de CNPJ...")
    try:
        run_import()
        logger.info("Importação agendada concluída.")
    except Exception:
        logger.exception("Falha na importação agendada")


if __name__ == "__main__":
    scheduler = BlockingScheduler(timezone="America/Sao_Paulo")
    scheduler.add_job(scheduled_import, "cron", day=20, hour=3, minute=0)
    logger.info("Scheduler iniciado — próxima importação: dia 20 às 03:00 (America/Sao_Paulo).")
    scheduler.start()
