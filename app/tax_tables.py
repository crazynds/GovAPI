"""Tabelas do Simples Nacional (Anexos I a V, LC 123/2006 com redação da LC
155/2016, em vigor desde 01/01/2018). Valores oficiais e estáveis (só mudam
por lei), por isso ficam hardcoded em vez de numa tabela do banco.

Cada anexo tem 6 faixas de RBT12 (receita bruta dos últimos 12 meses), cada
uma com uma aliquota nominal e um valor a deduzir -- a aliquota EFETIVA (a
que realmente incide sobre a receita do mês) é:

    effective_rate = (RBT12 * nominal_rate - deduction_amount) / RBT12

Fonte: anexos da LC 123/2006 -- confira o texto oficial antes de usar em
produção fiscal real, isso aqui é uma referência, não parecer tributário.
"""

BRACKET_MAX = 4_800_000.00

ANNEX_I = [  # Comércio
    (180_000.00, 0.0400, 0.00),
    (360_000.00, 0.0730, 5_940.00),
    (720_000.00, 0.0950, 13_860.00),
    (1_800_000.00, 0.1070, 22_500.00),
    (3_600_000.00, 0.1430, 87_300.00),
    (4_800_000.00, 0.1900, 378_000.00),
]

ANNEX_II = [  # Indústria
    (180_000.00, 0.0450, 0.00),
    (360_000.00, 0.0780, 5_940.00),
    (720_000.00, 0.1000, 13_860.00),
    (1_800_000.00, 0.1120, 22_500.00),
    (3_600_000.00, 0.1470, 85_500.00),
    (4_800_000.00, 0.3000, 720_000.00),
]

ANNEX_III = [  # Serviços (geral)
    (180_000.00, 0.0600, 0.00),
    (360_000.00, 0.1120, 9_360.00),
    (720_000.00, 0.1350, 17_640.00),
    (1_800_000.00, 0.1600, 35_640.00),
    (3_600_000.00, 0.2100, 125_640.00),
    (4_800_000.00, 0.3300, 648_000.00),
]

ANNEX_IV = [  # Serviços (construção, vigilância, advocacia, etc. -- §6º-C)
    (180_000.00, 0.0450, 0.00),
    (360_000.00, 0.0900, 8_100.00),
    (720_000.00, 0.1020, 12_420.00),
    (1_800_000.00, 0.1400, 39_780.00),
    (3_600_000.00, 0.2200, 183_780.00),
    (4_800_000.00, 0.3300, 828_000.00),
]

ANNEX_V = [  # Serviços intelectuais/regulados sujeitos ao Fator R (§5º-D)
    (180_000.00, 0.1550, 0.00),
    (360_000.00, 0.1800, 4_500.00),
    (720_000.00, 0.1950, 9_900.00),
    (1_800_000.00, 0.2050, 17_100.00),
    (3_600_000.00, 0.2300, 62_100.00),
    (4_800_000.00, 0.3050, 540_000.00),
]

ANNEXES = {"I": ANNEX_I, "II": ANNEX_II, "III": ANNEX_III, "IV": ANNEX_IV, "V": ANNEX_V}

ANNEX_DESCRIPTIONS = {
    "I": "Comércio",
    "II": "Indústria",
    "III": "Serviços (geral)",
    "IV": "Serviços de construção, vigilância, advocacia e afins (§6º-C)",
    "V": "Serviços intelectuais/regulados sujeitos ao Fator R (§5º-D)",
}

FACTOR_R_THRESHOLD = 0.28  # >= 28% de folha/receita -> Anexo III no lugar do V


def bracket_for(annex: list[tuple[float, float, float]], rbt12: float) -> tuple[int, float, float]:
    """Retorna (numero_da_faixa (1-6), nominal_rate, deduction_amount)
    pra um RBT12 dado. RBT12 acima do limite (4.8M) usa a última faixa --
    empresa nesse caso já está no limite do Simples, alíquota é referencial."""
    for i, (limit, rate, deduction) in enumerate(annex, start=1):
        if rbt12 <= limit:
            return i, rate, deduction
    number, rate, deduction = len(annex), annex[-1][1], annex[-1][2]
    return number, rate, deduction


def effective_rate(rbt12: float, nominal_rate: float, deduction_amount: float) -> float:
    if rbt12 <= 0:
        return 0.0
    return max(0.0, (rbt12 * nominal_rate - deduction_amount) / rbt12)


def calculate_factor_r(gross_revenue_12m: float, payroll_12m: float) -> float:
    if gross_revenue_12m <= 0:
        return 0.0
    return payroll_12m / gross_revenue_12m
