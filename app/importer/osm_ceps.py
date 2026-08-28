"""Preenche `cep_coordenadas` em massa a partir do extrato do Brasil do
OpenStreetMap (via Geofabrik), em vez de geocodificar CEP a CEP contra a
API pública do Nominatim -- bater na API 1x por CEP pros ~1.6 milhão de
CEPs da base violaria a política de uso deles (que pede uso local pra
geocodificação em massa) e levaria semanas.

O extrato é um arquivo estático (atualizado ~1x/dia), sem limite de taxa:
    1. Baixa brazil-latest.osm.pbf (~2GB)
    2. `osmium tags-filter` extrai só os elementos com addr:postcode
    3. `osmium export` converte pra GeoJSONSeq (uma feature por linha --
       streaming, sem carregar tudo na memória)
    4. Cada linha com CEP de 8 dígitos vira uma linha em cep_coordenadas
       (COPY + INSERT ... ON CONFLICT DO NOTHING -- nunca sobrescreve uma
       coordenada exata já cacheada via BrasilAPI)

Cobertura é parcial (o OSM no Brasil é mantido por voluntários, mais denso
em cidades grandes) -- não é um substituto de geocodificar cada CEP com
precisão, é um fallback muito melhor que nada, carregado de uma vez.
"""

import csv
import io
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger("importer")

EXTRACT_URL = "https://download.geofabrik.de/south-america/brazil-latest.osm.pbf"


def _download(path: str) -> None:
    logger.info("Baixando extrato do Brasil (OpenStreetMap/Geofabrik, ~2GB)...")
    with httpx.stream("GET", EXTRACT_URL, timeout=None, follow_redirects=True) as response:
        response.raise_for_status()
        downloaded = 0
        with open(path, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
    logger.info("Extrato baixado: %.1fGB", downloaded / 1024**3)


def _filter_and_export(pbf_path: str, filtered_path: str, geojsonseq_path: str) -> None:
    logger.info("Filtrando elementos com CEP (addr:postcode)...")
    subprocess.run(
        ["osmium", "tags-filter", pbf_path, "nwr/addr:postcode", "-o", filtered_path, "--overwrite"],
        check=True,
    )
    logger.info("Exportando pra GeoJSONSeq...")
    subprocess.run(
        ["osmium", "export", filtered_path, "-o", geojsonseq_path, "-f", "geojsonseq", "--overwrite"],
        check=True,
    )


def _rows(geojsonseq_path: str):
    """Le o GeoJSONSeq linha a linha (streaming) e produz (cep, lat, lon)
    unicos -- primeira ocorrencia de cada CEP vence, as seguintes (mesmo
    CEP, ponto ligeiramente diferente) sao ignoradas."""
    seen = set()
    with open(geojsonseq_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                feature = json.loads(line)
            except json.JSONDecodeError:
                continue

            cep_raw = feature.get("properties", {}).get("addr:postcode")
            if not cep_raw:
                continue
            cep = re.sub(r"\D", "", cep_raw)
            if len(cep) != 8 or cep in seen:
                continue

            geometry = feature.get("geometry", {})
            # Só ponto -- prédio/rua exportado como polígono/linha tem
            # `coordinates` aninhado (lista de anéis/pontos), não um par
            # [lon, lat] direto; checar o tipo evita desempacotar isso como
            # se fosse coordenada (virava lixo em latitude/longitude).
            if geometry.get("type") != "Point":
                continue
            coords = geometry.get("coordinates")
            if not coords or len(coords) != 2:
                continue

            seen.add(cep)
            lon, lat = coords
            yield cep, lat, lon


def import_ceps_from_osm(db: Session) -> int:
    """Baixa o extrato, extrai pontos com CEP e carrega em
    `cep_coordenadas` (só preenche o que ainda não existe). Retorna quantos
    CEPs novos foram inseridos."""
    work_dir = settings.download_dir
    os.makedirs(work_dir, exist_ok=True)
    pbf_path = os.path.join(work_dir, "brazil.osm.pbf")
    filtered_path = os.path.join(work_dir, "brazil-addresses.osm.pbf")
    geojsonseq_path = os.path.join(work_dir, "brazil-addresses.geojsonseq")

    try:
        _download(pbf_path)
        _filter_and_export(pbf_path, filtered_path, geojsonseq_path)
        os.remove(pbf_path)

        logger.info("Carregando CEPs extraídos no banco...")
        db.execute(text("DROP TABLE IF EXISTS tmp_osm_ceps"))
        db.execute(text("CREATE TEMP TABLE tmp_osm_ceps (cep text, latitude text, longitude text)"))
        db.commit()

        # Cursor pego só agora, depois do commit acima -- pego antes, a
        # conexão ainda seria devolvida ao pool do SQLAlchemy nesse commit
        # e o COPY abaixo rodaria num cursor "órfão" (visto na prática:
        # "set_session cannot be used inside a transaction" na query seguinte).
        raw_cursor = db.connection().connection.cursor()

        def csv_lines():
            buf = io.StringIO()
            writer = csv.writer(buf)
            count = 0
            for cep, lat, lon in _rows(geojsonseq_path):
                writer.writerow([cep, lat, lon])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
                count += 1
                if count % 50_000 == 0:
                    logger.info("Lidos %d CEPs únicos do extrato...", count)

        from app.importer.pipeline import _IteratorFile  # reaproveita o streamer de COPY

        raw_cursor.copy_expert(
            "COPY tmp_osm_ceps (cep, latitude, longitude) FROM STDIN WITH (FORMAT csv)",
            _IteratorFile(csv_lines()),
        )

        result = db.execute(text("""
            INSERT INTO cep_coordenadas (cep, latitude, longitude, source, updated_at)
            SELECT cep, latitude::numeric, longitude::numeric, 'osm_extract', :now
            FROM tmp_osm_ceps
            ON CONFLICT (cep) DO NOTHING
        """), {"now": datetime.now(timezone.utc)})
        inserted = result.rowcount
        db.execute(text("DROP TABLE tmp_osm_ceps"))
        db.commit()

        logger.info("OSM: %d CEPs novos carregados em cep_coordenadas.", inserted)
        return inserted
    finally:
        for path in (pbf_path, filtered_path, geojsonseq_path):
            if os.path.exists(path):
                os.remove(path)
