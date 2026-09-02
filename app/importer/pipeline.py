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
from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.cnpj import BRANCH_SPAN
from app.config import settings
from app.db import Base, SessionLocal
from app.importer import client
from app.importer import municipalities as municipalities_bootstrap
from app.importer.csv_reader import read_csv
from app import stats_rollup
from app.importer.progress import ProgressDisplay, human_bytes, log_through
from app.importer.rows import GROUP_SPECS, Counters
from app.models import (
    Cnae,
    CompanyStaging,
    Country,
    EstablishmentStaging,
    ImportFile,
    ImportRun,
    ImportStep,
    LegalNature,
    Municipality,
    Qualification,
    RegistrationStatusReason,
    SimplesStaging,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("importer")

GROUPS = ["reference", "simples", "companies", "establishments", "partners"]

# Prefixo do nome do arquivo NA RECEITA -- e o nome dela, nao o nosso, entao
# fica em portugues (`Municipios0.zip`, `Socios1.zip`). `files_for_group` casa
# por `startswith`, e um prefixo traduzido aqui nao casa com arquivo nenhum: o
# grupo simplesmente importaria zero arquivos, sem erro.
STEP_PREFIXES = {
    "reference": ["Cnaes", "Municipios", "Motivos", "Naturezas", "Paises", "Qualificacoes"],
    "simples": ["Simples"],
    "companies": ["Empresas"],
    "establishments": ["Estabelecimentos"],
    "partners": ["Socios"],
}

REFERENCE_SPECS = {
    "Cnaes.zip": (["code", "description"], Cnae, "code"),
    "Municipios.zip": (["receita_code", "name"], Municipality, "receita_code"),
    "Motivos.zip": (["code", "description"], RegistrationStatusReason, "code"),
    "Naturezas.zip": (["code", "description"], LegalNature, "code"),
    "Paises.zip": (["code", "description"], Country, "code"),
    "Qualificacoes.zip": (["code", "description"], Qualification, "code"),
}

MAX_ATTEMPTS_PER_STAGE = 3

# Quanto o CSV descompactado cresce em relacao ao zip -- so pra reservar disco
# antes de baixar, quando o tamanho real ainda nao e conhecido. Medido nos
# arquivos da Receita (texto ISO-8859-1 muito repetitivo): ~4-5x.
CSV_EXPANSION_FACTOR = 5

STEPS = ("download", "extract", "import")

DB_PROGRESS_INTERVAL = 1.0  # grava no banco no maximo 1x/s -- a barra na tela cobre o resto.


# As tres tabelas de staging, na ordem em que o build as le. Dropadas no fim
# do `import-all` (ver app.cli) e recriadas por `ensure_staging_tables` no
# inicio do proximo import: sao ~63M linhas de scratch UNLOGGED que nao
# servem pra nada depois do swap, e deixa-las pra tras so ocupava disco --
# elas nasciam na migration e ninguem nunca as removia.
STAGING_MODELS = (EstablishmentStaging, CompanyStaging, SimplesStaging)
STAGING_TABLES = tuple(model.__tablename__ for model in STAGING_MODELS)


def ensure_staging_tables(db: Session) -> None:
    """Cria as tabelas de staging que estiverem faltando.

    O import nao pode depender de elas terem sobrado do run anterior: o
    `import-all` as dropa no fim, de proposito. `create_all` com
    `checkfirst=True` (default) e no-op quando ja existem, e sai com o
    `UNLOGGED` e os indices que os models declaram -- e por isso que a DDL
    vive nos models e nao num CREATE TABLE escrito a mao aqui.
    """
    Base.metadata.create_all(db.get_bind(), tables=[m.__table__ for m in STAGING_MODELS])


def drop_staging_tables(db: Session) -> None:
    """Dropa as tabelas de staging. Chamado no fim do `import-all`, quando o
    swap ja aconteceu e o conteudo delas nao serve mais pra nada."""
    db.execute(text(f"DROP TABLE IF EXISTS {', '.join(STAGING_TABLES)}"))
    db.commit()


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
        self._progress_sessions: dict[int, Session] = {}
        self._sessions_lock = threading.Lock()
        self.imported_rows = 0  # total carregado no run, logado no fim

    # -- sessoes ----------------------------------------------------------

    def session(self) -> Session:
        """Sessao de dados da thread atual (COPY, UPSERT, build)."""
        return self._session_from(self._sessions)

    def progress_session(self) -> Session:
        """Sessao SO pra escrever progresso, separada da de dados.

        Duas razoes. A primeira e correcao: gravar progresso na sessao de dados
        faz um commit no meio do carregamento, e um commit devolve a conexao ao
        pool -- com tres threads disputando o mesmo pool, o proximo uso pode
        pegar OUTRA conexao, e a tabela TEMP do COPY (que e por conexao) some
        no meio do caminho. A segunda e semantica: progresso e escrituracao,
        nao deve ser desfeito junto com um rollback dos dados.
        """
        return self._session_from(self._progress_sessions)

    def _session_from(self, pool: dict[int, Session]) -> Session:
        key = threading.get_ident()
        with self._sessions_lock:
            db = pool.get(key)
            if db is None:
                db = SessionLocal()
                pool[key] = db
            return db

    def close_sessions(self) -> None:
        with self._sessions_lock:
            for pool in (self._sessions, self._progress_sessions):
                for db in pool.values():
                    db.close()
                pool.clear()

    # -- execucao ---------------------------------------------------------

    def run(self) -> None:
        # Todo o logging sai pelo display enquanto o import roda -- ver
        # ProgressDisplay.log.
        with log_through(self.display):
            self._run()

    def _run(self) -> None:
        main_db = self.session()
        logger.info("Orçamento de disco: %s", human_bytes(self.budget.total))

        progress = self.progress_session()
        _set_run(progress, period=self.period, status="running", message="iniciando")
        _reset_steps(progress, self.period)

        # O `import-all` dropa o staging no fim (ver app.cli.import_all), entao
        # ele pode simplesmente nao existir aqui -- inclusive num `import-cnpj`
        # avulso, muito depois do ultimo import-all.
        ensure_staging_tables(main_db)

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
        _finish_steps(self.progress_session())

        if self.error:
            raise self.error

        # Independente de `run_build` (que so governa o build de
        # establishments): o swap de partners acontece sempre que o grupo
        # "partners" fez parte deste run, nao so quando "build" foi pedido
        # explicitamente -- partners nao depende de establishments em nada.
        # `_finalize_partners` e um no-op seguro se partners_new nao existir
        # (grupo nao fez parte deste `--only`).
        _finalize_partners(main_db, progress, self.display)

        if self.run_build:
            _build_final_table(main_db, progress, self.period, self.display)

        _set_run(progress, period=self.period, status="success", message="importação concluída")

    def _plan(self, db: Session) -> list[Job]:
        all_files = client.list_files(self.period)
        jobs: list[Job] = []
        period_dir = os.path.join(settings.download_dir, self.period)
        os.makedirs(period_dir, exist_ok=True)

        for group in self.groups:
            group_files = files_for_group(all_files, group)
            pending = [f for f in group_files if not _already_imported(db, self.period, f)]
            logger.info("Grupo %s: %d arquivo(s), %d pendente(s)", group, len(group_files), len(pending))

            if group == "partners" and group_files and len(pending) == len(group_files):
                # partners nao tem merge entre arquivos (cada Socios<N>.zip
                # cobre uma faixa disjunta de cnpj_root) -- so carregar. Mas
                # carrega numa tabela-SOMBRA (partners_new), nao na `partners` ao
                # vivo: truncar a tabela viva deixaria /partners/* respondendo
                # vazio pelos minutos que o import leva, e cada INSERT direto
                # nela pagaria WAL linha a linha (ela e LOGGED, a API depende
                # de durabilidade). partners_new e UNLOGGED ate o swap no fim
                # (ver _finalize_partners) -- mesmo padrao de establishments_new.
                #
                # So cria a sombra numa run NOVA (nenhum arquivo do grupo
                # ainda marcado em ImportFile); numa RETOMADA, partners_new ja
                # existe com o que foi carregado antes de parar.
                _create_partners_shadow(db)
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
        # zero em caso de falha -- ver ImportFile), entao esperar o WAL
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
        db = self.progress_session()
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
        db = self.progress_session()
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
        progress = self.progress_session()
        if job.group == "reference":
            job.rows = _import_reference(db, progress, job, self.display, slot)
        else:
            job.rows = _import_group(db, progress, job, self.display, slot)


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
        pipeline.session().rollback()  # senao a sessao fica com a transacao abortada
        _set_run(pipeline.progress_session(), status="failed", message=str(exc)[:255])
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


def _import_group(db: Session, progress: Session, job: Job, display: ProgressDisplay, slot: int) -> int:
    spec = GROUP_SPECS[job.group]
    quoted_cols = ", ".join(f'"{c}"' for c in spec.columns)

    # COPY (protocolo nativo do Postgres) pra uma tabela temporaria com o
    # MESMO tipo do destino, depois UM upsert em massa -- ao invés de milhares
    # de INSERT ... ON CONFLICT parametrizados. Testado: e a diferenca entre
    # minutos e horas num arquivo de dezenas de milhoes de linhas. Como a temp
    # ja tem os tipos finais, o COPY faz o parse em C e o INSERT ... SELECT
    # abaixo nao precisa de um unico CAST.
    # Tudo daqui ate o commit final roda numa transacao SO. Nao e detalhe: a
    # tabela TEMP pertence a CONEXAO, e um commit no meio devolveria a conexao
    # ao pool -- com os tres estagios disputando o mesmo pool, o passo seguinte
    # poderia pegar outra conexao e a temp sumiria (visto na pratica, no COPY
    # de 50M linhas do Simples.zip: "relation tmp_staging_load does not exist").
    # Por isso tambem o progresso vai por `progress`, uma sessao separada.
    tmp_table = "tmp_staging_load"
    db.execute(text(f"DROP TABLE IF EXISTS {tmp_table}"))
    # `AS SELECT ... WITH NO DATA` e nao `LIKE`: da exatamente as colunas que o
    # COPY manda, com os tipos do destino, e sem constraint nenhuma. Um `LIKE`
    # traria tambem as colunas que nao carregamos -- inclusive o `id` de
    # partners, que vem NOT NULL mas sem o default da sequence (LIKE nao copia
    # default), e o COPY morria com NotNullViolation.
    # ON COMMIT DROP: some sozinha no commit e no rollback, entao uma tentativa
    # que falhou no meio nao deixa lixo pra proxima.
    db.execute(text(
        f"CREATE TEMP TABLE {tmp_table} ON COMMIT DROP AS "
        f"SELECT {quoted_cols} FROM {spec.table} WITH NO DATA"
    ))

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
        malformed_logged = 0
        for row in read_csv(job.csv_path, spec.csv_columns, on_progress=on_progress):
            try:
                values = spec.transform(row, job.counters)
            except ValueError as exc:
                # Uma linha que nao da pra montar (CNPJ com tamanho errado
                # mesmo depois de limpo, por exemplo) nao pode derrubar o
                # arquivo inteiro -- um COPY de dezenas de milhoes de linhas
                # cancela e reinicia do zero (3x) por causa de UMA linha ruim
                # da fonte (visto na pratica). Conta, loga só as primeiras
                # (evita flood se o problema for sistemico) e segue o arquivo.
                job.counters.malformed += 1
                malformed_logged += 1
                if malformed_logged <= 5:
                    logger.warning("%s: linha descartada (%s): %r", job.file, exc, row)
                continue
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
        progress, "import", period=job.period, group=job.group, current_file=job.file, status="running",
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
                -- ocorrencia mais recente pela branch fisica de carga.
                SELECT DISTINCT ON ({key_cols}) *
                FROM {tmp_table}
                ORDER BY {key_cols}, ctid DESC
            ) AS deduped
            ON CONFLICT ({key_cols}) DO UPDATE SET {set_clause}
        """))
    else:
        db.execute(text(f"INSERT INTO {spec.table} ({quoted_cols}) SELECT {quoted_cols} FROM {tmp_table}"))

    db.commit()  # a temp cai junto (ON COMMIT DROP)
    logger.info("Mesclado %s no destino em %.1fs", job.file, time.monotonic() - t0)

    _set_step(
        progress, "import", period=job.period, group=job.group, current_file=job.file, status="running",
        processed_rows=count, message=f"{count} linhas",
    )
    return count


def _import_reference(db: Session, progress: Session, job: Job, display: ProgressDisplay, slot: int) -> int:
    columns, model, key = REFERENCE_SPECS[job.file]

    csv_size = os.path.getsize(job.csv_path)
    bar = display.bar(slot, f"  importando {job.file}", total=csv_size, unit="bytes")

    def on_progress(bytes_read: int, _total: int, rows_read: int) -> None:
        bar.update(bytes_read, extra=f"{rows_read} linhas")

    rows = read_csv(job.csv_path, columns, on_progress=on_progress)
    if job.file == "Municipios.zip":
        count = _merge_municipalities_receita(db, rows)
    else:
        count = _upsert_reference_rows(db, model.__table__, key, rows)

    bar.update(csv_size, extra=f"{count} linhas", force=True)
    _set_step(
        progress, "import", period=job.period, group=job.group, current_file=job.file, status="running",
        processed_rows=count, message=f"{count} linhas",
    )
    logger.info("Importado %s: %d linhas", job.file, count)
    return count


def _upsert_reference_rows(db: Session, table, key: str, rows) -> int:
    count = 0
    for row in rows:
        stmt = pg_insert(table).values(**row)
        update_cols = {c: stmt.excluded[c] for c in row if c != key}
        stmt = stmt.on_conflict_do_update(index_elements=[key], set_=update_cols)
        db.execute(stmt)
        count += 1
    db.commit()
    return count


def _merge_municipalities_receita(db: Session, rows) -> int:
    """`Municipios.zip` da Receita so traz codigo+nome -- sem UF, sem codigo
    IBGE. Ate aqui, `municipalities` ja foi bootstrapada pela API do IBGE (ver
    app.importer.municipalities.import_municipalities, que roda ANTES de tudo), com
    ibge_code/uf exatos. Este passo so precisa achar, pra cada linha da
    Receita, a linha correspondente ja existente -- por nome normalizado,
    unica chave em comum entre as duas fontes -- e completar `receita_code`
    nela.

    Nome duplicado entre estados (a Receita nao manda UF pra desempatar) e o
    unico jeito de isso falhar: nesse caso a linha nova entra so com
    receita_code+nome, sem ibge_code/uf, igual ao caso de "nome sem
    correspondencia" -- ambos ficam pra tras do FK com `postal_codes` ate
    alguem resolver a ambiguidade a mao, mas nao travam o import.
    """
    existing = db.query(Municipality.id, Municipality.name).all()
    by_name: dict[str, list[int]] = {}
    for municipality_id, name in existing:
        by_name.setdefault(municipalities_bootstrap.normalize_name(name), []).append(municipality_id)

    count = matched = ambiguous = unmatched = 0
    for row in rows:
        code = (row.get("receita_code") or "").strip()
        if not code.isdigit():
            continue
        receita_code = int(code)
        name = (row.get("name") or "").strip()
        count += 1

        candidates = by_name.get(municipalities_bootstrap.normalize_name(name), [])
        if len(candidates) == 1:
            db.execute(
                update(Municipality.__table__)
                .where(Municipality.id == candidates[0])
                .values(receita_code=receita_code, name=name)
            )
            matched += 1
        else:
            if len(candidates) > 1:
                ambiguous += 1
            else:
                unmatched += 1
            stmt = pg_insert(Municipality.__table__).values(receita_code=receita_code, name=name)
            stmt = stmt.on_conflict_do_update(index_elements=["receita_code"], set_={"name": stmt.excluded.name})
            db.execute(stmt)

    db.commit()
    logger.info(
        "Municípios (Receita): %d casados com o IBGE por name, %d name ambíguo (repete em >1 UF), "
        "%d sem correspondência -- esses %d ficam sem ibge_code/uf até alguém resolver a mão.",
        matched, ambiguous, unmatched, ambiguous + unmatched,
    )
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
# NAO usar predicado parcial em `registration_status = 2` aqui de novo. Custou
# caro: os indices de uf/main_cnae eram parciais assim, mas a busca so filtra
# situacao quando o cliente pede (`?status=`), e o default e "todas as
# situacoes". Sem `registration_status = 2` no WHERE o Postgres nao consegue
# provar o predicado e descarta o indice inteiro -- `?uf=RR` (a menor UF do
# pais) ia a seq scan e batia timeout, enquanto `?uf=RR&status=ativa`
# respondia em 0,35s. Indice parcial so vale quando o predicado dele esta
# SEMPRE no WHERE da query, o que aqui nao acontece.
DEFERRED_INDEXES = [
    # So o indice da FK de `cep`. `establishments` deixou de ser ponto de
    # ENTRADA de busca: a rota entra por `establishment_cnaes` (cidade + CNAE,
    # ja em ordem de `cnpj`) e chega aqui pela PK, uma linha por resultado da
    # pagina -- ver models.Establishment. Os indices de `uf`, `main_cnae`,
    # `registration_status` e os dois GIN de trigrama sairam junto com os
    # filtros que os usavam (migration b2d5f8a91c04): sao ~72M linhas
    # reconstruidas a cada import, e indice que ninguem le e so escrita cara.
    ("ix_establishments_cep", "(cep)", "cep IS NOT NULL", None),
]

# Indice da relacao N:N empresa-CNAE (ver models.EstablishmentCnae). Mesma
# logica de adiar: a tabela e maior que `establishments` (uma linha por CNAE da
# empresa, nao por empresa) e cria o indice depois do bulk load.
CNAES_DEFERRED_INDEXES = [
    # UM indice, e ele e a busca inteira: `municipality_id` lider porque toda
    # busca entra por lugar (cidade `= id`, ou estado -> `IN (as cidades da
    # UF)`), `cnae` como segundo predicado de igualdade, `cnpj` no fim pra
    # saida sair ordenada dentro de cada cidade.
    ("ix_establishment_cnaes_municipality_cnae_cnpj", "(municipality_id, cnae, cnpj)", None, None),
]

PARTNERS_DEFERRED_INDEXES = [
    ("ix_partners_cnpj_root", "(cnpj_root)", None, None),
    ("ix_partners_partner_tax_id", "(partner_tax_id)", "partner_tax_id IS NOT NULL", None),
    # `?name=` e ILIKE '%x%' -- ver o comentario em DEFERRED_INDEXES.
    ("ix_partners_partner_name_trgm", "(partner_name gin_trgm_ops)", None, "gin"),
]


def _create_partners_shadow(db: Session) -> None:
    """Cria `partners_new` -- UNLOGGED, sem indices secundarios (adiados pro
    fim, ver `_finalize_partners`) -- pra receber o INSERT direto de cada
    Socios<N>.zip (ver GROUP_SPECS['partners'].table)."""
    db.execute(text("DROP TABLE IF EXISTS partners_new"))
    db.execute(text("CREATE UNLOGGED TABLE partners_new (LIKE partners INCLUDING DEFAULTS)"))
    # LIKE...INCLUDING DEFAULTS copia o DEFAULT nextval(...) literal -- ficaria
    # preso a sequence da tabela ANTIGA, e o DROP TABLE partners_old do swap
    # (que apaga a sequence dona de partners_old.id) quebraria o default de
    # partners_new. Sequence propria antes de qualquer INSERT usar o default.
    db.execute(text("CREATE SEQUENCE IF NOT EXISTS partners_new_id_seq OWNED BY partners_new.id"))
    db.execute(text("ALTER TABLE partners_new ALTER COLUMN id SET DEFAULT nextval('partners_new_id_seq')"))
    db.execute(text("ALTER TABLE partners_new ADD CONSTRAINT partners_new_pkey PRIMARY KEY (id)"))


def _finalize_partners(db: Session, progress: Session, display: ProgressDisplay) -> None:
    """Indices + SET LOGGED + swap atomico de partners_new -> partners.

    No-op se `partners_new` nao existir -- acontece quando o grupo "partners" nao
    fez parte deste run (`--only` sem "partners").
    """
    slot = STEPS.index("import")
    if db.execute(text("SELECT to_regclass('partners_new')")).scalar() is None:
        return

    _set_step(progress, "build", group="build", status="running", message="índices de partners")
    for name, cols, where, using in PARTNERS_DEFERRED_INDEXES:
        display.set(slot, f"  build: criando índice {name}")
        using_sql = f" USING {using}" if using else ""
        where_sql = f" WHERE {where}" if where else ""
        db.execute(text(f'CREATE INDEX "{name}_new" ON partners_new{using_sql} {cols}{where_sql}'))
        db.commit()

    # Mesmo motivo do establishments_new: grava a tabela inteira no WAL de uma
    # vez, em vez de a cada linha/indice durante o bulk load.
    _set_step(progress, "build", group="build", status="running", message="tornando partners durável (SET LOGGED)")
    display.set(slot, "  build: SET LOGGED em partners_new")
    db.execute(text("ALTER TABLE partners_new SET LOGGED"))
    db.commit()

    display.set(slot, "  build: swap atômico de partners")
    db.execute(text("ALTER TABLE partners RENAME TO partners_old"))
    db.execute(text("ALTER TABLE partners_new RENAME TO partners"))
    # So depois de dropar a tabela antiga (e a sequence dona de partners_old.id
    # junto) pra liberar os nomes canonicos sem colisao.
    db.execute(text("DROP TABLE partners_old"))
    for name, _cols, _where, _using in PARTNERS_DEFERRED_INDEXES:
        db.execute(text(f'ALTER INDEX "{name}_new" RENAME TO "{name}"'))
    db.execute(text("ALTER INDEX partners_new_pkey RENAME TO partners_pkey"))
    db.execute(text("ALTER SEQUENCE partners_new_id_seq RENAME TO partners_id_seq"))
    db.commit()

    # Mesmo motivo do ANALYZE em establishments: o swap deixa a tabela sem
    # estatistica pro planner.
    display.set(slot, "  build: ANALYZE em partners")
    db.execute(text("ANALYZE partners"))
    db.commit()

    _set_step(progress, "build", group="build", status="success", message="partners trocado atomicamente")


def _build_cnaes_table(db: Session, display: ProgressDisplay, slot: int) -> None:
    """Monta `establishment_cnaes_new` -- uma linha por (empresa, CNAE).

    Sai do staging, nao de `establishments_new`: o staging tem quase tudo que a
    tabela precisa (cnpj, principal, array de secundarios, uf, cellphone) e a
    relacao e 1:1 com o que acabou de ser inserido. A unica excecao e
    `municipality_id`, que so existe depois do join com `municipalities` -- o
    mesmo que `_build_final_table` faz, repetido aqui pelo mesmo codigo.

    O LATERAL abre a empresa em uma linha por CNAE: o principal, mais os
    secundarios. `array_remove` tira o principal da lista de secundarios --
    a Receita as vezes repete, e sem isso a PK (cnpj, cnae) quebraria no fim;
    `DISTINCT` cobre repeticao dentro do proprio array. `coalesce` porque o
    array e NULL (nao vazio) quando nao ha secundario nenhum, e `main_cnae`
    NULL cai no WHERE.

    Sem `uf` nem `is_main`. A UF sai de `municipalities` no join da busca (a
    tabela tem ~5.570 linhas -- e um hash), e "e secundario?" e `cnae <>
    establishments.main_cnae`, que a serializacao ja resolve. Guardar as duas
    aqui era copia sem leitor depois que a busca passou a entrar por
    `municipality_id` -- ver models.EstablishmentCnae.

    UNLOGGED e sem indice nenhum durante o load, igual `establishments_new`:
    PK e indices vem depois, em `_finalize_cnaes`.
    """
    display.set(slot, "  build: INSERT establishment_cnaes_new ...")
    t0 = time.monotonic()
    db.execute(text("DROP TABLE IF EXISTS establishment_cnaes_new"))
    db.execute(text("""
        CREATE UNLOGGED TABLE establishment_cnaes_new (
            cnpj bigint NOT NULL,
            cnae integer NOT NULL,
            municipality_id integer,
            has_cellphone boolean NOT NULL
        )
    """))
    db.execute(text("ALTER TABLE establishment_cnaes_new SET (fillfactor = 100)"))
    db.commit()

    inserted = db.execute(text("""
        INSERT INTO establishment_cnaes_new
            (cnpj, cnae, municipality_id, has_cellphone)
        SELECT s.cnpj, c.cnae, m.id, s.cellphone IS NOT NULL
        FROM establishments_staging s
        -- Mesmo join de `_build_final_table`: o staging so tem o codigo da
        -- Receita, e a copia aqui precisa ser o MESMO `municipality_id` que
        -- foi pra `establishments`, senao o filtro por cidade divergiria entre
        -- as duas tabelas. `municipalities` tem ~5.570 linhas -- hash join.
        LEFT JOIN municipalities m ON m.receita_code = s.municipality_code
        CROSS JOIN LATERAL (
            SELECT s.main_cnae AS cnae
            UNION ALL
            SELECT DISTINCT unnest(array_remove(
                coalesce(s.secondary_cnaes, '{}'::integer[]),
                s.main_cnae
            ))
        ) c
        WHERE c.cnae IS NOT NULL
    """))
    db.commit()
    rows = inserted.rowcount

    # `municipality_id` e a coluna LIDER do unico indice da tabela, e a busca
    # entra por ela com INNER JOIN em `municipalities` -- se ela vier NULL, a
    # rota devolve zero pra tudo, com a tabela cheia e sem erro nenhum. Ja
    # aconteceu (a coluna entrou por migration e ficou NULL ate um import
    # completo rodar), e custou horas justamente por ser calado. Uma varredura
    # aqui, na tabela recem-escrita e ainda quente, e barata perto disso.
    orphans = db.execute(text(
        "SELECT count(*) FROM establishment_cnaes_new WHERE municipality_id IS NULL"
    )).scalar_one()
    if rows and orphans == rows:
        raise RuntimeError(
            "establishment_cnaes_new ficou com municipality_id NULL em TODAS as "
            f"{rows} linhas -- o join com `municipalities` nao casou nada. Sem essa "
            "coluna a busca devolve zero pra qualquer filtro. Provavel causa: "
            "`municipalities.receita_code` vazio (a fase 1 do import-all, "
            "`import-municipalities`, e o Municipios.zip da Receita precisam ter "
            "rodado antes desta)."
        )

    logger.info(
        "establishment_cnaes_new: %d linhas em %.1fs (%d sem cidade).",
        rows, time.monotonic() - t0, orphans,
    )


def rebuild_cnae_municipalities(db: Session) -> tuple[int, int]:
    """Preenche `establishment_cnaes.municipality_id` a partir de
    `establishments`, SEM reimportar nada. Devolve (linhas, linhas sem cidade).

    Existe por um caso concreto: `municipality_id` entrou na tabela N:N por
    migration, ou seja NULL em toda linha ja gravada, e quem preenche e o
    `_build_cnaes_table` -- que so roda num `import-all` completo. Enquanto ele
    nao rodava, a busca (que hoje entra por essa coluna, com INNER JOIN em
    `municipalities`) devolvia zero pra tudo. Isso reconstroi so a tabela N:N a
    partir do que ja esta no banco: `establishments.municipality_id` ja esta
    preenchido, e a relacao e 1:1 por `cnpj`.

    Reconstroi + troca em vez de dar UPDATE. Um UPDATE em ~150M linhas reescreve
    todas elas (MVCC), dobra a tabela em bloat, e joga o equivalente a tabela
    inteira no WAL -- e essa base ja mostrou o que checkpoint sob carga faz aqui.
    Um INSERT ... SELECT em tabela UNLOGGED nova, indice depois e RENAME atomico
    no fim e o mesmo caminho que o import ja usa, e nao deixa bloat.

    Os CNAEs em si (que sao o dado que nao da pra rederivar sem o arquivo da
    Receita) saem da propria tabela; so `municipality_id` e `has_cellphone` sao
    recalculados do lado de `establishments`, que e a fonte deles.

    LEFT JOIN de proposito: uma linha cuja empresa nao esta em `establishments`
    continua existindo, com a cidade NULL -- ela ja era invisivel pra busca, e
    sumir com ela calada seria pior que mante-la. O retorno diz quantas sao.
    """
    missing_column = db.execute(text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'establishment_cnaes' AND column_name = 'uf'
    """)).first()
    if missing_column:
        raise RuntimeError(
            "`establishment_cnaes` ainda tem a coluna `uf`: rode `alembic upgrade head` "
            "antes deste comando, senao o swap deixaria a tabela num formato que a "
            "migration seguinte nao consegue aplicar."
        )

    t0 = time.monotonic()
    logger.info("Reconstruindo establishment_cnaes com a cidade de establishments...")

    db.execute(text("DROP TABLE IF EXISTS establishment_cnaes_new"))
    db.execute(text("""
        CREATE UNLOGGED TABLE establishment_cnaes_new (
            cnpj bigint NOT NULL,
            cnae integer NOT NULL,
            municipality_id integer,
            has_cellphone boolean NOT NULL
        )
    """))
    db.execute(text("ALTER TABLE establishment_cnaes_new SET (fillfactor = 100)"))
    db.commit()

    inserted = db.execute(text("""
        INSERT INTO establishment_cnaes_new (cnpj, cnae, municipality_id, has_cellphone)
        SELECT ec.cnpj, ec.cnae, e.municipality_id,
               coalesce(e.cellphone IS NOT NULL, false)
        FROM establishment_cnaes ec
        LEFT JOIN establishments e ON e.cnpj = ec.cnpj
    """))
    db.commit()
    rows = inserted.rowcount
    logger.info("  %d linhas copiadas em %.1fs", rows, time.monotonic() - t0)

    logger.info("  criando PK e indice (isso e a maior parte do tempo)...")
    db.execute(text(
        "ALTER TABLE establishment_cnaes_new "
        "ADD CONSTRAINT establishment_cnaes_new_pkey PRIMARY KEY (cnpj, cnae)"
    ))
    db.commit()
    for name, cols, where, using in CNAES_DEFERRED_INDEXES:
        using_sql = f" USING {using}" if using else ""
        where_sql = f" WHERE {where}" if where else ""
        db.execute(text(
            f'CREATE INDEX "{name}_new" ON establishment_cnaes_new{using_sql} {cols}{where_sql}'
        ))
        db.commit()

    logger.info("  SET LOGGED (grava no WAL)...")
    db.execute(text("ALTER TABLE establishment_cnaes_new SET LOGGED"))
    db.commit()

    orphans = db.execute(text(
        "SELECT count(*) FROM establishment_cnaes_new WHERE municipality_id IS NULL"
    )).scalar_one()

    # Swap atomico, o mesmo de _build_final_table: a tabela antiga sai e a nova
    # entra na mesma transacao, entao nenhuma request ve as duas nem nenhuma.
    logger.info("  swap atômico...")
    db.execute(text("DROP TABLE IF EXISTS establishment_cnaes_old"))
    db.execute(text("ALTER TABLE establishment_cnaes RENAME TO establishment_cnaes_old"))
    db.execute(text("ALTER TABLE establishment_cnaes_new RENAME TO establishment_cnaes"))
    # A tabela antiga sai ANTES dos ALTER INDEX -- os indices dela ainda seguram
    # os nomes canonicos, e renomear por cima colidiria. Mesma ordem de
    # _build_final_table, pelo mesmo motivo.
    db.execute(text("DROP TABLE establishment_cnaes_old"))
    db.execute(text("ALTER INDEX establishment_cnaes_new_pkey RENAME TO establishment_cnaes_pkey"))
    for name, _cols, _where, _using in CNAES_DEFERRED_INDEXES:
        db.execute(text(f'ALTER INDEX "{name}_new" RENAME TO "{name}"'))
    db.commit()

    # Sem estatistica o planner nao sabe nada da tabela recem-trocada, e a
    # primeira busca pagaria um plano ruim.
    logger.info("  ANALYZE...")
    db.execute(text("ANALYZE establishment_cnaes"))
    db.commit()

    logger.info(
        "establishment_cnaes reconstruida: %d linhas em %.1fs (%d sem cidade).",
        rows, time.monotonic() - t0, orphans,
    )
    return rows, orphans


