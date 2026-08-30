"""Busca de estabelecimentos.

O banco guarda tudo em tipo numerico compacto (CNPJ em base 36, CNAE/UF/porte
como inteiro, telefone sem o +55 -- ver app/models.py). A traducao pros
formatos publicos acontece toda aqui: `_serialize` na saida, `_apply_filters`
na entrada. O contrato da API e o mesmo de quando as colunas eram texto.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Query as ORMQuery, Session

from sqlalchemy import text

from app import ceps as ceps_codec
from app import cnpj as cnpj_codec
from app.db import get_db
from app.models import Cnae, Establishment, Motivo, Municipio, NaturezaJuridica
from app.regions import UF_TO_REGIAO, uf_code, uf_name, ufs_for_regiao
from app.pagination import SortKey, make_fingerprint, paginate
from app.schemas import AddressOut, EstablishmentOut, EstablishmentPage, EstablishmentStatsOut

router = APIRouter(prefix="/establishments", tags=["establishments"])

# Codigos oficiais de porte (Receita) -- ver layout do arquivo Empresas.
# Chaveadas pelo codigo numerico (como fica no banco); a API continua
# devolvendo/aceitando a forma "00"/"01"/... de dois digitos.
COMPANY_SIZE_LABELS = {
    0: "Não informado",
    1: "Micro empresa",
    3: "Empresa de pequeno porte",
    5: "Demais (médio/grande porte)",
}

# Codigos oficiais de situacao cadastral (Receita) -- ver layout do arquivo
# Estabelecimentos. Aceito tanto o codigo quanto o label como filtro.
SITUACAO_LABELS = {
    1: "nula",
    2: "ativa",
    3: "suspensa",
    4: "inapta",
    8: "baixada",
}
SITUACAO_CODES_BY_LABEL = {label: code for code, label in SITUACAO_LABELS.items()}


def _code(value: int | None, width: int = 2) -> str | None:
    """Codigo numerico -> a string com zero a esquerda que a API sempre expos."""
    return f"{value:0{width}d}" if value is not None else None


def _resolve_situacao_codes(values: list[str]) -> list[int]:
    """Aceita label ("ativa") ou codigo ("02"/"2"), devolve o inteiro do banco."""
    codes = []
    for raw in values:
        value = raw.strip().lower()
        if value in SITUACAO_CODES_BY_LABEL:
            codes.append(SITUACAO_CODES_BY_LABEL[value])
        elif value.isdigit():
            codes.append(int(value))
        else:
            raise HTTPException(422, f"Situação cadastral desconhecida: {raw!r}")
    return codes


def _cnae_codes_to_int(values: list[str]) -> list[int]:
    codes = []
    for raw in values:
        digits = "".join(c for c in raw if c.isdigit())
        if not digits:
            raise HTTPException(422, f"Código CNAE inválido: {raw!r}")
        codes.append(int(digits))
    return codes


# Largura do `code` nas tabelas de referencia, que continuam em texto (sao de
# alguns milhares de linhas -- compactar nao pagaria, e o `code` e a interface
# publica). Precisamos dela pra reconstruir a string a partir do inteiro
# guardado em establishments e casar no indice, em vez de castar a coluna (o
# que descartaria o indice).
CODE_WIDTHS = {"cnae": 7, "natureza": 4, "motivo": 2}


def _ceps_from_correios(db: Session, wanted: set[int]) -> dict[int, dict]:
    """Endereço dos Correios pelos CEPs da página atual, numa query só."""
    wanted = {c for c in wanted if c}
    if not wanted:
        return {}

    rows = db.execute(
        text(
            "SELECT cep, logradouro, bairro, municipio, uf "
            "FROM correios_cep WHERE cep = ANY(:ceps)"
        ),
        {"ceps": list(wanted)},
    ).mappings().all()
    return {row["cep"]: dict(row) for row in rows}


def _address(e: Establishment, correios: dict[int, dict]) -> AddressOut:
    """Monta o endereço a partir de um dos dois caminhos possíveis.

    Ou o estabelecimento está vinculado a um CEP -- e aí o endereço vem das
    colunas mais o join com os Correios -- ou não está, e aí o registro bruto da
    Receita está inteiro em `address`. Ver _build_final_table.
    """
    if e.address:
        # Sem vínculo de CEP: CEP ausente, digitado errado ou fora da base dos
        # Correios. Município/UF ainda saem do código da Receita, que é
        # independente do CEP.
        raw = e.address
        return AddressOut(
            cep=raw.get("cep"),
            street=raw.get("logradouro"),
            number=raw.get("numero"),
            complement=raw.get("complemento"),
            district=raw.get("bairro"),
            municipio=e.municipio.name if e.municipio else None,
            uf=uf_name(e.uf),
            source="receita",
        )

    cep_row = correios.get(e.cep) if e.cep else None
    return AddressOut(
        cep=ceps_codec.to_str(e.cep),
        # street/district só estão gravados quando o CEP não os resolve (CEP de
        # localidade); nos outros casos vêm do join, sem duplicar em ~63M linhas.
        street=e.street or (cep_row or {}).get("logradouro"),
        number=e.address_number,
        complement=e.address_complement,
        district=e.district or (cep_row or {}).get("bairro"),
        municipio=(cep_row or {}).get("municipio") or (e.municipio.name if e.municipio else None),
        uf=(cep_row or {}).get("uf") or uf_name(e.uf),
        source="correios" if cep_row and not e.street else ("receita" if e.street or e.district else None),
    )


def _code_descriptions(db: Session, model, codes: set[int], width: int) -> dict[int, str]:
    """{codigo numerico: descricao} pras tabelas de referencia."""
    wanted = {c for c in codes if c is not None}
    if not wanted:
        return {}
    as_text = {f"{c:0{width}d}": c for c in wanted}
    rows = db.query(model.code, model.description).filter(model.code.in_(as_text)).all()
    return {as_text[code]: description for code, description in rows if code in as_text}


def _serialize(
    e: Establishment,
    cnae_map: dict[int, str] | None = None,
    natureza_map: dict[int, str] | None = None,
    motivo_map: dict[int, str] | None = None,
    correios: dict[int, dict] | None = None,
) -> EstablishmentOut:
    cnae_map = cnae_map or {}
    natureza_map = natureza_map or {}
    motivo_map = motivo_map or {}
    secondary = e.secondary_cnaes or []
    cnae_width = CODE_WIDTHS["cnae"]

    return EstablishmentOut(
        # 14 posicoes sem pontuacao -- o DV e recalculado do corpo (nao e
        # guardado). Mesmo formato de quando a coluna era varchar(14).
        cnpj=cnpj_codec.full(e.cnpj),
        company_name=e.company_name,
        trade_name=e.trade_name,
        is_headquarters=e.is_headquarters,
        is_mei=e.is_mei,
        is_simples=e.is_simples,
        company_size=_code(e.company_size),
        company_size_label=COMPANY_SIZE_LABELS.get(e.company_size),
        natureza_juridica_code=_code(e.natureza_juridica, CODE_WIDTHS["natureza"]),
        natureza_juridica_description=natureza_map.get(e.natureza_juridica),
        main_cnae_code=_code(e.main_cnae, cnae_width),
        main_cnae_description=cnae_map.get(e.main_cnae),
        secondary_cnae_codes=[f"{c:0{cnae_width}d}" for c in secondary],
        secondary_cnae_descriptions=[cnae_map[c] for c in secondary if c in cnae_map],
        municipio_name=e.municipio.name if e.municipio else None,
        uf=uf_name(e.uf),
        email=e.email,
        phone=_e164(e.phone),
        cellphone=_e164(e.cellphone),
        cellphone_confidence=e.cellphone_confidence,
        opened_at=e.opened_at,
        situacao_cadastral=_code(e.situacao_cadastral),
        situacao_cadastral_label=SITUACAO_LABELS.get(e.situacao_cadastral),
        motivo_situacao_cadastral_code=_code(e.motivo_situacao_cadastral, CODE_WIDTHS["motivo"]),
        motivo_situacao_cadastral_description=motivo_map.get(e.motivo_situacao_cadastral),
        address=_address(e, correios or {}),
    )


def _e164(national: int | None) -> str | None:
    """O banco guarda so DDD+numero; o +55 e constante (base so tem Brasil)."""
    return f"+55{national}" if national else None


def _serialize_many(db: Session, items: list[Establishment]) -> list[EstablishmentOut]:
    cnae_codes: set[int] = set()
    natureza_codes: set[int] = set()
    motivo_codes: set[int] = set()
    for e in items:
        if e.main_cnae is not None:
            cnae_codes.add(e.main_cnae)
        cnae_codes.update(e.secondary_cnaes or [])
        if e.natureza_juridica is not None:
            natureza_codes.add(e.natureza_juridica)
        if e.motivo_situacao_cadastral is not None:
            motivo_codes.add(e.motivo_situacao_cadastral)

    cnae_map = _code_descriptions(db, Cnae, cnae_codes, CODE_WIDTHS["cnae"])
    natureza_map = _code_descriptions(db, NaturezaJuridica, natureza_codes, CODE_WIDTHS["natureza"])
    motivo_map = _code_descriptions(db, Motivo, motivo_codes, CODE_WIDTHS["motivo"])
    correios = _ceps_from_correios(db, {e.cep for e in items if e.cep})
    return [_serialize(e, cnae_map, natureza_map, motivo_map, correios) for e in items]


def _apply_filters(
    query: ORMQuery,
    *,
    cnae_codes: list[str] | None,
    cnae_match: str,
    uf: list[str] | None,
    regiao: str | None,
    municipio_codes: list[str] | None,
    company_size: list[str] | None,
    is_mei: bool | None,
    is_simples: bool | None,
    is_headquarters: bool | None,
    name: str | None,
    situacao: list[str] | None,
    only_with_cellphone: bool,
    only_with_email: bool,
    has_phone: bool | None,
    opened_after: date | None,
    opened_before: date | None,
) -> ORMQuery:
    if cnae_codes:
        # `overlap` (o operador && do Postgres) sobre INTEGER[] usa o indice
        # GIN de secondary_cnaes; a versao anterior castava um JSON pra JSONB
        # linha a linha, o que nao usava indice nenhum e varria a tabela.
        codes = _cnae_codes_to_int(cnae_codes)
        per_code = [
            or_(Establishment.main_cnae == code, Establishment.secondary_cnaes.overlap([code]))
            for code in codes
        ]
        query = query.filter(and_(*per_code) if cnae_match == "all" else or_(*per_code))

    ufs = set(uf or [])
    if regiao:
        regiao_ufs = ufs_for_regiao(regiao)
        if not regiao_ufs:
            raise HTTPException(422, f"Região desconhecida: {regiao!r} (use norte/nordeste/centro-oeste/sudeste/sul)")
        ufs |= set(regiao_ufs)
    if ufs:
        codes = [uf_code(u) for u in ufs]
        unknown = [u for u, c in zip(ufs, codes) if c is None]
        if unknown:
            raise HTTPException(422, f"UF desconhecida: {sorted(unknown)}")
        query = query.filter(Establishment.uf.in_(codes))

    if municipio_codes:
        codes = [int(c) for c in municipio_codes if str(c).strip().isdigit()]
        if not codes:
            raise HTTPException(422, f"Código de município inválido: {municipio_codes}")
        query = query.filter(Establishment.municipio.has(Municipio.receita_code.in_(codes)))

    if company_size:
        sizes = [int(c) for c in company_size if str(c).strip().isdigit()]
        if not sizes:
            raise HTTPException(422, f"Código de porte inválido: {company_size}")
        query = query.filter(Establishment.company_size.in_(sizes))

    if is_mei is not None:
        query = query.filter(Establishment.is_mei == is_mei)

    if is_simples is not None:
        query = query.filter(Establishment.is_simples == is_simples)

    if is_headquarters is not None:
        query = query.filter(Establishment.is_headquarters == is_headquarters)

    if name:
        pattern = f"%{name}%"
        query = query.filter(
            or_(Establishment.company_name.ilike(pattern), Establishment.trade_name.ilike(pattern))
        )

    if situacao:
        query = query.filter(Establishment.situacao_cadastral.in_(_resolve_situacao_codes(situacao)))

    if only_with_cellphone:
        query = query.filter(Establishment.cellphone.isnot(None))

    if only_with_email:
        query = query.filter(Establishment.email.isnot(None))

    if has_phone is not None:
        query = query.filter(Establishment.phone.isnot(None) if has_phone else Establishment.phone.is_(None))

    if opened_after:
        query = query.filter(Establishment.opened_at >= opened_after)

    if opened_before:
        query = query.filter(Establishment.opened_at <= opened_before)

    return query


@router.get("", response_model=EstablishmentPage)
def search(
    cnae_codes: list[str] | None = Query(None, description="Códigos CNAE (principal ou secundário)"),
    cnae_match: str = Query("any", pattern="^(any|all)$", description="'any': tem ao menos um dos CNAEs; 'all': tem todos"),
    uf: list[str] | None = Query(None, description="Uma ou mais UFs, ex: ?uf=SP&uf=RJ"),
    regiao: str | None = Query(None, description="norte/nordeste/centro-oeste/sudeste/sul, combina com uf"),
    municipio_codes: list[str] | None = Query(None),
    company_size: list[str] | None = Query(None, description="Códigos de porte (00/01/03/05), aceita múltiplos"),
    is_mei: bool | None = Query(None),
    is_simples: bool | None = Query(None),
    is_headquarters: bool | None = Query(None),
    name: str | None = Query(None, description="Busca por razão social ou nome fantasia"),
    situacao: list[str] | None = Query(
        None, description="Código (01/02/03/04/08) ou label (nula/ativa/suspensa/inapta/baixada); sem filtro, mostra todas"
    ),
    only_with_cellphone: bool = Query(True),
    only_with_email: bool = Query(False),
    has_phone: bool | None = Query(None, description="Filtra por ter (true) ou não ter (false) telefone fixo"),
    opened_after: date | None = Query(None, description="Data de abertura >= (YYYY-MM-DD)"),
    opened_before: date | None = Query(None, description="Data de abertura <= (YYYY-MM-DD)"),
    cursor: str | None = Query(
        None,
        description="Cursor da página anterior (`next_cursor`). Sem ele, começa do início. "
                    "Filtros e ordenação precisam ser os mesmos que geraram o cursor.",
    ),
    limit: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Busca paginada por cursor: devolve `next_cursor`, que você repassa em
    `?cursor=` pra próxima página. Não há `total` nem número de páginas --
    contá-los custaria uma varredura completa da tabela a cada request (ver
    app/pagination.py).

    O resultado sai sempre na ordem da chave primária, e isso não é
    configurável: ordenar por qualquer outra coluna obriga o banco a ordenar o
    resultado filtrado inteiro antes de cortar a página, e nenhum `limit`
    escapa disso."""
    query = _apply_filters(
        db.query(Establishment),
        cnae_codes=cnae_codes,
        cnae_match=cnae_match,
        uf=uf,
        regiao=regiao,
        municipio_codes=municipio_codes,
        company_size=company_size,
        is_mei=is_mei,
        is_simples=is_simples,
        is_headquarters=is_headquarters,
        name=name,
        situacao=situacao,
        only_with_cellphone=only_with_cellphone,
        only_with_email=only_with_email,
        has_phone=has_phone,
        opened_after=opened_after,
        opened_before=opened_before,
    )

    # SEMPRE a PK, e so ela. O endpoint ja aceitou `sort_by`/`sort_dir`; foram
    # removidos porque eram uma armadilha exposta como funcionalidade. Ordenar
    # por coluna nao-chave obriga o banco a ordenar o resultado filtrado
    # INTEIRO antes de cortar a pagina -- pra saber quem tem o maior
    # `cellphone_confidence` em PR ele precisa olhar todas as linhas de PR, e
    # o `limit` nao ajuda. Era o que levava `?uf=PR&limit=1` a timeout.
    #
    # A PK sozinha ja e uma ordem TOTAL, entao serve de cursor sem desempate.
    keys = [SortKey(Establishment.cnpj, "cnpj", nullable=False)]

    items, next_cursor = paginate(
        query, keys, cursor, limit,
        make_fingerprint(
            cnae_codes=cnae_codes, cnae_match=cnae_match, uf=uf, regiao=regiao,
            municipio_codes=municipio_codes, company_size=company_size, is_mei=is_mei,
            is_simples=is_simples, is_headquarters=is_headquarters, name=name,
            situacao=situacao, only_with_cellphone=only_with_cellphone,
            only_with_email=only_with_email, has_phone=has_phone,
            opened_after=opened_after, opened_before=opened_before,
        ),
    )

    return EstablishmentPage(data=_serialize_many(db, items), next_cursor=next_cursor, limit=limit)


