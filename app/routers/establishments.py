"""Busca de estabelecimentos.

O banco guarda tudo em tipo numerico compacto (CNPJ em base 36, CNAE/UF/porte
como inteiro, telefone sem o +55 -- ver app/models.py). A traducao pros
formatos publicos acontece toda aqui: `_serialize` na saida, `_apply_filters`
na entrada. O contrato da API e o mesmo de quando as colunas eram texto.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Query as ORMQuery, Session

from sqlalchemy import text

from app import ceps as ceps_codec
from app import cnpj as cnpj_codec
from app.db import get_db
from app.models import (
    Cnae,
    Establishment,
    EstablishmentCnae,
    EstablishmentCnaeStats as C,
    EstablishmentStats as S,
    RegistrationStatusReason,
    Municipality,
    LegalNature,
)
from app.regions import UF_TO_REGION, uf_code, uf_name
from app.schemas import AddressOut, EstablishmentOut, EstablishmentPage, EstablishmentStatsOut

router = APIRouter(prefix="/establishments", tags=["establishments"])

# Codigos oficiais de porte (Receita) -- ver layout do arquivo Empresas.
# Chaveadas pelo codigo numerico (como fica no banco); a API continua
# devolvendo/aceitando a forma "00"/"01"/... de dois digitos.
COMPANY_SIZE_LABELS = {
    0: "Não informado",
    1: "Micro company",
    3: "Empresa de pequeno porte",
    5: "Demais (médio/grande porte)",
}

# Codigos oficiais de situacao cadastral (Receita) -- ver layout do arquivo
# Estabelecimentos. Aceito tanto o codigo quanto o label como filtro.
STATUS_LABELS = {
    1: "nula",
    2: "ativa",
    3: "suspensa",
    4: "inapta",
    8: "baixada",
}


def _code(value: int | None, width: int = 2) -> str | None:
    """Codigo numerico -> a string com zero a esquerda que a API sempre expos."""
    return f"{value:0{width}d}" if value is not None else None


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
CODE_WIDTHS = {"cnae": 7, "legal_nature": 4, "reason": 2}


def _ceps_from_correios(db: Session, wanted: set[int]) -> dict[int, dict]:
    """Endereço dos Correios pelos CEPs da página atual, numa query só."""
    wanted = {c for c in wanted if c}
    if not wanted:
        return {}

    rows = db.execute(
        text(
            "SELECT cep, street, district, municipality, uf "
            "FROM postal_codes WHERE cep = ANY(:ceps)"
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
            street=raw.get("street"),
            number=raw.get("number"),
            complement=raw.get("complement"),
            district=raw.get("district"),
            municipality=e.municipality.name if e.municipality else None,
            uf=uf_name(e.uf),
            source="receita",
        )

    cep_row = correios.get(e.cep) if e.cep else None
    return AddressOut(
        cep=ceps_codec.to_str(e.cep),
        # street/district só estão gravados quando o CEP não os resolve (CEP de
        # localidade); nos outros casos vêm do join, sem duplicar em ~63M linhas.
        street=e.street or (cep_row or {}).get("street"),
        number=e.address_number,
        complement=e.address_complement,
        district=e.district or (cep_row or {}).get("district"),
        municipality=(cep_row or {}).get("municipality") or (e.municipality.name if e.municipality else None),
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


def _secondary_cnaes(db: Session, items: list[Establishment]) -> dict[int, list[int]]:
    """Os CNAEs secundarios das empresas da pagina, numa query so.

    Era uma coluna `integer[]` em `establishments`; virou linha em
    `establishment_cnaes` pra busca poder indexar (ver models.EstablishmentCnae).
    A leitura ficou uma query a mais por pagina, resolvida pelo prefixo `cnpj`
    da PK da tabela N:N sobre as ~25 empresas da resposta.

    "Secundario" e deduzido comparando com `establishments.main_cnae`, e nao lido
    de uma coluna `is_main`: a tabela N:N tem uma linha por CNAE DISTINTO da
    empresa (a PK garante), entao "todos menos o principal" e exatamente o que
    a coluna dizia -- sem guardar um booleano em dezenas de milhoes de linhas.
    """
    if not items:
        return {}

    main_by_cnpj = {e.cnpj: e.main_cnae for e in items}
    rows = (
        db.query(EstablishmentCnae.cnpj, EstablishmentCnae.cnae)
        .filter(EstablishmentCnae.cnpj.in_(main_by_cnpj))
        .all()
    )
    grouped: dict[int, list[int]] = {}
    for cnpj, cnae in rows:
        if cnae == main_by_cnpj.get(cnpj):
            continue
        grouped.setdefault(cnpj, []).append(cnae)
    # Ordem estavel na resposta: a tabela nao garante nenhuma, e o array antigo
    # saia na ordem do arquivo da Receita.
    for codes in grouped.values():
        codes.sort()
    return grouped


def _serialize(
    e: Establishment,
    secondary: list[int] | None = None,
    cnae_map: dict[int, str] | None = None,
    legal_nature_map: dict[int, str] | None = None,
    reason_map: dict[int, str] | None = None,
    correios: dict[int, dict] | None = None,
) -> EstablishmentOut:
    cnae_map = cnae_map or {}
    legal_nature_map = legal_nature_map or {}
    reason_map = reason_map or {}
    secondary = secondary or []
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
        legal_nature_code=_code(e.legal_nature, CODE_WIDTHS["legal_nature"]),
        legal_nature_description=legal_nature_map.get(e.legal_nature),
        main_cnae_code=_code(e.main_cnae, cnae_width),
        main_cnae_description=cnae_map.get(e.main_cnae),
        secondary_cnae_codes=[f"{c:0{cnae_width}d}" for c in secondary],
        secondary_cnae_descriptions=[cnae_map[c] for c in secondary if c in cnae_map],
        municipality_name=e.municipality.name if e.municipality else None,
        uf=uf_name(e.uf),
        email=e.email,
        phone=_e164(e.phone),
        cellphone=_e164(e.cellphone),
        cellphone_confidence=e.cellphone_confidence,
        opened_at=e.opened_at,
        registration_status=_code(e.registration_status),
        registration_status_label=STATUS_LABELS.get(e.registration_status),
        registration_status_reason_code=_code(e.registration_status_reason, CODE_WIDTHS["reason"]),
        registration_status_reason_description=reason_map.get(e.registration_status_reason),
        address=_address(e, correios or {}),
    )


def _e164(national: int | None) -> str | None:
    """O banco guarda so DDD+numero; o +55 e constante (base so tem Brasil)."""
    return f"+55{national}" if national else None


def _serialize_many(db: Session, items: list[Establishment]) -> list[EstablishmentOut]:
    secondary = _secondary_cnaes(db, items)

    cnae_codes: set[int] = set()
    legal_nature_codes: set[int] = set()
    reason_codes: set[int] = set()
    for e in items:
        if e.main_cnae is not None:
            cnae_codes.add(e.main_cnae)
        cnae_codes.update(secondary.get(e.cnpj, ()))
        if e.legal_nature is not None:
            legal_nature_codes.add(e.legal_nature)
        if e.registration_status_reason is not None:
            reason_codes.add(e.registration_status_reason)

    cnae_map = _code_descriptions(db, Cnae, cnae_codes, CODE_WIDTHS["cnae"])
    legal_nature_map = _code_descriptions(db, LegalNature, legal_nature_codes, CODE_WIDTHS["legal_nature"])
    reason_map = _code_descriptions(db, RegistrationStatusReason, reason_codes, CODE_WIDTHS["reason"])
    correios = _ceps_from_correios(db, {e.cep for e in items if e.cep})
    return [
        _serialize(e, secondary.get(e.cnpj), cnae_map, legal_nature_map, reason_map, correios)
        for e in items
    ]


def _resolve_place(uf: str | None, municipality_code: str | None) -> tuple[str, int]:
    """Valida o recorte geografico e devolve (tipo, valor ja em numero).

    Exatamente um dos dois, nunca nenhum e nunca os dois.

    OBRIGATORIO porque o unico indice de `establishment_cnaes` comeca em
    `municipality_id` (ver models.EstablishmentCnae): sem cidade nem estado no
    WHERE nao ha prefixo pra usar e a busca viraria uma varredura da tabela
    inteira -- exatamente o caso que essa rota existe pra nao ter.

    EXCLUSIVO porque um recorte ja contem o outro. Aceitar os dois juntos so
    criaria a combinacao que se contradiz (cidade fora do estado), que devolve
    vazio sem erro nenhum -- e um jeito calado de a busca parecer quebrada.
    """
    if (uf is None) == (municipality_code is None):
        raise HTTPException(
            422,
            "Informe exatamente um recorte geográfico: `uf` OU `municipality_code` "
            "(código IBGE da cidade) -- nunca os dois, nunca nenhum.",
        )

    if municipality_code is not None:
        digits = "".join(c for c in municipality_code if c.isdigit())
        if not digits:
            raise HTTPException(422, f"Código IBGE de município inválido: {municipality_code!r}")
        return "municipality", int(digits)

    code = uf_code(uf)
    if code is None:
        raise HTTPException(422, f"UF desconhecida: {uf!r}")
    return "uf", code


def _apply_filters(
    query: ORMQuery,
    *,
    cnae_codes: list[str] | None,
    uf: str | None,
    municipality_code: str | None,
    is_mei: bool | None,
    is_simples: bool | None,
    is_headquarters: bool | None,
    only_with_cellphone: bool,
    only_with_email: bool,
) -> ORMQuery:
    """A busca inteira, numa forma so:

        SELECT establishments.* FROM establishment_cnaes
        JOIN establishments ON establishment_cnaes.cnpj = establishments.cnpj
        JOIN municipalities ON municipalities.id = establishment_cnaes.municipality_id
        WHERE cnae IN (?) AND municipalities.ibge_code = ?   -- ou .uf = ?
          AND has_cellphone = ?
        ORDER BY cnpj LIMIT ?

    A ENTRADA e `establishment_cnaes`, nao `establishments`. E a inversao que
    faz essa rota funcionar: a tabela N:N tem `(municipality_id, cnae, cnpj)`
    num indice so, entao lugar e CNAE sao igualdade nas duas primeiras colunas
    e o corte sai de uma faixa continua do indice. `establishments` entra
    depois, pela PK, uma linha por resultado da pagina.

    Era ao contrario, e por isso dava timeout: a query saia de `establishments`
    com `cnpj IN (subquery)` mais `municipality_id`/`uf` repetidos no WHERE de
    fora. Com `ORDER BY cnpj LIMIT 25` e sem indice de `municipality_id`
    naquela tabela, o planner varria a PK em ordem de `cnpj` NACIONAL filtrando
    linha a linha ate juntar 25 de uma cidade -- dezenas de milhoes de linhas
    pra devolver 25.

    A UF vem de `municipalities`, nao de uma copia aqui: sao ~5.570 linhas, o
    join custa um hash, e o estado vira uma faixa por municipio no mesmo
    indice.

    Os filtros que sobraram de `establishments` (MEI, Simples, matriz, email)
    nao tem indice e nao precisam: eles rodam por linha sobre o punhado que o
    join ja selecionou.
    """
    kind, place = _resolve_place(uf, municipality_code)

    query = query.join(
        EstablishmentCnae, EstablishmentCnae.cnpj == Establishment.cnpj
    ).join(
        Municipality, Municipality.id == EstablishmentCnae.municipality_id
    )

    if kind == "municipality":
        query = query.filter(Municipality.ibge_code == place)
    else:
        query = query.filter(Municipality.uf == place)

    if cnae_codes:
        query = query.filter(EstablishmentCnae.cnae.in_(_cnae_codes_to_int(cnae_codes)))

    if only_with_cellphone:
        # A copia da tabela N:N, e nao `establishments.cellphone IS NOT NULL`:
        # e o mesmo predicado (a coluna e essa comparacao, materializada no
        # import) resolvido do lado que a busca ja esta varrendo.
        query = query.filter(EstablishmentCnae.has_cellphone)

    # Uma empresa tem uma linha na N:N por CNAE distinto -- todas com o mesmo
    # `municipality_id`. Sem isto, uma busca por cidade devolveria a mesma
    # empresa uma vez por CNAE dela, e uma busca por dois CNAEs devolveria
    # duas vezes quem tem os dois.
    #
    # DISTINCT ON e nao DISTINCT: o segundo compara a linha inteira (inclusive
    # o JSONB de `address`), o primeiro para no `cnpj`. Sem ORDER BY o Postgres
    # aceita e escolhe uma linha arbitraria de cada grupo -- e aqui tanto faz
    # qual: todas trazem a MESMA linha de `establishments`, so diferem no CNAE
    # que casou, que nao vai pra resposta.
    query = query.distinct(EstablishmentCnae.cnpj)

    if is_mei is not None:
        query = query.filter(Establishment.is_mei == is_mei)

    if is_simples is not None:
        query = query.filter(Establishment.is_simples == is_simples)

    if is_headquarters is not None:
        query = query.filter(Establishment.is_headquarters == is_headquarters)

    if only_with_email:
        query = query.filter(Establishment.email.isnot(None))

    return query


@router.get("", response_model=EstablishmentPage)
def search(
    cnae_codes: list[str] | None = Query(
        None,
        description="Códigos CNAE (principal ou secundário). Com vários, casa quem tem "
                    "ao menos um deles.",
    ),
    uf: str | None = Query(
        None, description="Sigla do estado, ex: RS. Exclusivo com `municipality_code`."
    ),
    municipality_code: str | None = Query(
        None,
        description="Código IBGE da cidade (7 dígitos). Exclusivo com `uf`. Um dos dois "
                    "é obrigatório.",
    ),
    is_mei: bool | None = Query(None),
    is_simples: bool | None = Query(None),
    is_headquarters: bool | None = Query(None),
    only_with_cellphone: bool = Query(True),
    only_with_email: bool = Query(False),
    offset: int = Query(0, ge=0, description="Quantas linhas pular. Página seguinte: offset + limit."),
    limit: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Busca paginada por `offset`/`limit`. Não há `total` nem número de
    páginas -- contá-los custaria uma varredura completa a cada request.
    `has_more` diz se existe próxima página, e sai de graça (lê uma linha a
    mais que o `limit`).

    O recorte geográfico (`uf` ou `municipality_code`, exatamente um) é
    obrigatório: é o que faz a busca entrar pelo índice em vez de varrer a
    tabela, e é também o que torna o `OFFSET` viável -- ver `_apply_filters`.

    SEM `ORDER BY`. As linhas saem na ordem em que o índice
    `(municipality_id, cnae, cnpj)` as entrega, que para um recorte de uma
    cidade e um CNAE já é a ordem de `cnpj`. O banco não *garante* ordem sem
    `ORDER BY`, e a consequência é real: se o plano mudar entre duas
    requisições, uma página pode repetir ou pular linhas. O que segura isso
    aqui é a propriedade da base -- `establishments` e `establishment_cnaes`
    nunca são escritas em uso, só substituídas inteiras num RENAME atômico
    (ver _build_final_table), então dentro de um mesmo snapshot a mesma query
    tem o mesmo plano e a mesma ordem."""
    query = _apply_filters(
        db.query(Establishment),
        cnae_codes=cnae_codes,
        uf=uf,
        municipality_code=municipality_code,
        is_mei=is_mei,
        is_simples=is_simples,
        is_headquarters=is_headquarters,
        only_with_cellphone=only_with_cellphone,
        only_with_email=only_with_email,
    )

    # Uma linha a mais que o pedido: a existencia dela E a resposta de
    # "tem proxima pagina?", sem contar nada. Ela nao vai pra resposta.
    rows = query.offset(offset).limit(limit + 1).all()
    has_more = len(rows) > limit

    return EstablishmentPage(
        data=_serialize_many(db, rows[:limit]),
        offset=offset,
        limit=limit,
        has_more=has_more,
    )


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


