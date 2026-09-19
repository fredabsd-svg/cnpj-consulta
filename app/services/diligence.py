"""Checklist de diligencia para uso contabil / KYC basico.

Somente dados publicos ja presentes em CompanyUnified — sem PII extra.
CPFs de socios permanecem mascarados (LGPD).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.schemas.company import CompanyUnified

StatusOk = Literal["ok", "alerta", "falha", "info"]


@dataclass(frozen=True, slots=True)
class DiligenceItem:
    """Item do checklist de diligencia."""

    id: str
    rotulo: str
    status: StatusOk
    detalhe: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _situacao_item(company: CompanyUnified) -> DiligenceItem:
    sit = (company.situacao_cadastral or "").strip().upper()
    if sit == "ATIVA":
        return DiligenceItem(
            "situacao_ativa",
            "Situacao cadastral ATIVA",
            "ok",
            "Empresa ativa na Receita Federal (conforme fontes).",
        )
    if not sit:
        return DiligenceItem(
            "situacao_ativa",
            "Situacao cadastral ATIVA",
            "alerta",
            "Situacao nao informada pelas fontes.",
        )
    return DiligenceItem(
        "situacao_ativa",
        "Situacao cadastral ATIVA",
        "falha",
        f"Situacao atual: {sit}. Confirme na fonte oficial antes de operar.",
    )


def _simples_mei_item(company: CompanyUnified) -> DiligenceItem:
    partes: list[str] = []
    if company.opcao_simples is None and company.opcao_mei is None:
        return DiligenceItem(
            "simples_mei",
            "Flags Simples / MEI informadas",
            "alerta",
            "Simples Nacional e MEI nao informados pelas fontes consultadas.",
        )
    if company.opcao_simples is True:
        partes.append("optante do Simples")
    elif company.opcao_simples is False:
        partes.append("nao optante do Simples")
    else:
        partes.append("Simples nao informado")
    if company.opcao_mei is True:
        partes.append("optante MEI")
    elif company.opcao_mei is False:
        partes.append("nao MEI")
    else:
        partes.append("MEI nao informado")
    return DiligenceItem(
        "simples_mei",
        "Flags Simples / MEI informadas",
        "ok",
        "; ".join(partes).capitalize() + ".",
    )


def _capital_item(company: CompanyUnified) -> DiligenceItem:
    if company.capital_social is None:
        return DiligenceItem(
            "capital",
            "Capital social informado",
            "alerta",
            "Capital social nao informado.",
        )
    from app.core.formatting import brl

    return DiligenceItem(
        "capital",
        "Capital social informado",
        "ok",
        f"Capital social presente ({brl(company.capital_social)}).",
    )


def _qsa_item(company: CompanyUnified) -> DiligenceItem:
    n = len(company.socios)
    if n == 0:
        return DiligenceItem(
            "qsa",
            "Quadro societario (QSA) presente",
            "info",
            "Nenhum socio informado (comum em EI/MEI). Confirme se esperado.",
        )
    return DiligenceItem(
        "qsa",
        "Quadro societario (QSA) presente",
        "ok",
        f"{n} socio(s)/administrador(es) listado(s). CPFs mascarados (LGPD).",
    )


def _divergencias_item(company: CompanyUnified) -> DiligenceItem:
    n = len(company.conflitos)
    if n == 0:
        return DiligenceItem(
            "divergencias",
            "Divergencias entre fontes",
            "ok",
            "Nenhuma divergencia detectada entre as fontes consultadas.",
        )
    return DiligenceItem(
        "divergencias",
        "Divergencias entre fontes",
        "alerta" if n < 3 else "falha",
        f"{n} campo(s) com divergencia. Revise antes de usar o dado.",
    )


def _fontes_item(company: CompanyUnified) -> DiligenceItem:
    total = len(company.fontes)
    ok = sum(1 for f in company.fontes if f.status == 200)
    if total == 0:
        return DiligenceItem(
            "fontes_ok",
            "Fontes consultadas OK",
            "falha",
            "Nenhuma fonte habilitada ou consultada.",
        )
    if ok == 0:
        return DiligenceItem(
            "fontes_ok",
            "Fontes consultadas OK",
            "falha",
            f"0 de {total} fontes responderam OK.",
        )
    if ok < total:
        return DiligenceItem(
            "fontes_ok",
            "Fontes consultadas OK",
            "alerta",
            f"{ok} de {total} fontes responderam OK.",
        )
    return DiligenceItem(
        "fontes_ok",
        "Fontes consultadas OK",
        "ok",
        f"{ok} de {total} fontes responderam OK.",
    )


def build_diligence_checklist(company: CompanyUnified) -> list[DiligenceItem]:
    """Monta o checklist de diligencia a partir da consulta unificada."""
    return [
        _situacao_item(company),
        _simples_mei_item(company),
        _capital_item(company),
        _qsa_item(company),
        _divergencias_item(company),
        _fontes_item(company),
    ]


def diligence_summary(items: list[DiligenceItem]) -> dict[str, int]:
    """Contagem por status para badges e testes."""
    counts = {"ok": 0, "alerta": 0, "falha": 0, "info": 0}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts
