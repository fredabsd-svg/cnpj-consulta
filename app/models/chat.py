"""Conversas do chat com IA e configuracao dos provedores de modelo.

`historico` guarda as mensagens no formato nativo do provedor (para continuar a
conversa sem perder nada, inclusive blocos de raciocinio que precisam voltar
intactos); `exibicao` guarda a versao para a tela (texto e chamadas de
ferramenta), independente do provedor.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ChatConversa(Base):
    __tablename__ = "chat_conversa"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    titulo: Mapped[str] = mapped_column(String(160), default="Nova conversa")
    provedor: Mapped[str] = mapped_column(String(40), nullable=False)
    modelo: Mapped[str] = mapped_column(String(160), nullable=False)
    historico: Mapped[list] = mapped_column(JSON, default=list)
    exibicao: Mapped[list] = mapped_column(JSON, default=list)
    tokens_entrada: Mapped[int] = mapped_column(Integer, default=0)
    tokens_saida: Mapped[int] = mapped_column(Integer, default=0)
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ChatProvedor(Base):
    """Chave e endereco de um provedor cadastrados pela tela (o .env tem prioridade).

    Nunca e devolvida ao navegador; o chat SQL somente-leitura nao enxerga esta tabela.
    """

    __tablename__ = "chat_provedor"

    provedor: Mapped[str] = mapped_column(String(40), primary_key=True)
    api_key: Mapped[str | None] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(String(300))
    modelos: Mapped[str | None] = mapped_column(Text)  # extras, separados por virgula
    habilitado: Mapped[bool] = mapped_column(Boolean, default=True)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