def _measure_columns(model, only_with_cellphone: bool, only_with_email: bool):
    """Quais medidas do agregado respondem (total, com_celular, com_email).

    `only_with_*` nao filtra linha no agregado: a linha nao e uma empresa, e um
    balde com contagens. Restringir a populacao e trocar QUAL medida faz o
    papel de cada numero.

    Pedindo so quem tem celular, o total passa a ser `with_cellphone`, e
    "quantos desses tem email" e a intersecao -- nao `with_email`, que conta
    tambem quem tem email e nao tem celular. Era exatamente esse cruzamento que
    faltava no agregado, e por isso `only_with_cellphone` caia na tabela grande
    antes de `with_cellphone_and_email` existir.
    """
    if only_with_cellphone and only_with_email:
        both = model.with_cellphone_and_email
        return both, both, both
    if only_with_cellphone:
        return model.with_cellphone, model.with_cellphone, model.with_cellphone_and_email
    if only_with_email:
        return model.with_email, model.with_cellphone_and_email, model.with_email
    return model.total, model.with_cellphone, model.with_email


def _rollup_query(
    db: Session,
    model,
    *,
    uf_code_value: int | None,
    is_mei: bool | None,
    is_simples: bool | None,
    is_headquarters: bool | None,
):
    """Os filtros de /stats aplicados a um dos agregados.

    Serve os dois (`EstablishmentStats` e `EstablishmentCnaeStats`): as
    dimensoes que interessam aqui existem com o mesmo nome nas duas. So os
    filtros que o agregado carrega chegam aqui -- quem decide e `uncovered`.
    """
    S = model
    query = db.query(model)

    if uf_code_value is not None:
        query = query.filter(S.uf == uf_code_value)
    if is_mei is not None:
        query = query.filter(S.is_mei.is_(is_mei))
    if is_simples is not None:
        query = query.filter(S.is_simples.is_(is_simples))
    if is_headquarters is not None:
        query = query.filter(S.is_headquarters.is_(is_headquarters))

    return query


