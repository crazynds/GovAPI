"""Busca de estabelecimentos.

O banco guarda tudo em tipo numerico compacto (CNPJ em base 36, CNAE/UF/porte
como inteiro, telefone sem o +55 -- ver app/models.py). A traducao pros
formatos publicos acontece toda aqui: `_serialize` na saida, `_apply_filters`
na entrada. O contrato da API e o mesmo de quando as colunas eram texto.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
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
from app.regions import UF_TO_REGION, uf_code, uf_name, ufs_for_region
from app.pagination import SortKey, make_fingerprint, paginate
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
STATUS_CODES_BY_LABEL = {label: code for code, label in STATUS_LABELS.items()}


def _code(value: int | None, width: int = 2) -> str | None:
    """Codigo numerico -> a string com zero a esquerda que a API sempre expos."""
    return f"{value:0{width}d}" if value is not None else None


def _resolve_status_codes(values: list[str]) -> list[int]:
    """Aceita label ("ativa") ou codigo ("02"/"2"), devolve o inteiro do banco."""
    codes = []
    for raw in values:
        value = raw.strip().lower()
        if value in STATUS_CODES_BY_LABEL:
            codes.append(STATUS_CODES_BY_LABEL[value])
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


def _secondary_cnaes(db: Session, cnpjs: set[int]) -> dict[int, list[int]]:
    """Os CNAEs secundarios das empresas da pagina, numa query so.

    Era uma coluna `integer[]` em `establishments`; virou linha em
    `establishment_cnaes` pra busca poder indexar (ver models.EstablishmentCnae).
    A leitura ficou uma query a mais por pagina, resolvida pelo prefixo `cnpj`
    da PK da tabela N:N sobre as ~25 empresas da resposta.
    """
    if not cnpjs:
        return {}

    rows = (
        db.query(EstablishmentCnae.cnpj, EstablishmentCnae.cnae)
        .filter(EstablishmentCnae.cnpj.in_(cnpjs), EstablishmentCnae.is_main.is_(False))
        .all()
    )
    grouped: dict[int, list[int]] = {}
    for cnpj, cnae in rows:
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
    secondary = _secondary_cnaes(db, {e.cnpj for e in items})

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


def _apply_filters(
    query: ORMQuery,
    *,
    cnae_codes: list[str] | None,
    uf: list[str] | None,
    region: str | None,
    municipality_codes: list[str] | None,
    company_size: list[str] | None,
    is_mei: bool | None,
    is_simples: bool | None,
    is_headquarters: bool | None,
    name: str | None,
    status: list[str] | None,
    only_with_cellphone: bool,
    only_with_email: bool,
    has_phone: bool | None,
    opened_after: date | None,
    opened_before: date | None,
) -> ORMQuery:
    # UF antes do CNAE porque o filtro de CNAE reaproveita os codigos: eles vao
    # empurrados pra DENTRO da subquery da tabela N:N (ver abaixo).
    ufs = set(uf or [])
    if region:
        region_ufs = ufs_for_region(region)
        if not region_ufs:
            raise HTTPException(422, f"Região desconhecida: {region!r} (use norte/nordeste/centro-oeste/sudeste/sul)")
        ufs |= set(region_ufs)
    uf_codes: list[int] = []
    if ufs:
        uf_codes = [uf_code(u) for u in ufs]
        unknown = [u for u, c in zip(ufs, uf_codes) if c is None]
        if unknown:
            raise HTTPException(422, f"UF desconhecida: {sorted(unknown)}")
        query = query.filter(Establishment.uf.in_(uf_codes))

    # Cidade ANTES do CNAE, como a UF e pelo mesmo motivo: `municipality_id` vai
    # empurrado pra DENTRO da subquery da tabela N:N.
    municipality_ids: list[int] = []
    if municipality_codes:
        receita_codes = [int(c) for c in municipality_codes if str(c).strip().isdigit()]
        if not receita_codes:
            raise HTTPException(422, f"Código de município inválido: {municipality_codes}")
        # Resolve receita_code -> id AQUI, numa consulta a parte, em vez de
        # deixar como subquery correlacionada (`.has(...)`). A tabela tem ~5.570
        # linhas, entao isso e uma consulta de microssegundos; como `.has()`,
        # virava um EXISTS reavaliado por linha candidata de `establishments` --
        # sem indice util, filtrar por cidade saia mais caro que nao filtrar.
        municipality_ids = [
            row[0] for row in query.session.query(Municipality.id)
            .filter(Municipality.receita_code.in_(receita_codes)).all()
        ]
        if not municipality_ids:
            raise HTTPException(422, f"Município não encontrado: {municipality_codes}")
        query = query.filter(Establishment.municipality_id.in_(municipality_ids))

    if cnae_codes:
        # Semi-join na tabela N:N: "as empresas que tem algum destes CNAEs".
        # Antes era `main_cnae = X OR secondary_cnaes && [X]`, um OR entre btree
        # e GIN que nao produz saida ordenada por `cnpj` -- e com `ORDER BY cnpj
        # LIMIT n` o planner acabava varrendo a PK filtrando linha a linha, as
        # 63M. Aqui e igualdade numa coluna so.
        #
        # Os filtros de recorte sao REPETIDOS dentro da subquery, mesmo ja
        # estando no WHERE de fora. E o ponto todo de a tabela N:N carregar
        # copias de `uf`, `municipality_id` e `has_cellphone`: com elas aqui, um
        # indice unico devolve os candidatos ja filtrados e em ordem de `cnpj`;
        # sem elas, o banco traria todos os candidatos do CNAE no pais inteiro
        # pra descobrir na tabela grande, um por um, quais servem.
        #
        # Um `IN` numa coluna so, e nao um ramo por CNAE unido por UNION ALL.
        # O UNION ALL foi tentado e MEDIDO (8M linhas sinteticas): o Postgres
        # nao gera MergeAppend a partir dele -- sai `Append` + `Sort`, ou seja o
        # mesmo trabalho do `IN` com um no a mais. O `IN` ficou mais rapido
        # (1.9ms contra 2.7ms) e e mais simples.
        #
        # MergeAppend so aparece com `ORDER BY cnpj LIMIT n` DENTRO de cada
        # ramo (medido: 0.23ms, lendo 27 linhas em vez de 19 mil). Mas isso e
        # INCORRETO aqui: a subquery nao carrega todos os filtros da busca --
        # porte, MEI, situacao, nome e data ficam no WHERE de fora. Um ramo que
        # devolve so as 25 primeiras entrega candidatos que o filtro externo
        # ainda pode descartar, e a pagina viria curta calada, perdendo linhas.
        # So daria pra usar se a subquery fosse o filtro inteiro.
        #
        # O que sobra do `Sort` aqui e barato porque `municipality_id`/`uf`
        # podam o conjunto ANTES dele: sao dezenas de milhares de linhas, nao
        # milhoes. O caso que continua caro e multi-CNAE sem cidade nem UF --
        # ver o comentario no fim de _apply_filters.
        codes = _cnae_codes_to_int(cnae_codes)
        codes_matching = select(EstablishmentCnae.cnpj).where(EstablishmentCnae.cnae.in_(codes))
        if municipality_ids:
            # Cidade e mais seletiva que UF e ja a implica, entao aqui vai so
            # ela -- e o que faz a subquery cair no indice
            # (cnae, municipality_id, cnpj). Somar a UF junto so daria ao
            # planner motivo pra hesitar entre os dois indices; o predicado de
            # UF continua no WHERE de fora, onde custa nada.
            codes_matching = codes_matching.where(
                EstablishmentCnae.municipality_id.in_(municipality_ids))
        elif uf_codes:
            codes_matching = codes_matching.where(EstablishmentCnae.uf.in_(uf_codes))
        if only_with_cellphone:
            # A coluna crua, sem `== True`/`IS TRUE`: e a forma que casa
            # LITERALMENTE com o predicado do indice parcial
            # (`... WHERE has_cellphone`). O Postgres so usa um indice parcial
            # se conseguir provar o predicado a partir do WHERE, e escrever isso
            # de outro jeito e apostar nessa prova sem precisar.
            codes_matching = codes_matching.where(EstablishmentCnae.has_cellphone)
        matching = codes_matching
        query = query.filter(Establishment.cnpj.in_(matching))

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

    if status:
        query = query.filter(Establishment.registration_status.in_(_resolve_status_codes(status)))

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
    cnae_codes: list[str] | None = Query(
        None,
        description="Códigos CNAE (principal ou secundário). Com vários, casa quem tem "
                    "ao menos um deles.",
    ),
    uf: list[str] | None = Query(None, description="Uma ou mais UFs, ex: ?uf=SP&uf=RJ"),
    region: str | None = Query(None, description="norte/nordeste/centro-oeste/sudeste/sul, combina com uf"),
    municipality_codes: list[str] | None = Query(None),
    company_size: list[str] | None = Query(None, description="Códigos de porte (00/01/03/05), aceita múltiplos"),
    is_mei: bool | None = Query(None),
    is_simples: bool | None = Query(None),
    is_headquarters: bool | None = Query(None),
    name: str | None = Query(None, description="Busca por razão social ou name fantasia"),
    status: list[str] | None = Query(
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
        uf=uf,
        region=region,
        municipality_codes=municipality_codes,
        company_size=company_size,
        is_mei=is_mei,
        is_simples=is_simples,
        is_headquarters=is_headquarters,
        name=name,
        status=status,
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
            cnae_codes=cnae_codes, uf=uf, region=region,
            municipality_codes=municipality_codes, company_size=company_size, is_mei=is_mei,
            is_simples=is_simples, is_headquarters=is_headquarters, name=name,
            status=status, only_with_cellphone=only_with_cellphone,
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
    uf_codes: list[int],
    company_size: list[str] | None,
    status: list[str] | None,
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

    if uf_codes:
        query = query.filter(S.uf.in_(uf_codes))
    if company_size:
        query = query.filter(S.company_size.in_([int(c) for c in company_size if str(c).strip().isdigit()]))
    if status:
        query = query.filter(S.registration_status.in_(_resolve_status_codes(status)))
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
    uf: list[str] | None = Query(None),
    region: str | None = Query(None),
    municipality_codes: list[str] | None = Query(None),
    company_size: list[str] | None = Query(None),
    is_mei: bool | None = Query(None),
    is_simples: bool | None = Query(None),
    is_headquarters: bool | None = Query(None),
    name: str | None = Query(None),
    status: list[str] | None = Query(None),
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
        uf=uf,
        region=region,
        municipality_codes=municipality_codes,
        company_size=company_size,
        is_mei=is_mei,
        is_simples=is_simples,
        is_headquarters=is_headquarters,
        name=name,
        status=status,
        only_with_cellphone=only_with_cellphone,
        only_with_email=only_with_email,
        has_phone=has_phone,
        opened_after=opened_after,
        opened_before=opened_before,
    )

    # As UFs que sobraram depois de resolver `region` -- mesma conta que
    # `_apply_filters` faz. Serve pra saber quando `by_uf` e deduzivel do total
    # em vez de precisar de um GROUP BY. `_apply_filters` ja validou os nomes,
    # entao aqui nao ha UF desconhecida.
    resolved_ufs = set(uf or [])
    if region:
        resolved_ufs |= set(ufs_for_region(region))
    uf_codes = [uf_code(u) for u in sorted(resolved_ufs)]

    # Um unico CNAE tem resposta exata no agregado por CNAE. Varios nao: uma
    # empresa que tenha dois deles esta em dois baldes e a soma a contaria duas
    # vezes -- ver models.EstablishmentCnaeStats.
    single_cnae = None
    if cnae_codes:
        codes = _cnae_codes_to_int(cnae_codes)
        if len(set(codes)) == 1:
            single_cnae = codes[0]

    # Filtros que agregado nenhum carrega -- ver models.EstablishmentStats.
    uncovered = bool(
        name or municipality_codes or opened_after or opened_before
        or has_phone is not None
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
            db, model, uf_codes=uf_codes, company_size=company_size, status=status,
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
    elif len(uf_codes) == 1:
        # Filtrando por uma UF so, `by_uf` e o proprio total -- sem consulta.
        by_uf_rows = [(uf_codes[0], total)]

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
