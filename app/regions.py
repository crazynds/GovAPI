"""Mapeamento estático UF -> região (IBGE). Não muda com frequência."""

REGIOES: dict[str, list[str]] = {
    "norte": ["AC", "AP", "AM", "PA", "RO", "RR", "TO"],
    "nordeste": ["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"],
    "centro-oeste": ["DF", "GO", "MT", "MS"],
    "sudeste": ["ES", "MG", "RJ", "SP"],
    "sul": ["PR", "RS", "SC"],
}

UF_TO_REGIAO: dict[str, str] = {uf: regiao for regiao, ufs in REGIOES.items() for uf in ufs}

# UF armazenada como SMALLINT (2 bytes em vez de 3 de um varchar(2)) -- o
# codigo e so a posicao na lista ordenada, atribuido aqui e nunca derivado de
# outra coisa, entao nao pode ser reordenado sem reimportar. `EX` cobre o
# estabelecimento no exterior, que a Receita marca assim.
UFS: list[str] = sorted(UF_TO_REGIAO) + ["EX"]

UF_TO_CODE: dict[str, int] = {uf: i for i, uf in enumerate(UFS, start=1)}
CODE_TO_UF: dict[int, str] = {code: uf for uf, code in UF_TO_CODE.items()}


def uf_code(uf: str | None) -> int | None:
    return UF_TO_CODE.get(uf.strip().upper()) if uf else None


def uf_name(code: int | None) -> str | None:
    return CODE_TO_UF.get(code) if code is not None else None


def ufs_for_regiao(regiao: str) -> list[str] | None:
    return REGIOES.get(regiao.lower().strip())
