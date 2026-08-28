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
from app.importer.progress import ProgressDisplay, human_bytes

logger = logging.getLogger("importer")

EXTRACT_URL = "https://download.geofabrik.de/south-america/brazil-latest.osm.pbf"

SLOT_DOWNLOAD = 0
SLOT_READ = 1
SLOTS = 2


def _download(path: str, display: ProgressDisplay) -> None:
    """Baixa o extrato, retomando de onde parou se `path` já tem um pedaço em
    disco (de uma tentativa anterior que falhou depois do download -- filtro,
    export ou carga no banco). ~2GB pela rede é o passo mais caro do processo
    de longe; refazer do zero a cada retry porque um passo LOCAL falhou
    depois seria jogar fora a parte mais lenta por causa da mais rápida.
    """
    resume_from = os.path.getsize(path) if os.path.exists(path) else 0
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}

    with httpx.stream("GET", EXTRACT_URL, timeout=None, follow_redirects=True, headers=headers) as response:
        if response.status_code == 416:
            # O que já está em disco cobre o arquivo inteiro -- uma tentativa
            # anterior baixou tudo e só falhou depois, no filtro/export/carga.
            logger.info("Extrato já baixado por completo, pulando pro próximo passo.")
            return

        response.raise_for_status()
        # Servidor pode ignorar o Range (não é padrão, mas nem todo mirror
        # honra) e mandar o arquivo inteiro de novo com 200 -- nesse caso o
        # que já estava em disco não serve, recomeça do zero.
        resumed = resume_from > 0 and response.status_code == 206
        if not resumed:
            resume_from = 0

        total = _content_total(response, resume_from)
        bar = display.bar(SLOT_DOWNLOAD, "  baixando extrato OSM", total=total, unit="bytes")
        downloaded = resume_from
        bar.update(downloaded, force=True)

        with open(path, "ab" if resumed else "wb") as f:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                bar.update(downloaded)
        bar.update(downloaded, force=True)
        bar.close()
    logger.info(
        "Extrato baixado: %s%s", human_bytes(downloaded), " (retomado de tentativa anterior)" if resumed else ""
    )


