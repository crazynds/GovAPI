"""Enriquece `municipalities` com população estimada e área territorial via
API pública do IBGE (SIDRA/Agregados), sem chave.

Casa por `ibge_code` -- exato, sem ambiguidade -- porque `app.importer.
municipalities.import_municipalities` já bootstrapou `municipalities` com o código IBGE
de cada linha antes disso rodar (ver ordem em app.cli.import_all). O SIDRA
devolve esse mesmo código em `locality.id` de cada série, então não há
por que casar por nome (frágil: nomes se repetem entre estados, tipo várias
"Buritis") como este módulo fazia antes de `ibge_code` estar disponível cedo.

Fontes:
- Agregado 6579 (Estimativas de população), variável 9324
- Agregado 1301 (Área territorial), variável 615
"""

import logging

import httpx
from sqlalchemy.orm import Session

from app.models import Municipality

logger = logging.getLogger("importer")

POPULATION_URL = "https://servicodados.ibge.gov.br/api/v3/agregados/6579/periodos/-1/variaveis/9324?localidades=N6[all]"
AREA_URL = "https://servicodados.ibge.gov.br/api/v3/agregados/1301/periodos/-1/variaveis/615?localidades=N6[all]"


def _fetch_series(url: str) -> dict[int, float]:
    """Retorna {ibge_code: valor}."""
    response = httpx.get(url, timeout=60)
    response.raise_for_status()
    data = response.json()
    series = data[0]["resultados"][0]["series"]

    result = {}
    for entry in series:
        values = entry["serie"]
        if not values:
            continue
        value = next(iter(values.values()))
        if value in (None, "-", "..", "...", ""):
            continue
        result[int(entry["localidade"]["id"])] = float(value)
    return result


def import_ibge(db: Session) -> tuple[int, int]:
    """Busca população e área de todos os municípios de uma vez e faz
    UPDATE em `municipalities` casando por `ibge_code`. Retorna
    (municipalities_with_population, municipalities_with_area)."""
    logger.info("Buscando população estimada (IBGE/SIDRA agregado 6579)...")
    population = _fetch_series(POPULATION_URL)
    logger.info("Buscando área territorial (IBGE/SIDRA agregado 1301)...")
    area = _fetch_series(AREA_URL)

    municipalities = db.query(Municipality).filter(Municipality.ibge_code.isnot(None)).all()
    pop_matched = 0
    area_matched = 0

    for m in municipalities:
        pop_value = population.get(m.ibge_code)
        area_value = area.get(m.ibge_code)

        if pop_value is not None:
            m.population = int(pop_value)
            pop_matched += 1
        if area_value is not None:
            m.area_km2 = area_value
            area_matched += 1

    db.commit()
    logger.info(
        "IBGE: %d/%d municípios com população, %d/%d com área",
        pop_matched, len(municipalities), area_matched, len(municipalities),
    )
    return pop_matched, area_matched
