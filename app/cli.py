import typer
from sqlalchemy import text

from app import ceps
from app.config import settings
from app.db import SessionLocal
from app.importer.pipeline import run_import
from app.migrate import run_migrations

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
    nome+UF com os municípios já importados. Precisa que `import-cnpj`
    já tenha rodado ao menos uma vez (usa o UF preenchido no build).
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
    Geocodifica o centroide de cada município (latitude/longitude) via
    Nominatim/OpenStreetMap, sem chave -- usado como fallback de baixa
    precisão em /enderecos/proximos e /enderecos/buscar?lat=&lon= quando
    um CEP específico ainda não tem coordenada exata cacheada.

    Só ~5570 chamadas (uma por município), a 1 req/s (política de uso do
    Nominatim) -- leva cerca de 1h40 na primeira vez. Pula município que já
    tem coordenada, então rodar de novo depois de uma queda só continua.
    """
    from app.importer.geocoding import geocode_municipios

    db = SessionLocal()
    try:
        geocoded, total = geocode_municipios(db)
        typer.echo(f"Geocodificação: {geocoded}/{total} municípios pendentes resolvidos.")
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


@cli.command("import-all")
def import_all(
    skip_municipios_geo: bool = typer.Option(
        False, "--skip-municipios-geo", help="Pula a geocodificação de municípios (a etapa lenta, ~1h40)."
    ),
):
    """
    Roda todas as importações, na ordem em que uma depende da outra.

    A ordem não é preferência, é dependência:

      1. CEPs dos Correios (e-DNE) -- tem que vir primeiro. O build do CNPJ
         liga cada estabelecimento ao seu CEP e, quando o CEP já resolve o
         endereço, deixa de guardar logradouro/bairro. Sem a base de CEP no
         lugar, esse dado é duplicado em dezenas de milhões de linhas e a
         FOREIGN KEY de `establishments.cep` não é criada.
      2. Coordenadas em massa (extrato do OSM) -- preenche lat/long dos CEPs
         que acabaram de entrar.
      3. CNPJ da Receita Federal -- é aqui que o vínculo com o CEP acontece.
         Também é o que preenche `municipios.uf`, que os dois passos
         seguintes exigem.
      4. População e área dos municípios (IBGE) -- casa por nome+UF.
      5. Centroide dos municípios (Nominatim) -- fallback de baixa precisão
         na busca por proximidade. É a etapa lenta: ~5570 chamadas a 1 req/s
         (política de uso do Nominatim), cerca de 1h40 na primeira vez. Pula
         município que já tem coordenada, então rodar de novo só continua de
         onde parou. Use --skip-municipios-geo pra deixar pra depois.

    Use os comandos individuais se só precisar atualizar uma das bases -- mas
    respeite a ordem acima ao encadeá-los.
    """
    typer.echo("== 1/5 CEPs (Correios / e-DNE) ==")
    _import_ceps(source=None)

    typer.echo("== 2/5 Coordenadas de CEP (extrato do OpenStreetMap) ==")
    from app.importer.osm_ceps import import_ceps_from_osm

    db = SessionLocal()
    try:
        typer.echo(f"OSM: {import_ceps_from_osm(db)} CEPs com coordenada nova.")
    finally:
        db.close()

    typer.echo("== 3/5 CNPJ (Receita Federal) ==")
    _import_cnpj(period=None, only=None)

    typer.echo("== 4/5 População e área dos municípios (IBGE) ==")
    from app.importer.ibge import import_ibge

    db = SessionLocal()
    try:
        pop, area = import_ibge(db)
        typer.echo(f"IBGE: {pop} municípios com população, {area} com área.")
    finally:
        db.close()

    if skip_municipios_geo:
        typer.echo("== 5/5 Centroide dos municípios -- pulado (--skip-municipios-geo) ==")
    else:
        typer.echo("== 5/5 Centroide dos municípios (Nominatim, ~1h40) ==")
        from app.importer.geocoding import geocode_municipios

        db = SessionLocal()
        try:
            geocoded, total = geocode_municipios(db)
            typer.echo(f"Geocodificação: {geocoded}/{total} municípios pendentes resolvidos.")
        finally:
            db.close()

    typer.echo("Todas as importações concluídas.")


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

    db = SessionLocal()
    try:
        db.execute(text(f"DROP TABLE IF EXISTS {ceps.SCRATCH_TABLE}"))
        db.commit()

        loader = DneLoader(
            settings.database_url,
            dne_source=source,
            table_names={"cep_unificado": ceps.SCRATCH_TABLE},
        )
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
