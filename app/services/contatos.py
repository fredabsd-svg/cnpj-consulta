"""Contatos complementares: telefones, e-mails e sites obtidos FORA da base da
Receita (site da propria empresa, documento enviado pelo cliente, contato
direto...). Regras:

- So entram por cadastro manual de um administrador, com a FONTE declarada.
  Nada e preenchido, completado ou inferido automaticamente.
- "Verificado" e uma declaracao de quem cadastrou, com data. Sem data, o
  contato aparece como "nao verificado".
- Classificacao de telefone usa o plano de numeracao da Anatel: celular (SMP)
  = 9 + numero de 8 digitos iniciado por 6-9 (ou so os 8 digitos, formato
  anterior ao nono digito); fixo (STFC) = 8 digitos iniciados por 2-5; o resto
  fica "indeterminado".
- Podem ser corrigidos e removidos a qualquer momento; a remocao apaga o
  registro (a auditoria guarda so o tipo e o CNPJ, nunca o valor).
- Retencao: contatos sem verificacao ha mais de CONTATOS_RETENCAO_DIAS podem
  ser descartados pelo painel.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, func, or_, select

from app.config import get_settings
from app.db import get_session_factory, init_database
from app.models.base_cnpj import ContatoComplementar

TIPOS = ("telefone", "email", "site")
_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}$")


class ContatoInvalido(ValueError):
    pass


def classificar_telefone(digitos: str) -> str:
    """Plano de numeracao da Anatel (mesmo criterio da base oficial)."""
    if len(digitos) == 11 and digitos[2] == "9" and digitos[3] in "6789":
        return "celular"
    if len(digitos) == 10 and digitos[2] in "6789":
        return "celular"  # formato antigo, sem o nono digito
    if len(digitos) == 10 and digitos[2] in "2345":
        return "fixo"
    return "indeterminado"


def normalizar(tipo: str, valor: str) -> tuple[str, str | None]:
    """(valor normalizado, subtipo). Levanta ContatoInvalido."""
    valor = (valor or "").strip()
    if tipo == "telefone":
        d = re.sub(r"\D", "", valor)
        if d.startswith("0"):
            d = d.lstrip("0")
        if d.startswith("55") and len(d) in (12, 13):
            d = d[2:]
        if len(d) not in (10, 11) or d[:2] in ("00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "10"):
            raise ContatoInvalido("Telefone deve ter DDD + número (10 ou 11 dígitos).")
        return d, classificar_telefone(d)
    if tipo == "email":
        v = valor.lower()
        if not _EMAIL_RE.match(v) or len(v) > 254:
            raise ContatoInvalido("E-mail inválido.")
        return v, None
    if tipo == "site":
        v = valor if "://" in valor else "https://" + valor
        partes = urlsplit(v)
        if partes.scheme not in ("http", "https") or "." not in (partes.hostname or ""):
            raise ContatoInvalido("Site inválido (use o endereço completo, ex.: https://empresa.com.br).")
        host = (partes.hostname or "").lower().removeprefix("www.")
        return host + (partes.path.rstrip("/") if partes.path not in ("", "/") else ""), None
    raise ContatoInvalido("Tipo de contato inválido.")


def _dict(c: ContatoComplementar) -> dict[str, Any]:
    dias = get_settings().contatos_desatualizado_dias
    ref = c.data_verificacao
    return {
        "id": c.id, "cnpj": c.cnpj, "tipo": c.tipo, "valor": c.valor, "valor_normalizado": c.valor_normalizado,
        "subtipo": c.subtipo, "fonte": c.fonte, "url_fonte": c.url_fonte, "verificado": c.verificado,
        "data_verificacao": c.data_verificacao, "difere_do_oficial": c.difere_do_oficial,
        "observacao": c.observacao, "criado_em": c.criado_em, "atualizado_em": c.atualizado_em,
        "desatualizado": bool(c.verificado and ref and (date.today() - ref).days > dias),
        "origem_tipo": "complementar",
    }


def listar(cnpj: str) -> list[dict]:
    init_database()
    with get_session_factory()() as s:
        rows = s.scalars(
            select(ContatoComplementar).where(ContatoComplementar.cnpj == cnpj).order_by(
                ContatoComplementar.tipo, ContatoComplementar.id)
        )
        return [_dict(c) for c in rows]


def _difere(tipo: str, normalizado: str, oficiais: list[dict]) -> bool:
    valores = {o.get("valor") for o in oficiais if o.get("tipo") == tipo and o.get("valido")}
    return bool(valores) and normalizado not in valores


def salvar(
    cnpj: str,
    *,
    tipo: str,
    valor: str,
    fonte: str,
    url_fonte: str | None = None,
    verificado: bool = False,
    data_verificacao: date | None = None,
    observacao: str | None = None,
    contato_id: int | None = None,
    oficiais: list[dict] | None = None,
) -> dict:
    if not re.fullmatch(r"[0-9A-Z]{12}\d{2}", cnpj or ""):
        raise ContatoInvalido("CNPJ inválido.")
    if tipo not in TIPOS:
        raise ContatoInvalido("Tipo de contato inválido.")
    fonte = (fonte or "").strip()
    if len(fonte) < 3:
        raise ContatoInvalido("Informe a fonte do contato (ex.: site oficial da empresa, e-mail do cliente).")
    if url_fonte:
        url_fonte = url_fonte.strip()
        if urlsplit(url_fonte).scheme not in ("http", "https"):
            raise ContatoInvalido("O endereço da fonte deve começar com http:// ou https://.")
    if verificado and data_verificacao is None:
        data_verificacao = date.today()
    if data_verificacao and data_verificacao > date.today():
        raise ContatoInvalido("A data de verificação não pode estar no futuro.")
    norm, subtipo = normalizar(tipo, valor)
    init_database()
    with get_session_factory()() as s:
        if contato_id:
            c = s.get(ContatoComplementar, contato_id)
            if c is None or c.cnpj != cnpj:
                raise LookupError(contato_id)
        else:
            dup = s.scalars(select(ContatoComplementar).where(
                ContatoComplementar.cnpj == cnpj, ContatoComplementar.tipo == tipo,
                ContatoComplementar.valor_normalizado == norm)).first()
            if dup:
                raise ContatoInvalido("Este contato já está cadastrado para a empresa.")
            c = ContatoComplementar(cnpj=cnpj)
            s.add(c)
        c.tipo, c.valor, c.valor_normalizado, c.subtipo = tipo, valor.strip()[:320], norm, subtipo
        c.fonte, c.url_fonte = fonte[:200], (url_fonte or None)
        c.verificado = bool(verificado)
        c.data_verificacao = data_verificacao if verificado else None
        c.observacao = (observacao or "").strip()[:500] or None
        c.difere_do_oficial = _difere(tipo, norm, oficiais or [])
        s.commit()
        return _dict(c)


def remover(cnpj: str, contato_id: int) -> dict:
    with get_session_factory()() as s:
        c = s.get(ContatoComplementar, contato_id)
        if c is None or c.cnpj != cnpj:
            raise LookupError(contato_id)
        info = {"tipo": c.tipo, "cnpj": c.cnpj}
        s.delete(c)
        s.commit()
        return info


def descartar_por_retencao(dias: int | None = None) -> int:
    """Remove contatos nao verificados (ou com verificacao vencida) ha mais de N dias."""
    dias = dias or get_settings().contatos_retencao_dias
    limite_data = date.today() - timedelta(days=dias)
    limite_dt = datetime.now() - timedelta(days=dias)
    init_database()
    with get_session_factory()() as s:
        n = s.execute(delete(ContatoComplementar).where(or_(
            (ContatoComplementar.data_verificacao.is_(None)) & (ContatoComplementar.atualizado_em < limite_dt),
            ContatoComplementar.data_verificacao < limite_data,
        ))).rowcount
        s.commit()
        return n or 0


def estatisticas() -> dict:
    init_database()
    with get_session_factory()() as s:
        total = s.scalar(select(func.count()).select_from(ContatoComplementar)) or 0
        verificados = s.scalar(select(func.count()).select_from(ContatoComplementar).where(
            ContatoComplementar.verificado.is_(True))) or 0
        empresas = s.scalar(select(func.count(func.distinct(ContatoComplementar.cnpj)))) or 0
    return {"total": total, "verificados": verificados, "empresas": empresas}


# ---------- usado pela busca avancada ----------

MAX_CNPJS_FILTRO = 200_000


def cnpjs_com(
    *,
    tipo: str | None = None,
    subtipo: str | None = None,
    verificado: bool | None = None,
    desatualizado: bool | None = None,
    difere: bool | None = None,
    verificado_desde: date | None = None,
    dominio_email: str | None = None,
) -> list[str]:
    """CNPJs com algum contato complementar que satisfaz o criterio."""
    init_database()
    q = select(ContatoComplementar.cnpj).distinct()
    if tipo:
        q = q.where(ContatoComplementar.tipo == tipo)
    if subtipo:
        q = q.where(ContatoComplementar.subtipo == subtipo)
    if verificado is not None:
        q = q.where(ContatoComplementar.verificado.is_(verificado))
    if difere is not None:
        q = q.where(ContatoComplementar.difere_do_oficial.is_(difere))
    if verificado_desde:
        q = q.where(ContatoComplementar.data_verificacao >= verificado_desde)
    if desatualizado is not None:
        corte = date.today() - timedelta(days=get_settings().contatos_desatualizado_dias)
        cond = ContatoComplementar.verificado.is_(True) & (ContatoComplementar.data_verificacao < corte)
        q = q.where(cond if desatualizado else ~cond)
    if dominio_email:
        q = q.where(ContatoComplementar.tipo == "email",
                    ContatoComplementar.valor_normalizado.like(f"%@{dominio_email.lower().lstrip('@')}"))
    with get_session_factory()() as s:
        return list(s.scalars(q.limit(MAX_CNPJS_FILTRO)))
