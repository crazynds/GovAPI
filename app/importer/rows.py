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


def _documento(identificador: str | None, raw: str | None) -> int | None:
    """CPF/CNPJ de socio como inteiro.

    A Receita entrega o CPF mascarado por LGPD (`***123456**`): so os 6 digitos
    do meio existem, e e isso que fica guardado. PJ/estrangeiro vem com o CNPJ
    completo, que vai em base 36 como qualquer outro CNPJ nosso.
    """
    if not raw:
        return None

    cleaned = "".join(c for c in raw.upper() if c.isalnum())
    if not cleaned:
        return None

    if (identificador or "").strip() == "1" and len(cleaned) in (cnpj_codec.BODY_LEN, cnpj_codec.BODY_LEN + 2):
        try:
            return cnpj_codec.parse(cleaned)
        except ValueError:
            return None

    return int(cleaned) if cleaned.isdigit() else None


def _cep(value: str | None) -> int | None:
    """CEP -> int (8 digitos cabem folgado num INTEGER).

    A Receita traz CEP com e sem pontuacao, e as vezes lixo ("00000000",
    zeros, tamanho errado) -- nesses casos NULL, senao o vinculo com
    `correios_cep` aponta pra um CEP que nao existe.
    """
    if not value:
        return None
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) != 8:
        return None
    return int(digits) or None


def _logradouro(tipo: str | None, nome: str | None) -> str | None:
    """"RUA" + "DAS FLORES" -> "RUA DAS FLORES" (a Receita separa os dois)."""
    parts = [p for p in (_text(tipo), _text(nome)) if p]
    return " ".join(parts) or None


def _cpf(raw: str | None) -> int | None:
    """CPF mascarado (representante legal) -> os digitos que sobraram."""
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    return int(digits) if digits else None


def _transform_simples(row: dict, counters: Counters) -> list | None:
    simples, mei = _bool(row.get("opcao_simples")), _bool(row.get("opcao_mei"))
    if not simples and not mei:
        # Linha que diz "nao e Simples nem MEI" nao carrega informacao: o build
        # faz LEFT JOIN com `coalesce(..., false)`, entao a ausencia da linha da
        # exatamente o mesmo resultado. O arquivo lista tambem quem ja optou e
        # saiu, entao isso descarta uma fatia grande das ~45M linhas.
        counters.skipped += 1
        return None

    return [cnpj_codec.basico_to_int(row["cnpj_basico"] or ""), simples, mei]


def _transform_empresas(row: dict, _counters: Counters) -> list | None:
    return [
        cnpj_codec.basico_to_int(row["cnpj_basico"] or ""),
        _int(row.get("porte_empresa")),
        _int(row.get("natureza_juridica")),
        _text(row.get("razao_social")),
    ]


def _transform_estabelecimentos(row: dict, counters: Counters) -> list | None:
    basico = f"{row['cnpj_basico'] or '':0>8}"
    ordem = f"{row['cnpj_ordem'] or '':0>4}"
    body = (basico + ordem).upper()
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

    phone, cellphone, confidence = _phone(row.get("ddd_1"), row.get("telefone_1"))

    return [
        value,
        phone,
        cellphone,
        _int(row.get("cnae_fiscal_principal")),
        _int(row.get("municipio_codigo")),
        _cep(row.get("cep")),
        _date(row.get("data_inicio_atividade")),
        uf,
        _int(row.get("situacao_cadastral")),
        _int(row.get("motivo_situacao_cadastral")),
        confidence,
        (row.get("identificador_matriz_filial") or "").strip() == "1",
        _int_array(row.get("cnae_fiscal_secundaria")),
        _text(row.get("nome_fantasia")),
        _text(row.get("correio_eletronico")),
        _logradouro(row.get("tipo_logradouro"), row.get("logradouro")),
        _text(row.get("numero")),
        _text(row.get("complemento")),
        _text(row.get("bairro")),
    ]


