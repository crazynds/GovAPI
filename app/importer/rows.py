"""Traducao linha-a-linha do CSV da Receita pros tipos do banco.

Tudo que vira numero, vira numero aqui -- no meio do streaming pro COPY, antes
de tocar o Postgres. Sao chamadas de builtin em C (`int`, `int(s, 36)`), custo
irrelevante perto do I/O de um arquivo de 20GB, e em troca o staging ja nasce
nos tipos finais: o build da tabela final fica sendo um INSERT ... SELECT sem
um unico CAST, e o disco guarda 8 bytes onde guardava 15.

Cada grupo declara:
  csv_columns -- o layout do arquivo (posicional, sem cabecalho)
  table       -- tabela de destino
  columns     -- colunas do destino, na ordem em que o COPY as manda
  key         -- chave natural pro UPSERT (None = INSERT direto, sem merge)
  transform   -- (row: dict) -> list | None   (None descarta a linha)
"""

from dataclasses import dataclass
from typing import Callable

from app import cnpj as cnpj_codec
from app.importer.phone import parse as parse_phone
from app.regions import uf_code


@dataclass(frozen=True)
class GroupSpec:
    csv_columns: list[str]
    table: str
    columns: list[str]
    key: list[str] | None
    transform: Callable[[dict, "Counters"], list | None]


class Counters:
    """Contadores de anomalia da fonte, logados ao fim de cada arquivo -- sem
    isso um CSV inconsistente passa em silencio."""

    def __init__(self) -> None:
        self.dv_mismatch = 0
        self.bad_uf = 0
        self.skipped = 0
        self.malformed = 0

    def summary(self) -> str:
        parts = []
        if self.dv_mismatch:
            parts.append(f"{self.dv_mismatch} DV(s) divergentes do calculado")
        if self.bad_uf:
            parts.append(f"{self.bad_uf} UF(s) desconhecida(s)")
        if self.skipped:
            parts.append(f"{self.skipped} linha(s) descartada(s)")
        if self.malformed:
            parts.append(f"{self.malformed} linha(s) malformada(s) (não deu pra montar um CNPJ válido)")
        return "; ".join(parts)


def _int(value: str | None) -> int | None:
    """Numerico da Receita -> int. Campos vazios e zeros-only viram NULL."""
    if not value:
        return None
    value = value.strip()
    if not value or not value.isdigit():
        return None
    number = int(value)
    return number or None


def _date(value: str | None) -> str | None:
    """YYYYMMDD -> YYYY-MM-DD. A Receita usa 00000000 pra "sem data"."""
    if not value or len(value) != 8 or value == "00000000":
        return None
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"


def _bool(value: str | None) -> bool:
    return (value or "").strip().upper() == "S"


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _int_array(value: str | None) -> str | None:
    """Lista de CNAEs separada por virgula -> literal de array do Postgres.

    NULL (e nao '{}') quando nao ha nenhum: um array vazio custa ~24 bytes por
    linha, e a maioria das empresas nao tem CNAE secundario.
    """
    if not value:
        return None
    codes = [c for c in (part.strip() for part in value.split(",")) if c.isdigit()]
    if not codes:
        return None
    return "{" + ",".join(str(int(c)) for c in codes) + "}"


def _phone(ddd: str | None, number: str | None) -> tuple[int | None, int | None, int]:
    """(fixo, celular, confianca) como inteiros nacionais, sem o +55.

    Roda no import e nao no build de proposito: com o telefone ja parseado no
    staging, o build nao precisa do passo de UPDATE que criava uma linha morta
    por estabelecimento.
    """
    parsed = parse_phone((ddd or "") + (number or ""))
    if not parsed:
        return None, None, 0

    national = int(parsed["ddd"] + parsed["number"])
    if parsed["type"] == "mobile":
        return None, national, parsed["confidence"]
    return national, None, 0


