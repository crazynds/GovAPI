from datetime import datetime, timezone

import typer
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import ceps
from app.config import settings
from app.db import SessionLocal
from app.importer.pipeline import run_import
from app.migrate import run_migrations
from app.models import ImportAllRun

cli = typer.Typer()

# Nome/esquema da tabela de CEP vivem em app/ceps.py -- ver o módulo.
CORREIOS_CEP_TABLE = ceps.TABLE


@cli.command()
def migrate():
    """Aplica as migrations pendentes (Alembic) -- já roda sozinho no boot
    do container (ver docker-entrypoint.sh); use manualmente se precisar."""
    run_migrations()
    typer.echo("Migrations em dia.")


@cli.command("reset-db")
def reset_db(
    yes: bool = typer.Option(False, "--yes", "-y", help="Não pede confirmação."),
):
    """
    APAGA TODOS OS DADOS: derruba o schema `public` inteiro e reaplica as
    migrations, deixando um banco vazio e no schema mais recente.

    DROP SCHEMA (e não um drop tabela a tabela) porque a tabela `correios_cep`
    é criada pelo edne-correios-loader, fora do metadata do SQLAlchemy -- um
    drop pelos models deixaria ela para trás.
    """
    target = f"{settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}"
    if not yes:
        typer.confirm(
            f"Isso APAGA TODOS OS DADOS de {target}. Continuar?",
            abort=True,
        )

    from sqlalchemy import text

    from app.db import engine

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    typer.echo(f"Schema `public` recriado em {target}.")

    run_migrations()
    typer.echo("Migrations aplicadas — banco vazio e em dia.")


@cli.command("import-municipios")
def import_municipios_command():
    """
    Bootstrapa `municipios` a partir da API de Localidades do IBGE (sem
    chave, uma request só) -- ibge_code + nome + UF exatos, sem fuzzy match.

    Roda ANTES de tudo (CEPs, CNPJ): é o que dá a `correios_cep` uma FOREIGN
    KEY de verdade pra `municipios`, fechando a cadeia establishments.cep ->
    correios_cep.municipio_cod_ibge -> municipios.ibge_code. O código de
    município da própria Receita (`receita_code`) só chega depois, com o
    `Municipios.zip` dela (dentro de `import-cnpj`), casado por nome contra
    as linhas que este comando já criou.
    """
    from app.importer.municipios import import_municipios

    db = SessionLocal()
    try:
        count = import_municipios(db)
        typer.echo(f"Municípios: {count} carregados do IBGE.")
    finally:
        db.close()


@cli.command("import-cnpj")
def import_cnpj(
    period: str = typer.Option(None, help="Período específico (YYYY-MM-DD). Padrão: descobre o mais recente."),
    only: list[str] = typer.Option(None, help="Grupos a rodar: reference, simples, empresas, estabelecimentos, build."),
):
    """Baixa, descompacta e importa a base pública de CNPJ da Receita Federal."""
    _import_cnpj(period=period, only=only)
    typer.echo("Importação de CNPJ concluída.")


@cli.command("import-ceps")
def import_ceps(
    source: str = typer.Option(None, help="Zip/diretório/URL do e-DNE Básico. Padrão: baixa a versão mais recente direto dos Correios."),
):
    """
    Baixa (dos próprios Correios, sem login) e importa o e-DNE Básico —
    base oficial e gratuita de CEPs do Brasil — numa tabela unificada
    (`correios_cep`), usando o pacote edne-correios-loader
    (https://github.com/cauethenorio/edne-correios-loader).
    """
    _import_ceps(source=source)
    typer.echo(f"Importação de CEPs concluída — tabela `{CORREIOS_CEP_TABLE}`.")


@cli.command("import-ibge")
def import_ibge_command():
    """
    Busca população estimada e área territorial de todos os municípios de
    uma vez via API pública do IBGE (SIDRA/Agregados, sem chave) e casa por
    ibge_code -- exato, sem fuzzy match. Precisa que `import-municipios`
    já tenha rodado (é ele que preenche `ibge_code` em `municipios`).
    """
    from app.importer.ibge import import_ibge

    db = SessionLocal()
    try:
        pop, area = import_ibge(db)
        typer.echo(f"IBGE: {pop} municípios com população, {area} com área.")
    finally:
        db.close()


