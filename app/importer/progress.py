"""Barra de progresso de uma linha só (reescreve via \\r), sem dependência
externa -- pra download/extração (bytes) e importação (linhas + bytes do
CSV). Renderiza no máximo a cada `min_interval` segundos, então pode ser
chamada a cada iteração de um loop grande sem custo real."""

import sys
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
    BAR_WIDTH = 24

    def __init__(self, label: str, total: int | None = None, unit: str = "bytes", min_interval: float = 0.2):
        self.label = label
        self.total = total
        self.unit = unit
        self.min_interval = min_interval
        self.start = time.monotonic()
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

        rate = current / self.elapsed()
        rate_str = f"{self._fmt(rate)}/s"

        if self.total:
            pct = min(100.0, current * 100 / self.total)
            filled = int(self.BAR_WIDTH * pct / 100)
            bar = "#" * filled + "-" * (self.BAR_WIDTH - filled)
            line = f"\r{self.label} [{bar}] {pct:5.1f}% {self._fmt(current)}/{self._fmt(self.total)} {rate_str}"
        else:
            line = f"\r{self.label} {self._fmt(current)} {rate_str}"

        if extra:
            line += f" {extra}"

        pad = max(0, self._last_len - len(line))
        sys.stdout.write(line + " " * pad)
        sys.stdout.flush()
        self._last_len = len(line)

    def close(self) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()