def _build_final_table(db: Session, progress: Session, period: str, display: ProgressDisplay) -> None:
    slot = STEPS.index("import")
    _set_step(progress, "build", period=period, group="build", status="running", message="montando establishments")

    # So nesta sessao, nao global -- o default do Postgres (4MB/64MB) e feito
    # pra muitas conexoes pequenas concorrentes (a API), nao pro hash join de
    # varias tabelas de dezenas de milhoes de linhas nem pro CREATE INDEX dos
    # indices adiados que rodam logo abaixo. Sem isso, cada um dos ate 3 hash
    # joins grandes do INSERT corre risco de derramar em disco (temp files),
    # que e ordens de magnitude mais lento que ficar em RAM.
    db.execute(text(f"SET work_mem = '{settings.build_work_mem_mb}MB'"))
    db.execute(text(f"SET maintenance_work_mem = '{settings.build_maintenance_work_mem_mb}MB'"))

    db.execute(text("DROP TABLE IF EXISTS establishments_new"))
    # UNLOGGED aqui, LOGGED soh antes do swap (ver mais abaixo): o INSERT de
    # dezenas de milhoes de linhas e a criacao dos indices deferidos, feitos
    # numa tabela LOGGED comum, geram WAL completo pra cada linha e cada
    # entrada de indice -- o unico trecho pesado do pipeline que ainda pagava
    # esse custo, quando o staging inteiro ja evita WAL de proposito (ver
    # `_staging()`). UNLOGGED elimina isso durante o bulk load; `SET LOGGED`
    # no fim grava tudo de uma vez, como uma operacao so, em vez de
    # incrementalmente a cada INSERT/CREATE INDEX.
    #
    # Sem INCLUDING INDEXES: os secundarios sao criados depois do bulk load
    # (ver DEFERRED_INDEXES). So a PK de cnpj entra antes, porque o ON CONFLICT
    # do INSERT abaixo precisa dela.
    db.execute(text("CREATE UNLOGGED TABLE establishments_new (LIKE establishments INCLUDING DEFAULTS)"))
    db.execute(text("ALTER TABLE establishments_new ADD CONSTRAINT establishments_new_pkey PRIMARY KEY (cnpj)"))
    # Sem UPDATE depois do bulk load, entao nao ha por que reservar espaco
    # livre por pagina pra HOT update (LIKE nao copia storage parameters).
    db.execute(text("ALTER TABLE establishments_new SET (fillfactor = 100)"))
    db.commit()

    # As tabelas de staging chegam aqui recem-carregadas via COPY, e o
    # Postgres so atualiza as estatisticas do planner (pg_class.reltuples)
    # atraves de VACUUM/ANALYZE -- nunca automaticamente so por causa do
    # volume inserido. Sem isso, o JOIN logo abaixo (potencialmente dezenas de
    # milhoes de linhas de cada lado) pode ser planejado com estatisticas
    # zeradas/desatualizadas e sair como nested loop em vez de hash join --
    # a diferenca entre minutos e nunca terminar. O autovacuum eventualmente
    # faria isso sozinho, mas nao ha garantia de que rode a tempo desse INSERT
    # que roda logo em seguida do bulk load.
    _set_step(progress, "build", period=period, group="build", status="running", message="analisando staging")
    display.set(slot, "  build: ANALYZE nas tabelas de staging")
    for table in ("establishments_staging", "companies_staging", "simples_staging"):
        db.execute(text(f"ANALYZE {table}"))
    db.commit()

    t0 = time.monotonic()
    display.set(slot, "  build: INSERT establishments_new ...")
    # A tabela existe sempre (e um model nosso), mas fica VAZIA ate
    # `import-ceps` rodar -- e ai nenhum estabelecimento casaria, o que jogaria
    # as ~63M linhas pro JSON de excecao. Por isso o teste e "tem CEP", nao
    # "tem tabela".
    has_ceps = db.execute(text("SELECT EXISTS (SELECT 1 FROM postal_codes)")).scalar()
    if has_ceps:
        logger.info("Vinculando endereços a postal_codes")
        # Os dois lados sao INTEGER agora -- join direto, sem cast nenhum.
        cep_join = "LEFT JOIN postal_codes c ON c.cep = e.cep"
        # Ou o estabelecimento esta vinculado a um CEP -- e ai o endereco vive
        # nas colunas, com logradouro/bairro vindo do join -- ou nao esta, e ai
        # o registro bruto da Receita vai inteiro pro JSON. Nunca os dois.
        #
        # "Nao vinculado" cobre os dois casos: CEP ausente no arquivo da
        # Receita, e CEP que nao existe na base dos Correios (digitado errado,
        # extinto, endereco no exterior). Guardar o CEP orfao na coluna nao
        # serviria pra nada -- nao resolve endereco nenhum -- e impediria uma
        # FOREIGN KEY pra postal_codes.
        linked = "c.cep IS NOT NULL"
        cep_sql = f"CASE WHEN {linked} THEN e.cep END"
        street_sql = f"CASE WHEN {linked} AND c.street IS NULL THEN e.street END"
        district_sql = f"CASE WHEN {linked} AND c.district IS NULL THEN e.district END"
        number_sql = f"CASE WHEN {linked} THEN e.number END"
        complement_sql = f"CASE WHEN {linked} THEN e.complement END"
        # `strip_nulls` pra nao gravar chave com null: sao poucas linhas, mas
        # um objeto so com o que existe e menor e mais facil de ler.
        address_sql = f"""
            CASE WHEN NOT ({linked}) AND (
                e.cep IS NOT NULL OR e.street IS NOT NULL OR e.number IS NOT NULL
                OR e.complement IS NOT NULL OR e.district IS NOT NULL
            ) THEN jsonb_strip_nulls(jsonb_build_object(
                'cep', lpad(e.cep::text, 8, '0'),
                'street', e.street,
                'number', e.number,
                'complement', e.complement,
                'district', e.district
            )) END
        """
    else:
        logger.warning(
            "Base de CEP vazia -- rode `import-ceps` antes pra vincular os endereços "
            "(sem ela, street/district ficam duplicados e não há FK de cep)."
        )
        # Sem a tabela de CEP nao da pra saber quem casa: mantem tudo nas
        # colunas, como veio da Receita. Jogar as ~63M linhas no JSON seria o
        # oposto do que essa coluna existe pra fazer.
        cep_join = ""
        cep_sql = "e.cep"
        street_sql = "e.street"
        district_sql = "e.district"
        number_sql = "e.number"
        complement_sql = "e.complement"
        address_sql = "NULL::jsonb"

    imported = db.execute(text(f"""
        INSERT INTO establishments_new
            (cnpj, phone, cellphone, main_cnae, municipality_id, cep, opened_at, uf, company_size,
             registration_status, legal_nature, registration_status_reason, cellphone_confidence,
             is_headquarters, is_mei, is_simples, company_name, trade_name, email,
             address_number, address_complement, street, district, address)
        SELECT
            e.cnpj,
            e.phone,
            e.cellphone,
            e.main_cnae,
            m.id,
            {cep_sql},
            e.activity_start_date,
            e.uf,
            emp.company_size,
            e.registration_status,
            emp.legal_nature,
            e.registration_status_reason,
            e.cellphone_confidence,
            e.is_headquarters,
            coalesce(s.mei_option, false),
            coalesce(s.simples_option, false),
            coalesce(emp.company_name, e.trade_name, ''),
            e.trade_name,
            e.email,
            {number_sql},
            {complement_sql},
            {street_sql},
            {district_sql},
            {address_sql}
        FROM establishments_staging e
        -- `e.cnpj / {BRANCH_SPAN}` e a raiz do CNPJ: em base 36, tirar as 4
        -- ultimas posicoes e uma divisao inteira. Assim o staging nao precisa
        -- guardar uma coluna `cnpj_root` repetindo 8 bytes por linha.
        LEFT JOIN companies_staging emp ON emp.cnpj_root = e.cnpj / {BRANCH_SPAN}
        LEFT JOIN simples_staging s ON s.cnpj_root = e.cnpj / {BRANCH_SPAN}
        LEFT JOIN municipalities m ON m.receita_code = e.municipality_code
        {cep_join}
        ON CONFLICT (cnpj) DO NOTHING
    """))
    db.commit()
    logger.info("establishments_new: %d linhas em %.1fs", imported.rowcount, time.monotonic() - t0)

    if has_ceps:
        # Cobertura do vinculo por CEP. E o numero que decide se uma FOREIGN
        # KEY de establishments.cep -> postal_codes.cep e possivel: ela so pode
        # existir se este "orfaos" for zero, porque a Receita tambem publica CEP
        # com digitacao errada, extinto, ou de endereco no exterior.
        # Agora que o CEP orfao vira NULL, a contagem sai direto das colunas:
        # `address IS NOT NULL` e exatamente "nao casou com a base dos Correios".
        #
        # display.set explicito aqui: sem isso a tela ficava presa em "INSERT
        # establishments_new ..." durante essa contagem (um count(*) com 3
        # FILTER varrendo as ~70M linhas recem-inseridas) -- ja rodou o
        # suficiente, sozinho, pra alguem achar que o INSERT reiniciou.
        display.set(slot, "  build: contando cobertura de CEP")
        cov = db.execute(text("""
            SELECT count(*) FILTER (WHERE cep IS NOT NULL) AS vinculados,
                   count(*) FILTER (WHERE cep IS NOT NULL AND street IS NULL) AS resolvidos,
                   count(*) FILTER (WHERE address IS NOT NULL) AS sem_vinculo
            FROM establishments_new
        """)).mappings().one()
        # `resolvidos` e SUBCONJUNTO de `vinculados` (todo resolvido tambem
        # tem cep, so que sem precisar do endereco da Receita) -- por isso o
        # log explicita "dos quais" em vez de listar como se fossem 3 grupos
        # que iam somar o total. So `vinculados + sem_vinculo` fecha o total.
        locality = cov["vinculados"] - cov["resolvidos"]
        logger.info(
            "CEP: %d vinculados + %d sem vínculo (endereço em address) = %d no total. "
            "Dos vinculados: %d com logradouro resolvido pelos Correios, %d de CEP de localidade "
            "(sem rua cadastrada -- usam o endereço da Receita mesmo com CEP vinculado).",
            cov["vinculados"], cov["sem_vinculo"], cov["vinculados"] + cov["sem_vinculo"],
            cov["resolvidos"], locality,
        )

    # Relacao N:N empresa-CNAE, o caminho de busca de `?cnae_codes=`
    # (ver models.EstablishmentCnae). Aqui, com o staging ainda carregado.
    _set_step(progress, "build", period=period, group="build", status="running", message="tabela de CNAEs")
    _build_cnaes_table(db, display, slot)

    # `Municipios.zip` da Receita so tem codigo+nome, sem UF -- pega o UF de
    # qualquer estabelecimento daquele municipio (sao 1:1) enquanto o staging
    # ainda existe. Sem isso Municipality.uf fica sempre NULL, o filtro por UF de
    # /municipalities/search devolve vazio e o import-ibge (que casa por nome+UF)
    # nao casa nada.
    _set_step(progress, "build", period=period, group="build", status="running", message="preenchendo UF dos municípios")
    display.set(slot, "  build: preenchendo UF dos municípios")
    # `municipalities.uf` e SMALLINT com o MESMO codigo do staging (o de
    # app/regions.py, ver a migration b2d5f8a91c04) -- entao a UF vai direto,
    # sem o join contra a tabela de siglas que existia aqui enquanto a coluna
    # era texto.
    db.execute(text("""
        UPDATE municipalities m SET uf = v.uf
        FROM (SELECT DISTINCT ON (municipality_code) municipality_code, uf
              FROM establishments_staging
              WHERE municipality_code IS NOT NULL AND uf IS NOT NULL) v
        WHERE v.municipality_code = m.receita_code AND m.uf IS DISTINCT FROM v.uf
    """))
    db.commit()

    # Pais do municipio. A base so tem municipio brasileiro, entao e a mesma
    # linha de `countries` pra todos -- 105 e o codigo do Brasil na tabela de
    # referencia da Receita, importada no grupo "reference" desta mesma fase.
    # Idempotente e best-effort: sem a referencia importada, fica NULL.
    db.execute(text("""
        UPDATE municipalities m SET country_id = c.id
        FROM countries c
        WHERE btrim(c.code) = '105' AND m.country_id IS DISTINCT FROM c.id
    """))
    db.commit()

    if has_ceps:
        # Depois do bulk load, nao antes: validar 63M linhas durante o INSERT
        # sairia bem mais caro que uma varredura unica no fim. So com a base de
        # CEP populada -- sem ela o `cep` fica como veio da Receita e nao ha
        # garantia de que casa.
        _set_step(progress, "build", period=period, group="build", status="running", message="FK de cep")
        display.set(slot, "  build: validando FK de cep")
        db.execute(text("""
            ALTER TABLE establishments_new
            ADD CONSTRAINT establishments_cep_fkey
            FOREIGN KEY (cep) REFERENCES postal_codes (cep)
        """))
        db.commit()

    # Os indices de trigrama abaixo dependem dela. A migration ja cria, mas o
    # import roda contra bancos que podem ter sido restaurados de dump.
    db.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    db.commit()

    for table, indexes in (
        ("establishments_new", DEFERRED_INDEXES),
        ("establishment_cnaes_new", CNAES_DEFERRED_INDEXES),
    ):
        for name, cols, where, using in indexes:
            _set_step(progress, "build", period=period, group="build", status="running", message=f"índice {name}")
            display.set(slot, f"  build: criando índice {name}")
            using_sql = f" USING {using}" if using else ""
            where_sql = f" WHERE {where}" if where else ""
            db.execute(text(f'CREATE INDEX "{name}_new" ON {table}{using_sql} {cols}{where_sql}'))
            db.commit()

    # A PK da tabela N:N tambem e adiada: ela nao serve pro load (nao ha ON
    # CONFLICT ali, o LATERAL ja garante unicidade) e sim pra leitura por
    # pagina. Criar depois e ordens de magnitude mais rapido -- e e aqui que um
    # CNAE repetido que tenha escapado do `array_remove`/`DISTINCT` apareceria,
    # como erro, em vez de virar linha duplicada calada.
    _set_step(progress, "build", period=period, group="build", status="running", message="PK de establishment_cnaes")
    display.set(slot, "  build: PK de establishment_cnaes_new")
    db.execute(text(
        "ALTER TABLE establishment_cnaes_new "
        "ADD CONSTRAINT establishment_cnaes_new_pkey PRIMARY KEY (cnpj, cnae)"
    ))
    db.commit()

    # Grava a tabela inteira no WAL de uma vez -- unico ponto em que o bulk
    # load paga esse custo, em vez de a cada linha/indice.
    _set_step(progress, "build", period=period, group="build", status="running", message="tornando establishments_new durável (SET LOGGED)")
    display.set(slot, "  build: SET LOGGED (grava no WAL)")
    db.execute(text("ALTER TABLE establishments_new SET LOGGED"))
    db.execute(text("ALTER TABLE establishment_cnaes_new SET LOGGED"))
    db.commit()

    # Agregado de /establishments/stats, montado do MESMO snapshot e trocado no
    # mesmo swap -- e isso que garante que ele nunca discorda da tabela. Uma
    # varredura de establishments_new, aqui onde ela ainda esta quente.
    _set_step(progress, "build", period=period, group="build", status="running", message="agregado de stats")
    display.set(slot, "  build: agregado de stats")
    db.execute(text(f"DROP TABLE IF EXISTS {stats_rollup.TABLE}_new"))
    db.execute(text(stats_rollup.create_sql(f"{stats_rollup.TABLE}_new")))
    db.execute(text(stats_rollup.build_sql(f"{stats_rollup.TABLE}_new", "establishments_new")))
    for statement in stats_rollup.index_sql(f"{stats_rollup.TABLE}_new", "_new"):
        db.execute(text(statement))
    db.commit()

    # Segundo agregado, por CNAE principal-ou-secundario. Passada separada
    # porque o grao e uma linha por (empresa, CNAE) -- nao da pra sair do mesmo
    # GROUP BY do agregado acima. Sai do join com a tabela N:N.
    _set_step(progress, "build", period=period, group="build", status="running", message="agregado de stats por CNAE")
    display.set(slot, "  build: agregado de stats por CNAE")
    db.execute(text(f"DROP TABLE IF EXISTS {stats_rollup.CNAE_TABLE}_new"))
    db.execute(text(stats_rollup.cnae_create_sql(f"{stats_rollup.CNAE_TABLE}_new")))
    db.execute(text(stats_rollup.cnae_build_sql(
        f"{stats_rollup.CNAE_TABLE}_new", "establishments_new", "establishment_cnaes_new",
    )))
    for statement in stats_rollup.cnae_index_sql(f"{stats_rollup.CNAE_TABLE}_new", "_new"):
        db.execute(text(statement))
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

    # A tabela N:N troca no MESMO swap -- e disso que depende ela nunca
    # discordar de `establishments` (ver models.EstablishmentCnae: as colunas
    # `uf`/`has_cellphone` sao copias, e o que as mantem corretas e as duas
    # sairem do mesmo snapshot e virarem visiveis juntas). `IF EXISTS` porque
    # no primeiro import a tabela canonica ainda nao existe.
    db.execute(text("DROP TABLE IF EXISTS establishment_cnaes_old"))
    db.execute(text("ALTER TABLE IF EXISTS establishment_cnaes RENAME TO establishment_cnaes_old"))
    db.execute(text("ALTER TABLE establishment_cnaes_new RENAME TO establishment_cnaes"))
    db.execute(text("DROP TABLE IF EXISTS establishment_cnaes_old"))
    for name, _cols, _where, _using in CNAES_DEFERRED_INDEXES:
        db.execute(text(f'ALTER INDEX "{name}_new" RENAME TO "{name}"'))
    db.execute(text(
        "ALTER INDEX establishment_cnaes_new_pkey RENAME TO establishment_cnaes_pkey"
    ))

    # O agregado entra no mesmo RENAME: as duas tabelas viram visiveis juntas,
    # entao nao existe instante em que /stats responda sobre um snapshot e
    # /establishments sobre outro.
    for table, indexes in (
        (stats_rollup.TABLE, stats_rollup.INDEXES),
        (stats_rollup.CNAE_TABLE, stats_rollup.CNAE_INDEXES),
    ):
        db.execute(text(f"DROP TABLE IF EXISTS {table}_old"))
        db.execute(text(f"ALTER TABLE IF EXISTS {table} RENAME TO {table}_old"))
        db.execute(text(f"ALTER TABLE {table}_new RENAME TO {table}"))
        db.execute(text(f"DROP TABLE IF EXISTS {table}_old"))
        for name, _cols in indexes:
            db.execute(text(f'ALTER INDEX "{name}_new" RENAME TO "{name}"'))
        db.execute(text(f"ALTER INDEX {table}_new_pkey RENAME TO {table}_pkey"))

    db.execute(text("TRUNCATE TABLE companies_staging, simples_staging, establishments_staging"))
    db.execute(text("DELETE FROM import_files WHERE period = :period"), {"period": period})
    db.commit()

    # O RENAME nao carrega estatistica nenhuma: pro planner, `establishments`
    # acabou de virar uma tabela de 70M+ linhas sem uma linha de pg_statistic.
    # Sem isso ele erra a cardinalidade por ordens de magnitude e escolhe seq
    # scan onde tem indice. Autovacuum acabaria fazendo, mas so depois de um
    # tempo -- e nesse meio tempo a API fica inutilizavel.
    display.set(slot, "  build: ANALYZE na tabela final")
    _set_step(progress, "build", period=period, group="build", status="running", message="ANALYZE establishments")
    db.execute(text("ANALYZE establishments"))
    db.execute(text("ANALYZE establishment_cnaes"))
    db.execute(text(f"ANALYZE {stats_rollup.TABLE}"))
    db.execute(text(f"ANALYZE {stats_rollup.CNAE_TABLE}"))
    db.commit()

    display.set(slot, "")
    _set_step(
        progress, "build", period=period, group="build", status="success",
        processed_rows=imported.rowcount, message="tabela final trocada atomicamente",
    )