@cli.command("import-municipios-geo")
def import_municipios_geo_command():
    """
    Preenche o centroide de cada município (latitude/longitude) a partir de um
    dataset público estático (kelvins/municipios-brasileiros, casado por
    código IBGE exato) -- usado como fallback de baixa precisão em
    /enderecos/proximos e /enderecos/buscar?lat=&lon= quando um CEP específico
    ainda não tem coordenada exata cacheada.

    Uma request só (~5570 municípios de uma vez), não mais 1 por município via
    Nominatim -- isso era ~1h40 pela política de 1 req/s deles. Precisa que
    `import-municipios` já tenha rodado (o match é por código IBGE, que é
    ele quem preenche em `municipios` -- não depende de `import-ibge`, que só
    cuida de população/área, um enriquecimento à parte).
    """
    from app.importer.geocoding import geocode_municipios

    db = SessionLocal()
    try:
        geocoded, total = geocode_municipios(db)
        typer.echo(f"Geocodificação: {geocoded} município(s) atualizado(s) (dataset tem {total}).")
    finally:
        db.close()


@cli.command("import-ceps-osm")
def import_ceps_osm_command():
    """
    Baixa o extrato do Brasil do OpenStreetMap (Geofabrik, ~2GB, sem
    limite de taxa) e carrega em massa os CEPs com coordenada nele em
    `correios_cep` -- só preenche CEP que ainda não tem coordenada (nunca
    sobrescreve uma coordenada exata já cacheada via BrasilAPI).

    Rode depois do `import-ceps`: assim preenche a coordenada dos CEPs que já
    têm endereço, em vez de criar linha só-coordenada.

    Alternativa a geocodificar CEP a CEP contra o Nominatim: pra ~1.6
    milhão de CEPs isso violaria a política de uso da API pública deles
    (que pede uso local pra geocodificação em massa) e levaria semanas.
    Cobertura é parcial (depende de quanto o OSM foi mapeado na região),
    mas cai de uma vez, sem chamadas de API.
    """
    from app.importer.osm_ceps import import_ceps_from_osm

    db = SessionLocal()
    try:
        inserted = import_ceps_from_osm(db)
        typer.echo(f"OSM: {inserted} CEPs novos carregados.")
    finally:
        db.close()


# Ordem das 6 fases de `import_all` -- a mesma ordem em que elas rodam, e a
# mesma dos nomes das colunas de ImportAllRun.
IMPORT_ALL_PHASES = ["municipios", "ceps", "ceps_osm", "cnpj", "ibge", "municipios_geo"]


