from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import ceps
from app.db import get_db
from app.models import Cep
from app.pagination import (
    SqlSortKey,
    decode_cursor,
    encode_cursor,
    keyset_sql,
    make_fingerprint,
    order_by_sql,
)
from app.regions import ufs_for_regiao
from app.schemas import EnderecoPageOut

router = APIRouter(prefix="/enderecos", tags=["enderecos"])

# Base oficial e gratuita dos Correios (e-DNE Básico). É um model nosso
# (models.CorreiosCep), mas as buscas aqui são SQL direto por causa do cálculo
# de distância e dos filtros dinâmicos. `cep` é INTEGER no banco e sai
# formatado com zero à esquerda -- ver ceps.select_columns.
CORREIOS_CEP_TABLE = ceps.TABLE
CORREIOS_CEP_COLUMNS = ceps.select_columns()
_CORREIOS_CEP_COLUMNS_PREFIXED_E = ceps.select_columns("e")

# UF -> nome, IBGE não muda isso com frequência (dado estático).
ESTADOS = [
    {"uf": "AC", "nome": "Acre"}, {"uf": "AL", "nome": "Alagoas"},
    {"uf": "AP", "nome": "Amapá"}, {"uf": "AM", "nome": "Amazonas"},
    {"uf": "BA", "nome": "Bahia"}, {"uf": "CE", "nome": "Ceará"},
    {"uf": "DF", "nome": "Distrito Federal"}, {"uf": "ES", "nome": "Espírito Santo"},
    {"uf": "GO", "nome": "Goiás"}, {"uf": "MA", "nome": "Maranhão"},
    {"uf": "MT", "nome": "Mato Grosso"}, {"uf": "MS", "nome": "Mato Grosso do Sul"},
    {"uf": "MG", "nome": "Minas Gerais"}, {"uf": "PA", "nome": "Pará"},
    {"uf": "PB", "nome": "Paraíba"}, {"uf": "PR", "nome": "Paraná"},
    {"uf": "PE", "nome": "Pernambuco"}, {"uf": "PI", "nome": "Piauí"},
    {"uf": "RJ", "nome": "Rio de Janeiro"}, {"uf": "RN", "nome": "Rio Grande do Norte"},
    {"uf": "RS", "nome": "Rio Grande do Sul"}, {"uf": "RO", "nome": "Rondônia"},
    {"uf": "RR", "nome": "Roraima"}, {"uf": "SC", "nome": "Santa Catarina"},
    {"uf": "SP", "nome": "São Paulo"}, {"uf": "SE", "nome": "Sergipe"},
    {"uf": "TO", "nome": "Tocantins"},
]


@router.get("/estados")
def estados():
    return ESTADOS


