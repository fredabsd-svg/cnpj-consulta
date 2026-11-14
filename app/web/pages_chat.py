"""Chat com IA (/chat): pagina, configuracao de provedores e conversa em fluxo.

So para o administrador: o modelo usa todas as funcoes do app, inclusive as
que alteram dados. As chaves dos provedores nunca saem do servidor.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse

from app.chat import chatgpt, conversas, exportar, motor, oauth, provedores
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


def _nomes(modelos: list[str]) -> dict[str, str]:
    return {m: n for m in modelos if (n := provedores.nome_modelo(m))}


def _extras_login(pid: str) -> dict[str, Any]:
    """Login sem chave (OAuth) disponivel para o provedor e, no ChatGPT, a conta conectada."""
    if pid == "chatgpt":
        return {"login": True, "conta": chatgpt.conta()}
    return {"login": oauth.disponivel(pid)}


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
    return [{**p.publico(), "modelos": m, "nomes_modelos": _nomes(m), **_extras_login(p.id),
             "modelo_padrao": _modelo_padrao(p) or (m[0] if m else ""),
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
    return {**p.publico(), "modelos": modelos, "nomes_modelos": _nomes(modelos), **_extras_login(p.id),
            "modelo_padrao": _modelo_padrao(p) or (modelos[0] if modelos else "")}


pagina = [Depends(_habilitado), Depends(exigir_admin_pagina)]


def _voltar_ao_chat(**params: str) -> RedirectResponse:
    return RedirectResponse("/chat?" + urlencode(params), status_code=303)


@router.get("/chat/login/chatgpt/iniciar", dependencies=pagina)
async def login_chatgpt_iniciar(request: Request, nova_conta: str = "") -> RedirectResponse:
    """Continuar com o ChatGPT: login oficial da OpenAI (o retorno vai para 127.0.0.1)."""
    porta = request.url.port or (443 if request.url.scheme == "https" else 80)
    origem = f"{request.url.scheme}://{request.url.netloc}"
    url = await asyncio.to_thread(chatgpt.iniciar, porta, origem, nova_conta=nova_conta == "1")
    return RedirectResponse(url, status_code=303)


@router.get(chatgpt.CAMINHO_RETORNO, dependencies=[Depends(_habilitado)])
async def login_chatgpt_retorno(request: Request, code: str = "", state: str = "", client_id: str = "",
                                error: str = "") -> RedirectResponse:
    # Sem exigir o cookie do admin: a OpenAI devolve para 127.0.0.1, e o admin pode
    # estar usando o app por "localhost" (outro cookie). Quem protege e o `state`,
    # que so o admin consegue criar (rota /iniciar) e vale uma vez, por 10 minutos.
    pend = chatgpt.pendente(state)
    voltar = pend["voltar_para"] if pend else ""
    try:
        r = await chatgpt.concluir(codigo=code, estado=state, cliente_retorno=client_id, erro=error)
    except chatgpt.LoginInvalido as e:
        jobs.auditar("chat.provedor.login", ip=ip_de(request), detalhe="provedor=chatgpt ok=False")
        return RedirectResponse(voltar + "/chat?" + urlencode({"login_erro": str(e)}), status_code=303)
    provedores._cache_modelos.pop("chatgpt", None)
    jobs.auditar("chat.provedor.login", ip=ip_de(request), detalhe="provedor=chatgpt ok=True")
    params = {"login_ok": "chatgpt", **({"primeiro": "1"} if r["primeiro_login"] else {})}
    return RedirectResponse(r["voltar_para"] + "/chat?" + urlencode(params), status_code=303)


@router.post("/chat/api/provedores/chatgpt/sair", dependencies=api)
async def login_chatgpt_sair(request: Request) -> dict:
    confirmada = await chatgpt.sair()
    provedores._cache_modelos.pop("chatgpt", None)
    jobs.auditar("chat.provedor.sair", ip=ip_de(request), detalhe=f"provedor=chatgpt revogado={confirmada}")
    return {"saiu": True, "revogacao_confirmada": confirmada}


@router.get("/chat/login/{pid}/iniciar", dependencies=pagina)
async def login_iniciar(request: Request, pid: str) -> RedirectResponse:
    """Abre o login do provedor (OAuth com PKCE): sem copiar e colar chave."""
    if not oauth.disponivel(pid):
        raise HTTPException(status_code=404, detail="Este provedor não oferece login para outros aplicativos.")
    retorno = str(request.url_for("login_retorno", pid=pid))
    return RedirectResponse(oauth.iniciar(pid, retorno), status_code=303)


@router.get("/chat/login/{pid}/retorno", name="login_retorno", dependencies=pagina)
async def login_retorno(request: Request, pid: str, code: str = "", state: str = "") -> RedirectResponse:
    if not oauth.disponivel(pid):
        raise HTTPException(status_code=404, detail="Provedor desconhecido.")
    try:
        chave = await oauth.concluir(pid, code, state)
        await asyncio.to_thread(provedores.salvar, pid, api_key=chave)
    except oauth.LoginInvalido as e:
        jobs.auditar("chat.provedor.login", ip=ip_de(request), detalhe=f"provedor={pid} ok=False")
        return _voltar_ao_chat(login_erro=str(e))
    except ValueError as e:
        return _voltar_ao_chat(login_erro=str(e))
    provedores._cache_modelos.pop(pid, None)
    jobs.auditar("chat.provedor.login", ip=ip_de(request), detalhe=f"provedor={pid} ok=True")
    return _voltar_ao_chat(login_ok=pid)


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
    if "titulo" in dados:
        try:
            c = await asyncio.to_thread(conversas.renomear, conversa_id, str(dados["titulo"] or ""))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except LookupError as e:
            raise HTTPException(status_code=404, detail="Conversa não encontrada.") from e
    if dados.get("provedor") or dados.get("modelo"):
        prov, modelo = await asyncio.to_thread(_validar_modelo, {"provedor": dados.get("provedor") or c["provedor"],
                                                                 "modelo": dados.get("modelo")})
        if conversa_id in motor._em_andamento:
            raise HTTPException(status_code=409, detail="Aguarde a resposta terminar para trocar o modelo.")
        # Qualquer provedor/modelo, mesmo no meio da conversa: o motor converte o
        # historico para o formato do provedor escolhido (motor.para_anthropic/para_openai).
        c = await asyncio.to_thread(conversas.trocar_modelo, conversa_id, prov.id, modelo)
    return c


@router.delete("/chat/api/conversas/{conversa_id}", dependencies=api)
async def apagar_conversa(conversa_id: int) -> dict:
    if conversa_id in motor._em_andamento:
        raise HTTPException(status_code=409, detail="A conversa ainda está respondendo. Pare a resposta antes de apagar.")
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


_FORMATOS_EXPORTACAO = {
    "pdf": (exportar.gerar_pdf, "application/pdf"),
    "xlsx": (exportar.gerar_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}


@router.get("/chat/api/conversas/{conversa_id}/exportar", dependencies=api)
async def exportar_conversa(request: Request, conversa_id: int, formato: str = "pdf",
                            turno: int | None = None) -> Response:
    """Conversa inteira (ou so a resposta `turno`, contada a partir de 1) em PDF ou Excel."""
    if formato not in _FORMATOS_EXPORTACAO:
        raise HTTPException(status_code=422, detail="Formato inválido: use pdf ou xlsx.")
    c = await asyncio.to_thread(conversas.obter, conversa_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    gerar, tipo = _FORMATOS_EXPORTACAO[formato]
    try:
        conteudo = await asyncio.to_thread(gerar, c, turno)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    jobs.auditar("chat.exportacao", ip=ip_de(request),
                 detalhe=f"origem=chat conversa={conversa_id} formato={formato} turno={turno or 'todos'}")
    nome = exportar.nome_arquivo(c, formato, turno)
    return Response(conteudo, media_type=tipo, headers={
        "Content-Disposition": f'attachment; filename="{nome}"', "Cache-Control": "no-store"})


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
