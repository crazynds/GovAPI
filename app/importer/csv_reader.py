"""Le o CSV oficial da Receita: `;`-delimitado, sempre entre aspas duplas,
ISO-8859-1, sem cabecalho. `explode` manual em vez de csv.reader porque o
layout e sempre bem formado (mesma decisao tomada e validada no lado
Laravel contra 5000 linhas reais do mirror)."""

from collections.abc import Iterator


def read_csv(path: str, columns: list[str]) -> Iterator[dict]:
    with open(path, encoding="iso-8859-1", newline="") as f:
        for raw_line in f:
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
