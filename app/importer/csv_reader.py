"""Le o CSV oficial da Receita: `;`-delimitado, sempre entre aspas duplas,
ISO-8859-1, sem cabecalho.

Usa o `csv` da stdlib (nao um split manual) de proposito: ja foi tentado um
`split('";"')` a mao, e a Receita realmente tem linha com aspas mal
escapadas/desalinhadas no meio de dezenas de milhoes de linhas (visto na
pratica -- Estabelecimentos0.zip, mesma linha gerando corrupcao diferente
duas vezes, sinal de desalinhamento de campo de verdade, nao so pontuacao
solta). O `csv.reader` entende aspas duplicadas (`""` -> `"` literal) e campo
com newline embutido dentro de aspas (contabiliza como parte do mesmo
registro, nao como fim de linha) -- as duas coisas que o split manual nao
sabia fazer.
"""

import csv
import os
from collections.abc import Callable, Iterator

# (bytes_lidos, bytes_totais, linhas_lidas) -- chamado a cada linha, mas é
# barato (poucas contas); quem renderiza (ProgressBar) que decide throttle.
ProgressCallback = Callable[[int, int, int], None]


def read_csv(path: str, columns: list[str], on_progress: ProgressCallback | None = None) -> Iterator[dict]:
    total_bytes = os.path.getsize(path)
    # Uma lista em vez de closure com `nonlocal` -- o gerador que conta bytes
    # e passado pro `csv.reader` como sua fonte de linhas, entao o `yield` do
    # csv acontece de dentro da chamada do reader, nao do nosso loop direto.
    counters = [0, 0]  # [bytes_lidos, linhas_fisicas_lidas]

    def counted_lines(f):
        for raw_line in f:
            # ISO-8859-1 e 1 byte por caractere, então len() do texto já e o
            # total de bytes consumidos nessa linha -- conta ANTES de
            # sanitizar, pra bater com o tamanho real do arquivo em disco.
            counters[0] += len(raw_line)
            counters[1] += 1

            # Postgres nunca aceita 0x00 num campo de texto, seja qual for o
            # encoding da coluna -- e uma regra do backend, nao negociacao de
            # client_encoding (visto na pratica: CharacterNotInRepertoire no
            # COPY, cancelando a carga inteira). ISO-8859-1 decodifica
            # QUALQUER byte sem erro, 0x00 incluso, entao esse lixo passa
            # batido pela leitura e só explode la na frente -- remove aqui,
            # na origem, antes do csv.reader ver a linha.
            if "\x00" in raw_line:
                raw_line = raw_line.replace("\x00", "")

            yield raw_line

    with open(path, encoding="iso-8859-1", newline="") as f:
        reader = csv.reader(counted_lines(f), delimitr=";", quotechar='"')
        rows_read = 0
        for fields in reader:
            if not fields or fields == [""]:
                continue

            rows_read += 1
            if on_progress:
                # Bytes/linhas fisicas ate aqui (pode ser mais de 1 por
                # registro, se um campo tiver newline dentro de aspas) --
                # `rows_read` conta REGISTROS, que e o que importa pro
                # chamador (linhas do CSV, nao linhas fisicas do arquivo).
                on_progress(counters[0], total_bytes, rows_read)

            row = {}
            for name, value in zip(columns, fields):
                row[name] = value if value != "" else None

            yield row
