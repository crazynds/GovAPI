"""Mapeamento estático UF -> região (IBGE). Não muda com frequência."""

REGIOES: dict[str, list[str]] = {
    "norte": ["AC", "AP", "AM", "PA", "RO", "RR", "TO"],
    "nordeste": ["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"],
    "centro-oeste": ["DF", "GO", "MT", "MS"],
    "sudeste": ["ES", "MG", "RJ", "SP"],
    "sul": ["PR", "RS", "SC"],
}

UF_TO_REGIAO: dict[str, str] = {uf: regiao for regiao, ufs in REGIOES.items() for uf in ufs}


def ufs_for_regiao(regiao: str) -> list[str] | None:
    return REGIOES.get(regiao.lower().strip())