def _tax_id(identificador: str | None, raw: str | None) -> int | None:
    """CPF/CNPJ de socio como inteiro.

    A Receita entrega o CPF mascarado por LGPD (`***123456**`): so os 6 digitos
    do meio existem, e e isso que fica guardado. PJ/estrangeiro vem com o CNPJ
    completo, que vai em base 36 como qualquer outro CNPJ nosso.

    Pra socio PJ o valor guardado *tem* que ser base 36, porque a saida
    (`routers/partners._tax_id`) decodifica em base 36 sempre que
    `partner_type == 1`. Entao aqui a decisao e pelo identificador, e
    nao pelo tamanho: parte dos registros vem so com a raiz de 8 posicoes
    (`cnpj_codec.parse` completa com a ordem da matriz, 0001), e quando eles
    caiam no `int(cleaned)` decimal a API devolvia um documento inventado --
    e o registro ficava inalcancavel por `?tax_id=`. O que nem assim da um
    CNPJ valido vira NULL: qualquer inteiro fora da base 36 sairia corrompido
    na leitura de qualquer forma.
    """
    if not raw:
        return None

    cleaned = "".join(c for c in raw.upper() if c.isalnum())
    if not cleaned:
        return None

    if (identificador or "").strip() == "1":
        try:
            return cnpj_codec.parse(cleaned)
        except ValueError:
            return None

    return int(cleaned) if cleaned.isdigit() else None


def _cep(value: str | None) -> int | None:
    """CEP -> int (8 digitos cabem folgado num INTEGER).

    A Receita traz CEP com e sem pontuacao, e as vezes lixo ("00000000",
    zeros, tamanho errado) -- nesses casos NULL, senao o vinculo com
    `postal_codes` aponta pra um CEP que nao existe.
    """
    if not value:
        return None
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) != 8:
        return None
    return int(digits) or None


def _street(street_type: str | None, name: str | None) -> str | None:
    """"RUA" + "DAS FLORES" -> "RUA DAS FLORES" (a Receita separa os dois)."""
    parts = [p for p in (_text(street_type), _text(name)) if p]
    return " ".join(parts) or None


def _cpf(raw: str | None) -> int | None:
    """CPF mascarado (representante legal) -> os digitos que sobraram."""
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    return int(digits) if digits else None


def _transform_simples(row: dict, counters: Counters) -> list | None:
    simples, mei = _bool(row.get("simples_option")), _bool(row.get("mei_option"))
    if not simples and not mei:
        # Linha que diz "nao e Simples nem MEI" nao carrega informacao: o build
        # faz LEFT JOIN com `coalesce(..., false)`, entao a ausencia da linha da
        # exatamente o mesmo resultado. O arquivo lista tambem quem ja optou e
        # saiu, entao isso descarta uma fatia grande das ~45M linhas.
        counters.skipped += 1
        return None

    return [cnpj_codec.root_to_int(row["cnpj_root"] or ""), simples, mei]


def _transform_companies(row: dict, _counters: Counters) -> list | None:
    return [
        cnpj_codec.root_to_int(row["cnpj_root"] or ""),
        _int(row.get("company_size")),
        _int(row.get("legal_nature")),
        _text(row.get("company_name")),
    ]


def _transform_establishments(row: dict, counters: Counters) -> list | None:
    root = f"{row['cnpj_root'] or '':0>8}"
    branch = f"{row['cnpj_branch'] or '':0>4}"
    body = (root + branch).upper()
    value = cnpj_codec.to_int(body)

    # O DV nao e guardado (e derivado do corpo), mas confere-se com o da fonte
    # pra nao "consertar" um CNPJ errado em silencio.
    fonte_dv = (row.get("cnpj_dv") or "").strip()
    if fonte_dv and fonte_dv.zfill(2) != cnpj_codec.dv(body):
        counters.dv_mismatch += 1

    uf_raw = (row.get("uf") or "").strip().upper()
    uf = uf_code(uf_raw)
    if uf_raw and uf is None:
        counters.bad_uf += 1

    phone, cellphone, confidence = _phone(row.get("ddd_1"), row.get("phone_1"))

    return [
        value,
        phone,
        cellphone,
        _int(row.get("main_cnae")),
        _int(row.get("municipality_code")),
        _cep(row.get("cep")),
        _date(row.get("activity_start_date")),
        uf,
        _int(row.get("registration_status")),
        _int(row.get("registration_status_reason")),
        confidence,
        (row.get("headquarters_indicator") or "").strip() == "1",
        _int_array(row.get("secondary_cnaes")),
        _text(row.get("trade_name")),
        _text(row.get("email")),
        _street(row.get("street_type"), row.get("street")),
        _text(row.get("number")),
        _text(row.get("complement")),
        _text(row.get("district")),
    ]


