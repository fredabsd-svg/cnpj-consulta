"""API da busca avancada, da base local (consulta) e das listas.

Leitura e livre (como o restante do app, que so escuta em 127.0.0.1); as
operacoes administrativas ficam em app/api/admin.py.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.config import get_settings
from app.core.cnpj_validator import normalize_or_none
from app.receita import jobs
from app.services import busca_avancada as ba
from app.services import contatos as contatos_srv
from app.services import exportacao, ficha, listas

router = APIRouter(prefix="/api", tags=["busca"])


def params_de(request: Request) -> dict[str, Any]:
    """Query string -> dict com listas para nomes repetidos (uf=TO&uf=GO)."""
    out: dict[str, Any] = {}
    for k, v in request.query_params.multi_items():
        out.setdefault(k, []).append(v)
    return out


async def cancelavel[T](request: Request, fn: Callable[[threading.Event], T]) -> T:
    """Roda a consulta em thread e a interrompe se o cliente desistir
    (nova busca no navegador, aba fechada)."""
    evento = threading.Event()
    tarefa = asyncio.ensure_future(asyncio.to_thread(fn, evento))
    try:
        while not tarefa.done():
            await asyncio.wait({tarefa}, timeout=0.3)
            if not tarefa.done() and await request.is_disconnected():
                evento.set()
        return tarefa.result()
    except BaseException:
        evento.set()
        raise


def _erro(e: Exception) -> HTTPException:
    if isinstance(e, ba.FiltroInvalido):
        return HTTPException(400, str(e))
    if isinstance(e, ba.BuscaCancelada):
        return HTTPException(408, str(e))
    if isinstance(e, ba.BuscaIndisponivel | ficha.BaseIndisponivel):
        return HTTPException(503, str(e))
    raise e


def _filtros(request: Request) -> ba.Filtros:
    try:
        return ba.Filtros.de_params(params_de(request))
    except ba.FiltroInvalido as e:
        raise HTTPException(400, str(e)) from e


@router.get("/busca/campos")
async def campos() -> dict:
    """Catalogo de filtros, ordenacoes e colunas disponiveis."""
    return {
        "campos": ba.descricao_campos(),
        "grupos": ba.GRUPOS_CAMPOS,
        "ordenacoes": {k: v[0] for k, v in ba.ORDENACOES.items()},
        "colunas": {k: {"rotulo": v[0], "restrita": v[2]} for k, v in ba.COLUNAS.items()},
        "colunas_padrao": ba.COLUNAS_PADRAO,
    }


@router.get("/busca")
async def busca(request: Request, colunas: list[str] = Query(None), contar: bool = True) -> dict:
    """Busca avancada. Os filtros vao na query string (ver /api/busca/campos);
    valores repetidos do mesmo filtro sao OU; filtros diferentes sao E;
    `excluir_<filtro>` nega; `arvore` recebe o construtor E/OU/NAO em JSON."""
    f = _filtros(request)
    try:
        r = await cancelavel(request, lambda ev: ba.buscar(f, colunas=colunas, cancelar=ev, contar=contar))
    except Exception as e:  # noqa: BLE001
        raise _erro(e) from e
    if f.pagina == 1 and not f.vazio():
        await asyncio.to_thread(listas.registrar_recente, f.para_params(), ba.resumo_texto(f), r.total)
    return {
        "total": r.total, "pagina": r.pagina, "por_pagina": r.por_pagina, "paginas": r.paginas,
        "ordem": r.ordem, "direcao": r.direcao, "tempo_ms": r.tempo_ms,
        "mes_referencia": r.mes_referencia, "fonte": "Receita Federal — dados abertos do CNPJ (base local)",
        "itens": r.itens,
    }


@router.get("/busca/contar")
async def busca_contar(request: Request) -> dict:
    f = _filtros(request)
    try:
        n = await cancelavel(request, lambda ev: ba.contar(f))
    except Exception as e:  # noqa: BLE001
        raise _erro(e) from e
    return {"total": n}


@router.get("/busca/contadores")
async def busca_contadores(request: Request, dimensao: list[str] = Query(["uf", "situacao", "porte"])) -> dict:
    f = _filtros(request)
    try:
        return await cancelavel(request, lambda ev: ba.contadores(f, dimensao, cancelar=ev))
    except Exception as e:  # noqa: BLE001
        raise _erro(e) from e


@router.post("/busca/duplicidades")
async def busca_duplicidades(cnpjs: list[str] = Body(..., embed=True, max_length=200)) -> dict:
    try:
        return await asyncio.to_thread(ba.duplicidades, cnpjs)
    except Exception as e:  # noqa: BLE001
        raise _erro(e) from e


@router.get("/busca/exportar.{formato}")
async def busca_exportar(
    request: Request,
    formato: str,
    colunas: list[str] = Query(None),
    finalidade: str = Query("", max_length=300),
    selecionados: str = Query("", max_length=20_000, description="CNPJs separados por virgula (opcional)"),
):
    """Exporta o resultado (ou so os selecionados) em CSV ou Excel."""
    if formato not in ("csv", "xlsx"):
        raise HTTPException(404, "Formato não suportado.")
    params = params_de(request)
    for k in ("colunas", "finalidade", "selecionados", "pagina", "por_pagina"):
        params.pop(k, None)
    if selecionados.strip():
        params["cnpj"] = [c for c in selecionados.split(",") if c.strip()][:5000]
    try:
        f = ba.Filtros.de_params(params)
        cols = exportacao.validar_colunas(colunas, finalidade)
    except (ba.FiltroInvalido, exportacao.ExportacaoInvalida) as e:
        raise HTTPException(400, str(e)) from e
    settings = get_settings()
    try:
        total = await cancelavel(request, lambda ev: ba.contar(f))
    except Exception as e:  # noqa: BLE001
        raise _erro(e) from e
    limite = min(total, settings.exportacao_max_linhas)
    jobs.auditar(
        "busca.exportar",
        detalhe=f"formato={formato} linhas={limite} colunas={','.join(cols)} finalidade={finalidade.strip()[:200]}",
        ip=request.client.host if request.client else None,
    )
    if total > settings.exportacao_sincrona_max:
        exp = await asyncio.to_thread(exportacao.criar_exportacao, f, cols, formato, finalidade, limite)
        return {"segundo_plano": True, "exportacao": exp,
                "mensagem": f"{limite:,} linhas: a exportação continua em segundo plano.".replace(",", ".")}
    nome = f"busca-cnpj.{formato}"
    if formato == "csv":
        return StreamingResponse(
            exportacao.gerar_csv(f, cols, limite), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{nome}"'},
        )
    path = await asyncio.to_thread(exportacao.xlsx_temporario, f, cols, limite)
    from starlette.background import BackgroundTask

    return FileResponse(
        path, filename=nome,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.get("/exportacoes")
async def exportacoes_lista() -> list[dict]:
    return await asyncio.to_thread(exportacao.listar)


@router.get("/exportacoes/{exp_id}")
async def exportacao_status(exp_id: int) -> dict:
    exp = await asyncio.to_thread(exportacao.obter, exp_id)
    if not exp:
        raise HTTPException(404, "Exportação não encontrada.")
    return exp


@router.get("/exportacoes/{exp_id}/arquivo")
async def exportacao_arquivo(exp_id: int):
    exp = await asyncio.to_thread(exportacao.obter, exp_id)
    path = exportacao.caminho_arquivo(exp) if exp else None
    if not path:
        raise HTTPException(404, "Arquivo indisponível (ainda em andamento ou removido).")
    return FileResponse(path, filename=path.name)


# ---------- base local: consulta ----------


@router.get("/base/sugestoes")
async def sugestoes(q: str = Query("", max_length=100), limite: int = Query(8, ge=1, le=20)) -> list[dict]:
    """Autocompletar por CNPJ, razao social ou nome fantasia (prefixo)."""
    return await asyncio.to_thread(ficha.sugestoes, q, limite)


@router.get("/base/municipios")
async def municipios(uf: list[str] = Query(...)) -> list[dict]:
    try:
        return await asyncio.to_thread(ba.lista_municipios, uf[:27])
    except Exception as e:  # noqa: BLE001
        raise _erro(e) from e


@router.get("/base/dominio/{tabela}")
async def dominio(tabela: str, q: str = Query("", max_length=100)) -> list[dict]:
    """Tabelas oficiais de codigos: cnaes, naturezas, motivos, qualificacoes, paises."""
    try:
        return await asyncio.to_thread(ba.lista_dominio, tabela, q)
    except Exception as e:  # noqa: BLE001
        raise _erro(e) from e


@router.get("/base/atividades")
async def atividades() -> dict:
    """Palavras-chave de atividade e os prefixos CNAE oficiais que representam."""
    from app.services.cnae_palavras import PALAVRAS_CHAVE

    return PALAVRAS_CHAVE


@router.get("/base/empresa/{cnpj}")
async def ficha_empresa(cnpj: str) -> dict:
    """Ficha completa da base local (dados oficiais + contatos complementares)."""
    n = normalize_or_none(cnpj)
    if n is None:
        raise HTTPException(400, "CNPJ inválido.")
    try:
        dados = await asyncio.to_thread(ficha.ficha_local, n)
    except Exception as e:  # noqa: BLE001
        raise _erro(e) from e
    if dados is None:
        raise HTTPException(404, "CNPJ não encontrado na base local.")
    dados["contatos_complementares"] = await asyncio.to_thread(contatos_srv.listar, n)
    return dados


@router.get("/base/socio/{chave}")
async def ficha_socio(chave: str) -> dict:
    try:
        dados = await asyncio.to_thread(ficha.ficha_socio, chave)
    except Exception as e:  # noqa: BLE001
        raise _erro(e) from e
    if dados is None:
        raise HTTPException(404, "Sócio não encontrado.")
    return dados


# ---------- buscas salvas, recentes, listas ----------


@router.get("/buscas-salvas")
async def buscas_salvas() -> list[dict]:
    return await asyncio.to_thread(listas.buscas_salvas)


@router.post("/buscas-salvas", status_code=201)
async def salvar_busca(nome: str = Body(..., max_length=120), filtros: dict = Body(...)) -> dict:
    try:
        f = ba.Filtros.de_params(filtros)  # valida antes de guardar
        ba.compilar(f)
        return await asyncio.to_thread(listas.salvar_busca, nome, filtros)
    except (ValueError, ba.FiltroInvalido) as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/buscas-salvas/{busca_id}", status_code=204)
async def remover_busca(busca_id: int) -> None:
    try:
        await asyncio.to_thread(listas.remover_busca, busca_id)
    except LookupError as e:
        raise HTTPException(404, "Busca não encontrada.") from e


@router.get("/buscas-recentes")
async def buscas_recentes() -> list[dict]:
    return await asyncio.to_thread(listas.recentes, 20)


@router.get("/listas")
async def listas_todas() -> list[dict]:
    return await asyncio.to_thread(listas.listas)


@router.post("/listas", status_code=201)
async def criar_lista(nome: str = Body(..., max_length=120), descricao: str = Body("", max_length=500)) -> dict:
    try:
        return await asyncio.to_thread(listas.criar_lista, nome, descricao)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/listas/{lista_id}")
async def obter_lista(lista_id: int) -> dict:
    d = await asyncio.to_thread(listas.obter_lista, lista_id)
    if d is None:
        raise HTTPException(404, "Lista não encontrada.")
    return d


@router.post("/listas/{lista_id}/itens")
async def adicionar_itens(lista_id: int, itens: list[dict] = Body(..., embed=True, max_length=5000)) -> dict:
    try:
        n = await asyncio.to_thread(listas.adicionar, lista_id, itens)
    except LookupError as e:
        raise HTTPException(404, "Lista não encontrada.") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"adicionados": n}


@router.patch("/listas/{lista_id}/itens/{cnpj}")
async def anotar_item(lista_id: int, cnpj: str, observacao: str = Body("", embed=True, max_length=1000)) -> dict:
    try:
        await asyncio.to_thread(listas.anotar, lista_id, cnpj, observacao)
    except LookupError as e:
        raise HTTPException(404, "Item não encontrado.") from e
    return {"ok": True}


@router.delete("/listas/{lista_id}/itens/{cnpj}", status_code=204)
async def remover_item(lista_id: int, cnpj: str) -> None:
    try:
        await asyncio.to_thread(listas.remover_item, lista_id, cnpj)
    except LookupError as e:
        raise HTTPException(404, "Item não encontrado.") from e


@router.delete("/listas/{lista_id}", status_code=204)
async def remover_lista(lista_id: int) -> None:
    try:
        await asyncio.to_thread(listas.remover_lista, lista_id)
    except LookupError as e:
        raise HTTPException(404, "Lista não encontrada.") from e
