"""Pipeline de importacao: baixa -> descompacta -> apaga zip -> importa pra
staging -> apaga CSV, um arquivo por vez (evita acumular todos os zips/CSVs
de um grupo em disco ao mesmo tempo). Staging usa UPSERT (ON CONFLICT) com
unique constraint em cnpj_basico -- ja chega deduplicada, então o JOIN final
não precisa de DISTINCT ON/window function (caro numa tabela de dezenas de
milhões de linhas). Progresso é gravado em import_progress a cada lote, e
lido por GET /import/status."""

import logging
import os
import shutil
import time
import zipfile
from datetime import datetime, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.importer import client
from app.importer.csv_reader import read_csv
from app.importer.phone import parse as parse_phone
from app.importer.progress import ProgressBar, human_bytes
from app.models import (
    Cnae,
    EmpresaStaging,
    EstabelecimentoStaging,
    ImportLog,
    ImportProgress,
    Municipio,
    SimplesStaging,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("importer")

GROUPS = ["reference", "simples", "empresas", "estabelecimentos"]

STEP_PREFIXES = {
    "reference": ["Cnaes", "Municipios"],
    "simples": ["Simples"],
    "empresas": ["Empresas"],
    "estabelecimentos": ["Estabelecimentos"],
}

MAX_LENGTHS = {
    "cnpj_basico": 8,
    "cnpj_ordem": 4,
    "cnpj_dv": 2,
    "identificador_matriz_filial": 1,
    "situacao_cadastral": 2,
    "uf": 2,
    "ddd_1": 4,
    "opcao_simples": 1,
    "opcao_mei": 1,
    "porte_empresa": 2,
}

MAX_ATTEMPTS_PER_FILE = 3
BATCH_SIZE = 10_000


def files_for_group(files: list[str], group: str) -> list[str]:
    prefixes = STEP_PREFIXES[group]
    return sorted(f for f in files if any(f.startswith(p) for p in prefixes))


def run_import(period: str | None = None, only: list[str] | None = None) -> None:
    db = SessionLocal()
    try:
        # Staging é inteiramente reconstruível (UPSERT idempotente, refeito
        # do zero em caso de falha -- ver ImportLog), então esperar o WAL
        # sincronizar a cada commit não compra nada aqui além de lentidão.
        # Efeito: só a última fração de segundo de trabalho pode se perder
        # num crash do Postgres em si (não da aplicação) -- aceitável.
        db.execute(text("SET synchronous_commit = OFF"))

        resolved_period = period or client.discover_latest_period()
        all_files = client.list_files(resolved_period)
        groups = [g for g in GROUPS if not only or g in only]
        run_build = not only or "build" in only

        _set_progress(db, period=resolved_period, status="running", message="iniciando")

        for group in groups:
            group_files = files_for_group(all_files, group)
            logger.info("Grupo %s: %d arquivo(s)", group, len(group_files))
            for i, file in enumerate(group_files, start=1):
                logger.info("[%s] arquivo %d/%d: %s", group, i, len(group_files), file)
                _process_file(db, resolved_period, group, file)

        if run_build:
            _build_final_table(db, resolved_period)

        _set_progress(db, status="success", message="importação concluída")
    except Exception as exc:  # noqa: BLE001
        db.rollback()  # sem isso, a sessão fica com a transação abortada e
        # o próprio registro de falha abaixo falharia, mascarando o erro real.
        _set_progress(db, status="failed", message=str(exc)[:255])
        raise
    finally:
        db.close()


def _process_file(db: Session, period: str, group: str, file: str) -> None:
    if _already_imported(db, period, file):
        _set_progress(db, period=period, group=group, current_file=file, step="skip", message="já importado")
        return

    period_dir = os.path.join(settings.download_dir, period)
    os.makedirs(period_dir, exist_ok=True)
    zip_path = os.path.join(period_dir, file)
    csv_path = os.path.join(period_dir, file.removesuffix(".zip") + ".csv")

    attempt = 0
    while True:
        attempt += 1
        try:
            _download(db, period, group, file, zip_path)
            _extract(db, period, group, file, zip_path, csv_path)
            os.remove(zip_path)
            rows = _import_file(db, period, group, file, csv_path)
            os.remove(csv_path)
            _mark_imported(db, period, file, rows)
            return
        except Exception:  # noqa: BLE001
            logger.exception("Falha processando %s (tentativa %d/%d)", file, attempt, MAX_ATTEMPTS_PER_FILE)
            for path in (zip_path, csv_path):
                if os.path.exists(path):
                    os.remove(path)
            if attempt >= MAX_ATTEMPTS_PER_FILE:
                raise
            time.sleep(10 * attempt)


DB_PROGRESS_INTERVAL = 1.0  # grava no banco no máximo 1x/s -- a barra no console já cobre o resto.


def _download(db: Session, period: str, group: str, file: str, dest: str) -> None:
    url = client.period_url(period) + file
    total = client.file_size(url)
    downloaded = 0
    logger.info("Baixando %s/%s (%s)", group, file, human_bytes(total) if total else "tamanho desconhecido")

    bar = ProgressBar(f"  download {file}", total=total, unit="bytes")
    last_db_write = 0.0

    with httpx.stream("GET", url, timeout=None, follow_redirects=True) as response:
        response.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                bar.update(downloaded)

                now = time.monotonic()
                if now - last_db_write >= DB_PROGRESS_INTERVAL:
                    last_db_write = now
                    _set_progress(
                        db, period=period, group=group, current_file=file, step="download",
                        processed_rows=downloaded, message=f"{downloaded}/{total or '?'} bytes", log=False,
                    )

    bar.update(downloaded, force=True)
    bar.close()
    _set_progress(
        db, period=period, group=group, current_file=file, step="download",
        processed_rows=downloaded, message=f"{downloaded}/{total or '?'} bytes", log=False,
    )
    logger.info("Download de %s concluído: %s", file, human_bytes(downloaded))


def _extract(db: Session, period: str, group: str, file: str, zip_path: str, csv_path: str) -> None:
    _set_progress(db, period=period, group=group, current_file=file, step="extract", message="descompactando")

    with zipfile.ZipFile(zip_path) as zf:
        inner_name = zf.namelist()[0]
        total = zf.getinfo(inner_name).file_size
        bar = ProgressBar(f"  extraindo {file}", total=total, unit="bytes")
        written = 0

        with zf.open(inner_name) as src, open(csv_path, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                written += len(chunk)
                bar.update(written)

        bar.update(written, force=True)
        bar.close()

    logger.info("Extraído %s: %s", file, human_bytes(written))


def _import_file(db: Session, period: str, group: str, file: str, csv_path: str) -> int:
    if group == "reference":
        return _import_reference(db, period, group, file, csv_path)

    spec = {
        "simples": {
            "columns": [
                "cnpj_basico", "opcao_simples", "data_opcao_simples", "data_exclusao_simples",
                "opcao_mei", "data_opcao_mei", "data_exclusao_mei",
            ],
            "model": SimplesStaging,
            "keep": ["cnpj_basico", "opcao_simples", "opcao_mei"],
            "unique": ["cnpj_basico"],
        },
        "empresas": {
            "columns": [
                "cnpj_basico", "razao_social", "natureza_juridica", "qualificacao_responsavel",
                "capital_social", "porte_empresa", "ente_federativo",
            ],
            "model": EmpresaStaging,
            "keep": ["cnpj_basico", "razao_social", "porte_empresa"],
            "unique": ["cnpj_basico"],
        },
        "estabelecimentos": {
            "columns": [
                "cnpj_basico", "cnpj_ordem", "cnpj_dv", "identificador_matriz_filial", "nome_fantasia",
                "situacao_cadastral", "data_situacao_cadastral", "motivo_situacao_cadastral",
                "nome_cidade_exterior", "pais", "data_inicio_atividade", "cnae_fiscal_principal",
                "cnae_fiscal_secundaria", "tipo_logradouro", "logradouro", "numero", "complemento",
                "bairro", "cep", "uf", "municipio_codigo", "ddd_1", "telefone_1", "ddd_2", "telefone_2",
                "ddd_fax", "fax", "correio_eletronico", "situacao_especial", "data_situacao_especial",
            ],
            "model": EstabelecimentoStaging,
            "keep": [
                "cnpj_basico", "cnpj_ordem", "cnpj_dv", "identificador_matriz_filial", "nome_fantasia",
                "situacao_cadastral", "data_inicio_atividade", "cnae_fiscal_principal",
                "cnae_fiscal_secundaria", "uf", "municipio_codigo", "ddd_1", "telefone_1", "correio_eletronico",
            ],
            "unique": ["cnpj_basico", "cnpj_ordem", "cnpj_dv"],
            "date_fields": ["data_inicio_atividade"],
        },
    }[group]

    table = spec["model"].__table__
    date_fields = spec.get("date_fields", [])
    count = 0
    batch = []

    bar = ProgressBar(f"  importando {file}", total=os.path.getsize(csv_path), unit="bytes")
    last_db_write = 0.0

    def flush():
        # Sem commit aqui de propósito -- comitar a cada lote de milhões de
        # linhas é o maior gargalo (fsync do WAL por commit). O commit
        # acontece no ritmo do on_progress abaixo (~1x/s) e no final do
        # arquivo; se cair no meio, o arquivo é refeito do zero de qualquer
        # forma (UPSERT idempotente, ver ImportLog), então perder alguns
        # lotes não-comitados de um crash não muda o resultado final.
        nonlocal count
        if not batch:
            return
        stmt = pg_insert(table).values(batch)
        update_cols = {c: stmt.excluded[c] for c in batch[0] if c not in spec["unique"]}
        stmt = stmt.on_conflict_do_update(index_elements=spec["unique"], set_=update_cols)
        db.execute(stmt)
        count_local = len(batch)
        batch.clear()
        return count_local

    def on_progress(bytes_read: int, _total_bytes: int, rows_read: int) -> None:
        nonlocal last_db_write
        bar.update(bytes_read, extra=f"{rows_read} linhas")
        now = time.monotonic()
        if now - last_db_write >= DB_PROGRESS_INTERVAL:
            last_db_write = now
            _set_progress(
                db, period=period, group=group, current_file=file, step="import",
                processed_rows=rows_read, message=f"{rows_read} linhas", log=False,
            )

    for row in read_csv(csv_path, spec["columns"], on_progress=on_progress):
        mapped = {k: row.get(k) for k in spec["keep"]}
        for field in date_fields:
            mapped[field] = _parse_date(mapped.get(field))
        for field, max_len in MAX_LENGTHS.items():
            if mapped.get(field) and len(mapped[field]) > max_len:
                mapped[field] = mapped[field][:max_len]
        mapped["source_file"] = file
        batch.append(mapped)

        if len(batch) >= BATCH_SIZE:
            count += flush()

    count += flush() or 0
    bar.update(os.path.getsize(csv_path), extra=f"{count} linhas", force=True)
    bar.close()
    _set_progress(
        db, period=period, group=group, current_file=file, step="import",
        processed_rows=count, message=f"{count} linhas", log=False,
    )
    logger.info("Importado %s: %d linhas", file, count)
    return count


def _import_reference(db: Session, period: str, group: str, file: str, csv_path: str) -> int:
    spec = {
        "Cnaes.zip": (["code", "description"], Cnae, "code"),
        "Municipios.zip": (["receita_code", "name"], Municipio, "receita_code"),
    }[file]
    columns, model, key = spec
    table = model.__table__
    count = 0

    bar = ProgressBar(f"  importando {file}", total=os.path.getsize(csv_path), unit="bytes")

    def on_progress(bytes_read: int, _total_bytes: int, rows_read: int) -> None:
        bar.update(bytes_read, extra=f"{rows_read} linhas")

    for row in read_csv(csv_path, columns, on_progress=on_progress):
        stmt = pg_insert(table).values(**row)
        update_cols = {c: stmt.excluded[c] for c in row if c != key}
        stmt = stmt.on_conflict_do_update(index_elements=[key], set_=update_cols)
        db.execute(stmt)
        count += 1

    db.commit()
    bar.update(os.path.getsize(csv_path), extra=f"{count} linhas", force=True)
    bar.close()
    _set_progress(db, period=period, group=group, current_file=file, step="import", processed_rows=count, log=False)
    logger.info("Importado %s: %d linhas", file, count)
    return count


def _build_final_table(db: Session, period: str) -> None:
    _set_progress(db, period=period, group="build", step="build", message="montando establishments a partir do staging")

    db.execute(text("DROP TABLE IF EXISTS establishments_new"))
    db.execute(text("CREATE TABLE establishments_new (LIKE establishments INCLUDING ALL)"))
    # LIKE...INCLUDING ALL copia o DEFAULT nextval(...) literal -- a tabela
    # nova ficaria presa à sequence da tabela antiga, e o DROP TABLE do
    # swap falharia por dependência (visto na prática, não suposição).
    # Sequence própria antes de qualquer INSERT usar o default.
    db.execute(text("CREATE SEQUENCE IF NOT EXISTS establishments_new_id_seq OWNED BY establishments_new.id"))
    db.execute(text("ALTER TABLE establishments_new ALTER COLUMN id SET DEFAULT nextval('establishments_new_id_seq')"))

    # Índices secundários (não essenciais pro ON CONFLICT abaixo, só pras
    # queries da API depois) custam caro de manter linha a linha num INSERT
    # de milhões de linhas -- dropa os que o LIKE copiou e recria só depois
    # do bulk load, onde um CREATE INDEX é ordens de magnitude mais rápido.
    # Índice único de cnpj (usado pelo ON CONFLICT) e PK ficam intactos.
    deferred_indexes = []
    for col in ("main_cnae_code", "uf", "cellphone"):
        idx_name = db.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'establishments_new' AND indexdef ILIKE :pattern"
            ),
            {"pattern": f"%({col})%"},
        ).scalar()
        if idx_name:
            db.execute(text(f'DROP INDEX "{idx_name}"'))
            deferred_indexes.append(col)
    db.commit()

    imported = db.execute(text("""
        INSERT INTO establishments_new
            (cnpj, company_name, trade_name, is_headquarters, is_mei, is_simples,
             company_size, main_cnae_code, secondary_cnae_codes, municipio_id,
             uf, email, phone, cellphone, cellphone_confidence, opened_at)
        SELECT
            lpad(e.cnpj_basico, 8, '0') || lpad(e.cnpj_ordem, 4, '0') || lpad(e.cnpj_dv, 2, '0'),
            coalesce(nullif(emp.razao_social, ''), nullif(e.nome_fantasia, ''), ''),
            e.nome_fantasia,
            coalesce(e.identificador_matriz_filial = '1', false),
            coalesce(s.opcao_mei = 'S', false),
            coalesce(s.opcao_simples = 'S', false),
            emp.porte_empresa,
            e.cnae_fiscal_principal,
            '[]',
            m.id,
            e.uf,
            e.correio_eletronico,
            NULL,
            NULL,
            0,
            e.data_inicio_atividade
        FROM estabelecimentos_staging e
        LEFT JOIN empresas_staging emp ON emp.cnpj_basico = e.cnpj_basico
        LEFT JOIN simples_staging s ON s.cnpj_basico = e.cnpj_basico
        LEFT JOIN municipios m ON m.receita_code = e.municipio_codigo
        WHERE e.situacao_cadastral = '02'
        ON CONFLICT (cnpj) DO NOTHING
    """))
    db.commit()

    _fill_phones_and_secondary_cnaes(db, period)

    for col in deferred_indexes:
        _set_progress(db, period=period, group="build", step="build", message=f"recriando índice de {col}")
        db.execute(text(f'CREATE INDEX "ix_establishments_new_{col}" ON establishments_new ({col})'))
        db.commit()

    db.execute(text("ALTER TABLE establishments RENAME TO establishments_old"))
    db.execute(text("ALTER TABLE establishments_new RENAME TO establishments"))
    # Só depois de dropar a tabela antiga (e os índices dela junto) pra
    # liberar os nomes canônicos (ix_establishments_<col>) sem colisão.
    db.execute(text("DROP TABLE establishments_old"))
    for col in deferred_indexes:
        db.execute(text(f'ALTER INDEX "ix_establishments_new_{col}" RENAME TO "ix_establishments_{col}"'))
    db.execute(text("ALTER SEQUENCE establishments_new_id_seq RENAME TO establishments_id_seq"))
    db.execute(text("TRUNCATE TABLE empresas_staging, simples_staging, estabelecimentos_staging"))
    db.execute(text("DELETE FROM import_log WHERE period = :period"), {"period": period})
    db.commit()

    _set_progress(db, period=period, group="build", step="build", processed_rows=imported.rowcount, message="tabela final trocada atomicamente")


