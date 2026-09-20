"""Checklist de diligencia para uso contabil / KYC basico.

Somente dados publicos ja presentes em CompanyUnified — sem PII extra.
CPFs de socios permanecem mascarados (LGPD).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.schemas.company import CompanyUnified

StatusOk = Literal["ok", "alerta", "falha", "info"]

STATUS_LABELS: dict[str, str] = {
    "ok": "Regular",
    "alerta": "Atenção",
    "falha": "Crítico",
    "info": "Informativo",
}


@dataclass(frozen=True, slots=True)
class DiligenceItem:
    id: str
    rotulo: str
    status: StatusOk
    detalhe: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DiligenceVerdict:
    nivel: Literal["regular", "ressalvas", "critico"]
    titulo: str
    detalhe: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _situacao_item(company: CompanyUnified) -> DiligenceItem:
    sit = (company.situacao_cadastral or "").strip().upper()
    if sit == "ATIVA":
        return DiligenceItem("situacao_ativa", "Situação cadastral", "ok", "Situação cadastral ativa.")
    if not sit:
        return DiligenceItem("situacao_ativa", "Situação cadastral", "alerta", "Situação não informada.")
    return DiligenceItem("situacao_ativa", "Situação cadastral", "falha", f"Situação atual: {sit}.")


def _simples_mei_item(company: CompanyUnified) -> DiligenceItem:
    if company.opcao_simples is None and company.opcao_mei is None:
        return DiligenceItem("simples_mei", "Simples / MEI", "alerta", "Simples e MEI não informados.")
    return DiligenceItem("simples_mei", "Simples / MEI", "ok", "Enquadramento informado.")


def _capital_item(company: CompanyUnified) -> DiligenceItem:
    if company.capital_social is None:
        return DiligenceItem("capital", "Capital social", "alerta", "Capital social não informado.")
    return DiligenceItem("capital", "Capital social", "ok", "Capital social informado.")


def _qsa_item(company: CompanyUnified) -> DiligenceItem:
    if not company.socios:
        return DiligenceItem("qsa", "Quadro societário", "info", "Nenhum sócio informado.")
    return DiligenceItem("qsa", "Quadro societário", "ok", f"{len(company.socios)} sócio(s) listado(s).")


def _divergencias_item(company: CompanyUnified) -> DiligenceItem:
    n = len(company.conflitos)
    if n == 0:
        return DiligenceItem("divergencias", "Divergências", "ok", "Fontes alinhadas neste recorte.")
    return DiligenceItem(
        "divergencias",
        "Divergências",
        "alerta" if n < 3 else "falha",
        f"{n} campo(s) com valor diferente entre fontes.",
    )


def _fontes_item(company: CompanyUnified) -> DiligenceItem:
    total = len(company.fontes)
    ok = sum(1 for f in company.fontes if f.status == 200)
    if total == 0 or ok == 0:
        return DiligenceItem("fontes_ok", "Fontes", "falha", "Nenhuma fonte respondeu.")
    if ok < total:
        return DiligenceItem("fontes_ok", "Fontes", "alerta", f"{ok} de {total} fontes responderam.")
    return DiligenceItem("fontes_ok", "Fontes", "ok", f"{ok} fontes responderam.")


def build_diligence_checklist(company: CompanyUnified) -> list[DiligenceItem]:
    return [
        _situacao_item(company),
        _simples_mei_item(company),
        _capital_item(company),
        _qsa_item(company),
        _divergencias_item(company),
        _fontes_item(company),
    ]


def diligence_summary(items: list[DiligenceItem]) -> dict[str, int]:
    counts = {"ok": 0, "alerta": 0, "falha": 0, "info": 0}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


def build_diligence_verdict(items: list[DiligenceItem]) -> DiligenceVerdict:
    counts = diligence_summary(items)
    if counts["falha"]:
        return DiligenceVerdict(
            "critico",
            "Não use este recorte sozinho",
            "Há ponto crítico no cadastro. Confira na Receita Federal antes de contratar ou protocolar.",
        )
    if counts["alerta"]:
        return DiligenceVerdict(
            "ressalvas",
            "Siga com ressalvas",
            "Há ponto de atenção abaixo. O restante do cadastro pode ser usado com checagem pontual.",
        )
    return DiligenceVerdict(
        "regular",
        "Cadastro ativo, sem ressalvas neste recorte",
        "As fontes consultadas não apontaram conflito nem situação irregular. Isto não substitui certidão oficial.",
    )