@cli.command("import-all")
def import_all():
    """
    Roda todas as importações, na ordem em que uma depende da outra.

    A ordem não é preferência, é dependência:

      1. Municípios (API de Localidades do IBGE) -- tem que vir primeiro de
         todos. É o que dá a `correios_cep` uma FOREIGN KEY de verdade pra
         `municipios` (por ibge_code), fechando establishments.cep ->
         correios_cep.municipio_cod_ibge -> municipios.ibge_code.
      2. CEPs dos Correios (e-DNE) -- o build do CNPJ liga cada
         estabelecimento ao seu CEP e, quando o CEP já resolve o endereço,
         deixa de guardar logradouro/bairro. Sem a base de CEP no lugar, esse
         dado é duplicado em dezenas de milhões de linhas e a FOREIGN KEY de
         `establishments.cep` não é criada.
      3. Coordenadas em massa (extrato do OSM) -- preenche lat/long dos CEPs
         que acabaram de entrar. Best-effort: se falhar (o mirror do
         Geofabrik já deu 503, por exemplo), avisa e segue pra próxima etapa
         em vez de travar o resto -- é um enriquecimento, nada depende dela.
      4. CNPJ da Receita Federal -- é aqui que o vínculo com o CEP acontece.
         O `Municipios.zip` dela (código+nome, sem UF nem ibge_code) casa por
         nome com as linhas que a fase 1 já criou, preenchendo
         `receita_code`.
      5. População e área dos municípios (IBGE) -- casa por ibge_code exato.
      6. Centroide dos municípios -- dataset público estático (kelvins/
         municipios-brasileiros), casado por código IBGE exato numa request
         só. Já foi geocodificação município a município via Nominatim
         (1 req/s, ~1h40); não é mais.

    Se for cancelado no meio (Ctrl-C ou qualquer falha), a PRÓXIMA chamada
    retoma da fase em que parou -- pula toda fase já concluída na tentativa
    anterior, em vez de recomeçar da 1/6. Isso só vale enquanto a tentativa
    anterior não tiver terminado com sucesso: depois de um `import-all`
    completo, a próxima chamada é tratada como um refresh de verdade (mês que
    vem, novo período de CNPJ, e-DNE atualizado) e roda as 5 fases de novo.

    Use os comandos individuais se só precisar atualizar uma das bases -- mas
    respeite a ordem acima ao encadeá-los.
    """
    db = SessionLocal()
    try:
        run = db.get(ImportAllRun, 1)
        resuming = run is not None and run.status != "success"

        if not resuming:
            # Ou é a primeira vez, ou a tentativa anterior terminou com
            # sucesso -- as duas contam como "começar do zero", a segunda
            # porque um refresh periódico deve reprocessar tudo, não pular
            # fase nenhuma pra sempre.
            phases = dict.fromkeys(IMPORT_ALL_PHASES, "pending")
        else:
            phases = {name: getattr(run, name) for name in IMPORT_ALL_PHASES}
            already_done = [name for name, status in phases.items() if status == "success"]
            if already_done:
                typer.echo(f"Retomando: pulando {', '.join(already_done)} (já concluído(s) antes).")

        _set_import_all(db, status="running", **phases)

        # required=False só na 3/6: coordenadas do OSM são um enriquecimento
        # (fallback de baixa precisão via centroide do município já cobre a
        # busca por proximidade sem elas -- ver import-municipios-geo), não
        # uma dependência de nada depois dela no pipeline. Um mirror externo
        # fora do ar (visto na prática: 503 do Geofabrik) não devia travar
        # CNPJ/IBGE/geocodificação, que não dependem dela em nada.
        steps = [
            ("municipios", "1/6 Municípios (API de Localidades do IBGE)", _run_municipios, True),
            ("ceps", "2/6 CEPs (Correios / e-DNE)", lambda: _import_ceps(source=None), True),
            ("ceps_osm", "3/6 Coordenadas de CEP (extrato do OpenStreetMap)", _run_ceps_osm, False),
            ("cnpj", "4/6 CNPJ (Receita Federal)", lambda: _import_cnpj(period=None, only=None), True),
            ("ibge", "5/6 População e área dos municípios (IBGE)", _run_ibge, True),
            ("municipios_geo", "6/6 Centroide dos municípios (dataset estático)", _run_municipios_geo, True),
        ]

        # Estado final de cada fase, pra decidir o status GERAL no fim -- não dá
        # pra assumir "o loop não levantou exceção" == "tudo terminou": uma
        # fase opcional pode falhar sem abortar nada (ver `required` abaixo) e
        # ficar parada em "running". Começa igual a `phases` (o que já valia
        # antes desta chamada) e cada fase atualiza a sua conforme roda.
        final_status = dict(phases)

        for key, label, action, required in steps:
            if phases.get(key) == "success":
                typer.echo(f"== {label} -- já concluído ==")
                continue

            typer.echo(f"== {label} ==")
            # Só marca "running" antes e "success" depois -- sem try/except
            # pras fases obrigatórias. Se `action()` for interrompida (Ctrl-C,
            # crash, o que for), a fase fica parada em "running" (nunca chega
            # a "success"), e é exatamente esse status que a retomada usa pra
            # saber que ela não terminou e precisa ser refeita.
            _set_import_all(db, **{key: "running"})
            final_status[key] = "running"
            if required:
                action()
            else:
                # Fase opcional: uma falha (rede, serviço externo fora do ar)
                # não deve travar o resto do import-all. Fica em "running" --
                # não em "success" -- pra uma retomada tentar de novo depois,
                # e ela mesma continua idempotente (ver import_ceps_from_osm).
                try:
                    action()
                except Exception as exc:  # noqa: BLE001 -- deliberado: qualquer falha aqui é best-effort, não motivo pra abortar o resto
                    typer.echo(f"  Aviso: {label} falhou, pulando e seguindo pra próxima etapa: {exc}")
                    continue
            _set_import_all(db, **{key: "success"})
            final_status[key] = "success"

        # "success" geral exige TODAS as fases resolvidas (success ou skipped
        # de propósito) -- não só "o loop terminou sem lançar". Senão a
        # próxima chamada veria status="success" e trataria como refresh
        # periódico (reprocessa as 5 do zero, CNPJ incluso -- rebuild de
        # establishments, FK, índices, todo o custo de novo) só pra tentar de
        # novo uma fase opcional que falhou silenciosamente.
        if all(status in ("success", "skipped") for status in final_status.values()):
            _set_import_all(db, status="success", message="import-all concluído")
            typer.echo("Todas as importações concluídas.")
        else:
            pendentes = [name for name, status in final_status.items() if status not in ("success", "skipped")]
            _set_import_all(db, status="failed", message=f"pendente: {', '.join(pendentes)}"[:255])
            typer.echo(f"Concluído com pendências ({', '.join(pendentes)}) -- rode de novo pra tentar de novo.")
    finally:
        db.close()


