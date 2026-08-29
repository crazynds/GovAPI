"""Preenche o centroide de cada município (latitude/longitude) a partir de um
dataset público estático, em vez de geocodificar município a município contra
o Nominatim -- usado como fallback de baixa precisão (nível cidade) em
`/enderecos/proximos` e `/enderecos/buscar` quando um CEP específico ainda não
tem coordenada exata cacheada em `correios_cep`.

Geocodificar as ~5570 chamadas contra o Nominatim (mesmo respeitando o limite
de 1 req/s da política de uso deles) levava ~1h40 -- e cada nova troca de
municípios (nome mudou, novo município criado) pagaria isso de novo. O dataset
abaixo (kelvins/municipios-brasileiros, MIT, atualizado pela comunidade a
partir de fontes do IBGE) cobre os ~5570 municípios com o código IBGE exato,
então o match aqui é por código, não por nome -- diferente de app/importer/
ibge.py, que precisa casar por (nome, UF) porque a fonte dele (Receita) não
tem o código IBGE embutido.

Precisa que `municipios.ibge_code` já esteja preenchido (ver
app.importer.ibge.import_ibge, que roda antes na ordem de `import-all`).
"""

import csv
import io
import logging

import httpx
from sqlalchemy.orm import Session

from app.models import Municipio

logger = logging.getLogger("importer")

DATASET_URL = "https://raw.githubusercontent.com/kelvins/municipios-brasileiros/main/csv/municipios.csv"


def geocode_municipios(db: Session) -> tuple[int, int]:
    """Baixa o dataset uma vez e faz UPDATE em `municipios` casando por
    `ibge_code` exato. Retorna (municípios atualizados, total no dataset)."""
    logger.info("Baixando coordenadas de municípios (dataset estático, ~5570 linhas, uma request só)...")
    response = httpx.get(DATASET_URL, timeout=30)
    response.raise_for_status()

    reader = csv.DictReader(io.StringIO(response.text))
    # int(...): `Municipio.ibge_code` e Integer, mas csv.DictReader so devolve
    # str -- comparar str contra int nunca bate, e o lookup abaixo falharia
    # silenciosamente pra TODO municipio (visto na pratica, testando isto).
    coords = {int(row["codigo_ibge"]): (float(row["latitude"]), float(row["longitude"])) for row in reader}

    # So os que ja tem ibge_code (import-ibge casa isso por nome+UF antes) --
    # sem ele nao ha como bater com o dataset, que so identifica por codigo.
    candidatos = db.query(Municipio).filter(Municipio.ibge_code.isnot(None)).all()
    sem_ibge_code = db.query(Municipio).filter(Municipio.ibge_code.is_(None)).count()

    updated = 0
    for m in candidatos:
        coord = coords.get(m.ibge_code)
        if coord:
            m.latitude, m.longitude = coord
            updated += 1

    db.commit()
    logger.info(
        "Coordenadas: %d/%d municípios atualizados (dataset tem %d; %d município(s) sem ibge_code -- "
        "rode `import-ibge` primeiro pra esses casarem).",
        updated, len(candidatos), len(coords), sem_ibge_code,
    )
    return updated, len(coords)
