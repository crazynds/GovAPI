"""Processo separado (container `scheduler` no docker-compose) que dispara
todas as importações (CNPJ + CEPs) uma vez por mês -- mesmo horário usado
antes no scheduler do Laravel (dia 20, 03:00), dando folga pro mirror
publicar o período novo da Receita."""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.cli import _import_ceps, _import_cnpj

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")


def scheduled_import():
    logger.info("Iniciando importação agendada (CNPJ + CEPs)...")
    try:
        _import_cnpj(period=None, only=None)
        _import_ceps(source=None)
        logger.info("Importação agendada concluída.")
    except Exception:
        logger.exception("Falha na importação agendada")


if __name__ == "__main__":
    scheduler = BlockingScheduler(timezone="America/Sao_Paulo")
    scheduler.add_job(scheduled_import, "cron", day=20, hour=3, minute=0)
    logger.info("Scheduler iniciado — próxima importação: dia 20 às 03:00 (America/Sao_Paulo).")
    scheduler.start()
