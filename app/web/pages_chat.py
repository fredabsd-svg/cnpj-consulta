"""Chat com IA (/chat): pagina, configuracao de provedores e conversa em fluxo.

So para o administrador: o modelo usa todas as funcoes do app, inclusive as
que alteram dados. As chaves dos provedores nunca saem do servidor.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from app.chat import conversas, motor, provedores
from app.chat.ferramentas import arquivo_exportado_valido
from app.config import get_settings
from app.receita import jobs
from app.receita.armazenamento import exportacoes_dir
from app.services.admin_auth import exigir_admin_api, exigir_admin_pagina, ip_de
from app.web.pages import _render

router = APIRouter(include_in_schema=False)
MAX_MENSAGEM = 20_000


def _habilitado() -> None:
    if not get_settings().chat_habilitado:
        raise HTTPException(status_code=404, detail="Chat desligado (CHAT_HABILITADO=false).")


api = [Depends(_habilitado), Depends(exigir_admin_api)]


def _modelo_padrao(prov: provedores.Provedor) -> str:
    cfg = get_settings()
    if cfg.chat_modelo_padrao and prov.id == cfg.chat_provedor_padrao:
        return cfg.chat_modelo_padrao
    return prov.modelos[0] if prov.modelos else ""


@router.get("/chat", response_class=HTMLResponse, dependencies=[Depends(_habilitado), Depends(exigir_admin_pagina)])
async def chat(request: Request) -> HTMLResponse:
    provs = await asyncio.to_thread(provedores.provedores)
    return _render(request, "chat.html", {
        "active": "chat",
        "provedores": [p.publico() for p in provs],
        "algum_configurado": any(p.configurado for p in provs),
        "padrao": get_settings().chat_provedor_padrao,
    })


@router.get("/chat/api/provedores", dependencies=api)
async def listar_provedores() -> list[dict]:
    provs = await asyncio.to_thread(provedores.provedores)
    modelos = await asyncio.gather(*(provedores.listar_modelos(p) for p in provs))
    cfg = get_settings()
    return [{**p.publico(), "modelos": m, "modelo_padrao": _modelo_padrao(p) or (m[0] if m else ""),
             "padrao": p.id == cfg.chat_provedor_padrao} for p, m in zip(provs, modelos, strict=True)]


@router.post("/chat/api/provedores/{pid}", dependencies=api)
async def salvar_provedor(request: Request, pid: str, dados: dict[str, Any] = Body(...)) -> dict:
    chave = dados.get("api_key")
    try:
        p = await asyncio.to_thread(
            provedores.salvar, pid,
            api_key=str(chave) if isinstance(chave, str) else None,
            base_url=str(dados.get("base_url") or ""),
            modelos=str(dados.get("modelos") or ""),
            habilitado=bool(dados.get("habilitado", True)),
            apagar_chave=bool(dados.get("apagar_chave")),
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail="Provedor desconhecido.") from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    jobs.auditar("chat.provedor", ip=ip_de(request),
                 detalhe=f"provedor={pid} chave_alterada={bool(chave) or bool(dados.get('apagar_chave'))} "
                         f"habilitado={p.configurado}")
    modelos = await provedores.listar_modelos(p)
    return {**p.publico(), "modelos": modelos, "modelo_padrao": _modelo_padrao(p) or (modelos[0] if modelos else "")}


@router.get("/chat/api/conversas", dependencies=api)
async def listar_conversas() -> list[dict]:
    return await asyncio.to_thread(conversas.listar)


def _validar_modelo(dados: dict[str, Any]) -> tuple[provedores.Provedor, str]:
    pid = str(dados.get("provedor") or get_settings().chat_provedor_padrao)
    try:
        prov = provedores.resolver(pid)
    except LookupError as e:
        raise HTTPException(status_code=422, detail="Provedor desconhecido.") from e
    if not prov.configurado:
        raise HTTPException(status_code=422, detail=f"{prov.nome} não está configurado.")
    modelo = str(dados.get("modelo") or _modelo_padrao(prov)).strip()
    if not modelo or len(modelo) > 160:
        raise HTTPException(status_code=422, detail="Escolha um modelo.")
    return prov, modelo


@router.post("/chat/api/conversas", dependencies=api, status_code=201)
async def criar_conversa(dados: dict[str, Any] = Body(default_factory=dict)) -> dict:
    prov, modelo = await asyncio.to_thread(_validar_modelo, dados)
    return await asyncio.to_thread(conversas.criar, prov.id, modelo)


@router.get("/chat/api/conversas/{conversa_id}", dependencies=api)
async def ver_conversa(conversa_id: int) -> dict:
    c = await asyncio.to_thread(conversas.obter, conversa_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    return c


@router.patch("/chat/api/conversas/{conversa_id}", dependencies=api)
async def alterar_conversa(conversa_id: int, dados: dict[str, Any] = Body(...)) -> dict:
    c = await asyncio.to_thread(conversas.obter, conversa_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    if dados.get("titulo"):
        c = await asyncio.to_thread(conversas.renomear, conversa_id, str(dados["titulo"]))
    if dados.get("provedor") or dados.get("modelo"):
        prov, modelo = await asyncio.to_thread(_validar_modelo, {"provedor": dados.get("provedor") or c["provedor"],
                                                                 "modelo": dados.get("modelo")})
        atual = await asyncio.to_thread(provedores.resolver, c["provedor"])
        if prov.tipo != atual.tipo and c["exibicao"]:
            raise HTTPException(status_code=409, detail="Para trocar entre Claude e os outros provedores, "
                                                        "comece uma nova conversa.")
        c = await asyncio.to_thread(conversas.trocar_modelo, conversa_id, prov.id, modelo)
    return c


@router.delete("/chat/api/conversas/{conversa_id}", dependencies=api)
async def apagar_conversa(conversa_id: int) -> dict:
    if not await asyncio.to_thread(conversas.remover, conversa_id):
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    return {"removida": conversa_id}


@router.post("/chat/api/conversas/{conversa_id}/mensagens", dependencies=api)
async def enviar(request: Request, conversa_id: int, dados: dict[str, Any] = Body(...)) -> StreamingResponse:
    texto = str(dados.get("texto") or "").strip()
    if not texto:
        raise HTTPException(status_code=422, detail="Escreva uma mensagem.")
    if len(texto) > MAX_MENSAGEM:
        raise HTTPException(status_code=422, detail=f"Mensagem longa demais (máximo {MAX_MENSAGEM} caracteres).")
    c = await asyncio.to_thread(conversas.obter, conversa_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    # Auditoria sem o conteudo (pode ter dados pessoais): so quem, quando e para onde foi.
    jobs.auditar("chat.mensagem", ip=ip_de(request),
                 detalhe=f"origem=chat conversa={conversa_id} provedor={c['provedor']} modelo={c['modelo']}")

    async def eventos():
        async for ev in motor.responder(conversa_id, texto):
            yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(eventos(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/chat/arquivos/{nome}", dependencies=api)
async def baixar_arquivo(nome: str) -> FileResponse:
    if not arquivo_exportado_valido(nome):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    caminho = exportacoes_dir() / nome
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado (pode ter sido apagado).")
    tipo = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if nome.endswith(".xlsx") else "text/csv; charset=utf-8")
    return FileResponse(caminho, media_type=tipo, filename=nome)
