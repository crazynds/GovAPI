"""Bootstrap de `municipalities` a partir da API de Localidades do IBGE (sem
chave, uma request só) -- ibge_code + nome + UF exatos, direto da fonte,
sem fuzzy match nenhum.

Roda ANTES de tudo (CEPs, CNPJ): é o que dá a `postal_codes` uma FOREIGN KEY
de verdade pra `municipalities` (por ibge_code), fechando a cadeia
`establishments.cep -> postal_codes.municipality_ibge_code -> municipalities.ibge_code`.

O código de município da própria Receita (`municipalities.receita_code`) continua
vindo do `Municipios.zip` dela (grupo "reference" do import-cnpj, que roda
DEPOIS) -- esse arquivo não traz UF nem código IBGE, só código+nome, então
aquele import casa por nome contra as linhas que este módulo já criou (ver
app.importer.pipeline._import_reference).
"""

import logging
import unicodedata

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Municipality

logger = logging.getLogger("importer")

LOCALIDADES_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"


def normalize_name(name: str) -> str:
    """Remove acento e caixa -- usado tanto aqui (nenhuma ambiguidade, ibge_code
    é a chave) quanto no casamento por nome do Municipios.zip da Receita."""
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().strip().lower()


def _extract_uf(item: dict) -> str:
    """A maioria dos municípios tem `microrregion.mesorregion.UF`, mas
    distritos estaduais sem microrregião (ex: Fernando de Noronha/PE) só
    trazem `regiao-imediata.regiao-intermediaria.UF`."""
    microrregion = item.get("microrregiao")
    if microrregion:
        return microrregion["mesorregiao"]["UF"]["sigla"]
    return item["regiao-imediata"]["regiao-intermediaria"]["UF"]["sigla"]


def import_municipalities(db: Session) -> int:
    """Busca todos os municípios do IBGE de uma vez e faz upsert por
    `ibge_code` -- chave exata, sem ambiguidade (ao contrário do nome, que se
    repete entre estados). Retorna quantos foram processados."""
    logger.info("Buscando lista de municípios (IBGE/Localidades, uma request só)...")
    response = httpx.get(LOCALIDADES_URL, params={"orderBy": "nome"}, timeout=60)
    response.raise_for_status()
    data = response.json()

    count = 0
    for item in data:
        uf = _extract_uf(item)
        # Nome em maiuscula, pra bater com a convencao que o Municipios.zip da
        # Receita ja usa (esse import roda depois e sobrescreve `name` de
        # qualquer forma quando casa, mas ate la o nome fica no mesmo padrao).
        stmt = pg_insert(Municipality.__table__).values(ibge_code=item["id"], name=item["nome"].upper(), uf=uf)
        stmt = stmt.on_conflict_do_update(
            index_elements=["ibge_code"], set_={"name": stmt.excluded.name, "uf": stmt.excluded.uf}
        )
        db.execute(stmt)
        count += 1

    db.commit()
    logger.info("Municípios: %d carregados do IBGE.", count)
    return count
