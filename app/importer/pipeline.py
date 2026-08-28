"""Pipeline de importacao da base de CNPJ, em tres estagios paralelos.

    enfileira todos os arquivos (na ordem dos grupos)
            |
       [download] --fila--> [extract] --fila--> [import] --> build
       1 por vez            1 por vez           1 por vez

Como o `docker pull`: cada estagio processa um arquivo por vez, mas arquivos
diferentes ocupam estagios diferentes ao mesmo tempo -- enquanto um CSV de 20GB
esta sendo importado, o proximo zip ja esta baixando. Em serie (como era antes),
a rede ficava ociosa durante todo o import e o banco ocioso durante todo o
download.

O preco disso e ter mais de um arquivo em disco ao mesmo tempo, que era
justamente o que a versao serial evitava. Por isso a admissao de novos arquivos
e regida por um orcamento de bytes (`APP_DISK_BUDGET`), nao por contagem: os
arquivos pequenos pipelinam de verdade, e os gigantes degradam pra
quase-serial em vez de encher o disco.

O staging usa UPSERT (ON CONFLICT) com chave natural, entao ja chega
deduplicado e o JOIN do build nao precisa de DISTINCT ON. As conversoes de tipo
acontecem no streaming pro COPY (ver app/importer/rows.py), entao o staging ja
nasce nos tipos finais.
"""

