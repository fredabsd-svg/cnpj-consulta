"""Modelos do banco do app (SQLite) para a base local, busca e auditoria.

Os dados da Receita ficam no DuckDB (app/receita); aqui ficam so os dados
operacionais: tarefas de atualizacao, logs, auditoria administrativa,
contatos complementares, buscas salvas e listas de empresas.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BaseJob(Base):
    """Tarefa da base local: atualizar (baixar+importar), validar, verificar."""

    __tablename__ = "base_job"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)  # atualizar | validar
    mes: Mapped[str | None] = mapped_column(String(7))
    # pendente | executando | pausado | cancelado | falhou | concluido
    status: Mapped[str] = mapped_column(String(20), default="pendente", index=True)
    etapa: Mapped[str | None] = mapped_column(String(30))
    progresso: Mapped[float] = mapped_column(Float, default=0.0)  # 0-100 (geral)
    progresso_etapa: Mapped[float] = mapped_column(Float, default=0.0)
    arquivo_atual: Mapped[str | None] = mapped_column(String(120))
    bytes_total: Mapped[int] = mapped_column(Integer, default=0)
    bytes_feitos: Mapped[int] = mapped_column(Integer, default=0)
    velocidade_bps: Mapped[float] = mapped_column(Float, default=0.0)
    eta_segundos: Mapped[float | None] = mapped_column(Float)
    registros: Mapped[int] = mapped_column(Integer, default=0)
    mensagem: Mapped[str | None] = mapped_column(String(500))
    erro: Mapped[str | None] = mapped_column(Text)
    # Pedido do usuario que o processo de trabalho atende no proximo ponto seguro.
    solicitacao: Mapped[str | None] = mapped_column(String(20))  # pausar | cancelar
    opcoes: Mapped[dict] = mapped_column(JSON, default=dict)
    relatorio: Mapped[dict] = mapped_column(JSON, default=dict)
    pid: Mapped[int | None] = mapped_column(Integer)
    origem: Mapped[str] = mapped_column(String(20), default="manual")  # manual | agendada | cli
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    iniciado_em: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_em: Mapped[datetime | None] = mapped_column(DateTime)
    finalizado_em: Mapped[datetime | None] = mapped_column(DateTime)


class BaseJobLog(Base):
    """Log de processamento. Nunca contem conteudo de linhas (dados pessoais)."""

    __tablename__ = "base_job_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("base_job.id", ondelete="CASCADE"), index=True)
    em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    nivel: Mapped[str] = mapped_column(String(10), default="info")  # info | aviso | erro
    etapa: Mapped[str | None] = mapped_column(String(30))
    mensagem: Mapped[str] = mapped_column(String(1000))


class BaseConfig(Base):
    """Preferencias da base (agendamento, ultima verificacao...)."""

    __tablename__ = "base_config"

    chave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[dict] = mapped_column(JSON, default=dict)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AuditoriaAdmin(Base):
    """Trilha de auditoria das operacoes administrativas (sem dados pessoais)."""

    __tablename__ = "auditoria_admin"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    acao: Mapped[str] = mapped_column(String(60), nullable=False)
    resultado: Mapped[str] = mapped_column(String(20), default="ok")  # ok | negado | erro
    detalhe: Mapped[str | None] = mapped_column(String(1000))
    ip: Mapped[str | None] = mapped_column(String(64))


class ContatoComplementar(Base):
    """Contato obtido FORA da base da Receita, sempre com fonte declarada.

    Nunca e preenchido automaticamente: so entra por cadastro de um
    administrador, que informa a fonte e se/quando verificou o contato.
    """

    __tablename__ = "contato_complementar"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cnpj: Mapped[str] = mapped_column(String(14), index=True, nullable=False)
    tipo: Mapped[str] = mapped_column(String(10), nullable=False)  # telefone | email | site
    valor: Mapped[str] = mapped_column(String(320), nullable=False)
    valor_normalizado: Mapped[str] = mapped_column(String(320), index=True)
    # Classificacao tecnica (telefone: celular | fixo | indeterminado)
    subtipo: Mapped[str | None] = mapped_column(String(20))
    fonte: Mapped[str] = mapped_column(String(200), nullable=False)
    url_fonte: Mapped[str | None] = mapped_column(String(500))
    verificado: Mapped[bool] = mapped_column(Boolean, default=False)
    data_verificacao: Mapped[date | None] = mapped_column(Date)
    difere_do_oficial: Mapped[bool] = mapped_column(Boolean, default=False)
    observacao: Mapped[str | None] = mapped_column(String(500))
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("cnpj", "tipo", "valor_normalizado", name="uq_contato"),)


class BuscaSalva(Base):
    """Criterios de uma busca (re-executados a cada abertura)."""

    __tablename__ = "busca_salva"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    filtros: Mapped[dict] = mapped_column(JSON, nullable=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    ultima_execucao: Mapped[datetime | None] = mapped_column(DateTime)
    ultimo_total: Mapped[int | None] = mapped_column(Integer)


class BuscaRecente(Base):
    __tablename__ = "busca_recente"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    filtros: Mapped[dict] = mapped_column(JSON, nullable=False)
    resumo: Mapped[str] = mapped_column(String(500))
    total: Mapped[int | None] = mapped_column(Integer)
    executada_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ListaEmpresas(Base):
    """Lista de empresas escolhidas pelo usuario (fixa, com observacoes)."""

    __tablename__ = "lista_empresas"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    descricao: Mapped[str | None] = mapped_column(String(500))
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ListaItem(Base):
    __tablename__ = "lista_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    lista_id: Mapped[int] = mapped_column(ForeignKey("lista_empresas.id", ondelete="CASCADE"), index=True)
    cnpj: Mapped[str] = mapped_column(String(14), nullable=False)
    razao_social: Mapped[str | None] = mapped_column(String(300))
    observacao: Mapped[str | None] = mapped_column(String(1000))
    adicionado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (UniqueConstraint("lista_id", "cnpj", name="uq_lista_cnpj"),)


class Exportacao(Base):
    """Exportacao grande feita em segundo plano (arquivo em data/receita/exportacoes)."""

    __tablename__ = "exportacao"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    formato: Mapped[str] = mapped_column(String(5))  # csv | xlsx
    status: Mapped[str] = mapped_column(String(20), default="pendente")  # pendente|executando|concluido|falhou
    filtros: Mapped[dict] = mapped_column(JSON, default=dict)
    colunas: Mapped[list] = mapped_column(JSON, default=list)
    finalidade: Mapped[str | None] = mapped_column(String(300))
    total: Mapped[int | None] = mapped_column(Integer)
    feitas: Mapped[int] = mapped_column(Integer, default=0)
    arquivo: Mapped[str | None] = mapped_column(String(200))
    erro: Mapped[str | None] = mapped_column(String(500))
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finalizado_em: Mapped[datetime | None] = mapped_column(DateTime)


class SocioCpf(Base):
    """CPF completo de uma pessoa (socio ou representante), so para o administrador.

    O numero fica criptografado (Fernet); `cpf_indice` e um HMAC para achar pelo
    CPF sem guarda-lo em claro. `pessoa` = nome normalizado + digitos 4-9, como a
    Receita identifica o socio. Ver app/services/cpf.py.
    """

    __tablename__ = "socio_cpf"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pessoa: Mapped[str] = mapped_column(String(260), unique=True, index=True)
    nome: Mapped[str] = mapped_column(String(200))
    cpf_cifrado: Mapped[str] = mapped_column(Text)
    cpf_indice: Mapped[str] = mapped_column(String(64), index=True)
    cpf_mascarado: Mapped[str] = mapped_column(String(20))
    fonte: Mapped[str] = mapped_column(String(300))
    finalidade: Mapped[str] = mapped_column(String(300))
    origem: Mapped[str] = mapped_column(String(20), default="manual")  # manual | fonte_externa
    conferido: Mapped[bool] = mapped_column(Boolean, default=False)  # bate com os digitos publicados
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
