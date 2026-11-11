"""Contexto compartilhado do relatorio personalizado (web e export API)."""

from __future__ import annotations

from datetime import datetime

from app.core.formatting import brl, cnae, data_br, idade
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


def resumo_executivo(c: CompanyUnified) -> list[str]:
    """Frases curtas, em linguagem comum, so com o que as fontes informaram."""
    if not c.razao_social:
        return []
    frases = []
    e = c.endereco
    local = "/".join(filter(None, [e.municipio if e else None, e.uf if e else None]))
    f1 = c.razao_social
    if c.nome_fantasia and c.nome_fantasia != c.razao_social:
        f1 += f" ({c.nome_fantasia})"
    f1 += f" é {'uma filial' if c.matriz_filial == 'FILIAL' else 'a matriz'}"
    if c.natureza_juridica:
        f1 += f" de {c.natureza_juridica}"
    if c.porte:
        f1 += f", porte {c.porte.lower()}"
    if c.data_abertura:
        tempo = idade(c.data_abertura)
        f1 += f", aberta em {data_br(c.data_abertura)}" + (f" (há {tempo})" if tempo else "")
    if local:
        f1 += f", em {local}"
    frases.append(f1 + ".")
    if c.situacao_cadastral:
        f2 = f"A situação cadastral é {c.situacao_cadastral.upper()}"
        if c.data_situacao_cadastral:
            f2 += f" desde {data_br(c.data_situacao_cadastral)}"
        if c.motivo_situacao and c.situacao_cadastral.upper() != "ATIVA" and "SEM MOTIVO" not in c.motivo_situacao.upper():
            f2 += f" (motivo: {c.motivo_situacao.lower()})"
        frases.append(f2 + ".")
    if c.cnae_principal:
        f3 = f"Atividade principal: {c.cnae_principal.descricao or 'não descrita'} (CNAE {cnae(c.cnae_principal.codigo)})"
        n = len(c.cnaes_secundarios)
        if n:
            f3 += f", além de {n} atividade{'s' if n != 1 else ''} secundária{'s' if n != 1 else ''}"
        frases.append(f3 + ".")
    partes = []
    if c.capital_social is not None:
        partes.append(f"capital social declarado de {brl(c.capital_social)}")
    n = len(c.socios)
    partes.append(f"{n} sócio{'s' if n != 1 else ''} no quadro societário" if n
                  else "nenhum sócio informado (comum em empresário individual e MEI)")
    if c.filiais:
        partes.append(f"{len(c.filiais)} filial{'is' if len(c.filiais) != 1 else ''} listada{'s' if len(c.filiais) != 1 else ''}")
    texto = ", ".join(partes[:-1]) + " e " + partes[-1] if len(partes) > 1 else partes[0]
    frases.append(texto[0].upper() + texto[1:] + ".")
    if c.opcao_mei:
        frases.append("Enquadrada como MEI (dado cadastral; não comprova regularidade fiscal).")
    elif c.opcao_simples is not None:
        frases.append(("Optante pelo Simples Nacional" if c.opcao_simples else "Não optante pelo Simples Nacional")
                      + " (dado cadastral; não comprova regularidade fiscal).")
    return frases


def indicadores(c: CompanyUnified) -> list[dict[str, str]]:
    """Numeros de destaque do topo do relatorio."""
    if c.opcao_mei:
        regime = "MEI"
    elif c.opcao_simples:
        regime = "Simples Nacional"
    elif c.opcao_simples is False:
        regime = "Fora do Simples"
    else:
        regime = "—"
    n = len(c.socios)
    return [
        {"rotulo": "Tempo de atividade", "valor": idade(c.data_abertura) or "—",
         "detalhe": f"desde {data_br(c.data_abertura)}" if c.data_abertura else "abertura não informada"},
        {"rotulo": "Capital social", "valor": brl(c.capital_social) if c.capital_social is not None else "—",
         "detalhe": c.porte or "porte não informado"},
        {"rotulo": "Sócios", "valor": str(n) if n else "—",
         "detalhe": "no quadro societário" if n else "nenhum informado"},
        {"rotulo": "Filiais", "valor": str(len(c.filiais)) if c.filiais else "—",
         "detalhe": {"MATRIZ": "este CNPJ é a matriz", "FILIAL": "este CNPJ é filial"}.get(c.matriz_filial or "", "")},
        {"rotulo": "Regime", "valor": regime, "detalhe": "dado cadastral"},
    ]


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
        "resumo_executivo": resumo_executivo(company),
        "indicadores": indicadores(company),
        "status_labels": STATUS_LABELS,
        "consulta_em": data_br(agora),
        "protocolo": protocolo,
        "escritorio": clip_meta(escritorio),
        "responsavel": clip_meta(responsavel),
        "referencia": clip_meta(referencia),
        "cliente": clip_meta(cliente),
    }
