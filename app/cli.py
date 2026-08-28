import typer

from app.config import settings
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
