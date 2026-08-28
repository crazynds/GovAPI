"""Geocodifica o centroide de cada município via Nominatim (OpenStreetMap),
gratuito e sem chave -- usado como fallback de baixa precisão (nível
cidade) em `/enderecos/proximos` e `/enderecos/buscar` quando um CEP
específico ainda não tem coordenada exata cacheada em `cep_coordenadas`.

Só ~5570 chamadas (uma por município, uma vez só -- municípios não se
multiplicam) -- diferente de geocodificar CEP a CEP, que seriam ~1.6
milhão de chamadas e violaria a política de uso da API pública. Respeita
o limite de 1 req/s do Nominatim (https://operations.osmfoundation.org/policies/nominatim/)
e pula municípios que já têm coordenada -- então rodar de novo depois de
uma queda só continua de onde parou.
"""

import logging
import time

import httpx
from sqlalchemy.orm import Session

from app.models import Municipio

logger = logging.getLogger("importer")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim exige um User-Agent identificável (não o default de nenhuma lib) --
# recusa/bloqueia requisições genéricas.
HEADERS = {"User-Agent": "dados-gov-br (https://github.com/crazynds/GovAPI)"}
MIN_INTERVAL = 1.1  # >1s exigido pela política de uso


def geocode_municipios(db: Session) -> tuple[int, int]:
    """Geocodifica todo município ainda sem latitude/longitude. Retorna
    (geocodificados, total_sem_coordenada_antes)."""
    pending = db.query(Municipio).filter(
        Municipio.latitude.is_(None), Municipio.uf.isnot(None)
    ).all()
    total = len(pending)
    logger.info("Geocodificando %d município(s) via Nominatim (~%.0f min, 1 req/s)...", total, total * MIN_INTERVAL / 60)

    geocoded = 0
    last_request = 0.0

    for i, m in enumerate(pending, start=1):
        elapsed = time.monotonic() - last_request
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        last_request = time.monotonic()

        try:
            response = httpx.get(
                NOMINATIM_URL,
                params={"city": m.name, "state": m.uf, "country": "Brazil", "format": "json", "limit": 1},
                headers=HEADERS,
                timeout=15,
            )
            response.raise_for_status()
            results = response.json()
        except (httpx.HTTPError, ValueError):
            logger.warning("Falha geocodificando %s/%s -- tenta de novo na próxima execução", m.name, m.uf)
            continue

        if results:
            m.latitude = float(results[0]["lat"])
            m.longitude = float(results[0]["lon"])
            geocoded += 1

        if i % 100 == 0 or i == total:
            db.commit()
            logger.info("Geocodificados %d/%d municípios", i, total)

    db.commit()
    logger.info("Concluído: %d/%d municípios geocodificados", geocoded, total)
    return geocoded, total
