"""Le o CSV oficial da Receita: `;`-delimitado, sempre entre aspas duplas,
ISO-8859-1, sem cabecalho. `explode` manual em vez de csv.reader porque o
layout e sempre bem formado (mesma decisao tomada e validada no lado
Laravel contra 5000 linhas reais do mirror)."""

import os
from collections.abc import Callable, Iterator

# (bytes_lidos, bytes_totais, linhas_lidas) -- chamado a cada linha, mas é
# barato (poucas contas); quem renderiza (ProgressBar) que decide throttle.
ProgressCallback = Callable[[int, int, int], None]


def read_csv(path: str, columns: list[str], on_progress: ProgressCallback | None = None) -> Iterator[dict]:
    total_bytes = os.path.getsize(path)
    bytes_read = 0
    rows_read = 0

    with open(path, encoding="iso-8859-1", newline="") as f:
        for raw_line in f:
            # ISO-8859-1 e 1 byte por caractere, então len() do texto já e
            # o total de bytes consumidos nessa linha -- sem precisar de
            # f.tell(), que não é confiável durante iteração em modo texto.
            bytes_read += len(raw_line)
            rows_read += 1
            if on_progress:
                on_progress(bytes_read, total_bytes, rows_read)

            line = raw_line.rstrip("\r\n")
            if not line:
                continue

            if line.startswith('"') and line.endswith('"'):
                fields = line[1:-1].split('";"')
            else:
                fields = line.split(";")

            row = {}
            for name, value in zip(columns, fields):
                row[name] = value if value != "" else None

            yield row
