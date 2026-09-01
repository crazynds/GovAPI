"""Codec do CNPJ: 14 posicoes = 12 alfanumericas (raiz + ordem) + 2 digitos
verificadores.

As 12 posicoes alfanumericas cabem exatamente num BIGINT quando lidas em base
36 (36^12 = 4,74e18 < 9,22e18 do bigint), entao e assim que o CNPJ e
armazenado -- 8 bytes em vez de 15 como texto. O DV nao e guardado: e um
checksum mod-11 deterministico sobre as 12, recalculado na saida.

A codificacao preserva a ordem (string de largura fixa com zero a esquerda ->
o inteiro ordena igual), entao "CNPJs que comecam com a raiz X" e uma faixa
continua de inteiros -- ver `root_range`.
"""

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_BASE = 36

ROOT_LEN = 8
BRANCH_LEN = 4
BODY_LEN = ROOT_LEN + BRANCH_LEN  # 12 posicoes alfanumericas

# Quantos inteiros distintos uma unica raiz cobre (todas as ordens possiveis).
BRANCH_SPAN = _BASE**BRANCH_LEN

MAX_VALUE = _BASE**BODY_LEN - 1


def encode(root: str, branch: str) -> int:
    """Junta raiz + ordem (do CSV da Receita) num inteiro."""
    return to_int(f"{root:0>{ROOT_LEN}}{branch:0>{BRANCH_LEN}}")


def to_int(body: str) -> int:
    """Converte as 12 posicoes alfanumericas num inteiro.

    Filtra fora qualquer caractere que nao seja alfanumerico antes de validar
    o tamanho -- um CNPJ nunca tem nada alem disso, entao aspas/espaco que
    vazaram de um parsing de CSV mal comportado (visto na pratica: duas
    colunas coladas com as aspas originais ainda dentro, tipo
    '"20206097""0003"') sao descartados em vez de derrubar a linha. Ainda
    levanta ValueError se, depois de limpo, nao sobrarem exatamente 12
    posicoes -- aí o dado em si esta errado, nao so um caractere estranho a
    mais, e isso ainda derruba o arquivo pro retry de proposito.
    """
    body = "".join(c for c in body.upper() if c.isalnum())
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
    elif len(cleaned) == ROOT_LEN:
        # So a raiz: completa com a ordem da matriz (0001), pra quem passa os 8
        # primeiros digitos esperando "a empresa".
        cleaned = cleaned + "0001"

    return to_int(cleaned)


def root_to_int(root: str) -> int:
    """So a raiz (8 posicoes) como inteiro -- e assim que as tabelas de staging
    guardam `cnpj_root`, e o que o JOIN do build compara.

    Mesmo filtro de `to_int`: descarta qualquer caractere nao-alfanumerico
    antes de completar com zero a esquerda e validar o tamanho.
    """
    root = "".join(c for c in root.upper() if c.isalnum()).rjust(ROOT_LEN, "0")
    if len(root) != ROOT_LEN:
        raise ValueError(f"Raiz de CNPJ deve ter {ROOT_LEN} posições: {root!r}")
    return int(root, _BASE)


def root_from_int(root: int) -> str:
    """Inverso de `root_to_int`."""
    return decode(root * BRANCH_SPAN)[:ROOT_LEN]


def root_of_value(value: int) -> int:
    """A raiz, como inteiro, de um CNPJ completo ja codificado -- e o mesmo que
    "corta as 4 ultimas posicoes", que em base 36 e uma divisao inteira."""
    return value // BRANCH_SPAN


def root_range(root: str) -> tuple[int, int]:
    """Faixa fechada [lo, hi] que cobre todos os estabelecimentos de uma raiz.

    Substitui o `cnpj LIKE '<raiz>%'`, que nenhum indice atende, por um
    `BETWEEN` que a PK resolve -- so funciona porque a base 36 sobre string de
    largura fixa preserva a ordem.
    """
    lo = root_to_int(root) * BRANCH_SPAN
    return lo, lo + BRANCH_SPAN - 1