@router.get("/stats", response_model=EstablishmentStatsOut)
def stats(
    cnae_codes: list[str] | None = Query(None),
    uf: str | None = Query(None, description="Sigla do estado. Exclusivo com `municipality_code`."),
    municipality_code: str | None = Query(None, description="Código IBGE da cidade. Exclusivo com `uf`."),
    is_mei: bool | None = Query(None),
    is_simples: bool | None = Query(None),
    is_headquarters: bool | None = Query(None),
    only_with_cellphone: bool = Query(False),
    only_with_email: bool = Query(False),
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
        uf=uf,
        municipality_code=municipality_code,
        is_mei=is_mei,
        is_simples=is_simples,
        is_headquarters=is_headquarters,
        only_with_cellphone=only_with_cellphone,
        only_with_email=only_with_email,
    )

    # `_apply_filters` ja validou (exatamente um dos dois, e a UF existe).
    kind, place = _resolve_place(uf, municipality_code)
    uf_code_value = place if kind == "uf" else None

    # Um unico CNAE tem resposta exata no agregado por CNAE. Varios nao: uma
    # empresa que tenha dois deles esta em dois baldes e a soma a contaria duas
    # vezes -- ver models.EstablishmentCnaeStats.
    single_cnae = None
    if cnae_codes:
        codes = _cnae_codes_to_int(cnae_codes)
        if len(set(codes)) == 1:
            single_cnae = codes[0]

    # Filtros que agregado nenhum carrega -- ver models.EstablishmentStats.
    # `municipality_code` nao e dimensao de nenhum dos dois agregados (o grao
    # deles para na UF), entao busca por cidade sempre cai na tabela grande.
    uncovered = bool(
        uf_code_value is None
        or (cnae_codes and single_cnae is None)
        # Com filtro de CNAE o `top_cnaes` seria "os CNAEs principais de quem
        # tem este CNAE", e `main_cnae` nao e dimensao do agregado por CNAE.
        or (single_cnae is not None and include_breakdowns)
    )

    if uncovered:
        total, with_cellphone, with_email = base.with_entities(
            func.count(),
            func.count().filter(Establishment.cellphone.isnot(None)),
            func.count().filter(Establishment.email.isnot(None)),
        ).one()
        rows_source = None
        measures = None
    else:
        model = C if single_cnae is not None else S
        rollup = _rollup_query(
            db, model, uf_code_value=uf_code_value,
            is_mei=is_mei, is_simples=is_simples, is_headquarters=is_headquarters,
        )
        if single_cnae is not None:
            rollup = rollup.filter(C.cnae == single_cnae)

        measures = _measure_columns(model, only_with_cellphone, only_with_email)
        total, with_cellphone, with_email = rollup.with_entities(
            *(func.coalesce(func.sum(m), 0) for m in measures)
        ).one()
        rows_source = rollup

    by_uf_rows: list = []
    by_company_size_rows: list = []
    by_main_cnae_rows: list = []

    if include_breakdowns and rows_source is not None:
        # Sempre o agregado principal (S): CNAE unico + breakdowns cai em
        # `uncovered` la em cima, porque `main_cnae` nao e dimensao do agregado
        # por CNAE. Se isso mudar, estas tres linhas precisam escolher o model.
        #
        # A medida agrupada e a MESMA que virou `total` -- agrupar `total`
        # enquanto o total e `with_cellphone` faria as partes nao somarem o todo.
        measure = measures[0]
        by_uf_rows = rows_source.with_entities(S.uf, func.sum(measure)).group_by(S.uf).all()
        by_company_size_rows = (
            rows_source.with_entities(S.company_size, func.sum(measure))
            .group_by(S.company_size).all()
        )
        by_main_cnae_rows = (
            rows_source.with_entities(S.main_cnae, func.sum(measure))
            .filter(S.main_cnae.isnot(None)).group_by(S.main_cnae).all()
        )
    elif include_breakdowns:
        # Filtro nao coberto: GROUP BY na tabela grande, uma varredura cada.
        # Sem ORDER BY no SQL -- ordenar e trabalho pras poucas linhas
        # agregadas, feito em Python logo abaixo.
        by_uf_rows = base.with_entities(Establishment.uf, func.count()).group_by(Establishment.uf).all()
        by_company_size_rows = (
            base.with_entities(Establishment.company_size, func.count())
            .group_by(Establishment.company_size).all()
        )
        by_main_cnae_rows = (
            base.with_entities(Establishment.main_cnae, func.count())
            .filter(Establishment.main_cnae.isnot(None))
            .group_by(Establishment.main_cnae).all()
        )
    elif uf_code_value is not None:
        # Filtrando por uma UF so, `by_uf` e o proprio total -- sem consulta.
        by_uf_rows = [(uf_code_value, total)]

    # A ordenacao por contagem acontece aqui, sobre dezenas/centenas de linhas
    # ja agregadas -- de graca, e sem pedir sort nenhum ao banco.
    by_uf_rows = sorted(by_uf_rows, key=lambda r: r[1], reverse=True)
    by_company_size_rows = sorted(by_company_size_rows, key=lambda r: r[1], reverse=True)
    by_main_cnae_rows = sorted(by_main_cnae_rows, key=lambda r: r[1], reverse=True)[:top_cnaes]

    # Os group_by devolvem os codigos numericos do banco -- decodifica aqui,
    # nas poucas dezenas de linhas agregadas, nao nas 63M.
    by_uf = {(uf_name(code) or "desconhecido"): count for code, count in by_uf_rows}

    by_region: dict[str, int] = {}
    for code, count in by_uf_rows:
        region_key = UF_TO_REGION.get(uf_name(code), "desconhecida")
        by_region[region_key] = by_region.get(region_key, 0) + count

    cnae_width = CODE_WIDTHS["cnae"]
    return EstablishmentStatsOut(
        total=total,
        with_cellphone=with_cellphone,
        with_email=with_email,
        by_uf=by_uf,
        by_region=by_region,
        by_company_size={(_code(size) or "desconhecido"): count for size, count in by_company_size_rows},
        top_cnaes=[{"cnae_code": f"{code:0{cnae_width}d}", "total": count} for code, count in by_main_cnae_rows],
    )
