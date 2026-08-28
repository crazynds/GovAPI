from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    AnexoOut,
    AnexoTabelaOut,
    CalculoSimplesOut,
    FaixaOut,
    FatorROut,
)
from app.tax_tables import (
    ANEXO_DESCRICOES,
    ANEXOS,
    FATOR_R_LIMITE,
    aliquota_efetiva,
    calcular_fator_r,
    faixa_da_tabela,
)

router = APIRouter(prefix="/impostos", tags=["impostos"])


def _anexo_or_404(anexo: str) -> str:
    anexo = anexo.upper()
    if anexo not in ANEXOS:
        raise HTTPException(422, f"Anexo inválido: {anexo!r} (use I, II, III, IV ou V)")
    return anexo


@router.get("/simples/anexos", response_model=list[AnexoOut])
def listar_anexos():
    """Lista os 5 anexos do Simples Nacional e o que cada um cobre."""
    return [AnexoOut(anexo=a, descricao=d) for a, d in ANEXO_DESCRICOES.items()]


@router.get("/simples/anexos/{anexo}", response_model=AnexoTabelaOut)
def tabela_do_anexo(anexo: str):
    """Tabela completa de faixas (RBT12, alíquota nominal, valor a deduzir) de um anexo."""
    anexo = _anexo_or_404(anexo)
    faixas = [
        FaixaOut(faixa=i, rbt12_ate=limite, aliquota_nominal=aliquota, valor_a_deduzir=deducao)
        for i, (limite, aliquota, deducao) in enumerate(ANEXOS[anexo], start=1)
    ]
    return AnexoTabelaOut(anexo=anexo, descricao=ANEXO_DESCRICOES[anexo], faixas=faixas)


@router.get("/simples/calcular", response_model=CalculoSimplesOut)
def calcular_simples(
    anexo: str = Query(..., description="I, II, III, IV ou V"),
    rbt12: float = Query(..., gt=0, description="Receita bruta acumulada dos últimos 12 meses"),
    receita_mes: float | None = Query(None, ge=0, description="Receita do mês a tributar; se omitido, usa RBT12/12"),
):
    """Calcula a alíquota efetiva e o valor do DAS pelo Simples Nacional,
    dado o anexo, o RBT12 e (opcionalmente) a receita do mês. Fórmula oficial:
    aliquota_efetiva = (RBT12 * aliquota_nominal - valor_a_deduzir) / RBT12."""
    anexo = _anexo_or_404(anexo)
    faixa, aliquota_nominal, deducao = faixa_da_tabela(ANEXOS[anexo], rbt12)
    efetiva = aliquota_efetiva(rbt12, aliquota_nominal, deducao)
    base = receita_mes if receita_mes is not None else rbt12 / 12
    return CalculoSimplesOut(
        anexo=anexo,
        rbt12=rbt12,
        faixa=faixa,
        aliquota_nominal=aliquota_nominal,
        valor_a_deduzir=deducao,
        aliquota_efetiva=round(efetiva, 6),
        receita_mes=base,
        valor_imposto=round(base * efetiva, 2),
    )


@router.get("/fator-r", response_model=FatorROut)
def fator_r(
    receita_bruta_12m: float = Query(..., gt=0),
    folha_pagamento_12m: float = Query(..., ge=0, description="Salários + pró-labore + encargos, últimos 12 meses"),
):
    """Calcula o Fator R (folha de pagamento / receita bruta, ambos dos
    últimos 12 meses). Empresas de serviço sujeitas à regra do Fator R
    (§5º-D do art. 18 da LC 123/2006 -- profissões regulamentadas/intelectuais)
    tributam pelo Anexo III se Fator R >= 28%, senão pelo Anexo V."""
    r = calcular_fator_r(receita_bruta_12m, folha_pagamento_12m)
    elegivel = r >= FATOR_R_LIMITE
    return FatorROut(
        fator_r=round(r, 6),
        limite=FATOR_R_LIMITE,
        elegivel_anexo_iii=elegivel,
        anexo_sugerido="III" if elegivel else "V",
    )