@router.get("/cep/{cep}")
def buscar_cep(cep: str, db: Session = Depends(get_db)):
    """Consulta endereço por CEP na base oficial dos Correios (e-DNE). Se
    não achar (CEP fora da base importada, ou import ainda não rodou),
    consulta o ViaCEP (gratuito, sem chave) e grava o resultado na mesma
    tabela -- assim a próxima consulta pro mesmo CEP já vem local.

    Um CEP adicionado aqui via ViaCEP sobrevive ao próximo `import-ceps`: o
    import é upsert, não substituição (ver app/ceps.py).
    """
    value = ceps.to_int(cep)
    if value is None:
        raise HTTPException(422, "CEP deve ter 8 dígitos")

    from_correios = _query_correios_cep(db, value)
    if from_correios:
        coords = _get_or_fetch_coordinates(db, value)
        from_correios["latitude"] = coords[0] if coords else None
        from_correios["longitude"] = coords[1] if coords else None
        return from_correios

    try:
        response = httpx.get(f"https://viacep.com.br/ws/{ceps.to_str(value)}/json/", timeout=10)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Falha ao consultar o provedor de CEP: {exc}") from exc

    if data.get("erro"):
        raise HTTPException(404, "CEP não encontrado")

    municipio_cod_ibge = int(data["ibge"]) if data.get("ibge") else None
    if municipio_cod_ibge is not None:
        # `municipio_cod_ibge` agora tem FOREIGN KEY pra `municipios.ibge_code`
        # -- um código que o ViaCEP mande e que não exista lá (município
        # renomeado/fundido, ou `import-municipios` nunca rodou) faria o
        # INSERT abaixo falhar inteiro. NULL nesse caso, mesmo tratamento que
        # o upsert em massa já dá a código órfão (ver app.ceps.upsert_from).
        known = db.execute(text("SELECT 1 FROM municipios WHERE ibge_code = :c"), {"c": municipio_cod_ibge}).scalar()
        if not known:
            municipio_cod_ibge = None

    row = {
        "cep": value,
        "logradouro": data.get("logradouro") or None,
        "complemento": data.get("complemento") or None,
        "bairro": data.get("bairro") or None,
        "municipio": data.get("localidade"),
        "municipio_cod_ibge": municipio_cod_ibge,
        "uf": data.get("uf"),
        "nome": None,
    }

    # Só persiste se o ViaCEP trouxe município e UF; senão devolve a resposta
    # sem gravar. Não é mais uma restrição de schema (essas colunas são
    # nullable desde a fusão com as coordenadas), é critério: gravar um CEP sem
    # município nenhum não ajudaria nenhuma busca.
    if row["municipio"] and row["uf"]:
        db.execute(
            text(f"""
                INSERT INTO {CORREIOS_CEP_TABLE} ({", ".join(ceps.COLUMNS)})
                VALUES (:cep, :logradouro, :complemento, :bairro, :municipio, :municipio_cod_ibge, :uf, :nome)
                ON CONFLICT (cep) DO UPDATE SET
                    logradouro = excluded.logradouro, complemento = excluded.complemento,
                    bairro = excluded.bairro, municipio = excluded.municipio,
                    municipio_cod_ibge = excluded.municipio_cod_ibge, uf = excluded.uf
            """),
            row,
        )
        db.commit()

    coords = _get_or_fetch_coordinates(db, value)
    # De volta pras 8 posições com zero à esquerda, que é o que a API expõe.
    row["cep"] = ceps.to_str(value)
    row["latitude"] = coords[0] if coords else None
    row["longitude"] = coords[1] if coords else None
    return row


# Haversine em SQL puro (sem PostGIS) -- distância em km entre um ponto
# fixo (:lat/:lon) e uma coordenada `lat_final`/`lon_final` (ver
# _COORD_JOIN_SQL abaixo).
_DISTANCE_KM_SQL = """
    6371 * acos(
        greatest(-1, least(1,
            cos(radians(:lat)) * cos(radians(lat_final)) * cos(radians(lon_final) - radians(:lon))
            + sin(radians(:lat)) * sin(radians(lat_final))
        ))
    )
"""

# Coordenada exata do CEP (na própria linha, vinda do `import-ceps-osm` em
# massa ou cacheada sob demanda em GET /enderecos/cep/{cep}) quando
# existir; senão o centroide do município
# (municipios.latitude/longitude, ver `import-municipios-geo`) como
# fallback de baixa precisão -- sem isso, a busca por proximidade só
# funcionaria pra CEPs já consultados individualmente, o que na prática é
# quase nenhum. `exata` diz qual das duas foi usada.
_COORD_JOIN_SQL = """
    LEFT JOIN municipios mu ON mu.ibge_code = e.municipio_cod_ibge
    CROSS JOIN LATERAL (
        SELECT
            coalesce(e.latitude, mu.latitude) AS lat_final,
            coalesce(e.longitude, mu.longitude) AS lon_final,
            (e.latitude IS NOT NULL) AS exata
    ) coord
"""


