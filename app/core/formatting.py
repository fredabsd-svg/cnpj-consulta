"""Formatacao para exibicao no padrao brasileiro (web e CLI)."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

try:
    _TZ_BR: ZoneInfo | None = ZoneInfo("America/Sao_Paulo")
except Exception:  # noqa: BLE001 -- Windows sem tzdata: cai para hora local
    _TZ_BR = None


def brl(value: Any) -> str:
    """1234567.8 -> 'R$ 1.234.567,80'."""
    if value is None:
        return "-"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    s = f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def data_br(value: Any) -> str:
    """date/datetime/ISO -> 'dd/mm/aaaa' (datetime vira 'dd/mm/aaaa hh:mm' no horario de Brasilia)."""
    if value is None or value == "":
        return "-"
    if isinstance(value, str):
        try:
            # "2024-01-31" e "20240131" sao so data: nao podem virar "31/01/2024 00:00"
            value = date.fromisoformat(value) if len(value) <= 10 else datetime.fromisoformat(value)
        except ValueError:
            try:
                value = datetime.strptime(value, "%Y%m%d").date()
            except ValueError:
                return value
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_TZ_BR) if _TZ_BR else value.astimezone()
        return value.strftime("%d/%m/%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    return str(value)


def cep(value: Any) -> str:
    s = "".join(c for c in str(value or "") if c.isdigit())
    return f"{s[:5]}-{s[5:]}" if len(s) == 8 else (s or "-")


def telefone(ddd: Any, numero: Any) -> str:
    n = "".join(c for c in str(numero or "") if c.isdigit())
    if len(n) == 9:
        n = f"{n[:5]}-{n[5:]}"
    elif len(n) == 8:
        n = f"{n[:4]}-{n[4:]}"
    return f"({ddd}) {n}" if ddd else n


def cnae(codigo: Any) -> str:
    """'9430800' -> '9430-8/00'."""
    s = "".join(c for c in str(codigo or "") if c.isdigit())
    if len(s) == 7:
        return f"{s[:4]}-{s[4]}/{s[5:]}"
    return s or "-"


def cnpj(value: Any) -> str:
    from app.core.cnpj_validator import format as fmt

    try:
        return fmt(str(value))
    except ValueError:
        return str(value or "-")


NOMES_FONTES = {
    "receita_local": "Receita Federal (base local)",
    "minha_receita": "Minha Receita",
    "brasilapi": "BrasilAPI",
    "receitaws": "ReceitaWS",
    "cnpjws": "CNPJ.ws",
}


def fonte(nome: Any) -> str:
    return ", ".join(NOMES_FONTES.get(p.strip(), p.strip()) for p in str(nome or "").split(","))


_FONTE_ID = re.compile(r"\b(" + "|".join(sorted(NOMES_FONTES, key=len, reverse=True)) + r")\b")


def fontes_no_texto(texto: Any) -> str:
    """'receitaws: 100,00; brasilapi: 150,00' -> 'ReceitaWS: 100,00; BrasilAPI: 150,00'."""
    return _FONTE_ID.sub(lambda m: NOMES_FONTES[m.group(1)], str(texto or ""))


def idade(value: Any, hoje: date | None = None) -> str:
    """Tempo desde a data (abertura da empresa): '12 anos', '1 ano e 3 meses', '5 meses'."""
    if not isinstance(value, date):
        return ""
    if isinstance(value, datetime):
        value = value.date()
    hoje = hoje or date.today()
    meses = (hoje.year - value.year) * 12 + (hoje.month - value.month) - (hoje.day < value.day)
    if meses < 0:
        return ""
    anos, resto = divmod(meses, 12)
    if anos == 0:
        return "menos de 1 mês" if resto == 0 else f"{resto} {'mês' if resto == 1 else 'meses'}"
    txt = f"{anos} {'ano' if anos == 1 else 'anos'}"
    if anos < 3 and resto:
        txt += f" e {resto} {'mês' if resto == 1 else 'meses'}"
    return txt


# Celulas que Excel/LibreOffice interpretariam como formula (CSV injection).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value: Any) -> Any:
    """Valor pronto para CSV: None vira vazio, datas ISO e formulas neutralizadas."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value