def _content_total(response: httpx.Response, resume_from: int) -> int | None:
    """Tamanho total do arquivo, a partir do cabeçalho que a resposta trouxer.

    Num 206 (retomada), `Content-Length` é só o que falta -- o total vem do
    `Content-Range: bytes X-Y/TOTAL`. Num 200 (arquivo inteiro), o próprio
    `Content-Length` já é o total.
    """
    content_range = response.headers.get("content-range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1]
        if total.isdigit():
            return int(total)

    content_length = response.headers.get("content-length")
    if content_length and content_length.isdigit():
        return resume_from + int(content_length)

    return None


def _filter_and_export(pbf_path: str, filtered_path: str, geojsonseq_path: str) -> None:
    # `--progress`: osmium já tem barra própria (detecta TTY sozinho, fica
    # muda se a saída for um arquivo/log) -- os subprocessos herdam
    # stdout/stderr do processo Python, então ela aparece direto na tela sem
    # precisarmos parsear nada. Só é seguro chamar entre um `ProgressDisplay`
    # e outro (nunca com um bloco nosso aberto ao mesmo tempo), o que já é o
    # caso aqui: a barra de download fecha antes, a de leitura só abre depois.
    logger.info("Filtrando elementos com CEP (addr:postcode)...")
    subprocess.run(
        ["osmium", "tags-filter", pbf_path, "nwr/addr:postcode", "-o", filtered_path, "--overwrite", "--progress"],
        check=True,
    )
    logger.info("Exportando pra GeoJSONSeq...")
    subprocess.run(
        ["osmium", "export", filtered_path, "-o", geojsonseq_path, "-f", "geojsonseq", "--overwrite", "--progress"],
        check=True,
    )


def _rows(geojsonseq_path: str, display: ProgressDisplay):
    """Le o GeoJSONSeq linha a linha (streaming) e produz (cep, lat, lon)
    unicos -- primeira ocorrencia de cada CEP vence, as seguintes (mesmo
    CEP, ponto ligeiramente diferente) sao ignoradas."""
    seen = set()
    total = os.path.getsize(geojsonseq_path)
    bytes_read = 0
    bar = display.bar(SLOT_READ, "  lendo GeoJSONSeq", total=total, unit="bytes")

    with open(geojsonseq_path, encoding="utf-8") as f:
        for raw_line in f:
            # UTF-8 nao e 1 byte por caractere (acentos, "ç" etc.) -- o
            # tamanho em bytes vem de re-encodar a linha, nao de len() do
            # texto (que so contaria caracteres) nem de f.tell() (nao
            # confiavel durante iteracao em modo texto).
            bytes_read += len(raw_line.encode("utf-8"))
            bar.update(bytes_read, extra=f"{len(seen)} CEPs únicos")

            line = raw_line.strip()
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

    bar.update(bytes_read, extra=f"{len(seen)} CEPs únicos", force=True)
    bar.close()


def import_ceps_from_osm(db: Session) -> int:
    """Baixa o extrato, extrai pontos com CEP e carrega em
    `correios_cep` (só preenche coordenada que ainda não existe). Retorna
    quantos CEPs ganharam coordenada."""
    work_dir = settings.download_dir
    os.makedirs(work_dir, exist_ok=True)
    pbf_path = os.path.join(work_dir, "brazil.osm.pbf")
    filtered_path = os.path.join(work_dir, "brazil-addresses.osm.pbf")
    geojsonseq_path = os.path.join(work_dir, "brazil-addresses.geojsonseq")

    display = ProgressDisplay(SLOTS)
    try:
        _download(pbf_path, display)
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
            for cep, lat, lon in _rows(geojsonseq_path, display):
                writer.writerow([cep, lat, lon])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

        from app.importer.pipeline import _IteratorFile  # reaproveita o streamer de COPY

        raw_cursor.copy_expert(
            "COPY tmp_osm_ceps (cep, latitude, longitude) FROM STDIN WITH (FORMAT csv)",
            _IteratorFile(csv_lines()),
        )

        # DO UPDATE ... WHERE, e não DO NOTHING: depois da fusão das tabelas, a
        # maioria dos CEPs JÁ tem linha (veio do e-DNE) só que sem coordenada --
        # com DO NOTHING o import não preencheria nada. O `WHERE latitude IS
        # NULL` mantém a intenção original: nunca sobrescrever uma coordenada
        # que já existe (a da BrasilAPI é precisa; a do OSM só compartilha o
        # CEP).
        result = db.execute(text("""
            INSERT INTO correios_cep (cep, latitude, longitude, coord_source, coord_updated_at)
            -- cep é INTEGER na tabela; a temp recebe texto direto do COPY.
            SELECT cep::integer, latitude::numeric, longitude::numeric, 'osm_extract', :now
            FROM tmp_osm_ceps
            ON CONFLICT (cep) DO UPDATE SET
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                coord_source = EXCLUDED.coord_source,
                coord_updated_at = EXCLUDED.coord_updated_at
            WHERE correios_cep.latitude IS NULL
        """), {"now": datetime.now(timezone.utc)})
        inserted = result.rowcount
        db.execute(text("DROP TABLE tmp_osm_ceps"))
        db.commit()

        logger.info("OSM: %d CEPs com coordenada nova.", inserted)
        return inserted
    finally:
        display.close()
        # Só o pbf (o download, a parte cara -- ~2GB pela rede) sobrevive a
        # uma falha, pra uma próxima tentativa retomar em vez de rebaixar
        # tudo. filtered/geojsonseq são derivados locais, dropados sempre:
        # refazê-los a partir do pbf (que sobrou) é rápido, e não valem o
        # espaço em disco de ficar guardados até a próxima tentativa.
        for path in (filtered_path, geojsonseq_path):
            if os.path.exists(path):
                os.remove(path)
