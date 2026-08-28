"""Enriquece `municipios` com população estimada e área territorial via
API pública do IBGE (SIDRA/Agregados), sem chave. Um município nosso
(receita_code) tem código diferente do código IBGE de 7 dígitos -- por
isso casa por (nome normalizado, UF) em vez do código; nomes de município
se repetem entre estados (ex. várias "Buritis"), então UF é obrigatório
pra não casar município errado. Precisa que `uf` já esteja preenchido em
`municipios` (ver _build_final_table em app/importer/pipeline.py).

Fontes:
- Agregado 6579 (Estimativas de população), variável 9324
- Agregado 1301 (Área territorial), variável 615
"""

import logging
import re
import unicodedata

import httpx
from sqlalchemy.orm import Session

from app.models import Municipio

logger = logging.getLogger("importer")

POPULATION_URL = "https://servicodados.ibge.gov.br/api/v3/agregados/6579/periodos/-1/variaveis/9324?localidades=N6[all]"
AREA_URL = "https://servicodados.ibge.gov.br/api/v3/agregados/1301/periodos/-1/variaveis/615?localidades=N6[all]"

_UF_SUFFIX = re.compile(r"\s*-\s*([A-Z]{2})\s*$")


def _normalize_name(name: str) -> str:
    stripped = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return _UF_SUFFIX.sub("", stripped).strip().lower()


def _key(nome_ibge: str) -> tuple[str, str] | None:
    match = _UF_SUFFIX.search(nome_ibge)
    if not match:
        return None
    return _normalize_name(nome_ibge), match.group(1)


def _fetch_series(url: str) -> dict[tuple[str, str], tuple[str, float]]:
    """Retorna {(nome_normalizado, uf): (codigo_ibge, valor)}."""
    response = httpx.get(url, timeout=60)
    response.raise_for_status()
    data = response.json()
    series = data[0]["resultados"][0]["series"]

    result = {}
    for entry in series:
        localidade = entry["localidade"]
        valores = entry["serie"]
        if not valores:
            continue
        valor = next(iter(valores.values()))
        if valor in (None, "-", "..", "...", ""):
            continue
        key = _key(localidade["nome"])
        if key:
            result[key] = (localidade["id"], float(valor))
    return result


def import_ibge(db: Session) -> tuple[int, int]:
    """Busca população e área de todos os municípios de uma vez e faz
    UPDATE em `municipios` casando por (nome normalizado, UF). Retorna
    (municipios_com_populacao, municipios_com_area)."""
    logger.info("Buscando população estimada (IBGE/SIDRA agregado 6579)...")
    population = _fetch_series(POPULATION_URL)
    logger.info("Buscando área territorial (IBGE/SIDRA agregado 1301)...")
    area = _fetch_series(AREA_URL)

    municipios = db.query(Municipio).filter(Municipio.uf.isnot(None)).all()
    pop_matched = 0
    area_matched = 0

    for m in municipios:
        key = (_normalize_name(m.name), m.uf)
        pop_entry = population.get(key)
        area_entry = area.get(key)

        if pop_entry:
            m.ibge_code = pop_entry[0]
            m.population = int(pop_entry[1])
            pop_matched += 1
        if area_entry:
            m.ibge_code = area_entry[0]
            m.area_km2 = area_entry[1]
            area_matched += 1

    db.commit()
    logger.info(
        "IBGE: %d/%d municípios com população, %d/%d com área",
        pop_matched, len(municipios), area_matched, len(municipios),
    )
    return pop_matched, area_matched