import csv
import io
import logging
import os
import queue
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.importer import client
from app.importer.csv_reader import read_csv
from app.importer.progress import ProgressDisplay, human_bytes
from app.importer.rows import GROUP_SPECS, Counters
from app.regions import CODE_TO_UF
from app.models import (
    Cnae,
    ImportLog,
    ImportProgress,
    ImportRun,
    Motivo,
    Municipio,
    NaturezaJuridica,
    Pais,
    Qualificacao,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("importer")

GROUPS = ["reference", "simples", "empresas", "estabelecimentos", "socios"]

STEP_PREFIXES = {
    "reference": ["Cnaes", "Municipios", "Motivos", "Naturezas", "Paises", "Qualificacoes"],
    "simples": ["Simples"],
    "empresas": ["Empresas"],
    "estabelecimentos": ["Estabelecimentos"],
    "socios": ["Socios"],
}

REFERENCE_SPECS = {
    "Cnaes.zip": (["code", "description"], Cnae, "code"),
    "Municipios.zip": (["receita_code", "name"], Municipio, "receita_code"),
    "Motivos.zip": (["code", "description"], Motivo, "code"),
    "Naturezas.zip": (["code", "description"], NaturezaJuridica, "code"),
    "Paises.zip": (["code", "description"], Pais, "code"),
    "Qualificacoes.zip": (["code", "description"], Qualificacao, "code"),
}

MAX_ATTEMPTS_PER_STAGE = 3

# Quanto o CSV descompactado cresce em relacao ao zip -- so pra reservar disco
# antes de baixar, quando o tamanho real ainda nao e conhecido. Medido nos
# arquivos da Receita (texto ISO-8859-1 muito repetitivo): ~4-5x.
CSV_EXPANSION_FACTOR = 5

STEPS = ("download", "extract", "import")

DB_PROGRESS_INTERVAL = 1.0  # grava no banco no maximo 1x/s -- a barra na tela cobre o resto.


def files_for_group(files: list[str], group: str) -> list[str]:
    prefixes = STEP_PREFIXES[group]
    return sorted(f for f in files if any(f.startswith(p) for p in prefixes))


# --------------------------------------------------------------------------
# Orcamento de disco
# --------------------------------------------------------------------------


class DiskBudget:
    """Semaforo contado em bytes.

    `reserve` bloqueia enquanto nao couber, mas sempre admite um arquivo quando
    nada esta reservado -- senao um arquivo maior que o orcamento inteiro
    travaria o pipeline pra sempre em vez de so estourar o disco (que ao menos
    da um erro claro).
    """

    def __init__(self, total: int):
        self.total = total
        self._used = 0
        self._cond = threading.Condition()

    def reserve(self, amount: int, abort: threading.Event) -> bool:
        with self._cond:
            while self._used + amount > self.total and self._used > 0:
                if abort.is_set():
                    return False
                self._cond.wait(timeout=1.0)
            if abort.is_set():
                return False
            self._used += amount
            return True

    def release(self, amount: int) -> None:
        with self._cond:
            self._used = max(0, self._used - amount)
            self._cond.notify_all()

    def wake(self) -> None:
        """Destrava quem estiver esperando -- usado no abort."""
        with self._cond:
            self._cond.notify_all()


def _resolve_disk_budget() -> int:
    if settings.disk_budget > 0:
        return settings.disk_budget

    os.makedirs(settings.download_dir, exist_ok=True)
    free = shutil.disk_usage(settings.download_dir).free
    return int(free * 0.7)


# --------------------------------------------------------------------------
# Itens que trafegam entre os estagios
# --------------------------------------------------------------------------


@dataclass
class Job:
    period: str
    group: str
    file: str
    zip_path: str
    csv_path: str
    # Bytes reservados no orcamento de disco, devolvidos conforme os arquivos
    # sao apagados.
    reserved: int = 0
    zip_bytes: int = 0
    csv_bytes: int = 0
    rows: int = 0
    counters: Counters = field(default_factory=Counters)


# --------------------------------------------------------------------------
# Orquestracao
# --------------------------------------------------------------------------


class ImportPipeline:
    def __init__(self, period: str, groups: list[str], run_build: bool):
        self.period = period
        self.groups = groups
        self.run_build = run_build

        self.abort = threading.Event()
        self.error: BaseException | None = None
        self.budget = DiskBudget(_resolve_disk_budget())

        # maxsize=1: o estagio seguinte segura no maximo um arquivo pronto na
        # fila. Mais que isso so acumularia bytes em disco sem ganhar
        # paralelismo -- ja ha um arquivo em cada estagio.
        self.to_extract: queue.Queue[Job | None] = queue.Queue(maxsize=1)
        self.to_import: queue.Queue[Job | None] = queue.Queue(maxsize=1)

        self.display = ProgressDisplay(len(STEPS))
        # Uma sessao por thread: Session do SQLAlchemy nao e thread-safe.
        self._sessions: dict[int, Session] = {}
        self._sessions_lock = threading.Lock()
        self.imported_rows = 0  # total carregado no run, logado no fim

    # -- sessoes ----------------------------------------------------------

    def session(self) -> Session:
        key = threading.get_ident()
        with self._sessions_lock:
            db = self._sessions.get(key)
            if db is None:
                db = SessionLocal()
                self._sessions[key] = db
            return db

    def close_sessions(self) -> None:
        with self._sessions_lock:
            for db in self._sessions.values():
                db.close()
            self._sessions.clear()

    # -- execucao ---------------------------------------------------------

    def run(self) -> None:
        main_db = self.session()
        logger.info("Orçamento de disco: %s", human_bytes(self.budget.total))

        _set_run(main_db, period=self.period, status="running", message="iniciando")
        _reset_steps(main_db, self.period)

        jobs = self._plan(main_db)
        logger.info("%d arquivo(s) a processar em %s", len(jobs), self.period)

        threads = [
            threading.Thread(target=self._guard, args=(self._download_stage, jobs), name="download", daemon=True),
            threading.Thread(target=self._guard, args=(self._extract_stage, None), name="extract", daemon=True),
            threading.Thread(target=self._guard, args=(self._import_stage, None), name="import", daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.display.close()
        _finish_steps(self.session())

        if self.error:
            raise self.error

        if self.run_build:
            _build_final_table(main_db, self.period, self.display)

        _set_run(main_db, period=self.period, status="success", message="importação concluída")

    def _plan(self, db: Session) -> list[Job]:
        all_files = client.list_files(self.period)
        jobs: list[Job] = []
        period_dir = os.path.join(settings.download_dir, self.period)
        os.makedirs(period_dir, exist_ok=True)

        for group in self.groups:
            group_files = files_for_group(all_files, group)
            pending = [f for f in group_files if not _already_imported(db, self.period, f)]
            logger.info("Grupo %s: %d arquivo(s), %d pendente(s)", group, len(group_files), len(pending))

            if group == "socios" and group_files and len(pending) == len(group_files):
                # socios nao tem staging+swap (cada Socios<N>.zip cobre uma
                # faixa disjunta de cnpj_basico, nao tem o que dar merge entre
                # arquivos) -- zera uma vez no inicio de um run NOVO (nenhum
                # arquivo do grupo ainda marcado em ImportLog), senao um
                # reimport mensal ficaria empilhando linha repetida. Numa
                # RETOMADA nao zera, senao perderia os arquivos ja concluidos.
                db.execute(text("TRUNCATE socios"))
                db.commit()

            for file in pending:
                jobs.append(
                    Job(
                        period=self.period,
                        group=group,
                        file=file,
                        zip_path=os.path.join(period_dir, file),
                        csv_path=os.path.join(period_dir, file.removesuffix(".zip") + ".csv"),
                    )
                )
        return jobs

    def _guard(self, stage, jobs) -> None:
        """Roda um estagio; qualquer excecao aborta o pipeline inteiro.

        Sem isso, uma falha numa thread deixaria as outras esperando pra sempre
        numa fila que nunca mais recebe nada.
        """
        try:
            stage(jobs) if jobs is not None else stage()
        except BaseException as exc:  # noqa: BLE001 -- registra e propaga na thread principal
            logger.exception("Estágio %s falhou", threading.current_thread().name)
            if self.error is None:
                self.error = exc
            self.abort.set()
            self.budget.wake()
            # Destrava quem estiver bloqueado num put/get das filas.
            _drain(self.to_extract)
            _drain(self.to_import)

    # -- estagios ---------------------------------------------------------

    def _download_stage(self, jobs: list[Job]) -> None:
        bar_slot = STEPS.index("download")
        try:
            for job in jobs:
                if self.abort.is_set():
                    break

                # Sem Content-Length (size == 0) nao ha o que reservar, e o
                # arquivo entra sem contar pro orcamento -- e a mesma aposta que
                # a versao serial ja fazia, so que agora explicita.
                size = client.file_size(client.period_url(job.period) + job.file) or 0
                job.reserved = size + size * CSV_EXPANSION_FACTOR if size else 0
                if job.reserved and not self.budget.reserve(job.reserved, self.abort):
                    job.reserved = 0
                    break

                try:
                    self._retry(job, "download", lambda: self._download(job, bar_slot, size))
                except BaseException:
                    self.budget.release(job.reserved)
                    raise

                _put(self.to_extract, job, self.abort)
        finally:
            _put(self.to_extract, None, self.abort)

    def _extract_stage(self) -> None:
        bar_slot = STEPS.index("extract")
        try:
            while True:
                job = self.to_extract.get()
                if job is None or self.abort.is_set():
                    break
                self._retry(job, "extract", lambda: self._extract(job, bar_slot))
                _put(self.to_import, job, self.abort)
        finally:
            _put(self.to_import, None, self.abort)

    def _import_stage(self) -> None:
        bar_slot = STEPS.index("import")
        db = self.session()
        # Staging e inteiramente reconstruivel (UPSERT idempotente, refeito do
        # zero em caso de falha -- ver ImportLog), entao esperar o WAL
        # sincronizar a cada commit nao compra nada aqui alem de lentidao.
        db.execute(text("SET synchronous_commit = OFF"))
        db.commit()

        while True:
            job = self.to_import.get()
            if job is None or self.abort.is_set():
                break
            try:
                self._retry(job, "import", lambda: self._import(db, job, bar_slot))
            finally:
                # Depois de todas as tentativas, nao entre elas -- o retry
                # precisa do CSV ainda em disco pra reler.
                if os.path.exists(job.csv_path):
                    os.remove(job.csv_path)
                self.budget.release(job.reserved)
                job.reserved = 0
            _mark_imported(db, job.period, job.file, job.rows)
            self.imported_rows += job.rows

    # -- retry por estagio -------------------------------------------------

    def _retry(self, job: Job, step: str, action) -> None:
        """Re-tenta um estagio isoladamente.

        Por estagio, e nao pelo trio inteiro: uma falha de rede no download nao
        deve refazer um import de 20 minutos, e uma falha no import nao deve
        rebaixar 5GB.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                action()
                return
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Falha em %s de %s (tentativa %d/%d)", step, job.file, attempt, MAX_ATTEMPTS_PER_STAGE
                )
                if step == "import":
                    # Sem isso a sessao fica com a transacao abortada e toda
                    # query seguinte falha com "current transaction is
                    # aborted", mascarando o erro real.
                    self.session().rollback()
                if attempt >= MAX_ATTEMPTS_PER_STAGE or self.abort.is_set():
                    raise
                time.sleep(10 * attempt)

    # -- trabalho de cada estagio -----------------------------------------

    def _download(self, job: Job, slot: int, total: int) -> None:
        url = client.period_url(job.period) + job.file
        db = self.session()
        downloaded = 0
        logger.info("Baixando %s/%s (%s)", job.group, job.file, human_bytes(total) if total else "tamanho desconhecido")

        bar = self.display.bar(slot, f"  download  {job.file}", total=total or None, unit="bytes")
        last_db_write = 0.0

        with httpx.stream("GET", url, timeout=None, follow_redirects=True) as response:
            response.raise_for_status()
            with open(job.zip_path, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    if self.abort.is_set():
                        raise RuntimeError("abortado por falha em outro estágio")
                    f.write(chunk)
                    downloaded += len(chunk)
                    bar.update(downloaded)

                    now = time.monotonic()
                    if now - last_db_write >= DB_PROGRESS_INTERVAL:
                        last_db_write = now
                        _set_step(
                            db, "download", period=job.period, group=job.group, current_file=job.file,
                            status="running", processed_rows=downloaded, total_bytes=total or None,
                        )

        bar.update(downloaded, force=True)
        job.zip_bytes = downloaded
        _set_step(
            db, "download", period=job.period, group=job.group, current_file=job.file,
            status="running", processed_rows=downloaded, total_bytes=total or None,
            message=f"{human_bytes(downloaded)} baixados",
        )
        logger.info("Download de %s concluído: %s", job.file, human_bytes(downloaded))

    def _extract(self, job: Job, slot: int) -> None:
        db = self.session()
        written = 0

        with zipfile.ZipFile(job.zip_path) as zf:
            inner_name = zf.namelist()[0]
            total = zf.getinfo(inner_name).file_size
            bar = self.display.bar(slot, f"  extraindo {job.file}", total=total, unit="bytes")
            _set_step(
                db, "extract", period=job.period, group=job.group, current_file=job.file,
                status="running", processed_rows=0, total_bytes=total, message="descompactando",
            )

            with zf.open(inner_name) as src, open(job.csv_path, "wb") as dst:
                while True:
                    if self.abort.is_set():
                        raise RuntimeError("abortado por falha em outro estágio")
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    written += len(chunk)
                    bar.update(written)

            bar.update(written, force=True)

        os.remove(job.zip_path)
        # O zip ja saiu do disco; o que continua ocupando e so o CSV.
        self._rebalance(job, csv_bytes=written)

        _set_step(
            db, "extract", period=job.period, group=job.group, current_file=job.file,
            status="running", processed_rows=written, total_bytes=written,
            message=f"{human_bytes(written)} extraídos",
        )
        logger.info("Extraído %s: %s", job.file, human_bytes(written))

    def _rebalance(self, job: Job, csv_bytes: int) -> None:
        """Ajusta a reserva pro tamanho real do CSV, agora que ele e conhecido
        (a reserva inicial era uma estimativa sobre o tamanho do zip)."""
        job.csv_bytes = csv_bytes
        if not job.reserved:
            return
        excess = job.reserved - csv_bytes
        if excess > 0:
            self.budget.release(excess)
            job.reserved = csv_bytes
        # Se o CSV veio MAIOR que a estimativa, a reserva fica subestimada --
        # nao aumentamos aqui porque bloquear no meio do pipeline (com o
        # arquivo ja em disco) nao devolveria espaco nenhum. O orcamento e um
        # teto aproximado, e o CSV_EXPANSION_FACTOR e folgado de proposito.

    def _import(self, db: Session, job: Job, slot: int) -> None:
        if job.group == "reference":
            job.rows = _import_reference(db, job, self.display, slot)
        else:
            job.rows = _import_group(db, job, self.display, slot)


def _put(q: queue.Queue, item, abort: threading.Event) -> None:
    """put() que nao trava pra sempre se o consumidor morreu."""
    while True:
        try:
            q.put(item, timeout=1.0)
            return
        except queue.Full:
            if abort.is_set():
                return


def _drain(q: queue.Queue) -> None:
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass
    # Sentinelas pra destravar um get() bloqueado.
    for _ in range(2):
        try:
            q.put_nowait(None)
        except queue.Full:
            return


def run_import(period: str | None = None, only: list[str] | None = None) -> None:
    resolved_period = period or client.discover_latest_period()
    groups = [g for g in GROUPS if not only or g in only]
    run_build = not only or "build" in only

    pipeline = ImportPipeline(resolved_period, groups, run_build)
    try:
        pipeline.run()
    except BaseException as exc:  # noqa: BLE001
        db = pipeline.session()
        db.rollback()  # senao o proprio registro de falha abaixo falharia
        _set_run(db, status="failed", message=str(exc)[:255])
        raise
    finally:
        pipeline.display.close()
        pipeline.close_sessions()


# --------------------------------------------------------------------------
# Import de um arquivo
# --------------------------------------------------------------------------


class _IteratorFile:
    """Faz um gerador de strings parecer um arquivo pro `copy_expert` do
    psycopg2 -- streama linha a linha pro COPY sem montar o CSV inteiro
    em memoria de uma vez."""

    def __init__(self, lines):
        self._lines = lines
        self._buf = ""

    def read(self, size: int = 8192) -> str:
        while len(self._buf) < size:
            try:
                self._buf += next(self._lines)
            except StopIteration:
                break
        result, self._buf = self._buf[:size], self._buf[size:]
        return result


def _import_group(db: Session, job: Job, display: ProgressDisplay, slot: int) -> int:
    spec = GROUP_SPECS[job.group]
    quoted_cols = ", ".join(f'"{c}"' for c in spec.columns)

    # COPY (protocolo nativo do Postgres) pra uma tabela temporaria com o
    # MESMO tipo do destino, depois UM upsert em massa -- ao invés de milhares
    # de INSERT ... ON CONFLICT parametrizados. Testado: e a diferenca entre
    # minutos e horas num arquivo de dezenas de milhoes de linhas. Como a temp
    # ja tem os tipos finais, o COPY faz o parse em C e o INSERT ... SELECT
    # abaixo nao precisa de um unico CAST.
    tmp_table = "tmp_staging_load"
    db.execute(text(f"DROP TABLE IF EXISTS {tmp_table}"))
    # `AS SELECT ... WITH NO DATA` e nao `LIKE`: da exatamente as colunas que o
    # COPY manda, com os tipos do destino, e sem constraint nenhuma. Um `LIKE`
    # traria tambem as colunas que nao carregamos -- inclusive o `id` de
    # socios, que vem NOT NULL mas sem o default da sequence (LIKE nao copia
    # default), e o COPY morria com NotNullViolation.
    db.execute(text(
        f"CREATE TEMP TABLE {tmp_table} AS SELECT {quoted_cols} FROM {spec.table} WITH NO DATA"
    ))
    db.commit()

    csv_size = os.path.getsize(job.csv_path)
    bar = display.bar(slot, f"  importando {job.file}", total=csv_size, unit="bytes")
    stats = {"rows": 0}

    def on_progress(bytes_read: int, _total: int, rows_read: int) -> None:
        # So a barra local aqui -- nenhuma escrita no banco. O COPY abaixo usa
        # a MESMA conexao da sessao (precisa, pra participar da mesma
        # transacao); uma query nessa conexao nesse meio tempo quebraria o
        # protocolo do COPY (visto na pratica: "no COPY in progress").
        stats["rows"] = rows_read
        bar.update(bytes_read, extra=f"{rows_read} linhas")

    def csv_lines():
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in read_csv(job.csv_path, spec.csv_columns, on_progress=on_progress):
            values = spec.transform(row, job.counters)
            if values is None:
                continue
            writer.writerow(["" if v is None else v for v in values])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    raw_cursor = db.connection().connection.cursor()
    raw_cursor.copy_expert(
        f"COPY {tmp_table} ({quoted_cols}) FROM STDIN WITH (FORMAT csv, NULL '')",
        _IteratorFile(csv_lines()),
    )
    count = stats["rows"]

    bar.update(csv_size, extra=f"{count} linhas copiadas", force=True)
    logger.info("Copiado %s pro staging temporário: %d linhas", job.file, count)
    if job.counters.summary():
        logger.warning("%s: %s", job.file, job.counters.summary())

    _set_step(
        db, "import", period=job.period, group=job.group, current_file=job.file, status="running",
        processed_rows=count, message=f"mesclando {count} linhas no staging",
    )

    t0 = time.monotonic()
    if spec.key:
        key_cols = ", ".join(f'"{c}"' for c in spec.key)
        update_cols = [c for c in spec.columns if c not in spec.key]
        set_clause = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
        db.execute(text(f"""
            INSERT INTO {spec.table} ({quoted_cols})
            SELECT {quoted_cols}
            FROM (
                -- O CSV oficial as vezes repete a mesma chave dentro do mesmo
                -- arquivo (visto na pratica: Empresas2.zip) -- um unico INSERT
                -- ... ON CONFLICT DO UPDATE nao aceita conflitar duas vezes
                -- com a mesma linha, entao dedup primeiro, ficando com a
                -- ocorrencia mais recente pela ordem fisica de carga.
                SELECT DISTINCT ON ({key_cols}) *
                FROM {tmp_table}
                ORDER BY {key_cols}, ctid DESC
            ) AS deduped
            ON CONFLICT ({key_cols}) DO UPDATE SET {set_clause}
        """))
    else:
        db.execute(text(f"INSERT INTO {spec.table} ({quoted_cols}) SELECT {quoted_cols} FROM {tmp_table}"))

    db.execute(text(f"DROP TABLE {tmp_table}"))
    db.commit()
    logger.info("Mesclado %s no destino em %.1fs", job.file, time.monotonic() - t0)

    _set_step(
        db, "import", period=job.period, group=job.group, current_file=job.file, status="running",
        processed_rows=count, message=f"{count} linhas",
    )
    return count


def _import_reference(db: Session, job: Job, display: ProgressDisplay, slot: int) -> int:
    columns, model, key = REFERENCE_SPECS[job.file]
    table = model.__table__
    count = 0

    csv_size = os.path.getsize(job.csv_path)
    bar = display.bar(slot, f"  importando {job.file}", total=csv_size, unit="bytes")

    def on_progress(bytes_read: int, _total: int, rows_read: int) -> None:
        bar.update(bytes_read, extra=f"{rows_read} linhas")

    for row in read_csv(job.csv_path, columns, on_progress=on_progress):
        if key == "receita_code":
            # Coluna Integer no banco (pro JOIN do build casar tipo com
            # estabelecimentos_staging.municipio_codigo sem CAST).
            code = (row.get("receita_code") or "").strip()
            if not code.isdigit():
                continue
            row["receita_code"] = int(code)

        stmt = pg_insert(table).values(**row)
        update_cols = {c: stmt.excluded[c] for c in row if c != key}
        stmt = stmt.on_conflict_do_update(index_elements=[key], set_=update_cols)
        db.execute(stmt)
        count += 1

    db.commit()
    bar.update(csv_size, extra=f"{count} linhas", force=True)
    _set_step(
        db, "import", period=job.period, group=job.group, current_file=job.file, status="running",
        processed_rows=count, message=f"{count} linhas",
    )
    logger.info("Importado %s: %d linhas", job.file, count)
    return count


# --------------------------------------------------------------------------
# Build da tabela final
# --------------------------------------------------------------------------

# Indices secundarios (nao essenciais pro ON CONFLICT, so pras queries da API)
# custam caro de manter linha a linha num INSERT de dezenas de milhoes de
# linhas -- sao criados depois do bulk load, onde um CREATE INDEX e ordens de
# magnitude mais rapido. Parciais: as consultas da API sao quase sempre sobre
# empresas ativas e/ou com contato, e indexar as ~41M linhas que ninguem le
# custaria vários GB.
DEFERRED_INDEXES = [
    ("ix_establishments_cellphone", "(cellphone)", "cellphone IS NOT NULL", None),
    ("ix_establishments_uf", "(uf)", "situacao_cadastral = 2", None),
    ("ix_establishments_main_cnae", "(main_cnae)", "situacao_cadastral = 2", None),
    ("ix_establishments_secondary_cnaes", "(secondary_cnaes)", "secondary_cnaes IS NOT NULL", "gin"),
    ("ix_establishments_situacao_cadastral", "(situacao_cadastral)", None, None),
]


def _build_final_table(db: Session, period: str, display: ProgressDisplay) -> None:
    slot = STEPS.index("import")
    _set_step(db, "build", period=period, group="build", status="running", message="montando establishments")

    db.execute(text("DROP TABLE IF EXISTS establishments_new"))
    # Sem INCLUDING INDEXES: os secundarios sao criados depois do bulk load
    # (ver DEFERRED_INDEXES). So a PK de cnpj entra antes, porque o ON CONFLICT
    # do INSERT abaixo precisa dela.
    db.execute(text("CREATE TABLE establishments_new (LIKE establishments INCLUDING DEFAULTS)"))
    db.execute(text("ALTER TABLE establishments_new ADD CONSTRAINT establishments_new_pkey PRIMARY KEY (cnpj)"))
    # Sem UPDATE depois do bulk load, entao nao ha por que reservar espaco
    # livre por pagina pra HOT update (LIKE nao copia storage parameters).
    db.execute(text("ALTER TABLE establishments_new SET (fillfactor = 100)"))
    db.commit()

    t0 = time.monotonic()
    display.set(slot, "  build: INSERT establishments_new ...")
    imported = db.execute(text("""
        INSERT INTO establishments_new
            (cnpj, phone, cellphone, main_cnae, municipio_id, opened_at, uf, company_size,
             situacao_cadastral, natureza_juridica, motivo_situacao_cadastral, cellphone_confidence,
             is_headquarters, is_mei, is_simples, secondary_cnaes, company_name, trade_name, email)
        SELECT
            e.cnpj,
            e.phone,
            e.cellphone,
            e.cnae_fiscal_principal,
            m.id,
            e.data_inicio_atividade,
            e.uf,
            emp.porte_empresa,
            e.situacao_cadastral,
            emp.natureza_juridica,
            e.motivo_situacao_cadastral,
            e.cellphone_confidence,
            e.is_headquarters,
            coalesce(s.opcao_mei, false),
            coalesce(s.opcao_simples, false),
            e.cnae_fiscal_secundaria,
            coalesce(emp.razao_social, e.nome_fantasia, ''),
            e.nome_fantasia,
            e.correio_eletronico
        FROM estabelecimentos_staging e
        LEFT JOIN empresas_staging emp ON emp.cnpj_basico = e.cnpj_basico
        LEFT JOIN simples_staging s ON s.cnpj_basico = e.cnpj_basico
        LEFT JOIN municipios m ON m.receita_code = e.municipio_codigo
        ON CONFLICT (cnpj) DO NOTHING
    """))
    db.commit()
    logger.info("establishments_new: %d linhas em %.1fs", imported.rowcount, time.monotonic() - t0)

    # `Municipios.zip` da Receita so tem codigo+nome, sem UF -- pega o UF de
    # qualquer estabelecimento daquele municipio (sao 1:1) enquanto o staging
    # ainda existe. Sem isso Municipio.uf fica sempre NULL, o filtro por UF de
    # /municipios/search devolve vazio e o import-ibge (que casa por nome+UF)
    # nao casa nada.
    _set_step(db, "build", period=period, group="build", status="running", message="preenchendo UF dos municípios")
    # `municipios.uf` continua em texto (e a sigla que a API expoe, e a tabela
    # tem ~5,5k linhas), enquanto o staging guarda o codigo numerico -- dai o
    # join contra a lista de codigos, montada do mapa de app/regions.py pra nao
    # duplicar a tabela de siglas em SQL.
    uf_values = ", ".join(f"({code}, '{sigla}')" for code, sigla in sorted(CODE_TO_UF.items()))
    db.execute(text(f"""
        UPDATE municipios m SET uf = codes.sigla
        FROM (SELECT DISTINCT ON (municipio_codigo) municipio_codigo, uf
              FROM estabelecimentos_staging
              WHERE municipio_codigo IS NOT NULL AND uf IS NOT NULL) v
        JOIN (VALUES {uf_values}) AS codes(code, sigla) ON codes.code = v.uf
        WHERE v.municipio_codigo = m.receita_code AND m.uf IS DISTINCT FROM codes.sigla
    """))
    db.commit()

    for name, cols, where, using in DEFERRED_INDEXES:
        _set_step(db, "build", period=period, group="build", status="running", message=f"índice {name}")
        display.set(slot, f"  build: criando índice {name}")
        using_sql = f" USING {using}" if using else ""
        where_sql = f" WHERE {where}" if where else ""
        db.execute(text(f'CREATE INDEX "{name}_new" ON establishments_new{using_sql} {cols}{where_sql}'))
        db.commit()

    display.set(slot, "  build: swap atômico")
    db.execute(text("ALTER TABLE establishments RENAME TO establishments_old"))
    db.execute(text("ALTER TABLE establishments_new RENAME TO establishments"))
    # So depois de dropar a tabela antiga (e os indices dela junto) pra liberar
    # os nomes canonicos sem colisao.
    db.execute(text("DROP TABLE establishments_old"))
    for name, _cols, _where, _using in DEFERRED_INDEXES:
        db.execute(text(f'ALTER INDEX "{name}_new" RENAME TO "{name}"'))
    db.execute(text("ALTER INDEX establishments_new_pkey RENAME TO establishments_pkey"))
    db.execute(text("TRUNCATE TABLE empresas_staging, simples_staging, estabelecimentos_staging"))
    db.execute(text("DELETE FROM import_log WHERE period = :period"), {"period": period})
    db.commit()

    display.set(slot, "")
    _set_step(
        db, "build", period=period, group="build", status="success",
        processed_rows=imported.rowcount, message="tabela final trocada atomicamente",
    )


# --------------------------------------------------------------------------
# Estado / progresso
# --------------------------------------------------------------------------


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


def _finish_steps(db: Session) -> None:
    """Fecha as linhas de estagio no fim do run -- sem isso `/import/status`
    mostraria os tres estagios como `running` pra sempre depois de terminar."""
    for step in STEPS:
        existing = db.get(ImportProgress, step)
        if existing and existing.status == "running":
            _set_step(db, step, status="success", current_file=None)


def _reset_steps(db: Session, period: str) -> None:
    """Zera as linhas de estagio no inicio de um run, pra /import/status nao
    mostrar o arquivo do run anterior como se fosse deste."""
    for step in (*STEPS, "build"):
        _set_step(db, step, period=period, status="idle", processed_rows=0,
                  group=None, current_file=None, total_bytes=None, message=None)


def _set_step(db: Session, step: str, **fields) -> None:
    now = datetime.now(timezone.utc)
    fields["updated_at"] = now
    if fields.get("status") == "running":
        existing = db.get(ImportProgress, step)
        if not existing or existing.status != "running":
            fields["started_at"] = now

    stmt = pg_insert(ImportProgress.__table__).values(step=step, **fields)
    stmt = stmt.on_conflict_do_update(index_elements=["step"], set_={c: stmt.excluded[c] for c in fields})
    db.execute(stmt)
    db.commit()


def _set_run(db: Session, **fields) -> None:
    message = fields.get("message")
    if message:
        logger.info("%s", message)

    now = datetime.now(timezone.utc)
    fields["updated_at"] = now
    if fields.get("status") == "running":
        existing = db.get(ImportRun, 1)
        if not existing or existing.status != "running":
            fields["started_at"] = now

    stmt = pg_insert(ImportRun.__table__).values(id=1, **fields)
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_={c: stmt.excluded[c] for c in fields})
    db.execute(stmt)
    db.commit()
