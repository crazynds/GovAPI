from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    AnnexOut,
    AnnexTableOut,
    SimplesCalculationOut,
    BracketOut,
    FactorROut,
)
from app.tax_tables import (
    ANNEX_DESCRIPTIONS,
    ANNEXES,
    FACTOR_R_THRESHOLD,
    effective_rate,
    calculate_factor_r,
    bracket_for,
)

router = APIRouter(prefix="/taxes", tags=["taxes"])


def _annex_or_404(annex: str) -> str:
    annex = annex.upper()
    if annex not in ANNEXES:
        raise HTTPException(422, f"Anexo inválido: {annex!r} (use I, II, III, IV ou V)")
    return annex


@router.get("/simples/annexes", response_model=list[AnnexOut])
def listar_annexes():
    """Lista os 5 anexos do Simples Nacional e o que cada um cobre."""
    return [AnnexOut(annex=a, description=d) for a, d in ANNEX_DESCRIPTIONS.items()]


@router.get("/simples/annexes/{annex}", response_model=AnnexTableOut)
def table_for_annex(annex: str):
    """Tabela completa de faixas (RBT12, alíquota nominal, valor a deduzir) de um anexo."""
    annex = _annex_or_404(annex)
    brackets = [
        BracketOut(bracket=i, rbt12_ate=limit, nominal_rate=rate, deduction_amount=deduction)
        for i, (limit, rate, deduction) in enumerate(ANNEXES[annex], start=1)
    ]
    return AnnexTableOut(annex=annex, description=ANNEX_DESCRIPTIONS[annex], brackets=brackets)


@router.get("/simples/calculate", response_model=SimplesCalculationOut)
def calculate_simples(
    annex: str = Query(..., description="I, II, III, IV ou V"),
    rbt12: float = Query(..., gt=0, description="Receita bruta acumulada dos últimos 12 meses"),
    monthly_revenue: float | None = Query(None, ge=0, description="Receita do mês a tributar; se omitido, usa RBT12/12"),
):
    """Calcula a alíquota efetiva e o valor do DAS pelo Simples Nacional,
    dado o anexo, o RBT12 e (opcionalmente) a receita do mês. Fórmula oficial:
    effective_rate = (RBT12 * nominal_rate - deduction_amount) / RBT12."""
    annex = _annex_or_404(annex)
    bracket, nominal_rate, deduction = bracket_for(ANNEXES[annex], rbt12)
    efetiva = effective_rate(rbt12, nominal_rate, deduction)
    base = monthly_revenue if monthly_revenue is not None else rbt12 / 12
    return SimplesCalculationOut(
        annex=annex,
        rbt12=rbt12,
        bracket=bracket,
        nominal_rate=nominal_rate,
        deduction_amount=deduction,
        effective_rate=round(efetiva, 6),
        monthly_revenue=base,
        tax_amount=round(base * efetiva, 2),
    )


@router.get("/factor-r", response_model=FactorROut)
def factor_r(
    gross_revenue_12m: float = Query(..., gt=0),
    payroll_12m: float = Query(..., ge=0, description="Salários + pró-labore + encargos, últimos 12 meses"),
):
    """Calcula o Fator R (folha de pagamento / receita bruta, ambos dos
    últimos 12 meses). Empresas de serviço sujeitas à regra do Fator R
    (§5º-D do art. 18 da LC 123/2006 -- profissões regulamentadas/intelectuais)
    tributam pelo Anexo III se Fator R >= 28%, senão pelo Anexo V."""
    r = calculate_factor_r(gross_revenue_12m, payroll_12m)
    elegivel = r >= FACTOR_R_THRESHOLD
    return FactorROut(
        factor_r=round(r, 6),
        limit=FACTOR_R_THRESHOLD,
        elegivel_annex_iii=elegivel,
        annex_sugerido="III" if elegivel else "V",
    )
