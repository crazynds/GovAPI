"""Barras de progresso sem dependência externa -- pra download/extração
(bytes) e importação (linhas + bytes do CSV). Renderiza no máximo a cada
`min_interval` segundos, então pode ser chamada a cada iteração de um loop
grande sem custo real.

Os estágios do pipeline rodam em paralelo (ver app/importer/pipeline.py), então
há três barras vivas ao mesmo tempo. Reescrever a própria linha com `\\r` só
funciona pra uma; com três, elas se sobrescreveriam. Por isso as barras vivem
num `ProgressDisplay`, que é dono de um bloco de linhas na tela e o redesenha
inteiro sob um lock, subindo o cursor com ANSI. O log do pipeline passa pelo
mesmo display (ver `ProgressDisplay.log`), senão uma linha de log solta
desalinha o repaint e o bloco vira um rastro de linhas repetidas.
"""

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager


def human_bytes(n: float) -> str:
    for suffix in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or suffix == "GB":
            return f"{n:.0f}{suffix}" if suffix == "B" else f"{n:.1f}{suffix}"
        n /= 1024
    return f"{n:.1f}GB"  # inalcançável, só pra satisfazer o linter de retorno


def human_count(n: float) -> str:
    for suffix in ("", "k", "M", "B"):
        if abs(n) < 1000 or suffix == "B":
            return f"{n:.0f}{suffix}" if suffix == "" else f"{n:.1f}{suffix}"
        n /= 1000
    return f"{n:.1f}B"


