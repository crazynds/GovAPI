"""Codec do CNPJ: 14 posicoes = 12 alfanumericas (raiz + ordem) + 2 digitos
verificadores.

As 12 posicoes alfanumericas cabem exatamente num BIGINT quando lidas em base
36 (36^12 = 4,74e18 < 9,22e18 do bigint), entao e assim que o CNPJ e
armazenado -- 8 bytes em vez de 15 como texto. O DV nao e guardado: e um
checksum mod-11 deterministico sobre as 12, recalculado na saida.

A codificacao preserva a ordem (string de largura fixa com zero a esquerda ->
o inteiro ordena igual), entao "CNPJs que comecam com a raiz X" e uma faixa
continua de inteiros -- ver `basico_range`.
"""

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_BASE = 36

BASICO_LEN = 8
ORDEM_LEN = 4
BODY_LEN = BASICO_LEN + ORDEM_LEN  # 12 posicoes alfanumericas

# Quantos inteiros distintos uma unica raiz cobre (todas as ordens possiveis).
ORDEM_SPAN = _BASE**ORDEM_LEN

MAX_VALUE = _BASE**BODY_LEN - 1


def encode(basico: str, ordem: str) -> int:
    """Junta raiz + ordem (do CSV da Receita) num inteiro."""
    return to_int(f"{basico:0>{BASICO_LEN}}{ordem:0>{ORDEM_LEN}}")


def to_int(body: str) -> int:
    """Converte as 12 posicoes alfanumericas num inteiro.

    Levanta ValueError se vier algo fora do formato -- de proposito: durante o
    import isso derruba o arquivo pro retry em vez de gravar uma raiz truncada
    em silencio.
    """
    body = body.strip().upper()
    if len(body) != BODY_LEN:
        raise ValueError(f"CNPJ deve ter {BODY_LEN} posições alfanuméricas, veio {len(body)}: {body!r}")
    return int(body, _BASE)


def decode(value: int) -> str:
    """Inverso de `to_int` -- as 12 posicoes, com zero a esquerda."""
    if not 0 <= value <= MAX_VALUE:
        raise ValueError(f"Valor fora da faixa de um CNPJ: {value}")

    digits = []
    while value:
        value, rest = divmod(value, _BASE)
        digits.append(_ALPHABET[rest])
    return "".join(reversed(digits)).rjust(BODY_LEN, "0")


def dv(body: str) -> str:
    """Os 2 digitos verificadores das 12 posicoes.

    Regra oficial da Receita pro CNPJ alfanumerico: o valor de cada caractere e
    `ord(c) - 48` (o que faz '0'..'9' valerem 0..9 e 'A'..'Z' valerem 17..42),
    pesos 2..9 ciclicos da direita pra esquerda, mod 11, resto < 2 vira 0.
    Com os 12 sendo digitos, degenera exatamente na regra numerica antiga.
    """
    first = _checksum(body)
    second = _checksum(body + str(first))
    return f"{first}{second}"


def _checksum(partial: str) -> int:
    total = 0
    weight = 2
    for char in reversed(partial):
        total += (ord(char) - 48) * weight
        weight = 2 if weight == 9 else weight + 1
    rest = total % 11
    return 0 if rest < 2 else 11 - rest


def full(value: int) -> str:
    """CNPJ completo de 14 posicoes, sem pontuacao -- o formato devolvido pela
    API (mesmo de antes da coluna virar numerica)."""
    body = decode(value)
    return body + dv(body)


def format(value: int) -> str:  # noqa: A001 -- espelha o `format` builtin de proposito
    """CNPJ pontuado: `12.ABC.345/0001-42`."""
    body = decode(value)
    return f"{body[:2]}.{body[2:5]}.{body[5:8]}/{body[8:12]}-{dv(body)}"


def parse(text: str) -> int:
    """Le um CNPJ vindo do usuario -- com ou sem pontuacao, completo (14) ou so
    o corpo (12), maiusculo ou minusculo -- e devolve o inteiro.

    O DV, quando vem, e ignorado (e derivado do corpo, nao carrega informacao).
    """
    cleaned = "".join(c for c in text.upper() if c.isalnum())

    if len(cleaned) == BODY_LEN + 2:
        cleaned = cleaned[:BODY_LEN]
    elif len(cleaned) == BASICO_LEN:
        # So a raiz: completa com a ordem da matriz (0001), pra quem passa os 8
        # primeiros digitos esperando "a empresa".
        cleaned = cleaned + "0001"

    return to_int(cleaned)


def basico_to_int(basico: str) -> int:
    """So a raiz (8 posicoes) como inteiro -- e assim que as tabelas de staging
    guardam `cnpj_basico`, e o que o JOIN do build compara."""
    basico = f"{basico.strip():0>{BASICO_LEN}}".upper()
    if len(basico) != BASICO_LEN:
        raise ValueError(f"Raiz de CNPJ deve ter {BASICO_LEN} posições: {basico!r}")
    return int(basico, _BASE)


def basico_from_int(basico: int) -> str:
    """Inverso de `basico_to_int`."""
    return decode(basico * ORDEM_SPAN)[:BASICO_LEN]


def basico_of_value(value: int) -> int:
    """A raiz, como inteiro, de um CNPJ completo ja codificado -- e o mesmo que
    "corta as 4 ultimas posicoes", que em base 36 e uma divisao inteira."""
    return value // ORDEM_SPAN


def basico_range(basico: str) -> tuple[int, int]:
    """Faixa fechada [lo, hi] que cobre todos os estabelecimentos de uma raiz.

    Substitui o `cnpj LIKE '<raiz>%'`, que nenhum indice atende, por um
    `BETWEEN` que a PK resolve -- so funciona porque a base 36 sobre string de
    largura fixa preserva a ordem.
    """
    lo = basico_to_int(basico) * ORDEM_SPAN
    return lo, lo + ORDEM_SPAN - 1


