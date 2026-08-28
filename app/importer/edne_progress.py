"""Barra de progresso pro import do e-DNE (edne-correios-loader).

A lib só fala por `logging` -- sem isso, `import-ceps` fica minutos em
silêncio (download de dezenas de MB) ou solta uma linha crua de log por
tabela carregada, sem nenhuma noção de quanto falta. Dois pontos de extensão
que ela já expõe, sem precisar alterar o pacote:

  * `DneResolver.download_report_hook(read, total, hook_type)` -- chamado a
    cada bloco baixado. É o único hook de verdade que a lib tem.
  * `DneDatabaseWriter.populate_table(table_name, lines)` -- não tem hook,
    mas o gerador `lines` pode ser envolvido por fora pra contar progresso
    antes de chamar a implementação original.

As duas classes injetadas em `DneLoader.DneResolver`/`DneDatabaseWriter` (ver
`patched_loader`) são as mesmas duas que `DneLoader.load()` já usa por
default -- só herdam e adicionam a barra.
"""

from contextlib import contextmanager

from edne_correios_loader import DneLoader
from edne_correios_loader.dbwriter import DneDatabaseWriter
from edne_correios_loader.resolver import DneResolver

from app.importer.progress import ProgressDisplay, log_through

SLOT_DOWNLOAD = 0
SLOT_TABLE = 1
SLOTS = 2


def _resolver_with_progress(display: ProgressDisplay) -> type[DneResolver]:
    class ProgressDneResolver(DneResolver):
        def download_report_hook(self, read: int, total: int, hook_type: str) -> None:
            bar = getattr(self, "_bar", None)
            if hook_type == "start" or bar is None:
                bar = display.bar(SLOT_DOWNLOAD, "  baixando e-DNE", total=total if total > 0 else None)
                self._bar = bar
                self._downloaded = 0

            self._downloaded += read
            bar.update(self._downloaded, force=(hook_type == "finish"))

            if hook_type == "finish":
                bar.close()

    return ProgressDneResolver


def _writer_with_progress(display: ProgressDisplay) -> type[DneDatabaseWriter]:
    class ProgressDneDatabaseWriter(DneDatabaseWriter):
        def populate_table(self, table_name, lines):
            # Total de linhas não é conhecido de antemão (vem de um gerador
            # que lê os arquivos do DNE sob demanda) -- a barra conta sem
            # percentual, só pra mostrar que algo está avançando e em qual
            # tabela, em vez de travar em silêncio até o log de conclusão.
            bar = display.bar(SLOT_TABLE, f"  carregando {table_name}", unit="count")

            def counted():
                count = 0
                for line in lines:
                    count += 1
                    if count % 1000 == 0:
                        bar.update(count, extra="linhas")
                    yield line
                bar.update(count, extra="linhas", force=True)

            super().populate_table(table_name, counted())

    return ProgressDneDatabaseWriter


@contextmanager
def progress(loader: DneLoader):
    """Liga a barra de progresso num `DneLoader` já construído, pro `with`.

        loader = DneLoader(url, dne_source=source, table_names=...)
        with edne_progress.progress(loader):
            loader.load(table_set=...)

    Some no fim mesmo se `load()` falhar (barra fechada, logging devolvido).
    """
    display = ProgressDisplay(SLOTS)
    original_resolver, original_writer = loader.DneResolver, loader.DneDatabaseWriter
    loader.DneResolver = _resolver_with_progress(display)
    loader.DneDatabaseWriter = _writer_with_progress(display)

    try:
        with log_through(display):
            yield
    finally:
        loader.DneResolver, loader.DneDatabaseWriter = original_resolver, original_writer
        display.close()


__all__ = ["progress"]
