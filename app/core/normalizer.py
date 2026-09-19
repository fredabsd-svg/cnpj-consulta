"""Normalizacao de campos: nomes, telefones, CEP, datas."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from unidecode import unidecode

_PHONE_PATTERN = re.compile(r"\D+")


def normalize_text(value: Any) -> str | None:
    """Remove espacos das pontas e colapsa espacos internos (mantem caixa e acentos).

    Aceita numeros (algumas APIs mandam "numero" do endereco como int).
    """
    if value is None or isinstance(value, (dict, list)):
        return None
    v = str(value).strip()
    if not v:
        return None
    # Colapsa espacos multiplos
    v = re.sub(r"\s+", " ", v)
    return v


def normalize_name(value: str | None) -> str | None:
    """Normaliza nome proprio: Title Case, sem multiplos espacos."""
    n = normalize_text(value)
    if not n:
        return None
    # Title Case respeitando preposicoes minusculas comuns em portugues
    preps = {"de", "da", "do", "das", "dos", "e"}
    parts = n.lower().split(" ")
    out: list[str] = []
    for i, p in enumerate(parts):
        if i > 0 and p in preps:
            out.append(p)
        else:
            out.append(p.capitalize())
    return " ".join(out)


def normalize_name_for_search(value: str | None) -> str | None:
    """Normaliza nome para busca: sem acento, uppercase, sem pontuacao.

    Usado em queries LIKE/ILIKE contra a base local.
    """
    n = normalize_text(value)
    if not n:
        return None
    n = unidecode(n).upper()
    n = re.sub(r"[^A-Z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def normalize_phone(value: Any) -> str | None:
    """Mantem apenas digitos do telefone."""
    if not value:
        return None
    digits = _PHONE_PATTERN.sub("", str(value))
    return digits or None


def normalize_cep(value: Any) -> str | None:
    """CEP como 8 digitos (sem hifen). Aceita int sem zero a esquerda."""
    if not value:
        return None
    digits = _PHONE_PATTERN.sub("", str(value))
    if isinstance(value, int):
        digits = digits.zfill(8)
    return digits[:8] if len(digits) >= 8 else None


def parse_date(value: Any) -> date | None:
    """Converte string em date de forma robusta.

    Aceita ISO (YYYY-MM-DD) e formatos brasileiros comuns (DD/MM/YYYY).
    Retorna None se nao conseguir parsear.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value if not isinstance(value, datetime) else value.date()
    if isinstance(value, (int, float)):
        # Provavelmente timestamp -- nao tratamos aqui.
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    formats = (
        "%Y%m%d",  # base da Receita Federal (ex.: 20200101)
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%d/%m/%Y",
        "%d-%m-%Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_decimal(value: Any) -> float | None:
    """Converte string numerica em float, aceitando formato brasileiro e internacional.

    - "1.234.567,89" / "1000,00"  -> virgula e o separador decimal
    - "1,234,567.89" / "1000.00"  -> ponto e o separador decimal
    - "1.234.567"                 -> varios pontos sem virgula = milhar

    O separador decimal e sempre o ULTIMO entre ponto e virgula; um ponto
    unico sem virgula e tratado como decimal (formato das APIs JSON).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip().replace(" ", "").removeprefix("R$")
    if not s:
        return None
    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        s = s.replace(",", ".") if s.count(",") == 1 else s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def comparable(value: Any) -> str | None:
    """Forma canonica para comparar valores entre fontes (sem acento, caixa alta)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "SIM" if value else "NAO"
    if isinstance(value, float):
        return f"{value:.2f}"
    s = normalize_name_for_search(str(value))
    return s or None
