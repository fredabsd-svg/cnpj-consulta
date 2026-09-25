"""Conciliacao de resultados de multiplos provedores.

Regras:
1. Mantem o valor de cada fonte na procedencia (campos_procedencia).
2. Normaliza cada campo para uma forma canonica ANTES de comparar
   (sem acento, sem codigo numerico na frente da descricao, numeros com 2
   casas) -- diferencas so de formato nao sao conflito.
3. Prioriza fontes primarias sobre espelhos (ordem em _PRIORITY).
4. Confianca por campo:
   - "alta":  confirmado por 2+ fontes INDEPENDENTES e sem divergencia;
   - "media": uma unica fonte independente (BrasilAPI e proxy da Minha
     Receita, entao as duas juntas contam como uma so);
   - "baixa": fontes divergem.
5. Marca conflitos (valor divergente entre fontes), com o valor de cada uma.
6. Mostra "nao informado" quando o campo nao existir.
7. NUNCA preenche lacunas com dados inventados.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any

from app.core.normalizer import (
    comparable,
    normalize_cep,
    normalize_name,
    normalize_phone,
    normalize_text,
    parse_date,
    parse_decimal,
)
from app.providers.base import ProviderResult
from app.providers.receita_local import PORTES, SITUACOES, TIPOS_SOCIO, code_to_int
from app.schemas.company import (
    CNAE,
    Address,
    CompanyUnified,
    FieldProvenance,
    PartnerPublic,
    Phone,
    SourceEntry,
)

# Ordem de preferencia quando as fontes divergem (primarias primeiro).
_PRIORITY = ["receita_local", "minha_receita", "brasilapi", "cnpjws", "receitaws"]
# Fontes que NAO sao independentes entre si: chave e proxy do valor.
_SOURCE_GROUP = {"brasilapi": "minha_receita"}

_LABELS = {
    "razao_social": "Razao social",
    "nome_fantasia": "Nome fantasia",
    "situacao_cadastral": "Situacao cadastral",
    "data_situacao_cadastral": "Data da situacao",
    "motivo_situacao": "Motivo da situacao",
    "data_abertura": "Data de abertura",
    "porte": "Porte",
    "natureza_juridica": "Natureza juridica",
    "capital_social": "Capital social",
    "opcao_simples": "Simples Nacional",
    "opcao_mei": "MEI",
    "matriz_filial": "Matriz/filial",
    "email": "E-mail",
}
# Campos em que divergencia vira "conflito" visivel ao usuario.
_CONFLICT_FIELDS = (
    "razao_social",
    "nome_fantasia",
    "situacao_cadastral",
    "data_abertura",
    "porte",
    "natureza_juridica",
    "capital_social",
    "opcao_simples",
    "opcao_mei",
    "matriz_filial",
    "endereco",
    "telefones",
    "cnae_principal",
    "cnaes_secundarios",
)


def _rank(provider: str) -> int:
    return _PRIORITY.index(provider) if provider in _PRIORITY else len(_PRIORITY)


# ---------------------------------------------------------------------------
# Coercoes para forma canonica
# ---------------------------------------------------------------------------

_NATUREZA_PREFIX = re.compile(r"^\s*\d{3}-?\d\s*-\s*")
_QUAL_PREFIX = re.compile(r"^\s*\d+\s*-\s*")


def _coerce_situacao(value: Any) -> str | None:
    """Codigo (2, "02") ou texto ("Ativa") -> "ATIVA"."""
    if value is None or value == "":
        return None
    code = code_to_int(value)
    if code is not None:
        return SITUACOES.get(code, str(value))
    text = normalize_text(str(value))
    return text.upper() if text else None


def _coerce_porte(value: Any) -> str | None:
    """Unifica "MICRO EMPRESA", "Micro Empresa", "ME", 1, "01"..."""
    if value is None or value == "":
        return None
    code = code_to_int(value)
    if code is not None:
        return PORTES.get(code) if code else None  # 00 = nao informado
    c = comparable(value) or ""
    if "MICRO" in c or c == "ME":
        return "MICRO EMPRESA"
    if "PEQUENO" in c or c == "EPP":
        return "EMPRESA DE PEQUENO PORTE"
    if "DEMAIS" in c:
        return "DEMAIS"
    if "NAO INFORMADO" in c:
        return None
    return normalize_text(str(value))


def _coerce_natureza(value: Any) -> str | None:
    """Remove o codigo: '206-2 - Sociedade Empresaria Limitada' -> 'Sociedade Empresaria Limitada'."""
    if isinstance(value, dict):
        value = value.get("descricao")
    if value is None:
        return None
    return normalize_text(_NATUREZA_PREFIX.sub("", str(value)))


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    c = comparable(value) if value is not None else None
    if c in {"S", "SIM", "TRUE", "1"}:
        return True
    if c in {"N", "NAO", "FALSE", "0"}:
        return False
    return None


def _coerce_matriz(value: Any) -> str | None:
    if value is None or value == "":
        return None
    code = code_to_int(value)
    if code is not None:
        return {1: "MATRIZ", 2: "FILIAL"}.get(code)
    c = comparable(value) or ""
    if c.startswith("MATRIZ"):
        return "MATRIZ"
    if c.startswith("FILIAL"):
        return "FILIAL"
    return None


def _coerce_email(value: Any) -> str | None:
    text = normalize_text(value) if isinstance(value, str) else None
    return text.lower() if text else None


def _descricao(value: Any) -> str | None:
    """Campos que podem vir como {"descricao": ...} (CNPJ.ws) ou texto."""
    if isinstance(value, dict):
        value = value.get("descricao")
    if value is None or isinstance(value, (int, float)):
        return None
    return normalize_text(str(value))


def _cnae_code(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits or set(digits) == {"0"}:
        return None
    return digits.zfill(7)


# ---------------------------------------------------------------------------
# Extracao por provedor (cada fonte tem um schema diferente)
# ---------------------------------------------------------------------------


def _scalar_fields(raw: dict[str, Any], provider: str) -> dict[str, Any]:
    if provider == "receitaws":
        simples = raw.get("simples") or {}
        simei = raw.get("simei") or {}
        return {
            "razao_social": normalize_text(raw.get("nome")),
            "nome_fantasia": normalize_text(raw.get("fantasia")),
            "data_abertura": parse_date(raw.get("abertura")),
            "situacao_cadastral": _coerce_situacao(raw.get("situacao")),
            "data_situacao_cadastral": parse_date(raw.get("data_situacao")),
            "motivo_situacao": _descricao(raw.get("motivo_situacao")),
            "porte": _coerce_porte(raw.get("porte")),
            "natureza_juridica": _coerce_natureza(raw.get("natureza_juridica")),
            "capital_social": parse_decimal(raw.get("capital_social")),
            "opcao_simples": _coerce_bool(simples.get("optante")),
            "opcao_mei": _coerce_bool(simei.get("optante")),
            "matriz_filial": _coerce_matriz(raw.get("tipo")),
            "email": _coerce_email(raw.get("email")),
        }
    if provider == "cnpjws":
        est = raw.get("estabelecimento") or {}
        simples = raw.get("simples") or {}
        return {
            "razao_social": normalize_text(raw.get("razao_social")),
            "nome_fantasia": normalize_text(est.get("nome_fantasia")),
            "data_abertura": parse_date(est.get("data_inicio_atividade")),
            "situacao_cadastral": _coerce_situacao(est.get("situacao_cadastral")),
            "data_situacao_cadastral": parse_date(est.get("data_situacao_cadastral")),
            "motivo_situacao": _descricao(est.get("motivo_situacao_cadastral")),
            "porte": _coerce_porte(_descricao(raw.get("porte"))),
            "natureza_juridica": _coerce_natureza(raw.get("natureza_juridica")),
            "capital_social": parse_decimal(raw.get("capital_social")),
            "opcao_simples": _coerce_bool(simples.get("simples")),
            "opcao_mei": _coerce_bool(simples.get("mei")),
            "matriz_filial": _coerce_matriz(est.get("tipo")),
            "email": _coerce_email(est.get("email")),
        }
    if provider == "receita_local":
        return {
            "razao_social": normalize_text(raw.get("razao_social")),
            "nome_fantasia": normalize_text(raw.get("nome_fantasia")),
            "data_abertura": parse_date(raw.get("data_inicio_atividade")),
            "situacao_cadastral": _coerce_situacao(raw.get("situacao_cadastral")),
            "data_situacao_cadastral": parse_date(raw.get("data_situacao_cadastral")),
            "motivo_situacao": _descricao(raw.get("motivo_situacao_descricao")),
            "porte": _coerce_porte(raw.get("porte_empresa")),
            "natureza_juridica": _coerce_natureza(
                raw.get("natureza_juridica_descricao") or raw.get("natureza_juridica")
            ),
            "capital_social": parse_decimal(raw.get("capital_social")),
            "opcao_simples": _coerce_bool(raw.get("opcao_simples")),
            "opcao_mei": _coerce_bool(raw.get("opcao_mei")),
            "matriz_filial": _coerce_matriz(raw.get("identificador_matriz_filial")),
            "email": _coerce_email(raw.get("correio_eletronico")),
        }
    # BrasilAPI / Minha Receita -- schema identico
    return {
        "razao_social": normalize_text(raw.get("razao_social")),
        "nome_fantasia": normalize_text(raw.get("nome_fantasia")),
        "data_abertura": parse_date(raw.get("data_inicio_atividade")),
        "situacao_cadastral": _coerce_situacao(
            raw.get("situacao_cadastral") or raw.get("descricao_situacao_cadastral")
        ),
        "data_situacao_cadastral": parse_date(raw.get("data_situacao_cadastral")),
        "motivo_situacao": _descricao(raw.get("descricao_motivo_situacao_cadastral")),
        "porte": _coerce_porte(raw.get("porte") or raw.get("codigo_porte")),
        "natureza_juridica": _coerce_natureza(raw.get("natureza_juridica")),
        "capital_social": parse_decimal(raw.get("capital_social")),
        "opcao_simples": _coerce_bool(raw.get("opcao_pelo_simples")),
        "opcao_mei": _coerce_bool(raw.get("opcao_pelo_mei")),
        "matriz_filial": _coerce_matriz(
            raw.get("descricao_identificador_matriz_filial") or raw.get("identificador_matriz_filial")
        ),
        "email": _coerce_email(raw.get("email")),
    }


def _address_from(raw: dict[str, Any], provider: str) -> Address | None:
    """Extrai endereco respeitando variacoes de schema."""
    if provider == "receita_local":
        addr = Address(
            logradouro=normalize_text(
                " ".join(filter(None, [raw.get("tipo_logradouro"), raw.get("logradouro")]))
            ),
            numero=normalize_text(raw.get("numero")),
            complemento=normalize_text(raw.get("complemento")),
            bairro=normalize_text(raw.get("bairro")),
            municipio=normalize_text(raw.get("municipio_descricao") or raw.get("municipio")),
            uf=normalize_text(raw.get("uf")),
            cep=normalize_cep(raw.get("cep")),
        )
    elif provider == "receitaws":
        addr = Address(
            logradouro=normalize_text(raw.get("logradouro")),
            numero=normalize_text(raw.get("numero")),
            complemento=normalize_text(raw.get("complemento")),
            bairro=normalize_text(raw.get("bairro")),
            municipio=normalize_text(raw.get("municipio")),
            uf=normalize_text(raw.get("uf")),
            cep=normalize_cep(raw.get("cep")),
        )
    elif provider == "cnpjws":
        est = raw.get("estabelecimento") or {}
        cidade = est.get("cidade") or {}
        estado = est.get("estado") or {}
        addr = Address(
            logradouro=normalize_text(
                " ".join(filter(None, [est.get("tipo_logradouro"), est.get("logradouro")]))
            ),
            numero=normalize_text(est.get("numero")),
            complemento=normalize_text(est.get("complemento")),
            bairro=normalize_text(est.get("bairro")),
            municipio=normalize_text(cidade.get("nome") if isinstance(cidade, dict) else cidade),
            uf=normalize_text(estado.get("sigla") if isinstance(estado, dict) else estado),
            cep=normalize_cep(est.get("cep")),
        )
    else:  # BrasilAPI e Minha Receita
        addr = Address(
            logradouro=normalize_text(
                " ".join(
                    filter(None, [raw.get("descricao_tipo_de_logradouro"), raw.get("logradouro")])
                )
            ),
            numero=normalize_text(raw.get("numero")),
            complemento=normalize_text(raw.get("complemento")),
            bairro=normalize_text(raw.get("bairro")),
            municipio=normalize_text(raw.get("municipio")),
            uf=normalize_text(raw.get("uf")),
            cep=normalize_cep(raw.get("cep")),
        )
    return addr if any(addr.model_dump().values()) else None


def _phone(ddd: Any, numero: Any, tipo: str) -> Phone | None:
    d = normalize_phone(str(ddd)) if ddd else None
    n = normalize_phone(str(numero)) if numero else None
    if d and n and len(n) >= 8:
        return Phone(ddd=d[-2:], numero=n, tipo=tipo)
    return None


def _phone_from_full(value: Any, tipo: str) -> Phone | None:
    """Numero completo com DDD ("1123851939" ou "(11) 2385-1939")."""
    digits = normalize_phone(str(value)) if value else None
    if digits and len(digits) >= 10:
        return Phone(ddd=digits[:2], numero=digits[2:], tipo=tipo)
    return None


def _phones_from(raw: dict[str, Any], provider: str) -> list[Phone]:
    tipos = ("principal", "secundario")
    if provider == "receita_local":
        found = [
            _phone(raw.get(f"ddd_telefone_{i}"), raw.get(f"numero_telefone_{i}"), tipos[i - 1])
            for i in (1, 2)
        ]
    elif provider == "receitaws":
        parts = str(raw.get("telefone") or "").split("/")
        found = [_phone_from_full(p, tipos[min(i, 1)]) for i, p in enumerate(parts)]
    elif provider == "cnpjws":
        est = raw.get("estabelecimento") or {}
        found = [_phone(est.get(f"ddd{i}"), est.get(f"telefone{i}"), tipos[i - 1]) for i in (1, 2)]
    else:  # BrasilAPI / Minha Receita: DDD e numero vem juntos em ddd_telefone_N
        found = [_phone_from_full(raw.get(f"ddd_telefone_{i}"), tipos[i - 1]) for i in (1, 2)]
    return [p for p in found if p]


def _cnae_principal_from(raw: dict[str, Any], provider: str) -> CNAE | None:
    if provider == "receita_local":
        code, desc = raw.get("cnae_fiscal_principal"), raw.get("cnae_fiscal_principal_descricao")
    elif provider == "receitaws":
        ap = raw.get("atividade_principal") or [{}]
        item = ap[0] if isinstance(ap, list) and ap else {}
        code, desc = item.get("code"), item.get("text")
    elif provider == "cnpjws":
        ap = (raw.get("estabelecimento") or {}).get("atividade_principal") or {}
        code, desc = ap.get("subclasse") or ap.get("id"), ap.get("descricao")
    else:
        code, desc = raw.get("cnae_fiscal"), raw.get("cnae_fiscal_descricao")
    codigo = _cnae_code(code)
    return CNAE(codigo=codigo, descricao=normalize_text(desc)) if codigo else None


def _cnaes_secundarios_from(raw: dict[str, Any], provider: str) -> list[CNAE]:
    if provider == "receitaws":
        pairs = [(i.get("code"), i.get("text")) for i in raw.get("atividades_secundarias") or []]
    elif provider == "cnpjws":
        items = (raw.get("estabelecimento") or {}).get("atividades_secundarias") or []
        pairs = [(i.get("subclasse") or i.get("id"), i.get("descricao")) for i in items]
    else:  # receita_local, BrasilAPI, Minha Receita: lista de {codigo, descricao}
        pairs = [(i.get("codigo"), i.get("descricao")) for i in raw.get("cnaes_secundarios") or []]
    out: list[CNAE] = []
    for code, desc in pairs:
        codigo = _cnae_code(code)
        if codigo:
            out.append(CNAE(codigo=codigo, descricao=normalize_text(desc)))
    return out


def _partners_from(raw: dict[str, Any], provider: str) -> list[PartnerPublic]:
    out: list[PartnerPublic] = []
    if provider == "receita_local":
        for s in raw.get("socios") or []:
            out.append(
                PartnerPublic(
                    nome=normalize_name(s.get("nome_socio")),
                    tipo=TIPOS_SOCIO.get(code_to_int(s.get("identificador_de_socio")) or 0),
                    qualificacao=normalize_text(
                        s.get("qualificacao_socio_descricao") or s.get("qualificacao_socio")
                    ),
                    data_entrada=parse_date(s.get("data_entrada_sociedade")),
                    documento_mascarado=normalize_text(s.get("cpf_cnpj_socio")),
                    fonte=provider,
                )
            )
    elif provider == "receitaws":
        for s in raw.get("qsa") or []:
            out.append(
                PartnerPublic(
                    nome=normalize_name(s.get("nome")),
                    qualificacao=normalize_text(_QUAL_PREFIX.sub("", s.get("qual") or "")),
                    fonte=provider,
                )
            )
    elif provider == "cnpjws":
        for s in raw.get("socios") or []:
            out.append(
                PartnerPublic(
                    nome=normalize_name(s.get("nome")),
                    tipo=normalize_text(s.get("tipo")),
                    qualificacao=_descricao(s.get("qualificacao_socio")),
                    data_entrada=parse_date(s.get("data_entrada")),
                    documento_mascarado=normalize_text(s.get("cpf_cnpj_socio")),
                    fonte=provider,
                )
            )
    else:  # BrasilAPI / Minha Receita (schema identico)
        for s in raw.get("qsa") or []:
            out.append(
                PartnerPublic(
                    nome=normalize_name(s.get("nome_socio")),
                    tipo=TIPOS_SOCIO.get(code_to_int(s.get("identificador_de_socio")) or 0),
                    qualificacao=normalize_text(s.get("qualificacao_socio")),
                    data_entrada=parse_date(s.get("data_entrada_sociedade")),
                    documento_mascarado=normalize_text(s.get("cnpj_cpf_do_socio")),
                    fonte=provider,
                )
            )
    return [p for p in out if p.nome]


def _merge_partners(valid: list[ProviderResult]) -> list[PartnerPublic]:
    """Une o QSA de todas as fontes, deduplicando pelo NOME normalizado.

    ReceitaWS nao informa data de entrada nem documento; por isso a chave e
    so o nome. Campos faltantes sao completados pelas demais fontes.
    """
    merged: dict[str, PartnerPublic] = {}
    for r in valid:  # ja ordenado por prioridade
        for p in _partners_from(r.raw, r.provider):
            key = comparable(p.nome) or ""
            current = merged.get(key)
            if current is None:
                merged[key] = p
                continue
            for attr in ("tipo", "qualificacao", "data_entrada", "documento_mascarado"):
                if getattr(current, attr) is None and getattr(p, attr) is not None:
                    setattr(current, attr, getattr(p, attr))
            if r.provider not in current.fonte.split(", "):
                current.fonte = f"{current.fonte}, {r.provider}"
    return list(merged.values())


# ---------------------------------------------------------------------------
# Conciliacao
# ---------------------------------------------------------------------------


def _display(value: Any) -> str:
    if isinstance(value, bool):
        return "Sim" if value else "Nao"
    if isinstance(value, float):
        return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    return str(value)


def _comparable_value(value: Any) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
    return comparable(value)


# Tipo de logradouro: umas fontes mandam "AVENIDA PAULISTA", outras "AV PAULISTA"
# e outras so "PAULISTA" (o tipo vem em campo separado). Abreviacoes viram a
# forma por extenso; tipo AUSENTE em uma fonte nao e divergencia, tipo
# DIFERENTE ("RUA 1" x "AVENIDA 1") e.
_STREET_TYPES = {
    "AV": "AVENIDA", "AVENIDA": "AVENIDA", "R": "RUA", "RUA": "RUA",
    "AL": "ALAMEDA", "ALAMEDA": "ALAMEDA", "TV": "TRAVESSA", "TRAV": "TRAVESSA",
    "TRAVESSA": "TRAVESSA", "ROD": "RODOVIA", "RODOVIA": "RODOVIA", "EST": "ESTRADA",
    "ESTR": "ESTRADA", "ESTRADA": "ESTRADA", "PC": "PRACA", "PCA": "PRACA", "PRACA": "PRACA",
    "LGO": "LARGO", "LARGO": "LARGO", "LD": "LADEIRA", "LADEIRA": "LADEIRA", "VIELA": "VIELA",
    "BECO": "BECO", "SERVIDAO": "SERVIDAO", "PRAIA": "PRAIA", "PQ": "PARQUE", "PARQUE": "PARQUE",
    "CAM": "CAMINHO", "CAMINHO": "CAMINHO", "VD": "VIADUTO", "VIADUTO": "VIADUTO",
    "QD": "QUADRA", "QUADRA": "QUADRA",
}
# Abreviacoes comuns em bairro/complemento ("JD" = "JARDIM", "SL" = "SALA").
_ABBREVIATIONS = {
    "JD": "JARDIM", "VL": "VILA", "PQ": "PARQUE", "STA": "SANTA", "STO": "SANTO",
    "CJ": "CONJUNTO", "CONJ": "CONJUNTO", "SL": "SALA", "AND": "ANDAR", "BL": "BLOCO",
    "AP": "APARTAMENTO", "APTO": "APARTAMENTO", "LJ": "LOJA", "RES": "RESIDENCIAL",
    "DR": "DOUTOR", "PRES": "PRESIDENTE", "PROF": "PROFESSOR",
}
_ADDRESS_PARTS = ("logradouro", "numero", "complemento", "bairro", "municipio", "uf", "cep")


def _tokens(value: str | None) -> list[str]:
    return [_ABBREVIATIONS.get(t, t) for t in (comparable(value) or "").split()]


def _numero_key(numero: str | None) -> str:
    """'S/N', 'SN', 'S N' e 'SEM NUMERO' viram a mesma chave."""
    n = (comparable(numero) or "").replace(" ", "")
    return "SN" if n in {"SN", "SEMNUMERO", "SNO"} else n


def _street(logradouro: str | None, numero: str | None) -> tuple[str | None, str]:
    """'AV. Paulista 37' (numero 37) -> ('AVENIDA', 'PAULISTA'). So para comparar."""
    tokens = (comparable(logradouro) or "").split()
    tipo = None
    if len(tokens) > 1 and tokens[0] in _STREET_TYPES:
        tipo, tokens = _STREET_TYPES[tokens[0]], tokens[1:]
    num = _numero_key(numero)
    if len(tokens) > 1 and num and tokens[-1] == num:
        tokens = tokens[:-1]  # algumas fontes repetem o numero no logradouro
    return tipo, " ".join(_ABBREVIATIONS.get(t, t) for t in tokens)


def _address_part_key(addr: Address, part: str) -> Any:
    value = getattr(addr, part)
    if part == "logradouro":
        return _street(value, addr.numero)
    if part == "numero":
        return _numero_key(value)
    if part in ("complemento", "bairro"):
        return " ".join(_tokens(value))  # ordem importa: "SALA 1 ANDAR 2" != "SALA 2 ANDAR 1"
    if part == "cep":
        return value or ""
    return comparable(value) or ""


def _address_differs(entries: list[tuple[str, Address]]) -> bool:
    """Diverge se QUALQUER parte tem valores diferentes entre as fontes que a informam.

    Cada parte e comparada so entre quem a informou: uma fonte sem complemento
    apenas tem menos detalhe, mas nao "apaga" a comparacao das demais.
    """
    for part in _ADDRESS_PARTS:
        keys = [_address_part_key(a, part) for _, a in entries if getattr(a, part)]
        if part == "logradouro":
            if len({core for _, core in keys}) > 1 or len({t for t, _ in keys if t}) > 1:
                return True
        elif len(set(keys)) > 1:
            return True
    return False


def _address_confirmed(entries: list[tuple[str, Address]]) -> bool:
    """Confianca alta so quando 2+ fontes independentes informam a rua ou o CEP
    e mais alguma parte -- concordar so na UF nao confirma o endereco."""
    shared = {
        part
        for part in _ADDRESS_PARTS
        if len({_SOURCE_GROUP.get(p, p) for p, a in entries if getattr(a, part)}) >= 2
    }
    return len(shared) >= 2 and bool(shared & {"logradouro", "cep"})


def _address_display(addr: Address) -> str:
    bits: list[str] = []
    if addr.logradouro:
        bits.append(addr.logradouro)
    if addr.numero:
        bits.append(addr.numero)
    if addr.bairro:
        bits.append(addr.bairro)
    cidade = "/".join(filter(None, [addr.municipio, addr.uf]))
    if cidade:
        bits.append(cidade)
    if addr.cep:
        bits.append(addr.cep)
    return ", ".join(bits) if bits else "(vazio)"


def _phones_key(phones: list[Phone]) -> str:
    return "|".join(sorted(f"{(p.ddd or '')}{(p.numero or '')}" for p in phones))


def _phones_display(phones: list[Phone]) -> str:
    return "; ".join(f"({p.ddd}) {p.numero}" for p in phones) if phones else "(vazio)"


def _cnae_principal_key(cnae: CNAE) -> str:
    return cnae.codigo


def _cnae_principal_display(cnae: CNAE) -> str:
    if cnae.descricao:
        return f"{cnae.codigo} - {cnae.descricao}"
    return cnae.codigo


def _cnaes_secundarios_key(cnaes: list[CNAE]) -> str:
    return "|".join(sorted(c.codigo for c in cnaes))


def _cnaes_secundarios_display(cnaes: list[CNAE]) -> str:
    return ", ".join(sorted(c.codigo for c in cnaes)) if cnaes else "(vazio)"


def _record_field(
    *,
    field: str,
    label: str,
    non_empty: list[tuple[str, Any]],
    conflict_enabled: bool,
    by_provider: dict[str, ProviderResult],
    display_fn: Callable[[Any], str],
    provenance: list[FieldProvenance],
    conflitos: list[str],
    key_fn: Callable[[Any], Any] | None = None,
    differs_fn: Callable[[list[tuple[str, Any]]], bool] | None = None,
    confirmed_fn: Callable[[list[tuple[str, Any]]], bool] | None = None,
) -> Any | None:
    """Resolve um campo (escalar ou composto), anexa procedencia e conflitos.

    Divergencia: por padrao, chaves (`key_fn`) diferentes; campos compostos
    podem decidir com `differs_fn`. `confirmed_fn` diz se as fontes
    independentes de fato confirmam o valor (senao, confianca media).
    """
    if not non_empty:
        return None
    resolved = non_empty[0][1]
    groups = {_SOURCE_GROUP.get(p, p) for p, _ in non_empty}
    if differs_fn is not None:
        differs = differs_fn(non_empty)
    else:
        assert key_fn is not None
        differs = len({key_fn(v) for _, v in non_empty}) > 1
    if differs:
        confianca, obs = "baixa", "valor diverge entre fontes"
        if conflict_enabled:
            detalhes = "; ".join(f"{p}: {display_fn(v)}" for p, v in non_empty)
            conflitos.append(f"{label}: {detalhes}")
    elif len(groups) >= 2 and (confirmed_fn is None or confirmed_fn(non_empty)):
        confianca, obs = "alta", None
    elif len(groups) >= 2:
        confianca, obs = "media", "fontes informam partes diferentes, sem confirmar o valor"
    else:
        confianca = "media"
        obs = (
            "fonte unica"
            if len(non_empty) == 1
            else "confirmado apenas por espelho da mesma base"
        )

    for p, v in non_empty:
        src = by_provider.get(p)
        provenance.append(
            FieldProvenance(
                campo=field,
                valor=display_fn(v),
                fonte=p,
                data_consulta=src.fetched_at if src else None,
                data_atualizacao_fonte=src.updated_at if src else None,
                confianca=confianca,
                observacao=obs,
            )
        )
    return resolved


def reconcile(cnpj: str, results: list[ProviderResult]) -> CompanyUnified:
    """Combina os resultados de N provedores em um CompanyUnified."""
    fontes = [
        SourceEntry(
            fonte=r.provider,
            url=r.url,
            status=r.http_status,
            data_consulta=r.fetched_at,
            data_atualizacao=r.updated_at,
            campos_fornecidos=sorted(k for k in r.raw if not k.startswith("_"))[:40] if r.raw else [],
            erro=(r.extra or {}).get("error"),
            espelho_rfb=r.is_mirror_of_rfb,
        )
        for r in results
    ]

    valid = sorted(
        (r for r in results if r.http_status == 200 and r.raw), key=lambda r: _rank(r.provider)
    )
    if not valid:
        return CompanyUnified(cnpj=cnpj, cnpj_formatado=_format_cnpj(cnpj), fontes=fontes)

    primary = valid[0]
    by_provider = {r.provider: r for r in results}
    extracted = [(r.provider, _scalar_fields(r.raw, r.provider)) for r in valid]

    resolved: dict[str, Any] = {}
    provenance: list[FieldProvenance] = []
    conflitos: list[str] = []

    for field, label in _LABELS.items():
        non_empty = [(p, f[field]) for p, f in extracted if f.get(field) not in (None, "")]
        resolved[field] = _record_field(
            field=field,
            label=label,
            non_empty=non_empty,
            conflict_enabled=field in _CONFLICT_FIELDS,
            by_provider=by_provider,
            display_fn=_display,
            key_fn=_comparable_value,
            provenance=provenance,
            conflitos=conflitos,
        )

    # ---- Compostos: endereco, telefones, CNAEs (mesma logica de confianca) ----
    addr_entries = [
        (r.provider, addr)
        for r in valid
        if (addr := _address_from(r.raw, r.provider)) is not None
    ]
    endereco = _record_field(
        field="endereco",
        label="Endereco",
        non_empty=addr_entries,
        conflict_enabled=True,
        by_provider=by_provider,
        display_fn=_address_display,
        differs_fn=_address_differs,
        confirmed_fn=_address_confirmed,
        provenance=provenance,
        conflitos=conflitos,
    )

    phone_entries = [
        (r.provider, phones)
        for r in valid
        if (phones := _phones_from(r.raw, r.provider))
    ]
    telefones = _record_field(
        field="telefones",
        label="Telefones",
        non_empty=phone_entries,
        conflict_enabled=True,
        by_provider=by_provider,
        display_fn=_phones_display,
        key_fn=_phones_key,
        provenance=provenance,
        conflitos=conflitos,
    )

    cnae_entries = [
        (r.provider, cnae)
        for r in valid
        if (cnae := _cnae_principal_from(r.raw, r.provider)) is not None
    ]
    cnae_principal = _record_field(
        field="cnae_principal",
        label="CNAE principal",
        non_empty=cnae_entries,
        conflict_enabled=True,
        by_provider=by_provider,
        display_fn=_cnae_principal_display,
        key_fn=_cnae_principal_key,
        provenance=provenance,
        conflitos=conflitos,
    )

    sec_entries = [
        (r.provider, secs)
        for r in valid
        if (secs := _cnaes_secundarios_from(r.raw, r.provider))
    ]
    cnaes_secundarios = _record_field(
        field="cnaes_secundarios",
        label="CNAEs secundarios",
        non_empty=sec_entries,
        conflict_enabled=True,
        by_provider=by_provider,
        display_fn=_cnaes_secundarios_display,
        key_fn=_cnaes_secundarios_key,
        provenance=provenance,
        conflitos=conflitos,
    )

    return CompanyUnified(
        cnpj=cnpj,
        cnpj_formatado=_format_cnpj(cnpj),
        razao_social=resolved["razao_social"],
        nome_fantasia=resolved["nome_fantasia"],
        situacao_cadastral=resolved["situacao_cadastral"],
        data_situacao_cadastral=resolved["data_situacao_cadastral"],
        motivo_situacao=resolved["motivo_situacao"],
        data_abertura=resolved["data_abertura"],
        natureza_juridica=resolved["natureza_juridica"],
        porte=resolved["porte"],
        capital_social=resolved["capital_social"],
        endereco=endereco,
        telefones=telefones or [],
        email=resolved["email"],
        cnae_principal=cnae_principal,
        cnaes_secundarios=cnaes_secundarios or [],
        opcao_simples=resolved["opcao_simples"],
        opcao_mei=resolved["opcao_mei"],
        matriz_filial=resolved["matriz_filial"],
        filiais=primary.raw.get("filiais", []) if primary.provider == "receita_local" else [],
        socios=_merge_partners(valid),
        fontes=fontes,
        campos_procedencia=provenance,
        conflitos=conflitos,
    )


def _format_cnpj(cnpj: str) -> str:
    from app.core.cnpj_validator import format as fmt

    try:
        return fmt(cnpj)
    except ValueError:
        return cnpj
