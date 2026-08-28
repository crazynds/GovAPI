"""Tabelas do Simples Nacional (Anexos I a V, LC 123/2006 com redação da LC
155/2016, em vigor desde 01/01/2018). Valores oficiais e estáveis (só mudam
por lei), por isso ficam hardcoded em vez de numa tabela do banco.

Cada anexo tem 6 faixas de RBT12 (receita bruta dos últimos 12 meses), cada
uma com uma aliquota nominal e um valor a deduzir -- a aliquota EFETIVA (a
que realmente incide sobre a receita do mês) é:

    aliquota_efetiva = (RBT12 * aliquota_nominal - valor_a_deduzir) / RBT12

Fonte: anexos da LC 123/2006 -- confira o texto oficial antes de usar em
produção fiscal real, isso aqui é uma referência, não parecer tributário.
"""

FAIXA_MAX = 4_800_000.00

ANEXO_I = [  # Comércio
    (180_000.00, 0.0400, 0.00),
    (360_000.00, 0.0730, 5_940.00),
    (720_000.00, 0.0950, 13_860.00),
    (1_800_000.00, 0.1070, 22_500.00),
    (3_600_000.00, 0.1430, 87_300.00),
    (4_800_000.00, 0.1900, 378_000.00),
]

ANEXO_II = [  # Indústria
    (180_000.00, 0.0450, 0.00),
    (360_000.00, 0.0780, 5_940.00),
    (720_000.00, 0.1000, 13_860.00),
    (1_800_000.00, 0.1120, 22_500.00),
    (3_600_000.00, 0.1470, 85_500.00),
    (4_800_000.00, 0.3000, 720_000.00),
]

ANEXO_III = [  # Serviços (geral)
    (180_000.00, 0.0600, 0.00),
    (360_000.00, 0.1120, 9_360.00),
    (720_000.00, 0.1350, 17_640.00),
    (1_800_000.00, 0.1600, 35_640.00),
    (3_600_000.00, 0.2100, 125_640.00),
    (4_800_000.00, 0.3300, 648_000.00),
]

ANEXO_IV = [  # Serviços (construção, vigilância, advocacia, etc. -- §6º-C)
    (180_000.00, 0.0450, 0.00),
    (360_000.00, 0.0900, 8_100.00),
    (720_000.00, 0.1020, 12_420.00),
    (1_800_000.00, 0.1400, 39_780.00),
    (3_600_000.00, 0.2200, 183_780.00),
    (4_800_000.00, 0.3300, 828_000.00),
]

ANEXO_V = [  # Serviços intelectuais/regulados sujeitos ao Fator R (§5º-D)
    (180_000.00, 0.1550, 0.00),
    (360_000.00, 0.1800, 4_500.00),
    (720_000.00, 0.1950, 9_900.00),
    (1_800_000.00, 0.2050, 17_100.00),
    (3_600_000.00, 0.2300, 62_100.00),
    (4_800_000.00, 0.3050, 540_000.00),
]

ANEXOS = {"I": ANEXO_I, "II": ANEXO_II, "III": ANEXO_III, "IV": ANEXO_IV, "V": ANEXO_V}

ANEXO_DESCRICOES = {
    "I": "Comércio",
    "II": "Indústria",
    "III": "Serviços (geral)",
    "IV": "Serviços de construção, vigilância, advocacia e afins (§6º-C)",
    "V": "Serviços intelectuais/regulados sujeitos ao Fator R (§5º-D)",
}

FATOR_R_LIMITE = 0.28  # >= 28% de folha/receita -> Anexo III no lugar do V


def faixa_da_tabela(anexo: list[tuple[float, float, float]], rbt12: float) -> tuple[int, float, float]:
    """Retorna (numero_da_faixa (1-6), aliquota_nominal, valor_a_deduzir)
    pra um RBT12 dado. RBT12 acima do limite (4.8M) usa a última faixa --
    empresa nesse caso já está no limite do Simples, alíquota é referencial."""
    for i, (limite, aliquota, deducao) in enumerate(anexo, start=1):
        if rbt12 <= limite:
            return i, aliquota, deducao
    numero, aliquota, deducao = len(anexo), anexo[-1][1], anexo[-1][2]
    return numero, aliquota, deducao


def aliquota_efetiva(rbt12: float, aliquota_nominal: float, valor_a_deduzir: float) -> float:
    if rbt12 <= 0:
        return 0.0
    return max(0.0, (rbt12 * aliquota_nominal - valor_a_deduzir) / rbt12)


def calcular_fator_r(receita_bruta_12m: float, folha_pagamento_12m: float) -> float:
    if receita_bruta_12m <= 0:
        return 0.0
    return folha_pagamento_12m / receita_bruta_12m