def _run_municipios() -> None:
    from app.importer.municipios import import_municipios

    db = SessionLocal()
    try:
        count = import_municipios(db)
        typer.echo(f"Municípios: {count} carregados do IBGE.")
    finally:
        db.close()


def _run_ceps_osm() -> None:
    from app.importer.osm_ceps import import_ceps_from_osm

    db = SessionLocal()
    try:
        typer.echo(f"OSM: {import_ceps_from_osm(db)} CEPs com coordenada nova.")
    finally:
        db.close()


def _run_ibge() -> None:
    from app.importer.ibge import import_ibge

    db = SessionLocal()
    try:
        pop, area = import_ibge(db)
        typer.echo(f"IBGE: {pop} municípios com população, {area} com área.")
    finally:
        db.close()


def _run_municipios_geo() -> None:
    from app.importer.geocoding import geocode_municipios

    db = SessionLocal()
    try:
        geocoded, total = geocode_municipios(db)
        typer.echo(f"Geocodificação: {geocoded} município(s) atualizado(s) (dataset tem {total}).")
    finally:
        db.close()


def _set_import_all(db, **fields) -> None:
    now = datetime.now(timezone.utc)
    fields["updated_at"] = now
    if fields.get("status") == "running":
        existing = db.get(ImportAllRun, 1)
        if not existing or existing.status != "running":
            fields["started_at"] = now

    stmt = pg_insert(ImportAllRun.__table__).values(id=1, **fields)
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_={c: stmt.excluded[c] for c in fields})
    db.execute(stmt)
    db.commit()


def _import_cnpj(period: str | None, only: list[str] | None) -> None:
    run_import(period=period, only=only or None)


def _import_ceps(source: str | None) -> None:
    """Importa o e-DNE fazendo UPSERT, não DELETE + INSERT.

    O `DneLoader.load()` limpa a tabela alvo com um DELETE de todas as linhas
    antes de repovoar. Isso é ruim por dois motivos: qualquer FOREIGN KEY
    apontando pra `correios_cep` travaria esse DELETE, e durante a importação
    (que roda numa transação só) a base fica sem CEP nenhum.

    Em vez de mexer nas entranhas da lib, usamos o `table_names` que ela já
    expõe: ela monta a base nova numa tabela de scratch, e o merge pra tabela
    real é um UPSERT nosso. A lib nunca toca em `correios_cep`.
    """
    from edne_correios_loader import DneLoader, TableSetEnum

    from app.importer import edne_progress

    db = SessionLocal()
    try:
        loader = DneLoader(
            settings.database_url,
            dne_source=source,
            table_names={"cep_unificado": ceps.SCRATCH_TABLE},
        )
        # Nessa ordem: dropa tudo que sobrou de um run anterior (inclusive de
        # um que tenha falhado no meio, com tabela criada num schema velho --
        # ver ceps.reset_source_tables) e só then alarga o metadata em
        # memória, pra `create_all` dentro de `.load()` criar tudo do zero já
        # com o tipo largo. O e-DNE real não respeita as larguras que a
        # própria lib declara pro schema dela -- visto na prática: nome de
        # bairro/logradouro passando de VARCHAR(36)/(100) e o INSERT da lib
        # morrendo com StringDataRightTruncation.
        ceps.reset_source_tables(db, loader.metadata)
        ceps.widen_free_text_columns(loader.metadata)
        with edne_progress.progress(loader):
            loader.load(table_set=TableSetEnum.UNIFIED_CEP_ONLY)

        inserted, updated, stale = ceps.upsert_from(db, ceps.SCRATCH_TABLE)

        db.execute(text(f"DROP TABLE {ceps.SCRATCH_TABLE}"))
        db.commit()

        typer.echo(f"CEPs: {inserted} novos, {updated} atualizados.")
        if stale:
            typer.echo(
                f"Atenção: {stale} CEP(s) na tabela não vieram nesta versão do e-DNE "
                "(extintos ou remanejados). Mantidos de propósito -- o import é upsert, "
                "não substituição."
            )
    finally:
        db.close()


if __name__ == "__main__":
    cli()
