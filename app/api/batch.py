"""Endpoints REST: /api/batch (consulta em lote)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app.config import get_settings
from app.core.inbound_limit import reserver_for
from app.services.batch import BatchRow, batch_csv, parse_cnpj_list, run_batch, summarize

router = APIRouter(prefix="/api/batch", tags=["batch"])

_CNPJS = Query(
    ...,
    max_length=4000,
    description="CNPJs separados por virgula, espaco, ponto e virgula ou quebra de linha",
)


async def load_batch(request: Request, cnpjs: str) -> tuple[dict, list[BatchRow]]:
    parsed = parse_cnpj_list(cnpjs, get_settings().batch_max_items)
    if not parsed.validos:
        raise HTTPException(status_code=400, detail="Nenhum CNPJ valido informado.")
    rows = await run_batch(parsed.validos, reserve=reserver_for(request))
    meta = {
        "validos": len(parsed.validos),
        "invalidos": parsed.invalidos,
        "excedentes": parsed.excedentes,
        "maximo": get_settings().batch_max_items,
        "resumo": summarize(rows),
    }
    return meta, rows


@router.get("")
async def batch(request: Request, cnpjs: str = _CNPJS) -> dict:
    """Consulta varios CNPJs e devolve um resumo por empresa."""
    meta, rows = await load_batch(request, cnpjs)
    return meta | {"resultados": [r.to_dict() for r in rows]}


@router.get("/export.csv")
async def batch_export_csv(
    request: Request,
    cnpjs: str = _CNPJS,
    excel: bool = Query(True, description="true: ';' e BOM UTF-8 (Excel pt-BR); false: ','"),
) -> Response:
    """Exporta a consulta em lote em CSV (uma linha por empresa)."""
    _, rows = await load_batch(request, cnpjs)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return Response(
        batch_csv(rows, excel=excel),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="cnpj_lote_{stamp}.csv"'},
    )