class ProgressBar:
    """Uma linha de progresso. Sozinha, escreve com `\\r` na saída; dentro de um
    `ProgressDisplay`, só entrega a linha pronta e o display cuida do desenho."""

    BAR_WIDTH = 24

    def __init__(
        self,
        label: str,
        total: int | None = None,
        unit: str = "bytes",
        min_interval: float = 0.2,
        display: "ProgressDisplay | None" = None,
        slot: int = 0,
    ):
        self.label = label
        self.total = total
        self.unit = unit
        self.min_interval = min_interval
        self.start = time.monotonic()
        self._display = display
        self._slot = slot
        self._last_render = 0.0
        self._last_len = 0

    def elapsed(self) -> float:
        return max(time.monotonic() - self.start, 1e-6)

    def _fmt(self, n: float) -> str:
        return human_bytes(n) if self.unit == "bytes" else human_count(n)

    def update(self, current: int, *, extra: str = "", force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_render < self.min_interval:
            return
        self._last_render = now

        line = self.render(current, extra=extra)

        if self._display is not None:
            self._display.set(self._slot, line)
            return

        pad = max(0, self._last_len - len(line))
        sys.stdout.write("\r" + line + " " * pad)
        sys.stdout.flush()
        self._last_len = len(line)

    def render(self, current: int, *, extra: str = "") -> str:
        rate = current / self.elapsed()
        rate_str = f"{self._fmt(rate)}/s"

        if self.total:
            pct = min(100.0, current * 100 / self.total)
            filled = int(self.BAR_WIDTH * pct / 100)
            bar = "#" * filled + "-" * (self.BAR_WIDTH - filled)
            line = f"{self.label} [{bar}] {pct:5.1f}% {self._fmt(current)}/{self._fmt(self.total)} {rate_str}"
        else:
            line = f"{self.label} {self._fmt(current)} {rate_str}"

        if extra:
            line += f" {extra}"

        return line

    def close(self) -> None:
        if self._display is not None:
            self._display.set(self._slot, "")
            return
        sys.stdout.write("\n")
        sys.stdout.flush()


class ProgressDisplay:
    """Bloco de linhas fixas na tela, uma por estágio ativo do pipeline.

    O bloco é sempre reescrito no mesmo lugar: sobe o número de linhas que
    desenhou por último, apaga daí até o fim da tela (`\\033[J`) e redesenha.
    Sob lock, porque quem chama são as threads dos estágios.

    O `log()` existe porque o `logger` do pipeline escreve na MESMA tela: uma
    linha de log solta empurra o cursor e o "sobe N linhas" do repaint seguinte
    passa a cair sobre o log em vez de sobre o bloco -- o efeito é o bloco ir se
    reemitindo linha após linha em vez de se sobrescrever. Então o log também
    passa por aqui: apaga o bloco, escreve a mensagem (que fica no histórico) e
    redesenha o bloco embaixo. Ver `install_log_handler` em pipeline.py.

    Fora de um TTY (log de container, CI, pipe) não há cursor pra mover: o
    display se desliga, `log()` vira um print simples e as barras não aparecem.
    """

    def __init__(self, slots: int, stream=None):
        self.stream = stream or sys.stdout
        self.enabled = self.stream.isatty() and not os.environ.get("NO_COLOR")
        self._lines = [""] * slots
        # RLock: `log()` e `set()` podem ser reentrados pelo mesmo caminho
        # (uma barra que loga durante o update).
        self._lock = threading.RLock()
        self._drawn = 0  # quantas linhas o último desenho deixou na tela

    def bar(self, slot: int, label: str, total: int | None = None, unit: str = "bytes") -> ProgressBar:
        # Sempre ligada a este display -- inclusive quando ele está desligado,
        # e aí `set()` descarta e a barra fica silenciosa. Sem TTY uma barra é
        # só ruído: cada render viraria uma linha nova no log do container, e o
        # `logger` já reporta arquivo a arquivo (e `import_progress`, 1x/s).
        return ProgressBar(label, total=total, unit=unit, display=self, slot=slot)

    def set(self, slot: int, line: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._lines[slot] = line
            self._erase()
            self._draw()
            self.stream.flush()

    def log(self, message: str) -> None:
        """Escreve uma linha acima do bloco, sem quebrar o bloco."""
        with self._lock:
            if not self.enabled:
                self.stream.write(message + "\n")
                self.stream.flush()
                return
            self._erase()
            self.stream.write(message + "\n")
            self._draw()
            self.stream.flush()

    def _erase(self) -> None:
        if self._drawn:
            # Sobe até o topo do bloco e apaga dali até o fim da tela -- o bloco
            # é sempre a última coisa na tela, então isso é seguro e dispensa
            # limpar linha por linha.
            self.stream.write(f"\033[{self._drawn}A\r\033[J")
            self._drawn = 0

    def _draw(self) -> None:
        # Só os slots ocupados: um estágio ocioso não gasta linha.
        active = [line for line in self._lines if line]
        for line in active:
            self.stream.write("\r\033[K" + line + "\n")
        self._drawn = len(active)

    def close(self) -> None:
        with self._lock:
            self._lines = [""] * len(self._lines)
            if self.enabled:
                self._erase()
                self.stream.flush()


class DisplayLogHandler(logging.Handler):
    """Manda o logging pelo display, pra não desalinhar o bloco de barras.

    Instalado só durante o import (ver `install_log_handler`); fora dele o
    logging segue no handler normal.
    """

    def __init__(self, display: ProgressDisplay):
        super().__init__()
        self.display = display

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.display.log(self.format(record))
        except Exception:  # noqa: BLE001 -- contrato do logging: nunca derrubar quem loga
            self.handleError(record)


@contextmanager
def log_through(display: ProgressDisplay):
    """Enquanto durar o bloco, todo logging sai pelo `display`.

    Troca os handlers do logger raiz (o `basicConfig` do pipeline põe um
    StreamHandler no stderr, que escreveria na mesma tela do bloco sem saber
    dele) e devolve os originais no fim, mesmo em caso de erro.
    """
    root = logging.getLogger()
    original = root.handlers[:]
    handler = DisplayLogHandler(display)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root.handlers = [handler]
    try:
        yield
    finally:
        root.handlers = original
