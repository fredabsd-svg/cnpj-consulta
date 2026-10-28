"""Operacoes da base local usadas pelo painel administrativo, pela API e pelo CLI."""

from __future__ import annotations

import asyncio
import logging
import shutil
import traceback
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb

from app.config import Settings, get_settings
from app.receita import armazenamento as arm
from app.receita import jobs
from app.receita.duck import SCHEMA_VERSAO, conectar_leitura, metadados
from app.receita.fonte import FonteError, FonteReceita
from app.receita.pipeline import ETAPAS, Atualizacao, Interrompido, PipelineError, validar_base

log = logging.getLogger(__name__)

CFG_VERIFICACAO = "ultima_verificacao"
CFG_AGENDA = "agendamento"
AGENDA_PADRAO = {"ativo": False, "hora": 3, "dia_semana": None}  # todo dia as 3h (horario local)


def info_base_ativa(settings: Settings | None = None) -> dict[str, Any] | None:
    settings = settings or get_settings()
    ativa = arm.base_ativa(settings)
    if ativa is None:
        return None
    out: dict[str, Any] = {
        "arquivo": ativa.caminho.name,
        "caminho": str(ativa.caminho),
        "mes": ativa.mes,
        "publicada_em": ativa.publicada_em,
        "legado": ativa.legado,
        "tamanho": ativa.caminho.stat().st_size if ativa.caminho.exists() else 0,
        "contagens": {},
        "metadados": {},
        "erro": None,
    }
    try:
        with conectar_leitura(ativa.caminho) as conn:
            meta = metadados(conn)
            out["metadados"] = meta
            out["mes"] = out["mes"] or meta.get("mes_referencia")
            existentes = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
            for t in ("empresas", "estabelecimentos", "socios", "simples"):
                if t in existentes:
                    out["contagens"][t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            out["schema_atual"] = meta.get("schema_versao") == SCHEMA_VERSAO
    except duckdb.Error as e:
        out["erro"] = f"Nao foi possivel abrir a base: {e}"
    return out


def estado_geral(settings: Settings | None = None) -> dict[str, Any]:
    """Resumo para o painel: instalada? desatualizada? tarefa em andamento?"""
    settings = settings or get_settings()
    ativa = info_base_ativa(settings)
    verificacao = jobs.config_get(CFG_VERIFICACAO)
    ativo = jobs.job_ativo()
    ultimo = jobs.ultimo_job()
    remoto = verificacao.get("mes_remoto")
    if ativo:
        status = "em_andamento"
    elif ultimo and ultimo["status_efetivo"] in ("falhou", "interrompido", "pausado") and ultimo["tipo"] == "atualizar":
        status = {"falhou": "falhou", "interrompido": "interrompida", "pausado": "pausada"}[ultimo["status_efetivo"]]
    elif ativa is None:
        status = "nao_instalada"
    elif ativa.get("legado") or not ativa.get("schema_atual"):
        status = "formato_antigo"
    elif remoto and ativa.get("mes") and remoto > ativa["mes"]:
        status = "desatualizada"
    else:
        status = "atualizada"
    dd = arm.data_dir(settings)
    return {
        "status": status,
        "ativa": ativa,
        "verificacao": verificacao,
        "job_ativo": ativo,
        "ultimo_job": ultimo,
        "versoes": arm.listar_versoes(settings) if arm.versoes_dir(settings).exists() else [],
        "disco": {
            "pasta": str(dd.resolve()),
            "usado": arm.tamanho_pasta(dd),
            "livre": arm.espaco_livre(dd),
            "trabalho": arm.tamanho_pasta(dd / "trabalho"),
        },
        "agenda": jobs.config_get(CFG_AGENDA, AGENDA_PADRAO),
        "etapas": [{"codigo": c, "rotulo": r} for c, r, _ in ETAPAS],
    }


def verificar_atualizacao(settings: Settings | None = None, client=None) -> dict[str, Any]:
    """Consulta o portal e grava a versao mais recente disponivel."""
    settings = settings or get_settings()
    agora = datetime.now(UTC).isoformat()
    try:
        with FonteReceita(settings, client=client) as fonte:
            v = fonte.versao_mais_recente()
            meses = [m for m, _ in fonte.listar_meses()]
    except FonteError as e:
        res = {"verificado_em": agora, "erro": str(e)}
        anterior = jobs.config_get(CFG_VERIFICACAO)
        jobs.config_set(CFG_VERIFICACAO, {**anterior, **res})
        return res
    ativa = arm.base_ativa(settings)
    res = {
        "verificado_em": agora,
        "erro": None,
        "mes_remoto": v.mes,
        "publicado_em": v.modificado,
        "tamanho_total": v.tamanho_total,
        "arquivos": [{"nome": a.nome, "tamanho": a.tamanho, "modificado": a.modificado} for a in v.arquivos],
        "meses_disponiveis": meses[-12:],
        "mes_local": ativa.mes if ativa else None,
        "espaco_estimado": int(v.tamanho_total * 6.2),
        "nova_versao": bool(ativa is None or not ativa.mes or v.mes > ativa.mes),
    }
    jobs.config_set(CFG_VERIFICACAO, res)
    return res


def iniciar_atualizacao(mes: str | None, *, forcar: bool = False, origem: str = "manual",
                        em_processo: bool = True) -> dict:
    job = jobs.criar_job("atualizar", mes, opcoes={"forcar": forcar}, origem=origem)
    if em_processo:
        jobs.iniciar_processo(job["id"], arm.data_dir() / "logs")
    return job


def iniciar_validacao(em_processo: bool = True) -> dict:
    job = jobs.criar_job("validar", None)
    if em_processo:
        jobs.iniciar_processo(job["id"], arm.data_dir() / "logs")
    return job


def retomar(job_id: int, em_processo: bool = True) -> dict:
    job = jobs.preparar_retomada(job_id)
    if em_processo:
        jobs.iniciar_processo(job_id, arm.data_dir() / "logs")
    return job


def executar_job(job_id: int, *, client=None, sleep=None) -> str:
    """Corpo do processo de trabalho. Retorna o status final."""
    rep = jobs.JobReporter(job_id)
    job = rep.iniciar()
    if job.get("solicitacao") == "cancelar":
        rep.finalizar("cancelado", mensagem="Cancelada antes de iniciar.")
        return "cancelado"
    settings = get_settings()
    if job["tipo"] == "validar":
        return _executar_validacao(rep)
    kwargs = {"sleep": sleep} if sleep else {}
    atual = Atualizacao(
        job.get("mes"), settings=settings, reporter=rep, client=client,
        forcar=bool((job.get("opcoes") or {}).get("forcar")), **kwargs,
    )
    try:
        relatorio = atual.executar()
    except Interrompido as e:
        rel = atual.relatorio.as_dict()
        if e.motivo == "cancelar":
            if atual.mes:
                atual.descartar_trabalho(manter_downloads=True)
            rep.log("Atualizacao cancelada; a base ativa nao foi alterada. Downloads concluidos foram mantidos.",
                    "aviso", atual._etapa)
            rep.finalizar("cancelado", mensagem="Cancelada pelo usuario.", relatorio=rel)
            return "cancelado"
        rep.log("Atualizacao pausada; use Retomar para continuar de onde parou.", "info", atual._etapa)
        rep.finalizar("pausado", mensagem=f"Pausada em: {atual._etapa}.", relatorio=rel)
        return "pausado"
    except (PipelineError, FonteError) as e:
        rep.log(str(e), "erro", atual._etapa)
        rep.finalizar("falhou", mensagem=f"Falhou em: {atual._etapa}.", erro=str(e), relatorio=atual.relatorio.as_dict())
        return "falhou"
    except Exception as e:  # noqa: BLE001 -- qualquer erro vira falha retomavel
        log.exception("erro inesperado na tarefa %s", job_id)
        detalhe = "".join(traceback.format_exception_only(type(e), e)).strip()
        rep.log(f"Erro inesperado: {detalhe}", "erro", atual._etapa)
        rep.finalizar("falhou", mensagem=f"Falhou em: {atual._etapa}.", erro=detalhe, relatorio=atual.relatorio.as_dict())
        return "falhou"
    rep.atualizar(forcar=True, progresso=100.0, etapa="limpeza", arquivo_atual=None,
                  registros=sum(relatorio.get("contagens", {}).values()))
    rep.finalizar("concluido", mensagem=f"Base {relatorio['mes']} publicada.", relatorio=relatorio)
    jobs.config_set(CFG_VERIFICACAO, {**jobs.config_get(CFG_VERIFICACAO), "mes_local": relatorio["mes"],
                                      "nova_versao": False})
    return "concluido"


def _executar_validacao(rep: jobs.JobReporter) -> str:
    settings = get_settings()
    ativa = arm.base_ativa(settings)
    if ativa is None:
        rep.finalizar("falhou", erro="Nenhuma base instalada para validar.")
        return "falhou"
    rep.atualizar(forcar=True, etapa="validacao", progresso=10, mensagem=f"Validando {ativa.caminho.name}")
    try:
        with conectar_leitura(ativa.caminho) as conn:
            res = validar_base(conn, settings)
    except duckdb.Error as e:
        rep.log(f"Base ilegivel: {e}", "erro", "validacao")
        rep.finalizar("falhou", erro=f"Base ilegivel: {e}")
        return "falhou"
    for a in res["avisos"]:
        rep.log(a, "aviso", "validacao")
    for e in res["erros"]:
        rep.log(e, "erro", "validacao")
    rel = {"arquivo": ativa.caminho.name, **res}
    if res["erros"]:
        rep.finalizar("falhou", erro=" | ".join(res["erros"]), relatorio=rel)
        return "falhou"
    rep.atualizar(forcar=True, progresso=100)
    rep.finalizar("concluido", mensagem="Base ativa validada sem erros.", relatorio=rel)
    return "concluido"


def limpar_trabalho(settings: Settings | None = None) -> int:
    """Apaga areas de trabalho que nao pertencem a uma tarefa em andamento."""
    settings = settings or get_settings()
    if jobs.job_ativo():
        raise jobs.JobEmAndamento("Ha uma tarefa em andamento; pause ou cancele antes de limpar.")
    raiz = arm.data_dir(settings) / "trabalho"
    liberado = arm.tamanho_pasta(raiz)
    shutil.rmtree(raiz, ignore_errors=True)
    return liberado


# ---------- agendamento ----------


def proxima_execucao(agenda: dict, agora: datetime, ultima: datetime | None) -> datetime | None:
    if not agenda.get("ativo"):
        return None
    hora = int(agenda.get("hora", 3))
    alvo = agora.replace(hour=hora, minute=0, second=0, microsecond=0)
    dia = agenda.get("dia_semana")
    # Horario de hoje ja passou e nao rodou desde entao? E agora. Senao, o proximo.
    if ultima is None or ultima < alvo - timedelta(days=7):
        alvo -= timedelta(days=7)  # permite recuperar uma execucao perdida (PC desligado)
    for _ in range(15):
        if (dia is None or alvo.weekday() == int(dia)) and (ultima is None or alvo > ultima):
            return alvo
        alvo += timedelta(days=1)
    return None


async def agendador(intervalo: float = 600) -> None:
    """Laco do servidor: na hora agendada verifica o portal e, havendo versao
    nova e nenhuma tarefa em andamento, inicia a atualizacao em segundo plano."""
    while True:
        try:
            await asyncio.to_thread(_tick_agenda)
        except Exception as e:  # noqa: BLE001
            log.warning("agendador da base: %s", e)
        await asyncio.sleep(intervalo)


def _tick_agenda(agora: datetime | None = None, client=None, em_processo: bool = True) -> str | None:
    agenda = jobs.config_get(CFG_AGENDA, AGENDA_PADRAO)
    if not agenda.get("ativo"):
        return None
    agora = agora or datetime.now()
    ultima_txt = agenda.get("ultima_execucao")
    ultima = datetime.fromisoformat(ultima_txt) if ultima_txt else None
    alvo = proxima_execucao(agenda, agora, ultima)
    if alvo is None or alvo > agora:
        return None
    jobs.config_set(CFG_AGENDA, {**agenda, "ultima_execucao": agora.isoformat()})
    if jobs.job_ativo():
        return "ocupado"
    res = verificar_atualizacao(client=client)
    if res.get("erro") or not res.get("nova_versao"):
        return "sem_novidade"
    iniciar_atualizacao(res["mes_remoto"], origem="agendada", em_processo=em_processo)
    jobs.auditar("base.atualizacao_agendada", detalhe=f"mes={res['mes_remoto']}")
    return "iniciada"
