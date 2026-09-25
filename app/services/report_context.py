"""Contexto compartilhado do relatorio personalizado (web e export API)."""

from __future__ import annotations

from datetime import datetime

from app.core.formatting import data_br
from app.schemas.company import CompanyUnified
from app.services.diligence import (
    STATUS_LABELS,
    build_diligence_checklist,
    build_diligence_verdict,
    diligence_summary,
)
from app.services.sanctions import SanctionsCheck

_MAX_META = 80


def clip_meta(value: str | None, limit: int = _MAX_META) -> str:
    """Texto curto para cabecalho do dossie — sem persistir no banco."""
    if not value:
        return ""
    return " ".join(str(value).split())[:limit]


def build_report_context(
    company: CompanyUnified,
    *,
    sanctions: SanctionsCheck | None = None,
    escritorio: str = "",
    responsavel: str = "",
    referencia: str = "",
    cliente: str = "",
) -> dict:
    """Monta o contexto Jinja do relatorio A4."""
    diligence = build_diligence_checklist(company, sanctions)
    agora = datetime.now().astimezone()
    protocolo = f"DOC-{company.cnpj}-{agora.strftime('%Y%m%d-%H%M')}"
    return {
        "company": company,
        "diligence": diligence,
        "verdict": build_diligence_verdict(diligence),
        "summary": diligence_summary(diligence),
        "status_labels": STATUS_LABELS,
        "consulta_em": data_br(agora),
        "protocolo": protocolo,
        "escritorio": clip_meta(escritorio),
        "responsavel": clip_meta(responsavel),
        "referencia": clip_meta(referencia),
        "cliente": clip_meta(cliente),
    }
