"""Formatacao para exibicao no padrao brasileiro (web e CLI)."""

from __future__ import annotations

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
            value = datetime.fromisoformat(value)
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
