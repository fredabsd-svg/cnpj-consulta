"""Persistencia das conversas do chat (SQLite do app)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.db import get_session_factory, init_database
from app.models.chat import ChatConversa


def _resumo(c: ChatConversa) -> dict[str, Any]:
    return {"id": c.id, "titulo": c.titulo, "provedor": c.provedor, "modelo": c.modelo,
            "criado_em": c.criado_em.isoformat() if c.criado_em else None,
            "atualizado_em": c.atualizado_em.isoformat() if c.atualizado_em else None,
            "tokens_entrada": c.tokens_entrada, "tokens_saida": c.tokens_saida}


def criar(provedor: str, modelo: str) -> dict[str, Any]:
    init_database()
    with get_session_factory()() as s:
        c = ChatConversa(provedor=provedor, modelo=modelo, historico=[], exibicao=[],
                         atualizado_em=datetime.now())
        s.add(c)
        s.commit()
        return _resumo(c)


def listar(limite: int = 100) -> list[dict[str, Any]]:
    init_database()
    with get_session_factory()() as s:
        q = select(ChatConversa).order_by(ChatConversa.atualizado_em.desc(), ChatConversa.id.desc()).limit(limite)
        return [_resumo(c) for c in s.scalars(q)]


def obter(conversa_id: int, *, com_historico: bool = False) -> dict[str, Any] | None:
    init_database()
    with get_session_factory()() as s:
        c = s.get(ChatConversa, conversa_id)
        if c is None:
            return None
        d = {**_resumo(c), "exibicao": list(c.exibicao or [])}
        if com_historico:
            d["historico"] = list(c.historico or [])
        return d


TITULO_PADRAO = "Nova conversa"


def salvar(conversa_id: int, *, historico: list, exibicao: list, titulo: str | None = None,
           tokens_entrada: int = 0, tokens_saida: int = 0) -> None:
    with get_session_factory()() as s:
        c = s.get(ChatConversa, conversa_id)
        if c is None:
            return
        # Listas novas: o SQLAlchemy so detecta troca do valor JSON, nao mutacao.
        c.historico = list(historico)
        c.exibicao = list(exibicao)
        if titulo and c.titulo == TITULO_PADRAO:  # nao sobrescreve um titulo dado pelo usuario
            c.titulo = titulo[:160]
        c.tokens_entrada += tokens_entrada
        c.tokens_saida += tokens_saida
        c.atualizado_em = datetime.now()
        s.commit()


def trocar_modelo(conversa_id: int, provedor: str, modelo: str) -> dict[str, Any]:
    with get_session_factory()() as s:
        c = s.get(ChatConversa, conversa_id)
        if c is None:
            raise LookupError(conversa_id)
        c.provedor, c.modelo = provedor, modelo
        s.commit()
        return _resumo(c)


def renomear(conversa_id: int, titulo: str) -> dict[str, Any]:
    titulo = (titulo or "").strip()
    if not titulo:
        raise ValueError("Informe um título.")
    with get_session_factory()() as s:
        c = s.get(ChatConversa, conversa_id)
        if c is None:
            raise LookupError(conversa_id)
        c.titulo = titulo[:160]
        s.commit()
        return _resumo(c)


def remover(conversa_id: int) -> bool:
    with get_session_factory()() as s:
        c = s.get(ChatConversa, conversa_id)
        if c is None:
            return False
        s.delete(c)
        s.commit()
        return True
