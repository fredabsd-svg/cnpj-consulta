"""Controle de acesso da area administrativa.

O app roda so em 127.0.0.1 e as consultas nao tem login, mas as operacoes que
alteram dados (baixar/publicar/restaurar a base, editar contatos
complementares, ver auditoria) exigem um token de administrador:

- ADMIN_TOKEN no .env, ou um token aleatorio gerado no primeiro uso em
  data/admin_token.txt (permissao 600). Mostre-o com `cnpj admin-token`.
- Login em /admin/entrar cria um cookie de sessao assinado (HMAC-SHA256),
  HttpOnly e SameSite=Strict, valido por ADMIN_SESSAO_HORAS. Trocar o token
  invalida todas as sessoes.
- Scripts podem mandar `Authorization: Bearer <token>`.
- Tentativas erradas sao limitadas por IP e registradas na auditoria.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

from fastapi import HTTPException, Request

from app.config import get_settings

COOKIE = "cnpj_admin"
MAX_TENTATIVAS = 5
JANELA_TENTATIVAS = 300  # s

_lock = threading.Lock()
_falhas: dict[str, list[float]] = {}


def _arquivo_token() -> Path:
    return Path(get_settings().receita_data_dir).expanduser().parent / "admin_token.txt"


def token_admin() -> str:
    configurado = get_settings().admin_token.strip()
    if configurado:
        return configurado
    path = _arquivo_token()
    with _lock:
        try:
            atual = path.read_text(encoding="utf-8").strip()
            if len(atual) >= 16:
                return atual
        except OSError:
            pass
        novo = secrets.token_urlsafe(24)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(novo + "\n")
        return novo


def _chave() -> bytes:
    return hashlib.sha256(b"cnpj-consulta-admin|" + token_admin().encode()).digest()


def criar_sessao(agora: float | None = None) -> str:
    exp = int((time.time() if agora is None else agora) + get_settings().admin_sessao_horas * 3600)
    nonce = secrets.token_hex(8)
    msg = f"{exp}.{nonce}"
    sig = hmac.new(_chave(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def sessao_valida(valor: str | None, agora: float | None = None) -> bool:
    if not valor or valor.count(".") != 2:
        return False
    if not valor.isascii():
        return False
    exp, nonce, sig = valor.split(".")
    esperado = hmac.new(_chave(), f"{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(esperado, sig):
        return False
    try:
        return int(exp) > (time.time() if agora is None else agora)
    except ValueError:
        return False


def token_confere(candidato: str) -> bool:
    return hmac.compare_digest(candidato.strip().encode(), token_admin().encode())


def ip_de(request: Request) -> str:
    """IP do cliente para limite de tentativas e auditoria.

    O uvicorn troca o IP pelo X-Forwarded-For quando a conexao vem de 127.0.0.1;
    como todo cliente local vem de la, qualquer um inventaria um IP novo a cada
    tentativa. Sem CONFIAR_PROXY=true, o cabecalho e ignorado.
    """
    from app.config import get_settings

    if request.headers.get("x-forwarded-for") and not get_settings().confiar_proxy:
        return "127.0.0.1"
    return request.client.host if request.client else "?"


def bloqueado(ip: str) -> bool:
    agora = time.monotonic()
    with _lock:
        tentativas = [t for t in _falhas.get(ip, []) if agora - t < JANELA_TENTATIVAS]
        _falhas[ip] = tentativas
        return len(tentativas) >= MAX_TENTATIVAS


def registrar_falha(ip: str) -> None:
    with _lock:
        _falhas.setdefault(ip, []).append(time.monotonic())


def limpar_falhas() -> None:
    with _lock:
        _falhas.clear()


def eh_admin(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return token_confere(auth[7:])
    return sessao_valida(request.cookies.get(COOKIE))


def exigir_admin_api(request: Request) -> None:
    """Dependencia das rotas /api/admin/* (e do chat). Bearer errado conta como
    tentativa de login: entra no limite por IP e na auditoria."""
    bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
    if bearer:
        ip = ip_de(request)
        if bloqueado(ip):
            raise HTTPException(status_code=429, detail="Muitas tentativas. Aguarde 5 minutos.")
        if not eh_admin(request):
            from app.receita import jobs

            registrar_falha(ip)
            jobs.auditar("admin.login", resultado="negado", detalhe="token Bearer incorreto", ip=ip)
            raise HTTPException(status_code=401, detail="Token de administrador incorreto.")
        return
    if not eh_admin(request):
        raise HTTPException(status_code=401, detail="Acesso restrito ao administrador. Entre em /admin/entrar.")


class LoginNecessario(Exception):  # noqa: N818
    def __init__(self, destino: str) -> None:
        self.destino = destino


def exigir_admin_pagina(request: Request) -> None:
    """Dependencia das paginas administrativas: redireciona para o login."""
    if not eh_admin(request):
        raise LoginNecessario(request.url.path)
