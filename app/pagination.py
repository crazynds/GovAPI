"""Paginacao por cursor (keyset) -- substitui o page/offset das buscas.

Motivo da troca: as buscas paginadas montavam a resposta com um `total`, e pra
isso rodavam um `count()` sobre o resultado inteiro antes de devolver a pagina.
Numa tabela de ~72M linhas isso varre tudo mesmo com `per_page=1` -- um
`/establishments?uf=PR` levava a API a 504 por timeout enquanto contava milhoes
de linhas que ninguem ia ler. O OFFSET tem o mesmo defeito de fundo: pra chegar
na pagina N o Postgres precisa produzir e descartar N*per_page linhas.

Keyset resolve os dois: em vez de "pule 5000 linhas", a query diz "continue
depois DESTA linha", o que o indice resolve com um seek. O preco e que nao
existe mais numero total de paginas nem salto pra uma pagina arbitraria -- so
"proxima". Foi uma troca deliberada, ver README.

## Como a ordenacao precisa ser

Keyset exige uma ordem TOTAL: se duas linhas empatam na coluna de ordenacao, a
posicao relativa delas entre uma pagina e a proxima nao e garantida, e a linha
do empate ou se repete ou some. Por isso toda ordenacao aqui termina na chave
primaria como desempate.

Mas "precisa de uma ordem" nao quer dizer "pode ser qualquer ordem". Ordenar
pela PK sozinha e de graca: o indice ja existe e a pagina sai de uma faixa
continua dele. Ordenar por outra coluna obriga o banco a ordenar o resultado
filtrado INTEIRO antes de cortar a pagina -- pra saber quem tem o maior
`cellphone_confidence` em PR, ele precisa olhar todas as linhas de PR, e ai
nem o `LIMIT 1` salva. Por isso /establishments so ordena por outra coluna
quando o cliente pede `sort_by` explicitamente.

NULLS LAST e explicito, mas SO em coluna nullable -- ver `order_by_clause`,
onde essa distincao decide se o indice e usado ou nao.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, or_

# Sobe junto com qualquer mudanca no formato do payload, pra um cursor emitido
# por uma versao anterior ser recusado com 422 em vez de decodificar torto.
_VERSION = 1


@dataclass(frozen=True)
class Cursor:
    """Posicao exata da ultima linha da pagina anterior.

    `values` sao os valores das colunas de ordenacao dessa linha, na mesma
    ordem do ORDER BY (o ultimo e sempre a chave primaria). `fingerprint`
    amarra o cursor a busca que o gerou -- ver `_fingerprint`.
    """

    values: tuple[Any, ...]
    fingerprint: str


def _encode_value(value: Any) -> dict:
    """Valor -> JSON com o tipo junto.

    O tipo precisa viajar no cursor porque JSON nao distingue uma data de uma
    string qualquer, e o valor volta pra dentro de uma comparacao SQL contra
    uma coluna tipada -- mandar "2024-01-01" onde o Postgres espera `date`
    daria erro de tipo (ou pior, um cast implicito silencioso).
    """
    if value is None:
        return {"t": "null", "v": None}
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        return {"t": "int", "v": value}
    if isinstance(value, Decimal):
        return {"t": "float", "v": float(value)}
    if isinstance(value, float):
        return {"t": "float", "v": value}
    if isinstance(value, datetime):
        return {"t": "datetime", "v": value.isoformat()}
    if isinstance(value, date):
        return {"t": "date", "v": value.isoformat()}
    return {"t": "str", "v": str(value)}


def _decode_value(item: dict) -> Any:
    kind, raw = item.get("t"), item.get("v")
    if kind == "null":
        return None
    if kind == "bool":
        return bool(raw)
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "datetime":
        return datetime.fromisoformat(raw)
    if kind == "date":
        return date.fromisoformat(raw)
    if kind == "str":
        return str(raw)
    raise ValueError(f"tipo de valor desconhecido no cursor: {kind!r}")


def _fingerprint(parts: dict) -> str:
    """Identidade da busca que gerou o cursor (filtros + ordenacao).

    Continuar uma paginacao trocando os filtros no meio nao tem resposta
    correta: o cursor e uma posicao DENTRO de uma ordenacao especifica sobre um
    conjunto especifico. Sem essa amarra, `?uf=SP&cursor=<de uma busca por
    uf=PR>` devolveria um pedaco arbitrario do meio do resultado, calado. Com
    ela, vira 422.

    Hash e nao os parametros em claro so pra manter o cursor curto -- ele nao
    esconde nada que o cliente ja nao tenha mandado.
    """
    canonical = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.blake2b(canonical.encode(), digest_size=8).hexdigest()


def encode_cursor(values: tuple[Any, ...], fingerprint: str) -> str:
    payload = {"v": _VERSION, "k": [_encode_value(x) for x in values], "f": fingerprint}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(raw: str, fingerprint: str) -> Cursor:
    """Cursor opaco -> posicao. Levanta 422 pra qualquer coisa que nao case.

    Um cursor invalido e sempre erro do cliente (copiado errado, de outra
    busca, ou de uma versao anterior da API), nunca erro nosso -- por isso 422
    e nao 500. Cursor e algo que o cliente devolve pra API, entao todo o
    conteudo aqui e entrada nao confiavel: nada e usado antes de validado.
    """
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(422, "Cursor inválido") from exc

    if not isinstance(payload, dict) or payload.get("v") != _VERSION:
        raise HTTPException(422, "Cursor inválido ou de uma versão anterior da API")

    if payload.get("f") != fingerprint:
        raise HTTPException(
            422,
            "Cursor não corresponde a esta busca -- filtros e ordenação não podem "
            "mudar no meio da paginação. Comece de novo sem `cursor`.",
        )

    items = payload.get("k")
    if not isinstance(items, list) or not items:
        raise HTTPException(422, "Cursor inválido")

    try:
        values = tuple(_decode_value(x) for x in items)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, "Cursor inválido") from exc

    return Cursor(values=values, fingerprint=fingerprint)


@dataclass(frozen=True)
class SortKey:
    """Uma coluna do ORDER BY. `column` e a expressao SQLAlchemy; `attr` o nome
    do atributo pra ler o valor da linha e montar o proximo cursor.

    `nullable` existe por performance, nao por correcao: numa coluna NOT NULL
    os ramos de NULL do predicado sao sempre falsos, mas um
    `cnpj < :x OR cnpj IS NULL` no WHERE atrapalha o planner a reconhecer a
    faixa continua que o indice resolve com um seek -- exatamente o que a
    paginacao por keyset existe pra conseguir. Marcado como nao-nulo, o
    predicado sai como `cnpj < :x` limpo.
    """

    column: Any
    attr: str
    desc: bool = False
    nullable: bool = True


def order_by_clause(keys: list[SortKey]) -> list:
    """ORDER BY com NULLS LAST explicito -- mas SO em coluna que aceita NULL.

    Explicito porque o default do Postgres e assimetrico (NULLS LAST em ASC,
    NULLS FIRST em DESC), e o predicado de keyset abaixo assume NULLS LAST
    sempre; divergir aqui repetiria ou pularia linhas na virada da pagina.

    A ressalva do `nullable` e o que faz o indice ser usado. A mesma
    assimetria vale pra DEFINICAO do indice: `(col DESC)` e NULLS FIRST. O
    planner compara a ordenacao pedida com a do indice incluindo esse flag,
    entao pedir `col DESC NULLS LAST` contra um indice `(col DESC)` nao casa --
    ele descarta o indice e ordena o resultado inteiro na mao. Numa coluna NOT
    NULL a distincao nao muda resultado nenhum, so impede o match, entao aqui
    ela nao e emitida.
    """
    clauses = []
    for k in keys:
        column = k.column.desc() if k.desc else k.column.asc()
        clauses.append(column.nullslast() if k.nullable else column)
    return clauses


def keyset_filter(keys: list[SortKey], values: tuple[Any, ...]):
    """Predicado "vem depois desta linha", na ordem definida por `keys`.

    Expansao lexicografica padrao: uma linha vem depois se ja passou da
    primeira coluna, ou empatou nela e vem depois pelo resto. Como a ultima
    chave e sempre a PK (unica e NOT NULL), a recursao sempre termina numa
    comparacao decisiva -- nao existe empate ate o fim.
    """
    if len(keys) != len(values):
        raise HTTPException(422, "Cursor inválido")

    def beyond(key: SortKey, value: Any):
        # NULLS LAST: um NULL vem depois de qualquer valor, e nada vem depois
        # de um NULL (a nao ser outro NULL, resolvido no empate pela PK).
        if value is None:
            return None
        past = key.column < value if key.desc else key.column > value
        return or_(past, key.column.is_(None)) if key.nullable else past

    def equal(key: SortKey, value: Any):
        return key.column.is_(None) if value is None else key.column == value

    clauses = []
    for i, (key, value) in enumerate(zip(keys, values)):
        step = beyond(key, value)
        ties = [equal(k, v) for k, v in zip(keys[:i], values[:i])]
        if step is not None:
            clauses.append(and_(*ties, step) if ties else step)

    if not clauses:
        # Todos os valores NULL: nada vem depois, resultado vazio em vez de
        # um filtro vazio que devolveria a primeira pagina de novo.
        return None
    return or_(*clauses)


def page_values(row: Any, keys: list[SortKey]) -> tuple[Any, ...]:
    return tuple(getattr(row, k.attr) for k in keys)


def paginate(query, keys: list[SortKey], cursor: str | None, limit: int, fingerprint: str):
    """Aplica ordenacao + cursor + limite e devolve (linhas, próximo cursor).

    Busca `limit + 1` linhas: a existencia da linha extra e o que diz se ha
    proxima pagina, sem precisar contar nada. Ela e descartada da resposta.
    """
    if cursor:
        position = decode_cursor(cursor, fingerprint)
        condition = keyset_filter(keys, position.values)
        if condition is None:
            return [], None
        query = query.filter(condition)

    rows = query.order_by(*order_by_clause(keys)).limit(limit + 1).all()

    if len(rows) <= limit:
        return rows, None
    rows = rows[:limit]
    return rows, encode_cursor(page_values(rows[-1], keys), fingerprint)


@dataclass(frozen=True)
class SqlSortKey:
    """Versao de `SortKey` pras buscas montadas em SQL direto.

    `expr` e o SQL da coluna (ou da expressao inteira, no caso da distancia,
    que nao existe como coluna); `alias` e o nome com que ela sai no SELECT,
    pra ler o valor da linha e montar o proximo cursor.
    """

    expr: str
    alias: str
    desc: bool = False
    nullable: bool = True


def order_by_sql(keys: list[SqlSortKey]) -> str:
    """Mesma regra de `order_by_clause`: NULLS LAST so onde a coluna aceita
    NULL, senao o flag impede o match com o indice."""
    parts = []
    for k in keys:
        direction = "DESC" if k.desc else "ASC"
        parts.append(f"{k.expr} {direction} NULLS LAST" if k.nullable else f"{k.expr} {direction}")
    return ", ".join(parts)


def keyset_sql(keys: list[SqlSortKey], values: tuple[Any, ...], params: dict) -> str | None:
    """Mesma expansao lexicografica de `keyset_filter`, em SQL texto.

    Preenche `params` com os valores do cursor (nomes `cur0`, `cur1`, ...) --
    bind parameters e nao interpolacao: o cursor vem do cliente, e concatenar
    o conteudo dele na query seria injecao de SQL direta.
    """
    if len(keys) != len(values):
        raise HTTPException(422, "Cursor inválido")

    for i, value in enumerate(values):
        params[f"cur{i}"] = value

    clauses = []
    for i, (key, value) in enumerate(zip(keys, values)):
        if value is None:
            # NULLS LAST: nada vem depois de um NULL alem do empate, resolvido
            # nas chaves seguintes.
            continue
        comparison = "<" if key.desc else ">"
        step = f"{key.expr} {comparison} :cur{i}"
        if key.nullable:
            step = f"({step} OR {key.expr} IS NULL)"
        ties = [
            f"{k.expr} IS NULL" if v is None else f"{k.expr} = :cur{j}"
            for j, (k, v) in enumerate(zip(keys[:i], values[:i]))
        ]
        clauses.append(f"({' AND '.join(ties + [step])})" if ties else step)

    return f"({' OR '.join(clauses)})" if clauses else None


def make_fingerprint(**parts) -> str:
    """Fingerprint a partir dos filtros/ordenacao de um endpoint.

    Listas viram ordenadas: `?uf=SP&uf=RJ` e `?uf=RJ&uf=SP` sao a mesma busca e
    precisam gerar o mesmo fingerprint, senao um cursor valido seria recusado
    so porque o cliente reordenou os parametros repetidos.
    """
    normalized = {
        name: sorted(map(str, value)) if isinstance(value, (list, set, tuple)) else value
        for name, value in parts.items()
    }
    return _fingerprint(normalized)