def _transform_socios(row: dict, counters: Counters) -> list | None:
    nome = _text(row.get("nome_socio"))
    if not nome:
        # nome_socio e NOT NULL na tabela; sem ele a linha nao diz nada.
        counters.skipped += 1
        return None

    return [
        cnpj_codec.basico_to_int(row["cnpj_basico"] or ""),
        _documento(row.get("identificador_socio"), row.get("cpf_cnpj_socio")),
        _cpf(row.get("representante_legal")),
        _date(row.get("data_entrada_sociedade")),
        _int(row.get("identificador_socio")),
        _int(row.get("qualificacao_socio")),
        _int(row.get("qualificacao_representante_legal")),
        _int(row.get("pais")),
        # faixa_etaria 0 = "nao se aplica", que e informacao -- por isso `int`
        # direto em vez do `_int`, que trata 0 como ausente.
        int(row["faixa_etaria"]) if (row.get("faixa_etaria") or "").strip().isdigit() else None,
        nome,
        _text(row.get("nome_representante")),
    ]


GROUP_SPECS: dict[str, GroupSpec] = {
    "simples": GroupSpec(
        csv_columns=[
            "cnpj_basico", "opcao_simples", "data_opcao_simples", "data_exclusao_simples",
            "opcao_mei", "data_opcao_mei", "data_exclusao_mei",
        ],
        table="simples_staging",
        columns=["cnpj_basico", "opcao_simples", "opcao_mei"],
        key=["cnpj_basico"],
        transform=_transform_simples,
    ),
    "empresas": GroupSpec(
        csv_columns=[
            "cnpj_basico", "razao_social", "natureza_juridica", "qualificacao_responsavel",
            "capital_social", "porte_empresa", "ente_federativo",
        ],
        table="empresas_staging",
        columns=["cnpj_basico", "porte_empresa", "natureza_juridica", "razao_social"],
        key=["cnpj_basico"],
        transform=_transform_empresas,
    ),
    "estabelecimentos": GroupSpec(
        csv_columns=[
            "cnpj_basico", "cnpj_ordem", "cnpj_dv", "identificador_matriz_filial", "nome_fantasia",
            "situacao_cadastral", "data_situacao_cadastral", "motivo_situacao_cadastral",
            "nome_cidade_exterior", "pais", "data_inicio_atividade", "cnae_fiscal_principal",
            "cnae_fiscal_secundaria", "tipo_logradouro", "logradouro", "numero", "complemento",
            "bairro", "cep", "uf", "municipio_codigo", "ddd_1", "telefone_1", "ddd_2", "telefone_2",
            "ddd_fax", "fax", "correio_eletronico", "situacao_especial", "data_situacao_especial",
        ],
        table="estabelecimentos_staging",
        columns=[
            "cnpj", "phone", "cellphone", "cnae_fiscal_principal", "municipio_codigo",
            "cep", "data_inicio_atividade", "uf", "situacao_cadastral", "motivo_situacao_cadastral",
            "cellphone_confidence", "is_headquarters", "cnae_fiscal_secundaria", "nome_fantasia",
            "correio_eletronico", "logradouro", "numero", "complemento", "bairro",
        ],
        key=["cnpj"],
        transform=_transform_estabelecimentos,
    ),
    "socios": GroupSpec(
        csv_columns=[
            "cnpj_basico", "identificador_socio", "nome_socio", "cpf_cnpj_socio",
            "qualificacao_socio", "data_entrada_sociedade", "pais", "representante_legal",
            "nome_representante", "qualificacao_representante_legal", "faixa_etaria",
        ],
        table="socios",
        # Sem chave natural: cada Socios<N>.zip cobre uma faixa disjunta de
        # cnpj_basico, nao tem o que dar merge entre arquivos (a tabela e zerada
        # no inicio do grupo, ver run_import).
        columns=[
            "cnpj_basico", "cpf_cnpj_socio", "representante_legal", "data_entrada_sociedade",
            "identificador_socio", "qualificacao_socio", "qualificacao_representante_legal",
            "pais", "faixa_etaria", "nome_socio", "nome_representante",
        ],
        key=None,
        transform=_transform_socios,
    ),
}