def _fill_phones_and_secondary_cnaes(db: Session, period: str) -> None:
    result = db.execute(text("""
        SELECT cnpj_basico, cnpj_ordem, cnpj_dv, ddd_1, telefone_1, cnae_fiscal_secundaria
        FROM estabelecimentos_staging
        WHERE situacao_cadastral = '02'
    """))

    batch = []
    processed = 0

    def flush():
        nonlocal batch
        if not batch:
            return

        tuples = []
        params = {}
        for i, row in enumerate(batch):
            names = [f"p{i}_{j}" for j in range(len(row))]
            # Sem cast (`::json`) colado no bindparam aqui -- o parser de
            # `text()` do SQLAlchemy não reconhece `:nome::tipo` como
            # bindparam seguido de cast, e o valor sai sem substituir. O
            # cast entra no SET, sobre a coluna já materializada da VALUES.
            placeholders = ", ".join(f":{n}" for n in names)
            tuples.append(f"({placeholders})")
            params.update(dict(zip(names, row)))

        sql = (
            "UPDATE establishments_new AS t SET phone = v.phone, cellphone = v.cellphone, "
            "cellphone_confidence = v.confidence, secondary_cnae_codes = v.secondary::json "
            f"FROM (VALUES {', '.join(tuples)}) AS v(cnpj, phone, cellphone, confidence, secondary) "
            "WHERE t.cnpj = v.cnpj"
        )
        db.execute(text(sql), params)
        db.commit()
        batch = []

    for row in result:
        secondary = [c for c in (row.cnae_fiscal_secundaria or "").split(",") if c]
        phone = parse_phone((row.ddd_1 or "") + (row.telefone_1 or ""))

        if not phone and not secondary:
            continue

        cnpj = f"{row.cnpj_basico:0>8}{row.cnpj_ordem:0>4}{row.cnpj_dv:0>2}"
        batch.append((
            cnpj,
            phone["e164"] if phone and phone["type"] == "landline" else None,
            phone["e164"] if phone and phone["type"] == "mobile" else None,
            phone["confidence"] if phone and phone["type"] == "mobile" else 0,
            _json_array(secondary),
        ))
        processed += 1

        if len(batch) >= 500:
            flush()
            _set_progress(db, period=period, group="build", step="build", processed_rows=processed, message="telefone/celular")

    flush()