@router.get("/buscar", response_model=EnderecoPageOut)
def buscar_endereco(
    logradouro: str | None = Query(None),
    bairro: str | None = Query(None),
    municipio: str | None = Query(None),
    uf: list[str] | None = Query(None, description="Uma ou mais UFs, ex: ?uf=SP&uf=RJ"),
    regiao: str | None = Query(None, description="norte/nordeste/centro-oeste/sudeste/sul, combina com uf"),
    municipio_cod_ibge: int | None = Query(None),
    lat: float | None = Query(None, description="Ordena por distância a partir daqui (combine com lon)"),
    lon: float | None = Query(None, description="Ordena por distância a partir daqui (combine com lat)"),
    cursor: str | None = Query(
        None,
        description="Cursor da página anterior (`next_cursor`). Sem ele, começa do início. "
                    "Os filtros precisam ser os mesmos que geraram o cursor.",
    ),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Busca por texto/filtros na base oficial dos Correios (e-DNE) -- precisa
    do `import-ceps` já ter rodado, senão a tabela está vazia e o resultado
    também.

    Passando `lat`+`lon`, ordena por distância em vez de município/logradouro
    -- usa a coordenada exata do CEP quando já foi consultado (ver
    `/enderecos/cep/{cep}`), senão cai pro centroide do município (ver
    `import-municipios-geo`); cada item vem com `exata: true/false` dizendo
    qual das duas foi usada. CEPs sem nenhuma das duas (município ainda não
    geocodificado) não entram no resultado.

    Paginada por cursor: repasse o `next_cursor` da resposta em `?cursor=` pra
    próxima página. Ver app/pagination.py."""
    conditions = []
    # `limit + 1`: a linha extra so serve pra saber se existe proxima pagina,
    # e e descartada antes de responder -- ver app.pagination.paginate.
    params: dict = {"limit": limit + 1}

    if logradouro:
        conditions.append("logradouro ILIKE :logradouro")
        params["logradouro"] = f"%{logradouro}%"
    if bairro:
        conditions.append("bairro ILIKE :bairro")
        params["bairro"] = f"%{bairro}%"
    if municipio:
        conditions.append("municipio ILIKE :municipio")
        params["municipio"] = f"%{municipio}%"
    if municipio_cod_ibge:
        conditions.append("municipio_cod_ibge = :municipio_cod_ibge")
        params["municipio_cod_ibge"] = municipio_cod_ibge

    ufs = {u.upper() for u in (uf or [])}
    if regiao:
        regiao_ufs = ufs_for_regiao(regiao)
        if not regiao_ufs:
            raise HTTPException(422, f"Região desconhecida: {regiao!r} (use norte/nordeste/centro-oeste/sudeste/sul)")
        ufs |= set(regiao_ufs)
    if ufs:
        conditions.append("uf = ANY(:ufs)")
        params["ufs"] = list(ufs)

    fingerprint = make_fingerprint(
        logradouro=logradouro, bairro=bairro, municipio=municipio, uf=sorted(ufs),
        regiao=regiao, municipio_cod_ibge=municipio_cod_ibge, lat=lat, lon=lon,
    )

    order_by_distance = lat is not None and lon is not None
    if order_by_distance:
        params["lat"], params["lon"] = lat, lon
        conditions = conditions + ["coord.lat_final IS NOT NULL"]
        # A distancia nao e coluna, entao o keyset compara contra a expressao
        # inteira -- por isso `_DISTANCE_KM_SQL` e nao o alias: o alias do
        # SELECT nao existe no WHERE. Nunca e NULL aqui (`lat_final IS NOT
        # NULL` ja esta no filtro), daí `nullable=False`.
        keys = [
            SqlSortKey(_DISTANCE_KM_SQL, "distancia_km", nullable=False),
            SqlSortKey("e.cep", "cep", nullable=False),
        ]
    else:
        # So a PK, pelo mesmo motivo de /socios/buscar: os filtros daqui sao
        # todos `ILIKE '%x%'`, e ordenar por municipio/logradouro faria o
        # planner caminhar esse indice testando linha a linha.
        keys = [SqlSortKey("cep", "cep", nullable=False)]

    if cursor:
        position = decode_cursor(cursor, fingerprint)
        keyset = keyset_sql(keys, position.values, params)
        if keyset is None:
            return EnderecoPageOut(data=[], next_cursor=None, limit=limit)
        conditions = conditions + [keyset]

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    if order_by_distance:
        query_sql = f"""
            SELECT {_CORREIOS_CEP_COLUMNS_PREFIXED_E}, coord.exata, {_DISTANCE_KM_SQL} AS distancia_km
            FROM {CORREIOS_CEP_TABLE} e
            {_COORD_JOIN_SQL}
            {where_sql}
            ORDER BY {order_by_sql(keys)}
            LIMIT :limit
        """
    else:
        query_sql = f"""
            SELECT {CORREIOS_CEP_COLUMNS} FROM {CORREIOS_CEP_TABLE}
            {where_sql}
            ORDER BY {order_by_sql(keys)}
            LIMIT :limit
        """

    rows = [dict(row._mapping) for row in db.execute(text(query_sql), params)]

    if len(rows) <= limit:
        return EnderecoPageOut(data=rows, next_cursor=None, limit=limit)

    rows = rows[:limit]
    # `cep` sai do SELECT como texto com zero a esquerda (ver
    # ceps.select_columns), mas a coluna e INTEGER -- o cursor tem que levar o
    # inteiro, senao a comparacao no WHERE seria texto contra int.
    last = rows[-1]
    values = tuple(
        ceps.to_int(last[k.alias]) if k.alias == "cep" else last[k.alias]
        for k in keys
    )
    return EnderecoPageOut(data=rows, next_cursor=encode_cursor(values, fingerprint), limit=limit)


@router.get("/proximos")
def enderecos_proximos(
    lat: float = Query(..., description="Latitude do ponto de referência"),
    lon: float = Query(..., description="Longitude do ponto de referência"),
    raio_km: float = Query(5, gt=0, le=200, description="Raio de busca em km"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Lista CEPs dentro de um raio, ordenados por distância (mais próximo
    primeiro). Usa a coordenada exata do CEP quando já foi consultado (ver
    `/enderecos/cep/{cep}`), senão cai pro centroide do município (ver
    `import-municipios-geo`) -- cada item vem com `exata: true/false`
    dizendo qual das duas foi usada. Município ainda não geocodificado
    fica de fora até `import-municipios-geo` rodar."""
    result = db.execute(
        text(f"""
            SELECT {_CORREIOS_CEP_COLUMNS_PREFIXED_E}, coord.exata, {_DISTANCE_KM_SQL} AS distancia_km
            FROM {CORREIOS_CEP_TABLE} e
            {_COORD_JOIN_SQL}
            WHERE coord.lat_final IS NOT NULL AND {_DISTANCE_KM_SQL} <= :raio_km
            ORDER BY distancia_km ASC
            LIMIT :limit
        """),
        {"lat": lat, "lon": lon, "raio_km": raio_km, "limit": limit},
    )
    return [dict(row._mapping) for row in result]


def _get_or_fetch_coordinates(db: Session, cep: int) -> tuple[float, float] | None:
    """Lat/long do CEP. Se ainda não foi buscada, tenta a BrasilAPI (gratuita,
    sem chave) -- best-effort: nunca falha a consulta de endereço por causa
    disso, e grava mesmo quando não acha coordenada (`coord_source` marca a
    tentativa) pra não bater na API de novo pelo mesmo CEP."""
    cached = db.get(Cep, cep)
    if cached and cached.coord_source:
        if cached.latitude is None or cached.longitude is None:
            return None
        return float(cached.latitude), float(cached.longitude)

    latitude = longitude = None
    source = "brasilapi_sem_coordenada"
    try:
        response = httpx.get(f"https://brasilapi.com.br/api/cep/v2/{ceps.to_str(cep)}", timeout=8)
        if response.status_code == 200:
            coords = response.json().get("location", {}).get("coordinates", {})
            if coords.get("latitude") and coords.get("longitude"):
                latitude, longitude = float(coords["latitude"]), float(coords["longitude"])
                source = "brasilapi"
    except (httpx.HTTPError, ValueError, TypeError):
        return None  # falha na consulta -- não cacheia, tenta de novo na próxima

    # A linha pode já existir (veio do e-DNE, sem coordenada) ou não existir
    # (CEP que só a BrasilAPI conhece) -- upsert cobre os dois, e mexe só nas
    # colunas de coordenada, deixando o endereço intacto.
    stmt = pg_insert(Cep.__table__).values(
        cep=cep, latitude=latitude, longitude=longitude, coord_source=source,
        coord_updated_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["cep"],
        set_={"latitude": stmt.excluded.latitude, "longitude": stmt.excluded.longitude,
              "coord_source": stmt.excluded.coord_source,
              "coord_updated_at": stmt.excluded.coord_updated_at},
    )
    db.execute(stmt)
    db.commit()

    return (latitude, longitude) if latitude and longitude else None


def _query_correios_cep(db: Session, cep: int) -> dict | None:
    result = db.execute(
        text(f"SELECT {CORREIOS_CEP_COLUMNS} FROM {CORREIOS_CEP_TABLE} WHERE cep = :cep LIMIT 1"),
        {"cep": cep},
    ).first()
    return dict(result._mapping) if result else None