def _transform_partners(row: dict, counters: Counters) -> list | None:
    name = _text(row.get("partner_name"))
    if not name:
        # partner_name e NOT NULL na tabela; sem ele a linha nao diz nada.
        counters.skipped += 1
        return None

    return [
        cnpj_codec.root_to_int(row["cnpj_root"] or ""),
        _tax_id(row.get("partner_type"), row.get("partner_tax_id")),
        _cpf(row.get("legal_rep")),
        _date(row.get("partnership_start_date")),
        _int(row.get("partner_type")),
        _int(row.get("partner_qualification")),
        _int(row.get("legal_rep_qualification")),
        _int(row.get("country")),
        # age_range 0 = "nao se aplica", que e informacao -- por isso `int`
        # direto em vez do `_int`, que trata 0 como ausente.
        int(row["age_range"]) if (row.get("age_range") or "").strip().isdigit() else None,
        name,
        _text(row.get("legal_rep_name")),
    ]


GROUP_SPECS: dict[str, GroupSpec] = {
    "simples": GroupSpec(
        csv_columns=[
            "cnpj_root", "simples_option", "simples_option_date", "simples_exclusion_date",
            "mei_option", "mei_option_date", "mei_exclusion_date",
        ],
        table="simples_staging",
        columns=["cnpj_root", "simples_option", "mei_option"],
        key=["cnpj_root"],
        transform=_transform_simples,
    ),
    "companies": GroupSpec(
        csv_columns=[
            "cnpj_root", "company_name", "legal_nature", "responsible_qualification",
            "capital_social", "company_size", "ente_federativo",
        ],
        table="companies_staging",
        columns=["cnpj_root", "company_size", "legal_nature", "company_name"],
        key=["cnpj_root"],
        transform=_transform_companies,
    ),
    "establishments": GroupSpec(
        csv_columns=[
            "cnpj_root", "cnpj_branch", "cnpj_dv", "headquarters_indicator", "trade_name",
            "registration_status", "registration_status_date", "registration_status_reason",
            "foreign_city_name", "country", "activity_start_date", "main_cnae",
            "secondary_cnaes", "street_type", "street", "number", "complement",
            "district", "cep", "uf", "municipality_code", "ddd_1", "phone_1", "ddd_2", "phone_2",
            "ddd_fax", "fax", "email", "special_status", "special_status_date",
        ],
        table="establishments_staging",
        columns=[
            "cnpj", "phone", "cellphone", "main_cnae", "municipality_code",
            "cep", "activity_start_date", "uf", "registration_status", "registration_status_reason",
            "cellphone_confidence", "is_headquarters", "secondary_cnaes", "trade_name",
            "email", "street", "number", "complement", "district",
        ],
        key=["cnpj"],
        transform=_transform_establishments,
    ),
    "partners": GroupSpec(
        csv_columns=[
            "cnpj_root", "partner_type", "partner_name", "partner_tax_id",
            "partner_qualification", "partnership_start_date", "country", "legal_rep",
            "legal_rep_name", "legal_rep_qualification", "age_range",
        ],
        # `partners_new`, nao `partners`: carrega numa tabela-sombra que so vira
        # `partners` no fim de todo o grupo, via swap atomico -- ver
        # app.importer.pipeline._create_partners_shadow/_finalize_partners.
        table="partners_new",
        # Sem chave natural: cada Socios<N>.zip cobre uma faixa disjunta de
        # cnpj_root, nao tem o que dar merge entre arquivos.
        columns=[
            "cnpj_root", "partner_tax_id", "legal_rep", "partnership_start_date",
            "partner_type", "partner_qualification", "legal_rep_qualification",
            "country", "age_range", "partner_name", "legal_rep_name",
        ],
        key=None,
        transform=_transform_partners,
    ),
}
