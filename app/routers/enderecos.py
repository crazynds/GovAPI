from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import ceps
from app.db import get_db
from app.models import CepCoordenada
from app.regions import ufs_for_regiao

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

    row = {
        "cep": value,
        "logradouro": data.get("logradouro") or None,
        "complemento": data.get("complemento") or None,
        "bairro": data.get("bairro") or None,
        "municipio": data.get("localidade"),
        "municipio_cod_ibge": int(data["ibge"]) if data.get("ibge") else None,
        "uf": data.get("uf"),
        "nome": None,
    }

    # municipio/uf/municipio_cod_ibge são NOT NULL na tabela do e-DNE -- só
    # persiste se o ViaCEP realmente trouxe esses três; senão só devolve a
    # resposta sem gravar (ainda é melhor que dar erro pro cliente).
    if row["municipio"] and row["uf"] and row["municipio_cod_ibge"]:
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

# Coordenada exata do CEP (cep_coordenadas, cacheada sob demanda em
# GET /enderecos/cep/{cep}) quando existir; senão o centroide do município
# (municipios.latitude/longitude, ver `import-municipios-geo`) como
# fallback de baixa precisão -- sem isso, a busca por proximidade só
# funcionaria pra CEPs já consultados individualmente, o que na prática é
# quase nenhum. `exata` diz qual das duas foi usada.
_COORD_JOIN_SQL = """
    LEFT JOIN cep_coordenadas cc ON cc.cep = e.cep
    LEFT JOIN municipios mu ON mu.ibge_code = e.municipio_cod_ibge::text
    CROSS JOIN LATERAL (
        SELECT
            coalesce(cc.latitude, mu.latitude) AS lat_final,
            coalesce(cc.longitude, mu.longitude) AS lon_final,
            (cc.latitude IS NOT NULL) AS exata
    ) coord
"""


@router.get("/buscar")
def buscar_endereco(
    logradouro: str | None = Query(None),
    bairro: str | None = Query(None),
    municipio: str | None = Query(None),
    uf: list[str] | None = Query(None, description="Uma ou mais UFs, ex: ?uf=SP&uf=RJ"),
    regiao: str | None = Query(None, description="norte/nordeste/centro-oeste/sudeste/sul, combina com uf"),
    municipio_cod_ibge: int | None = Query(None),
    lat: float | None = Query(None, description="Ordena por distância a partir daqui (combine com lon)"),
    lon: float | None = Query(None, description="Ordena por distância a partir daqui (combine com lat)"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
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
    geocodificado) não entram no resultado."""
    conditions = []
    params: dict = {"limit": per_page, "offset": (page - 1) * per_page}

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

    order_by_distance = lat is not None and lon is not None
    if order_by_distance:
        params["lat"], params["lon"] = lat, lon
        distance_conditions = conditions + ["coord.lat_final IS NOT NULL"]
        query_sql = f"""
            SELECT {_CORREIOS_CEP_COLUMNS_PREFIXED_E}, coord.exata, {_DISTANCE_KM_SQL} AS distancia_km
            FROM {CORREIOS_CEP_TABLE} e
            {_COORD_JOIN_SQL}
            WHERE {' AND '.join(distance_conditions)}
            ORDER BY distancia_km ASC
            LIMIT :limit OFFSET :offset
        """
    else:
        query_sql = f"""
            SELECT {CORREIOS_CEP_COLUMNS} FROM {CORREIOS_CEP_TABLE}
            {f"WHERE {' AND '.join(conditions)}" if conditions else ""}
            ORDER BY municipio, logradouro
            LIMIT :limit OFFSET :offset
        """

    result = db.execute(text(query_sql), params)
    return [dict(row._mapping) for row in result]


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
    """Lat/long por CEP, cacheadas em `cep_coordenadas` (tabela nossa,
    imune ao rebuild do e-DNE). Se ainda não tem, busca na BrasilAPI
    (gratuita, sem chave) -- best-effort: nunca falha a consulta de
    endereço por causa disso, e cacheia mesmo quando não acha coordenada
    pra não bater na BrasilAPI de novo pro mesmo CEP sem coordenada."""
    cached = db.get(CepCoordenada, cep)
    if cached:
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

    stmt = pg_insert(CepCoordenada.__table__).values(
        cep=cep, latitude=latitude, longitude=longitude, source=source,
        updated_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["cep"],
        set_={"latitude": stmt.excluded.latitude, "longitude": stmt.excluded.longitude,
              "source": stmt.excluded.source, "updated_at": stmt.excluded.updated_at},
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