# --------------------------------------------------------------------------
# Estado / progresso
# --------------------------------------------------------------------------


def _already_imported(db: Session, period: str, filename: str) -> bool:
    return db.query(ImportFile).filter_by(period=period, filename=filename).first() is not None


def _mark_imported(db: Session, period: str, filename: str, rows: int) -> None:
    stmt = pg_insert(ImportFile.__table__).values(
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
        existing = db.get(ImportStep, step)
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
        existing = db.get(ImportStep, step)
        if not existing or existing.status != "running":
            fields["started_at"] = now

    stmt = pg_insert(ImportStep.__table__).values(step=step, **fields)
    stmt = stmt.on_conflict_do_update(index_elements=["step"], set_={c: stmt.excluded[c] for c in fields})
    db.execute(stmt)
    db.commit()


def _set_run(db: Session, **fields) -> None:
    """Escreve o estado da FASE de CNPJ na linha unica de `import_runs`.

    Traduz o vocabulario do pipeline (period/status/message) pras colunas
    `cnpj*` da tabela fundida: o `status` que chega aqui e o da fase, nao o
    geral do `import-all` -- esse e escrito por app.cli._set_import_all, e
    sobrescreve-lo daqui apagaria o progresso das outras cinco fases.
    """
    message = fields.pop("message", None)
    if message:
        logger.info("%s", message)

    status = fields.pop("status", None)
    period = fields.pop("period", None)
    assert not fields, f"campo nao mapeado pra import_runs: {sorted(fields)}"

    now = datetime.now(timezone.utc)
    # `updated_at` e NOT NULL e esta linha pode nascer aqui (import-cnpj
    # rodando sozinho, sem import-all antes) -- por isso sempre vai no INSERT.
    values = {"cnpj_updated_at": now, "updated_at": now}
    if period is not None:
        values["cnpj_period"] = period
    if message is not None:
        values["cnpj_message"] = message
    if status is not None:
        values["cnpj"] = status
        if status == "running":
            existing = db.get(ImportRun, 1)
            if not existing or existing.cnpj != "running":
                values["cnpj_started_at"] = now

    stmt = pg_insert(ImportRun.__table__).values(id=1, **values)
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_={c: stmt.excluded[c] for c in values})
    db.execute(stmt)
    db.commit()
