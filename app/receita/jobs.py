"""Persistencia das tarefas da base (SQLite), compartilhada entre o servidor
web (que cria, consulta, pausa e cancela) e o processo de trabalho (que executa
e grava o progresso).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.db import get_session_factory, init_database
from app.models.base_cnpj import AuditoriaAdmin, BaseConfig, BaseJob, BaseJobLog
from app.receita import trava

log = logging.getLogger(__name__)

ATIVOS = ("pendente", "executando")
RETOMAVEIS = ("pausado", "falhou", "interrompido")
# Sem batimento por este tempo, o processo de trabalho e dado como interrompido
# (queda de energia, processo morto). A tarefa pode ser retomada.
HEARTBEAT_LIMITE = timedelta(seconds=120)


class JobEmAndamento(RuntimeError):
    pass


def _agora() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _to_dict(job: BaseJob) -> dict[str, Any]:
    d = {c.name: getattr(job, c.name) for c in BaseJob.__table__.columns}
    d["status_efetivo"] = status_efetivo(job)
    return d


def status_efetivo(job: BaseJob) -> str:
    """'interrompido' quando o processo parou de dar sinal de vida.

    Enquanto a trava de processo estiver presa, o processo de trabalho esta
    vivo (mesmo que o batimento tenha atrasado): nunca e dado como interrompido.
    """
    if job.status in ("executando", "pendente"):
        ref = job.heartbeat_em or job.iniciado_em or job.criado_em
        limite = HEARTBEAT_LIMITE if job.status == "executando" else HEARTBEAT_LIMITE * 2
        if job.status == "pendente":
            ref = job.criado_em
        if ref and _agora() - ref > limite and trava.em_uso() is None:
            return "interrompido"
    return job.status


def criar_job(tipo: str, mes: str | None, *, opcoes: dict | None = None, origem: str = "manual") -> dict:
    init_database()
    # Conferir "ha tarefa ativa?" e inserir precisa ser atomico entre pedidos
    # simultaneos (dois cliques, painel + agendador, CLI).
    with trava.exclusivo("criar-tarefa"), get_session_factory()() as s:
        for job in s.scalars(select(BaseJob).where(BaseJob.status.in_(ATIVOS))):
            if status_efetivo(job) in ATIVOS:
                raise JobEmAndamento(f"Ja existe uma tarefa em andamento (#{job.id}).")
            job.status = "falhou"
            job.erro = job.erro or "Processo interrompido (sem sinal de vida)."
        job = BaseJob(tipo=tipo, mes=mes, opcoes=opcoes or {}, origem=origem, status="pendente")
        s.add(job)
        s.commit()
        return _to_dict(job)


def obter(job_id: int) -> dict | None:
    with get_session_factory()() as s:
        job = s.get(BaseJob, job_id)
        return _to_dict(job) if job else None


def job_ativo() -> dict | None:
    with get_session_factory()() as s:
        for job in s.scalars(
            select(BaseJob).where(BaseJob.status.in_(ATIVOS)).order_by(BaseJob.id.desc())
        ):
            if status_efetivo(job) in ATIVOS:
                return _to_dict(job)
    return None


def ultimo_job(tipo: str | None = None) -> dict | None:
    with get_session_factory()() as s:
        q = select(BaseJob).order_by(BaseJob.id.desc()).limit(1)
        if tipo:
            q = q.where(BaseJob.tipo == tipo)
        job = s.scalars(q).first()
        return _to_dict(job) if job else None


def historico(limite: int = 30) -> list[dict]:
    with get_session_factory()() as s:
        rows = s.scalars(select(BaseJob).order_by(BaseJob.id.desc()).limit(limite))
        return [_to_dict(j) for j in rows]


def logs(job_id: int, limite: int = 200, nivel: str | None = None) -> list[dict]:
    with get_session_factory()() as s:
        q = select(BaseJobLog).where(BaseJobLog.job_id == job_id)
        if nivel:
            q = q.where(BaseJobLog.nivel == nivel)
        rows = s.scalars(q.order_by(BaseJobLog.id.desc()).limit(limite))
        return [
            {"em": r.em, "nivel": r.nivel, "etapa": r.etapa, "mensagem": r.mensagem}
            for r in reversed(list(rows))
        ]


def solicitar(job_id: int, pedido: str) -> dict:
    """Pede pausa/cancelamento; o processo atende no proximo ponto seguro."""
    assert pedido in ("pausar", "cancelar")
    with get_session_factory()() as s:
        job = s.get(BaseJob, job_id)
        if job is None:
            raise LookupError(job_id)
        efetivo = status_efetivo(job)
        if efetivo in ("pendente", "executando"):
            job.solicitacao = pedido
        elif pedido == "cancelar" and efetivo in RETOMAVEIS:
            job.status, job.finalizado_em = "cancelado", _agora()
            job.mensagem = "Cancelada pelo usuario."
        else:
            raise ValueError(f"A tarefa esta {efetivo}; nada a {pedido}.")
        s.commit()
        return _to_dict(job)


def preparar_retomada(job_id: int) -> dict:
    with trava.exclusivo("criar-tarefa"), get_session_factory()() as s:
        job = s.get(BaseJob, job_id)
        if job is None:
            raise LookupError(job_id)
        if status_efetivo(job) not in RETOMAVEIS:
            raise ValueError("So tarefas pausadas, interrompidas ou com falha podem ser retomadas.")
        if (outro := job_ativo()) and outro["id"] != job_id:
            raise JobEmAndamento(f"Ja existe uma tarefa em andamento (#{outro['id']}).")
        if (pid := trava.em_uso()) is not None:
            raise JobEmAndamento(
                f"A atualizacao ainda esta em execucao (processo {pid}). Aguarde terminar ou use Pausar."
            )
        job.status, job.solicitacao, job.erro = "pendente", None, None
        job.criado_em = _agora()  # reinicia o prazo do batimento ate o processo subir
        job.finalizado_em = None
        s.commit()
        return _to_dict(job)


class JobReporter:
    """Usado pelo processo de trabalho: progresso com escrita limitada."""

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        self._ultimo_flush = 0.0
        self._pendente: dict[str, Any] = {}
        # O pipeline atualiza o progresso de varias threads (downloads paralelos).
        self._lock = threading.RLock()

    def iniciar(self) -> dict:
        with get_session_factory()() as s:
            job = s.get(BaseJob, self.job_id)
            if job is None:
                raise LookupError(self.job_id)
            job.status = "executando"
            job.pid = os.getpid()
            job.iniciado_em = job.iniciado_em or _agora()
            job.heartbeat_em = _agora()
            s.commit()
            return _to_dict(job)

    def atualizar(self, forcar: bool = False, **campos: Any) -> None:
        with self._lock:
            self._pendente.update(campos)
            if not forcar and time.monotonic() - self._ultimo_flush < 1.0:
                return
            self._flush()

    def _flush(self) -> None:
        campos, self._pendente = self._pendente, {}
        with get_session_factory()() as s:
            job = s.get(BaseJob, self.job_id)
            if job is None:
                return
            for k, v in campos.items():
                setattr(job, k, v)
            job.heartbeat_em = _agora()
            s.commit()
        self._ultimo_flush = time.monotonic()

    def batimento(self) -> None:
        """Sinal de vida independente do progresso (operacoes longas do DuckDB)."""
        with self._lock:
            self._flush()

    def solicitacao(self) -> str | None:
        with get_session_factory()() as s:
            job = s.get(BaseJob, self.job_id)
            return job.solicitacao if job else "cancelar"

    def log(self, mensagem: str, nivel: str = "info", etapa: str | None = None) -> None:
        log.log({"erro": logging.ERROR, "aviso": logging.WARNING}.get(nivel, logging.INFO), "%s", mensagem)
        with get_session_factory()() as s:
            s.add(BaseJobLog(job_id=self.job_id, nivel=nivel, etapa=etapa, mensagem=mensagem[:1000]))
            s.commit()

    def finalizar(self, status: str, *, mensagem: str | None = None, erro: str | None = None,
                  relatorio: dict | None = None) -> None:
        self._pendente = {}
        with get_session_factory()() as s:
            job = s.get(BaseJob, self.job_id)
            if job is None:
                return
            job.status = status
            job.solicitacao = None
            job.mensagem = mensagem or job.mensagem
            job.erro = erro
            if relatorio is not None:
                job.relatorio = relatorio
            job.velocidade_bps, job.eta_segundos = 0.0, None
            job.heartbeat_em = _agora()
            if status in ("concluido", "cancelado", "falhou"):
                job.finalizado_em = _agora()
            s.commit()


# ---------- processo de trabalho ----------


def iniciar_processo(job_id: int, log_dir: Path) -> int:
    """Executa a tarefa num processo separado (a interface nao trava e a tarefa
    sobrevive a recarga da pagina). Saida em log_dir/job-<id>.log."""
    log_dir.mkdir(parents=True, exist_ok=True)
    saida = open(log_dir / f"job-{job_id}.log", "ab")  # noqa: SIM115 -- herdado pelo filho
    kwargs: dict[str, Any] = {"stdout": saida, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(  # noqa: S603 -- comando fixo, sem shell
        [sys.executable, "-m", "app.cli", "base", "executar", str(job_id)],
        cwd=os.getcwd(),
        **kwargs,
    )
    saida.close()
    with get_session_factory()() as s:
        job = s.get(BaseJob, job_id)
        if job is not None:
            job.pid = proc.pid
            s.commit()
    return proc.pid


# ---------- configuracao e auditoria ----------


def config_get(chave: str, padrao: dict | None = None) -> dict:
    init_database()
    with get_session_factory()() as s:
        row = s.get(BaseConfig, chave)
        return dict(row.valor) if row and row.valor is not None else dict(padrao or {})


def config_set(chave: str, valor: dict) -> None:
    with get_session_factory()() as s:
        row = s.get(BaseConfig, chave)
        if row is None:
            s.add(BaseConfig(chave=chave, valor=valor))
        else:
            row.valor = valor
        s.commit()


def auditar(acao: str, *, detalhe: str | None = None, resultado: str = "ok", ip: str | None = None) -> None:
    """Registra operacao administrativa. NAO passe dados pessoais em `detalhe`."""
    try:
        init_database()
        with get_session_factory()() as s:
            s.add(AuditoriaAdmin(acao=acao, detalhe=(detalhe or "")[:1000], resultado=resultado, ip=ip))
            s.commit()
    except Exception as e:  # noqa: BLE001 -- auditoria nunca derruba a operacao
        log.error("falha ao gravar auditoria (%s): %s", acao, e)


def auditoria(limite: int = 100) -> list[dict]:
    with get_session_factory()() as s:
        rows = s.scalars(select(AuditoriaAdmin).order_by(AuditoriaAdmin.id.desc()).limit(limite))
        return [
            {"em": r.em, "acao": r.acao, "resultado": r.resultado, "detalhe": r.detalhe, "ip": r.ip}
            for r in rows
        ]


def podar_logs(dias: int = 180) -> int:
    limite = _agora() - timedelta(days=dias)
    with get_session_factory()() as s:
        n = s.execute(delete(BaseJobLog).where(BaseJobLog.em < limite)).rowcount
        s.commit()
        return n or 0
