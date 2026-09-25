"""Consulta em lote: varios CNPJs de uma vez, com resumo por empresa.

- Aceita CNPJs separados por linha, espaco, virgula ou ponto e virgula, com
  ou sem mascara (numericos e alfanumericos). Duplicados sao ignorados.
- Consulta com concorrencia limitada; o cache local serve o que ja foi visto.
- O limite de entrada por IP vale por CNPJ que precisa ir as fontes (cache
  nao conta): quando acaba, os restantes voltam como "limite atingido".
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from app.core.cnpj_validator import format as format_cnpj
from app.core.cnpj_validator import normalize_or_none, strip
from app.core.formatting import csv_cell
from app.services.company_query import query_company_async
from app.services.diligence import build_diligence_checklist, build_diligence_verdict

_SPLIT = re.compile(r"[\s,;|]+")
CONCURRENCY = 4


class BatchLimitError(RuntimeError):
    """Limite de consultas por minuto atingido no meio do lote."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"limite de consultas atingido; libera em {wait:.0f}s")
        self.wait = wait


@dataclass(slots=True)
class BatchRow:
    cnpj: str
    cnpj_formatado: str
    status: str  # ok | nao_encontrado | limite | erro
    mensagem: str | None = None
    razao_social: str | None = None
    nome_fantasia: str | None = None
    situacao: str | None = None
    uf: str | None = None
    municipio: str | None = None
    data_abertura: str | None = None
    porte: str | None = None
    opcao_simples: bool | None = None
    opcao_mei: bool | None = None
    cnae_principal: str | None = None
    fontes_ok: int = 0
    fontes_total: int = 0
    conflitos: int = 0
    veredicto: str | None = None  # regular | ressalvas | critico
    do_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ParsedBatch:
    validos: list[str]
    invalidos: list[str]
    excedentes: int  # validos alem do maximo permitido (descartados)


def parse_cnpj_list(text: str, max_items: int) -> ParsedBatch:
    """Separa, valida e deduplica os CNPJs digitados (preserva a ordem)."""
    validos: list[str] = []
    invalidos: list[str] = []
    vistos: set[str] = set()
    for token in _SPLIT.split(text or ""):
        if not strip(token):
            continue
        n = normalize_or_none(token)
        if n is None:
            if token not in invalidos:
                invalidos.append(token[:24])
            continue
        if n not in vistos:
            vistos.add(n)
            validos.append(n)
    excedentes = max(0, len(validos) - max_items)
    return ParsedBatch(validos[:max_items], invalidos[:50], excedentes)


async def run_batch(
    cnpjs: list[str],
    *,
    reserve: Callable[[], float] | None = None,
    concurrency: int = CONCURRENCY,
) -> list[BatchRow]:
    """Consulta cada CNPJ e devolve uma linha-resumo por empresa, na ordem pedida.

    `reserve` devolve 0 quando ha vaga no limite de entrada (e consome a vaga)
    ou os segundos ate liberar; so e chamado quando o cache nao serve.
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    def on_miss() -> None:
        if reserve is not None and (wait := reserve()) > 0:
            raise BatchLimitError(wait)

    async def one(cnpj: str) -> BatchRow:
        row = BatchRow(cnpj=cnpj, cnpj_formatado=format_cnpj(cnpj), status="erro")
        async with sem:
            try:
                c = await query_company_async(cnpj, on_miss=on_miss)
            except BatchLimitError as e:
                row.status = "limite"
                row.mensagem = f"Limite de consultas por minuto atingido; tente de novo em {e.wait:.0f}s."
                return row
            except Exception:  # noqa: BLE001 -- uma falha nao derruba o lote
                row.mensagem = "Falha inesperada ao consultar."
                return row
        row.fontes_total = len(c.fontes)
        row.fontes_ok = sum(1 for f in c.fontes if f.status == 200)
        row.do_cache = c.origem_cache
        if not c.razao_social:
            row.status = "nao_encontrado"
            row.mensagem = "Nenhuma fonte retornou dados."
            return row
        e = c.endereco
        row.status = "ok"
        row.razao_social = c.razao_social
        row.nome_fantasia = c.nome_fantasia
        row.situacao = c.situacao_cadastral
        row.uf = e.uf if e else None
        row.municipio = e.municipio if e else None
        row.data_abertura = c.data_abertura.isoformat() if c.data_abertura else None
        row.porte = c.porte
        row.opcao_simples = c.opcao_simples
        row.opcao_mei = c.opcao_mei
        row.cnae_principal = c.cnae_principal.codigo if c.cnae_principal else None
        row.conflitos = len(c.conflitos)
        row.veredicto = build_diligence_verdict(build_diligence_checklist(c)).nivel
        return row

    return list(await asyncio.gather(*(one(c) for c in cnpjs)))


def summarize(rows: list[BatchRow]) -> dict[str, int]:
    """Contagens para os cartoes de resumo."""
    return {
        "total": len(rows),
        "ativas": sum(1 for r in rows if (r.situacao or "").upper() == "ATIVA"),
        "irregulares": sum(
            1 for r in rows if r.status == "ok" and (r.situacao or "").upper() not in ("", "ATIVA")
        ),
        "com_conflito": sum(1 for r in rows if r.conflitos),
        "nao_encontradas": sum(1 for r in rows if r.status == "nao_encontrado"),
        "pendentes": sum(1 for r in rows if r.status in ("limite", "erro")),
    }


CSV_COLUMNS = (
    ("cnpj", "CNPJ"),
    ("razao_social", "Razão social"),
    ("nome_fantasia", "Nome fantasia"),
    ("situacao", "Situação"),
    ("uf", "UF"),
    ("municipio", "Município"),
    ("data_abertura", "Abertura"),
    ("porte", "Porte"),
    ("opcao_simples", "Simples"),
    ("opcao_mei", "MEI"),
    ("cnae_principal", "CNAE principal"),
    ("conflitos", "Divergências"),
    ("fontes_ok", "Fontes OK"),
    ("veredicto", "Veredicto"),
    ("status", "Status da consulta"),
    ("mensagem", "Observação"),
)


def _csv_value(value: object) -> object:
    if isinstance(value, bool):
        return "Sim" if value else "Não"
    return csv_cell(value)


def batch_csv(rows: list[BatchRow], *, excel: bool = True) -> bytes | str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";" if excel else ",")
    writer.writerow([label for _, label in CSV_COLUMNS])
    for r in rows:
        data = r.to_dict()
        writer.writerow([_csv_value(data[key]) for key, _ in CSV_COLUMNS])
    text = buf.getvalue()
    return ("﻿" + text).encode("utf-8") if excel else text
