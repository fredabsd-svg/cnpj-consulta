"""Buscas salvas (criterios, re-executados a cada abertura), historico de
buscas recentes e listas de empresas (selecao fixa, com observacoes)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select

from app.core.cnpj_validator import is_valid
from app.db import get_session_factory, init_database
from app.models.base_cnpj import BuscaRecente, BuscaSalva, ListaEmpresas, ListaItem

MAX_RECENTES = 30
MAX_ITENS_LISTA = 5000
_CNPJ = re.compile(r"[0-9A-Z]{12}\d{2}")


def _d(obj) -> dict[str, Any]:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


# ---------- buscas salvas ----------


def salvar_busca(nome: str, filtros: dict, busca_id: int | None = None) -> dict:
    nome = (nome or "").strip()[:120]
    if not nome:
        raise ValueError("Dê um nome para a busca.")
    if not filtros:
        raise ValueError("Não há filtros para salvar.")
    init_database()
    with get_session_factory()() as s:
        b = s.get(BuscaSalva, busca_id) if busca_id else None
        if busca_id and b is None:
            raise LookupError(busca_id)
        if b is None:
            b = BuscaSalva(nome=nome, filtros=filtros)
            s.add(b)
        else:
            b.nome, b.filtros = nome, filtros
        s.commit()
        return _d(b)


def buscas_salvas() -> list[dict]:
    init_database()
    with get_session_factory()() as s:
        return [_d(b) for b in s.scalars(select(BuscaSalva).order_by(BuscaSalva.nome))]


def obter_busca(busca_id: int) -> dict | None:
    with get_session_factory()() as s:
        b = s.get(BuscaSalva, busca_id)
        return _d(b) if b else None


def marcar_execucao(busca_id: int, total: int) -> None:
    with get_session_factory()() as s:
        b = s.get(BuscaSalva, busca_id)
        if b:
            b.ultima_execucao, b.ultimo_total = datetime.now(), total
            s.commit()


def remover_busca(busca_id: int) -> None:
    with get_session_factory()() as s:
        b = s.get(BuscaSalva, busca_id)
        if b is None:
            raise LookupError(busca_id)
        s.delete(b)
        s.commit()


# ---------- recentes ----------


def _sem_cpf_nos_filtros(filtros: dict) -> dict:
    from app.core.normalizer import sem_cpf

    def limpar(v):
        if isinstance(v, str):
            return sem_cpf(v) or ""
        if isinstance(v, list):
            return [limpar(x) for x in v]
        return v

    return {k: limpar(v) for k, v in filtros.items()}


def registrar_recente(filtros: dict, resumo: str, total: int | None) -> None:
    """Historico local. Criterios com nome/documento de socio nao sao guardados
    (o resumo ja os omite e os filtros sao gravados sem esses valores)."""
    from app.services.busca_avancada import sem_dados_de_socio

    limpos = sem_dados_de_socio(filtros)
    if limpos != filtros or _sem_cpf_nos_filtros(filtros) != filtros:
        # Tinha criterio de socio ou CPF (razao social de MEI): sem ele seria OUTRA
        # busca (total e resumo errados), e com ele guardaria dado pessoal. Nao grava.
        return
    if not limpos:
        return
    init_database()
    with get_session_factory()() as s:
        ultima = s.scalars(select(BuscaRecente).order_by(BuscaRecente.id.desc()).limit(1)).first()
        if ultima and ultima.filtros == limpos:
            # Sem contagem (contar=false) nao apaga o total ja conhecido.
            ultima.total = total if total is not None else ultima.total
            ultima.executada_em = datetime.now()
        else:
            s.add(BuscaRecente(filtros=limpos, resumo=resumo[:500], total=total))
        s.flush()
        ids = list(s.scalars(select(BuscaRecente.id).order_by(BuscaRecente.id.desc()).offset(MAX_RECENTES)))
        if ids:
            s.execute(delete(BuscaRecente).where(BuscaRecente.id.in_(ids)))
        s.commit()


def recentes(limite: int = 10) -> list[dict]:
    init_database()
    with get_session_factory()() as s:
        from app.services.busca_avancada import sem_dados_de_socio

        out = []
        for b in s.scalars(select(BuscaRecente).order_by(BuscaRecente.id.desc()).limit(limite)):
            d = _d(b)
            # Linhas gravadas por versoes antigas podem ter dado de socio: nunca saem.
            filtros = dict(d.get("filtros") or {})
            if sem_dados_de_socio(filtros) != filtros or _sem_cpf_nos_filtros(filtros) != filtros:
                continue
            out.append(d)
        return out


def limpar_recentes() -> int:
    with get_session_factory()() as s:
        n = s.execute(delete(BuscaRecente)).rowcount
        s.commit()
        return n or 0


# ---------- listas ----------


def criar_lista(nome: str, descricao: str | None = None) -> dict:
    nome = (nome or "").strip()[:120]
    if not nome:
        raise ValueError("Dê um nome para a lista.")
    init_database()
    with get_session_factory()() as s:
        lst = ListaEmpresas(nome=nome, descricao=(descricao or "").strip()[:500] or None)
        s.add(lst)
        s.commit()
        return _d(lst)


def listas() -> list[dict]:
    init_database()
    with get_session_factory()() as s:
        contagem = dict(s.execute(select(ListaItem.lista_id, func.count()).group_by(ListaItem.lista_id)).all())
        out = []
        for lst in s.scalars(select(ListaEmpresas).order_by(ListaEmpresas.nome)):
            d = _d(lst)
            d["qtd"] = contagem.get(lst.id, 0)
            out.append(d)
        return out


def obter_lista(lista_id: int) -> dict | None:
    init_database()
    with get_session_factory()() as s:
        lst = s.get(ListaEmpresas, lista_id)
        if lst is None:
            return None
        d = _d(lst)
        d["itens"] = [_d(i) for i in s.scalars(
            select(ListaItem).where(ListaItem.lista_id == lista_id).order_by(ListaItem.adicionado_em.desc(), ListaItem.id.desc()))]
        return d


def renomear_lista(lista_id: int, nome: str, descricao: str | None) -> dict:
    with get_session_factory()() as s:
        lst = s.get(ListaEmpresas, lista_id)
        if lst is None:
            raise LookupError(lista_id)
        if nome.strip():
            lst.nome = nome.strip()[:120]
        if descricao is not None:  # None = mantem a descricao atual
            lst.descricao = descricao.strip()[:500] or None
        s.commit()
        return _d(lst)


def remover_lista(lista_id: int) -> None:
    with get_session_factory()() as s:
        lst = s.get(ListaEmpresas, lista_id)
        if lst is None:
            raise LookupError(lista_id)
        s.execute(delete(ListaItem).where(ListaItem.lista_id == lista_id))
        s.delete(lst)
        s.commit()


def adicionar(lista_id: int, itens: list[dict]) -> int:
    """itens: [{cnpj, razao_social?, observacao?}]. Retorna quantos entraram."""
    with get_session_factory()() as s:
        lst = s.get(ListaEmpresas, lista_id)
        if lst is None:
            raise LookupError(lista_id)
        existentes = set(s.scalars(select(ListaItem.cnpj).where(ListaItem.lista_id == lista_id)))
        if len(existentes) + len(itens) > MAX_ITENS_LISTA:
            raise ValueError(f"Uma lista comporta até {MAX_ITENS_LISTA} empresas.")
        n = 0
        for it in itens:
            cnpj = re.sub(r"[^0-9A-Za-z]", "", str(it.get("cnpj", ""))).upper()
            if not _CNPJ.fullmatch(cnpj) or not is_valid(cnpj) or cnpj in existentes:
                continue
            razao = it.get("razao_social")
            obs = it.get("observacao")
            s.add(ListaItem(lista_id=lista_id, cnpj=cnpj,
                            razao_social=str(razao).strip()[:200] or None if isinstance(razao, str | int) else None,
                            observacao=str(obs).strip()[:1000] or None if isinstance(obs, str | int | float) else None))
            existentes.add(cnpj)
            n += 1
        lst.atualizado_em = datetime.now()
        s.commit()
        return n


def anotar(lista_id: int, cnpj: str, observacao: str) -> None:
    with get_session_factory()() as s:
        item = s.scalars(select(ListaItem).where(ListaItem.lista_id == lista_id, ListaItem.cnpj == cnpj)).first()
        if item is None:
            raise LookupError(cnpj)
        item.observacao = (observacao or "").strip()[:1000] or None
        s.commit()


def remover_item(lista_id: int, cnpj: str) -> None:
    with get_session_factory()() as s:
        n = s.execute(delete(ListaItem).where(ListaItem.lista_id == lista_id, ListaItem.cnpj == cnpj)).rowcount
        s.commit()
        if not n:
            raise LookupError(cnpj)
