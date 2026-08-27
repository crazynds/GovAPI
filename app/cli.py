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
    run_import(period=period, only=only or None)
    typer.echo("Importação concluída.")


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
    from edne_correios_loader import DneLoader, TableSetEnum

    loader = DneLoader(
        settings.database_url,
        dne_source=source,
        table_names={"cep_unificado": CORREIOS_CEP_TABLE},
    )
    loader.load(table_set=TableSetEnum.UNIFIED_CEP_ONLY)
    typer.echo(f"Importação de CEPs concluída — tabela `{CORREIOS_CEP_TABLE}`.")


if __name__ == "__main__":
    cli()
