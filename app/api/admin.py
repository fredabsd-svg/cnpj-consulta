"""API administrativa: base de dados CNPJ, contatos complementares e auditoria.

Todas as rotas exigem administrador (cookie de sessao ou Bearer token) e
ficam registradas na auditoria. Operacoes destrutivas pedem `confirmar`.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from app.core.cnpj_validator import normalize_or_none
from app.receita import armazenamento as arm
from app.receita import jobs, servico
from app.receita.layout import MES_RE
from app.services import contatos as contatos_srv
from app.services.admin_auth import exigir_admin_api, ip_de

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(exigir_admin_api)])


def _auditar(request: Request, acao: str, detalhe: str = "", resultado: str = "ok") -> None:
    jobs.auditar(acao, detalhe=detalhe, resultado=resultado, ip=ip_de(request))


def _confirmar(valor: str | None, esperado: str = "CONFIRMAR") -> None:
    if (valor or "").strip().upper() != esperado:
        raise HTTPException(400, f'Operação destrutiva: envie confirmar="{esperado}".')


@router.get("/base/status")
async def base_status() -> dict:
    return await asyncio.to_thread(servico.estado_geral)


@router.get("/base/job/{job_id}")
async def base_job(job_id: int, logs: int = 80) -> dict:
    job = await asyncio.to_thread(jobs.obter, job_id)
    if job is None:
        raise HTTPException(404, "Tarefa não encontrada.")
    job["logs"] = await asyncio.to_thread(jobs.logs, job_id, min(max(logs, 1), 500))
    return job


@router.get("/base/historico")
async def base_historico() -> list[dict]:
    return await asyncio.to_thread(jobs.historico, 50)


@router.post("/base/verificar")
async def base_verificar(request: Request) -> dict:
    res = await asyncio.to_thread(servico.verificar_atualizacao)
    _auditar(request, "base.verificar", f"mes_remoto={res.get('mes_remoto')} erro={bool(res.get('erro'))}")
    return res


@router.post("/base/atualizar", status_code=202)
async def base_atualizar(request: Request, mes: str | None = Body(None, embed=True),
                         forcar: bool = Body(False, embed=True)) -> dict:
    if mes and not MES_RE.match(mes):
        raise HTTPException(400, "Mês no formato AAAA-MM.")
    try:
        job = await asyncio.to_thread(servico.iniciar_atualizacao, mes or None, forcar=forcar)
    except jobs.JobEmAndamento as e:
        _auditar(request, "base.atualizar", str(e), "negado")
        raise HTTPException(409, str(e)) from e
    _auditar(request, "base.atualizar", f"job={job['id']} mes={mes or 'mais recente'} forcar={forcar}")
    return job


@router.post("/base/job/{job_id}/{acao}")
async def base_job_acao(request: Request, job_id: int, acao: str,
                        confirmar: str | None = Body(None, embed=True)) -> dict:
    try:
        if acao == "pausar":
            job = await asyncio.to_thread(jobs.solicitar, job_id, "pausar")
        elif acao == "cancelar":
            _confirmar(confirmar)
            job = await asyncio.to_thread(jobs.solicitar, job_id, "cancelar")
        elif acao == "retomar":
            job = await asyncio.to_thread(servico.retomar, job_id)
        else:
            raise HTTPException(404, "Ação desconhecida.")
    except LookupError as e:
        raise HTTPException(404, "Tarefa não encontrada.") from e
    except (ValueError, jobs.JobEmAndamento) as e:
        raise HTTPException(409, str(e)) from e
    _auditar(request, f"base.{acao}", f"job={job_id}")
    return job


@router.post("/base/validar", status_code=202)
async def base_validar(request: Request) -> dict:
    try:
        job = await asyncio.to_thread(servico.iniciar_validacao)
    except jobs.JobEmAndamento as e:
        raise HTTPException(409, str(e)) from e
    _auditar(request, "base.validar", f"job={job['id']}")
    return job


@router.post("/base/restaurar")
async def base_restaurar(request: Request, confirmar: str | None = Body(None, embed=True)) -> dict:
    _confirmar(confirmar)
    if jobs.job_ativo():
        raise HTTPException(409, "Há uma tarefa em andamento.")
    try:
        nome = await asyncio.to_thread(arm.restaurar_anterior)
    except FileNotFoundError as e:
        raise HTTPException(409, str(e)) from e
    _auditar(request, "base.restaurar", f"arquivo={nome}")
    return {"ativa": nome}


@router.post("/base/versoes/remover")
async def base_remover_versao(request: Request, arquivo: str = Body(..., embed=True),
                              confirmar: str | None = Body(None, embed=True)) -> dict:
    _confirmar(confirmar)
    if not re.fullmatch(r"receita-[0-9A-Za-z\-]+\.duckdb", arquivo):
        raise HTTPException(400, "Arquivo inválido.")
    try:
        await asyncio.to_thread(arm.remover_versao, arquivo)
    except PermissionError as e:
        raise HTTPException(409, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, "Versão não encontrada.") from e
    except OSError as e:
        raise HTTPException(409, f"Não foi possível remover agora (arquivo em uso?): {e}") from e
    _auditar(request, "base.remover_versao", f"arquivo={arquivo}")
    return {"removida": arquivo}


@router.post("/base/limpar")
async def base_limpar(request: Request, confirmar: str | None = Body(None, embed=True)) -> dict:
    _confirmar(confirmar)
    try:
        liberado = await asyncio.to_thread(servico.limpar_trabalho)
    except jobs.JobEmAndamento as e:
        raise HTTPException(409, str(e)) from e
    _auditar(request, "base.limpar", f"bytes={liberado}")
    return {"liberado": liberado}


@router.put("/base/agenda")
async def base_agenda(request: Request, ativo: bool = Body(..., embed=True),
                      hora: int = Body(3, embed=True, ge=0, le=23),
                      dia_semana: int | None = Body(None, embed=True, ge=0, le=6)) -> dict:
    from datetime import datetime

    agenda = {"ativo": ativo, "hora": hora, "dia_semana": dia_semana,
              # comeca a contar agora: ligar o agendamento nao dispara na hora
              "ultima_execucao": datetime.now().isoformat()}
    await asyncio.to_thread(jobs.config_set, servico.CFG_AGENDA, agenda)
    _auditar(request, "base.agenda", f"ativo={ativo} hora={hora} dia={dia_semana}")
    return agenda


@router.get("/auditoria")
async def auditoria(limite: int = 200) -> list[dict]:
    return await asyncio.to_thread(jobs.auditoria, min(max(limite, 1), 1000))


# ---------- contatos complementares ----------


def _cnpj(cnpj: str) -> str:
    n = normalize_or_none(cnpj)
    if n is None:
        raise HTTPException(400, "CNPJ inválido.")
    return n


async def _oficiais(cnpj: str) -> list[dict]:
    from app.services.ficha import BaseIndisponivel, ficha_local

    try:
        f = await asyncio.to_thread(ficha_local, cnpj)
    except BaseIndisponivel:
        return []
    return f["contatos_oficiais"] if f else []


@router.post("/contatos/{cnpj}", status_code=201)
async def contato_criar(
    request: Request, cnpj: str,
    tipo: str = Body(...), valor: str = Body(..., max_length=320), fonte: str = Body(..., max_length=200),
    url_fonte: str | None = Body(None, max_length=500), verificado: bool = Body(False),
    data_verificacao: date | None = Body(None), observacao: str | None = Body(None, max_length=500),
) -> dict:
    n = _cnpj(cnpj)
    try:
        c = await asyncio.to_thread(
            contatos_srv.salvar, n, tipo=tipo, valor=valor, fonte=fonte, url_fonte=url_fonte,
            verificado=verificado, data_verificacao=data_verificacao, observacao=observacao,
            oficiais=await _oficiais(n),
        )
    except contatos_srv.ContatoInvalido as e:
        raise HTTPException(400, str(e)) from e
    _auditar(request, "contato.criar", f"cnpj={n} tipo={tipo} verificado={verificado}")
    return c


@router.put("/contatos/{cnpj}/{contato_id}")
async def contato_editar(
    request: Request, cnpj: str, contato_id: int,
    tipo: str = Body(...), valor: str = Body(..., max_length=320), fonte: str = Body(..., max_length=200),
    url_fonte: str | None = Body(None, max_length=500), verificado: bool = Body(False),
    data_verificacao: date | None = Body(None), observacao: str | None = Body(None, max_length=500),
) -> dict:
    n = _cnpj(cnpj)
    try:
        c = await asyncio.to_thread(
            contatos_srv.salvar, n, tipo=tipo, valor=valor, fonte=fonte, url_fonte=url_fonte,
            verificado=verificado, data_verificacao=data_verificacao, observacao=observacao,
            contato_id=contato_id, oficiais=await _oficiais(n),
        )
    except contatos_srv.ContatoInvalido as e:
        raise HTTPException(400, str(e)) from e
    except LookupError as e:
        raise HTTPException(404, "Contato não encontrado.") from e
    _auditar(request, "contato.editar", f"cnpj={n} id={contato_id} tipo={tipo}")
    return c


@router.delete("/contatos/{cnpj}/{contato_id}", status_code=204)
async def contato_remover(request: Request, cnpj: str, contato_id: int) -> None:
    n = _cnpj(cnpj)
    try:
        info = await asyncio.to_thread(contatos_srv.remover, n, contato_id)
    except LookupError as e:
        raise HTTPException(404, "Contato não encontrado.") from e
    _auditar(request, "contato.remover", f"cnpj={n} id={contato_id} tipo={info['tipo']}")


@router.post("/contatos/retencao")
async def contatos_retencao(request: Request, confirmar: str | None = Body(None, embed=True)) -> dict:
    _confirmar(confirmar)
    n = await asyncio.to_thread(contatos_srv.descartar_por_retencao)
    _auditar(request, "contato.retencao", f"removidos={n}")
    return {"removidos": n}