@router.get("/by-cnpj", response_model=list[EstablishmentOut])
def by_cnpjs(
    cnpjs: list[str] = Query(...),
    only_with_cellphone: bool = Query(False),
    db: Session = Depends(get_db),
):
    # Aceita com ou sem pontuação, completo (14) ou só a raiz (8). Antes a
    # comparação era direta contra a coluna de texto, então só a forma crua de
    # 14 posições casava -- e um CNPJ pontuado devolvia lista vazia sem erro.
    try:
        values = [cnpj_codec.parse(raw) for raw in cnpjs]
    except ValueError as exc:
        raise HTTPException(422, f"CNPJ inválido: {exc}") from exc

    query = db.query(Establishment).filter(Establishment.cnpj.in_(values))
    if only_with_cellphone:
        query = query.filter(Establishment.cellphone.isnot(None))
    return _serialize_many(db, query.all())


@router.get("/stats", response_model=EstablishmentStatsOut)
def stats(
    cnae_codes: list[str] | None = Query(None),
    cnae_match: str = Query("any", pattern="^(any|all)$"),
    uf: list[str] | None = Query(None),
    regiao: str | None = Query(None),
    municipio_codes: list[str] | None = Query(None),
    company_size: list[str] | None = Query(None),
    is_mei: bool | None = Query(None),
    is_simples: bool | None = Query(None),
    is_headquarters: bool | None = Query(None),
    name: str | None = Query(None),
    situacao: list[str] | None = Query(None),
    only_with_cellphone: bool = Query(False),
    only_with_email: bool = Query(False),
    has_phone: bool | None = Query(None),
    opened_after: date | None = Query(None),
    opened_before: date | None = Query(None),
    top_cnaes: int = Query(10, ge=1, le=50),
    include_breakdowns: bool = Query(
        False,
        description="Inclui as distribuições por UF, porte e CNAE. Desligado por padrão: cada "
                    "uma é um GROUP BY que precisa varrer todas as linhas que passam no filtro, "
                    "e num recorte grande (uma UF inteira) isso não termina em tempo de request.",
    ),
    db: Session = Depends(get_db),
):
    """Agregações sobre o mesmo conjunto de filtros de `/establishments`:
    total, distribuição por UF/região, por porte, e os CNAEs principais
    mais frequentes -- útil pra dimensionar uma campanha antes de paginar
    os resultados individuais."""
    base = _apply_filters(
        db.query(Establishment),
        cnae_codes=cnae_codes,
        cnae_match=cnae_match,
        uf=uf,
        regiao=regiao,
        municipio_codes=municipio_codes,
        company_size=company_size,
        is_mei=is_mei,
        is_simples=is_simples,
        is_headquarters=is_headquarters,
        name=name,
        situacao=situacao,
        only_with_cellphone=only_with_cellphone,
        only_with_email=only_with_email,
        has_phone=has_phone,
        opened_after=opened_after,
        opened_before=opened_before,
    )

    # As UFs que sobraram depois de resolver `regiao` -- mesma conta que
    # `_apply_filters` faz. Serve pra saber quando `by_uf` e deduzivel do total
    # em vez de precisar de um GROUP BY. `_apply_filters` ja validou os nomes,
    # entao aqui nao ha UF desconhecida.
    resolved_ufs = set(uf or [])
    if regiao:
        resolved_ufs |= set(ufs_for_regiao(regiao))
    uf_codes = [uf_code(u) for u in sorted(resolved_ufs)]

    # Uma passada em vez de tres. `total`, `with_cellphone` e `with_email` eram
    # tres count() separados sobre o mesmo conjunto -- tres varreduras
    # identicas. FILTER faz os tres contadores numa leitura so.
    total, with_cellphone, with_email = base.with_entities(
        func.count(),
        func.count().filter(Establishment.cellphone.isnot(None)),
        func.count().filter(Establishment.email.isnot(None)),
    ).one()

    by_uf_rows: list = []
    by_company_size_rows: list = []
    by_main_cnae_rows: list = []

    if uf_codes and not include_breakdowns:
        # Filtrando por UF, `GROUP BY uf` so pode devolver essas UFs -- e a
        # soma delas ja e o `total`. Com uma unica UF a resposta e exata sem
        # consultar nada; com varias nao da pra dividir o total entre elas, e
        # ai a distribuicao exige `include_breakdowns`.
        if len(uf_codes) == 1:
            by_uf_rows = [(uf_codes[0], total)]

    if include_breakdowns:
        # Cada um destes varre todas as linhas que passam no filtro. Sem
        # ORDER BY no SQL: ordenar e trabalho pras poucas linhas agregadas,
        # feito em Python logo abaixo, nao pro banco.
        by_uf_rows = (
            base.with_entities(Establishment.uf, func.count().label("total"))
            .group_by(Establishment.uf)
            .all()
        )
        by_company_size_rows = (
            base.with_entities(Establishment.company_size, func.count().label("total"))
            .group_by(Establishment.company_size)
            .all()
        )
        by_main_cnae_rows = (
            base.with_entities(Establishment.main_cnae, func.count().label("total"))
            .filter(Establishment.main_cnae.isnot(None))
            .group_by(Establishment.main_cnae)
            .all()
        )

    # A ordenacao por contagem acontece aqui, sobre dezenas/centenas de linhas
    # ja agregadas -- de graca, e sem pedir sort nenhum ao banco.
    by_uf_rows = sorted(by_uf_rows, key=lambda r: r[1], reverse=True)
    by_company_size_rows = sorted(by_company_size_rows, key=lambda r: r[1], reverse=True)
    by_main_cnae_rows = sorted(by_main_cnae_rows, key=lambda r: r[1], reverse=True)[:top_cnaes]

    # Os group_by devolvem os codigos numericos do banco -- decodifica aqui,
    # nas poucas dezenas de linhas agregadas, nao nas 63M.
    by_uf = {(uf_name(code) or "desconhecido"): count for code, count in by_uf_rows}

    by_regiao: dict[str, int] = {}
    for code, count in by_uf_rows:
        regiao_key = UF_TO_REGIAO.get(uf_name(code), "desconhecida")
        by_regiao[regiao_key] = by_regiao.get(regiao_key, 0) + count

    cnae_width = CODE_WIDTHS["cnae"]
    return EstablishmentStatsOut(
        total=total,
        with_cellphone=with_cellphone,
        with_email=with_email,
        by_uf=by_uf,
        by_regiao=by_regiao,
        by_company_size={(_code(size) or "desconhecido"): count for size, count in by_company_size_rows},
        top_cnaes=[{"cnae_code": f"{code:0{cnae_width}d}", "total": count} for code, count in by_main_cnae_rows],
    )
