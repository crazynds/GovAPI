"""Enriquece `municipios` com população estimada e área territorial via
API pública do IBGE (SIDRA/Agregados), sem chave.

Casa por `ibge_code` -- exato, sem ambiguidade -- porque `app.importer.
municipios.import_municipios` já bootstrapou `municipios` com o código IBGE
de cada linha antes disso rodar (ver ordem em app.cli.import_all). O SIDRA
devolve esse mesmo código em `localidade.id` de cada série, então não há
por que casar por nome (frágil: nomes se repetem entre estados, tipo várias
"Buritis") como este módulo fazia antes de `ibge_code` estar disponível cedo.

Fontes:
- Agregado 6579 (Estimativas de população), variável 9324
- Agregado 1301 (Área territorial), variável 615
"""

import logging

import httpx
from sqlalchemy.orm import Session

from app.models import Municipio

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
        valores = entry["serie"]
        if not valores:
            continue
        valor = next(iter(valores.values()))
        if valor in (None, "-", "..", "...", ""):
            continue
        result[int(entry["localidade"]["id"])] = float(valor)
    return result


def import_ibge(db: Session) -> tuple[int, int]:
    """Busca população e área de todos os municípios de uma vez e faz
    UPDATE em `municipios` casando por `ibge_code`. Retorna
    (municipios_com_populacao, municipios_com_area)."""
    logger.info("Buscando população estimada (IBGE/SIDRA agregado 6579)...")
    population = _fetch_series(POPULATION_URL)
    logger.info("Buscando área territorial (IBGE/SIDRA agregado 1301)...")
    area = _fetch_series(AREA_URL)

    municipios = db.query(Municipio).filter(Municipio.ibge_code.isnot(None)).all()
    pop_matched = 0
    area_matched = 0

    for m in municipios:
        pop_valor = population.get(m.ibge_code)
        area_valor = area.get(m.ibge_code)

        if pop_valor is not None:
            m.population = int(pop_valor)
            pop_matched += 1
        if area_valor is not None:
            m.area_km2 = area_valor
            area_matched += 1

    db.commit()
    logger.info(
        "IBGE: %d/%d municípios com população, %d/%d com área",
        pop_matched, len(municipios), area_matched, len(municipios),
    )
    return pop_matched, area_matched
