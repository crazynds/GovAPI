"""Barras de progresso sem dependência externa -- pra download/extração
(bytes) e importação (linhas + bytes do CSV). Renderiza no máximo a cada
`min_interval` segundos, então pode ser chamada a cada iteração de um loop
grande sem custo real.

Os estágios do pipeline rodam em paralelo (ver app/importer/pipeline.py), então
há três barras vivas ao mesmo tempo. Reescrever a própria linha com `\\r` só
funciona pra uma; com três, elas se sobrescreveriam. Por isso as barras vivem
num `ProgressDisplay`, que é dono de um bloco de N linhas na tela e redesenha o
bloco inteiro sob um lock, subindo o cursor com ANSI.
"""

import os
import sys
import threading
import time


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
    """Bloco de N linhas fixas na tela, uma por estágio do pipeline.

    Cada `set()` regrava o bloco inteiro: sobe N-1 linhas com `\\033[A`, escreve
    as N linhas limpando o resto de cada uma com `\\033[K`. Sob lock, porque
    quem chama são as threads dos estágios.

    Fora de um TTY (log de container, CI, pipe) o cursor não volta, então cada
    atualização viraria uma linha nova e o log inteiro seria barra de progresso.
    Nesse caso o display se desliga e deixa o `logger` do pipeline falando.
    """

    def __init__(self, slots: int, stream=None):
        self.stream = stream or sys.stdout
        self.enabled = self.stream.isatty() and not os.environ.get("NO_COLOR")
        self._lines = [""] * slots
        self._lock = threading.Lock()
        self._painted = False

    def bar(self, slot: int, label: str, total: int | None = None, unit: str = "bytes") -> ProgressBar:
        return ProgressBar(label, total=total, unit=unit, display=self if self.enabled else None, slot=slot)

    def set(self, slot: int, line: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._lines[slot] = line
            self._paint()

    def _paint(self) -> None:
        out = []
        if self._painted:
            out.append(f"\033[{len(self._lines)}A")
        for line in self._lines:
            out.append("\r\033[K" + line + "\n")
        self.stream.write("".join(out))
        self.stream.flush()
        self._painted = True

    def close(self) -> None:
        if not self.enabled or not self._painted:
            return
        with self._lock:
            self._lines = [""] * len(self._lines)
            self._paint()
