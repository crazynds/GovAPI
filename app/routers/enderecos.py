import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.db import get_db
from app.regions import ufs_for_regiao

router = APIRouter(prefix="/enderecos", tags=["enderecos"])

# Nome/esquema da tabela unificada criada pelo comando `import-ceps` (ver
# app/cli.py) via edne-correios-loader -- base oficial e gratuita dos
# Correios (e-DNE Básico). Não é um model SQLAlchemy nosso (a lib é dona da
# tabela e a reconstrói do zero a cada import), então falamos com ela via
# SQL direto -- inclusive pra criar via CREATE TABLE IF NOT EXISTS antes do
# primeiro `import-ceps` já ter rodado (ver _ensure_correios_cep_table).
CORREIOS_CEP_TABLE = "correios_cep"
CORREIOS_CEP_COLUMNS = "cep, logradouro, complemento, bairro, municipio, municipio_cod_ibge, uf, nome"

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

    Nota: como `import-ceps` reconstrói a tabela do zero a cada execução,
    um CEP adicionado aqui via ViaCEP é perdido no próximo import oficial
    -- aceitável, já que o e-DNE atualizado tende a cobrir o que faltava.
    """
    digits = re.sub(r"\D", "", cep)
    if len(digits) != 8:
        raise HTTPException(422, "CEP deve ter 8 dígitos")

    from_correios = _query_correios_cep(db, digits)
    if from_correios:
        return from_correios

    try:
        response = httpx.get(f"https://viacep.com.br/ws/{digits}/json/", timeout=10)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Falha ao consultar o provedor de CEP: {exc}") from exc

    if data.get("erro"):
        raise HTTPException(404, "CEP não encontrado")

    row = {
        "cep": digits,
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
        _ensure_correios_cep_table(db)
        db.execute(
            text(f"""
                INSERT INTO {CORREIOS_CEP_TABLE} ({CORREIOS_CEP_COLUMNS})
                VALUES (:cep, :logradouro, :complemento, :bairro, :municipio, :municipio_cod_ibge, :uf, :nome)
                ON CONFLICT (cep) DO UPDATE SET
                    logradouro = excluded.logradouro, complemento = excluded.complemento,
                    bairro = excluded.bairro, municipio = excluded.municipio,
                    municipio_cod_ibge = excluded.municipio_cod_ibge, uf = excluded.uf
            """),
            row,
        )
        db.commit()

    return row


@router.get("/buscar")
def buscar_endereco(
    logradouro: str | None = Query(None),
    bairro: str | None = Query(None),
    municipio: str | None = Query(None),
    uf: list[str] | None = Query(None, description="Uma ou mais UFs, ex: ?uf=SP&uf=RJ"),
    regiao: str | None = Query(None, description="norte/nordeste/centro-oeste/sudeste/sul, combina com uf"),
    municipio_cod_ibge: int | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Busca por texto/filtros na base oficial dos Correios (e-DNE) -- precisa
    do `import-ceps` já ter rodado, senão retorna lista vazia."""
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

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    try:
        result = db.execute(
            text(
                f"SELECT {CORREIOS_CEP_COLUMNS} FROM {CORREIOS_CEP_TABLE} {where} "
                "ORDER BY municipio, logradouro LIMIT :limit OFFSET :offset"
            ),
            params,
        )
        return [dict(row._mapping) for row in result]
    except ProgrammingError:
        db.rollback()
        return []


def _ensure_correios_cep_table(db: Session) -> None:
    """Cria a tabela se `import-ceps` nunca rodou -- mesmo esquema usado
    pelo edne-correios-loader, pra ficar compatível quando ele rodar depois."""
    db.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {CORREIOS_CEP_TABLE} (
            cep VARCHAR(8) PRIMARY KEY,
            logradouro VARCHAR(100),
            complemento VARCHAR(100),
            bairro VARCHAR(72),
            municipio VARCHAR(72) NOT NULL,
            municipio_cod_ibge INTEGER NOT NULL,
            uf VARCHAR(2) NOT NULL,
            nome VARCHAR(100)
        )
    """))


def _query_correios_cep(db: Session, cep: str) -> dict | None:
    try:
        result = db.execute(
            text(f"SELECT {CORREIOS_CEP_COLUMNS} FROM {CORREIOS_CEP_TABLE} WHERE cep = :cep LIMIT 1"),
            {"cep": cep},
        ).first()
    except ProgrammingError:
        # Tabela ainda não existe -- `import-ceps` nunca rodou nem nenhum
        # fallback de ViaCEP criou ela ainda. Cai pro ViaCEP normalmente.
        db.rollback()
        return None

    return dict(result._mapping) if result else None
