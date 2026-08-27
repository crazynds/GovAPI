"""Porta do PhoneNumberService.php original (Laravel) -- mesma lógica,
mesmos DDDs válidos, mesma detecção de número "lixo" (sequência/repetido)."""

import re

VALID_DDDS = {
    "11", "12", "13", "14", "15", "16", "17", "18", "19",
    "21", "22", "24", "27", "28",
    "31", "32", "33", "34", "35", "37", "38",
    "41", "42", "43", "44", "45", "46", "47", "48", "49",
    "51", "53", "54", "55",
    "61", "62", "63", "64", "65", "66", "67", "68", "69",
    "71", "73", "74", "75", "77", "79",
    "81", "82", "83", "84", "85", "86", "87", "88", "89",
    "91", "92", "93", "94", "95", "96", "97", "98", "99",
}


def is_garbage(number: str) -> bool:
    if re.fullmatch(r"(\d)\1+", number):
        return True

    first = int(number[0])
    n = len(number)
    ascending = "".join(str(d) for d in range(first, first + n))
    descending = "".join(str(d) for d in range(first, first - n, -1))

    return number == ascending or number == descending


def parse(raw: str | None) -> dict | None:
    if not raw:
        return None

    digits = re.sub(r"\D", "", raw)

    if digits.startswith("55") and len(digits) in (12, 13):
        digits = digits[2:]

    if len(digits) not in (10, 11):
        return None

    ddd, number = digits[:2], digits[2:]

    if ddd not in VALID_DDDS:
        return None

    confidence = 100

    if len(number) == 9:
        if number[0] != "9" or is_garbage(number[1:]):
            return None
        phone_type = "mobile"
    else:
        if is_garbage(number):
            return None

        first_digit = int(number[0])
        if 6 <= first_digit <= 9:
            phone_type = "mobile"
            number = "9" + number
            confidence = 80  # reconstruído, não confirmado
        elif 2 <= first_digit <= 5:
            phone_type = "landline"
        else:
            return None

    national_number = ddd + number

    return {
        "ddd": ddd,
        "number": number,
        "type": phone_type,
        "e164": f"+55{national_number}",
        "confidence": confidence,
    }
