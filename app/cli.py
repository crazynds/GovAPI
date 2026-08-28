import typer

from app.config import settings
from app.db import SessionLocal
from app.importer.pipeline import run_import
from app.migrate import run_migrations

cli = typer.Typer()

# Nome da tabela unificada de CEP gerada pelo edne-correios-loader -- ver
# app/routers/enderecos.py, que consulta essa tabela por nome fixo.
CORREIOS_CEP_TABLE = "correios_cep"


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


@cli.command("import-all")
def import_all():
    """
    Roda todas as importações de uma vez só: CNPJ da Receita Federal e
    CEPs dos Correios (e-DNE). Use os comandos `import-cnpj`/`import-ceps`
    em separado se só precisar atualizar uma das bases.
    """
    typer.echo("== CNPJ (Receita Federal) ==")
    _import_cnpj(period=None, only=None)

    typer.echo("== CEPs (Correios / e-DNE) ==")
    _import_ceps(source=None)

    typer.echo("Todas as importações concluídas.")


def _import_cnpj(period: str | None, only: list[str] | None) -> None:
    run_import(period=period, only=only or None)


def _import_ceps(source: str | None) -> None:
    from edne_correios_loader import DneLoader, TableSetEnum

    loader = DneLoader(
        settings.database_url,
        dne_source=source,
        table_names={"cep_unificado": CORREIOS_CEP_TABLE},
    )
    loader.load(table_set=TableSetEnum.UNIFIED_CEP_ONLY)


if __name__ == "__main__":
    cli()