def _json_array(items: list[str]) -> str:
    import json

    return json.dumps(items)


def _parse_date(value: str | None) -> str | None:
    if not value or len(value) != 8 or value == "00000000":
        return None
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"


def _already_imported(db: Session, period: str, filename: str) -> bool:
    return db.query(ImportLog).filter_by(period=period, filename=filename).first() is not None


def _mark_imported(db: Session, period: str, filename: str, rows: int) -> None:
    stmt = pg_insert(ImportLog.__table__).values(
        period=period, filename=filename, rows_imported=rows, imported_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["period", "filename"],
        set_={"rows_imported": stmt.excluded.rows_imported, "imported_at": stmt.excluded.imported_at},
    )
    db.execute(stmt)
    db.commit()


def _set_progress(db: Session, *, log: bool = True, **fields) -> None:
    if log:
        prefix = "/".join(p for p in (fields.get("group"), fields.get("current_file"), fields.get("step")) if p)
        logger.info("[%s] %s", prefix, fields.get("message", "")) if prefix else logger.info(fields.get("message", ""))

    fields["updated_at"] = datetime.now(timezone.utc)
    if fields.get("status") == "running" and "started_at" not in fields:
        existing = db.get(ImportProgress, 1)
        if not existing or existing.status != "running":
            fields["started_at"] = fields["updated_at"]

    stmt = pg_insert(ImportProgress.__table__).values(id=1, **fields)
    update_cols = {c: stmt.excluded[c] for c in fields}
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_cols)
    db.execute(stmt)
    db.commit()
