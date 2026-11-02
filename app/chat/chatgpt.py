"""Continuar com o ChatGPT: usar o plano do ChatGPT do usuario no chat, sem chave.

Fluxo oficial da OpenAI para apps locais e de codigo aberto ("Sign in with
ChatGPT", plano do ChatGPT; https://developers.openai.com/siwc/token-sharing-open-source):

1. OAuth com PKCE em https://auth.openai.com; no primeiro login o app se registra
   (client_id=dynamic_agent_client) e recebe um client_id proprio (oaiapp_...);
2. o retorno vai para http://127.0.0.1:<porta>/chat/login/chatgpt/retorno (a OpenAI
   so aceita loopback 127.0.0.1);
3. o codigo vira access token (1 h) + refresh token (30 dias, renovado a cada uso);
   o ID token e conferido (assinatura, emissor, audiencia, validade, nonce);
4. as respostas usam a Responses API (store=false, stream=true).

As credenciais ficam num arquivo so do dono (data/chatgpt.json, 0600), nunca no
navegador, em URLs ou em logs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

EMISSOR = "https://auth.openai.com"
AUTORIZAR = "https://auth.openai.com/api/accounts/authorize"
TOKEN = "https://auth.openai.com/api/accounts/oauth/token"
CONFIG_OIDC = "https://auth.openai.com/.well-known/openid-configuration"
RECURSO = "https://api.openai.com/v1"
API = "https://api.openai.com/v1"
CLIENTE_DINAMICO = "dynamic_agent_client"
ESCOPOS = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
ESCOPO_PLANO = "chatgpt.tokens.use.direct"
NOME_APP = "CNPJ Consulta"
CAMINHO_RETORNO = "/chat/login/chatgpt/retorno"
GERENCIAR_USO = "https://chatgpt.com/settings/usage"
VALIDADE_SEG = 600.0
RENOVAR_ANTES_SEG = 300.0

_lock = threading.Lock()
_travas: dict[int, asyncio.Lock] = {}
_pendentes: dict[str, dict[str, Any]] = {}
_oidc: dict[str, Any] = {}


class LoginInvalido(ValueError):
    pass


class SessaoExpirada(RuntimeError):
    """O refresh token nao vale mais: e preciso entrar de novo."""


# ---------------------------------------------------------------- credenciais


def _arquivo() -> Path:
    return Path(get_settings().receita_data_dir).expanduser().parent / "chatgpt.json"


def carregar() -> dict[str, Any]:
    try:
        dados = json.loads(_arquivo().read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError):
        return {}


def _gravar(dados: dict[str, Any]) -> None:
    """Grava de forma atomica, legivel so pelo dono."""
    path = _arquivo()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(dados, fh, ensure_ascii=False)
    os.replace(tmp, path)


def conectado() -> bool:
    c = carregar()
    return bool(c.get("refresh_token") or (c.get("access_token") and c.get("expira_em", 0) > time.time()))


def conta() -> dict[str, Any] | None:
    """Dados para a tela (sem tokens)."""
    c = carregar()
    if not conectado():
        return {"conectado": False, "registrado": bool(c.get("client_id")), "email": c.get("email")}
    return {"conectado": True, "registrado": True, "email": c.get("email"), "gerenciar_uso": GERENCIAR_USO}


def _id_host(c: dict[str, Any]) -> str:
    # Identificador estavel deste computador: escolhido e gravado ANTES do primeiro login.
    if not c.get("ext_agent_host_id"):
        c["ext_agent_host_id"] = f"urn:uuid:{uuid.uuid4()}"
        _gravar(c)
    return c["ext_agent_host_id"]


# ---------------------------------------------------------------- login


def _desafio(verificador: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest()).rstrip(b"=").decode()


def iniciar(porta: int, voltar_para: str, *, nova_conta: bool = False) -> str:
    """URL de login da OpenAI. `voltar_para` e a origem do app (ex.: http://localhost:8000)."""
    with _lock:
        c = carregar()
        host_id = _id_host(c)
        agora = time.monotonic()
        for k in [k for k, v in _pendentes.items() if agora - v["criado"] > VALIDADE_SEG]:
            del _pendentes[k]
        if len(_pendentes) > 50:
            for k in sorted(_pendentes, key=lambda k: _pendentes[k]["criado"])[:-50]:
                del _pendentes[k]
        cliente = c.get("client_id") if c.get("client_id") and not nova_conta else CLIENTE_DINAMICO
        estado, nonce, verificador = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        retorno = f"http://127.0.0.1:{int(porta)}{CAMINHO_RETORNO}"
        _pendentes[estado] = {"verificador": verificador, "nonce": nonce, "retorno": retorno, "cliente": cliente,
                              "voltar_para": voltar_para, "criado": agora}
    params: dict[str, str] = {
        "client_id": cliente, "response_type": "code", "redirect_uri": retorno, "scope": ESCOPOS,
        "resource": RECURSO, "state": estado, "nonce": nonce, "code_challenge": _desafio(verificador),
        "code_challenge_method": "S256", "ext_agent_host_id": host_id,
    }
    if cliente == CLIENTE_DINAMICO:
        params["agent_name_hint"] = NOME_APP
    else:
        if c.get("id_token"):
            params["id_token_hint"] = c["id_token"]
        if c.get("email"):
            params["login_hint"] = c["email"]
    return AUTORIZAR + "?" + urlencode(params)


def pendente(estado: str) -> dict[str, Any] | None:
    with _lock:
        return _pendentes.get(estado or "")


async def _config_oidc(cliente: httpx.AsyncClient) -> dict[str, Any]:
    if not _oidc.get("config"):
        r = await cliente.get(CONFIG_OIDC)
        r.raise_for_status()
        _oidc["config"] = r.json()
    return _oidc["config"]


async def _validar_id_token(cliente: httpx.AsyncClient, id_token: str, audiencia: str, nonce: str) -> dict:
    import jwt

    try:
        cabecalho = jwt.get_unverified_header(id_token)
        cfg = await _config_oidc(cliente)
        chaves = (await cliente.get(cfg["jwks_uri"])).json().get("keys") or []
        jwk = next((k for k in chaves if k.get("kid") == cabecalho.get("kid")), None)
        if jwk is None:
            raise LoginInvalido("Não foi possível conferir a assinatura do login da OpenAI.")
        alg = jwk.get("alg") or cabecalho.get("alg") or "RS256"
        if alg not in ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256"):
            raise LoginInvalido("Assinatura do login em formato não aceito.")
        dados = jwt.decode(id_token, key=jwt.PyJWK(jwk).key, algorithms=[alg], audience=audiencia, issuer=EMISSOR,
                           options={"require": ["exp", "iat", "sub", "iss", "aud"]}, leeway=60)
    except jwt.PyJWTError as e:
        raise LoginInvalido(f"Login da OpenAI recusado na conferência ({type(e).__name__}).") from e
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise LoginInvalido("Não foi possível conferir o login com a OpenAI. Tente de novo.") from e
    if not secrets.compare_digest(str(dados.get("nonce") or ""), nonce):
        raise LoginInvalido("Login da OpenAI recusado (nonce não confere).")
    return dados


async def concluir(*, codigo: str, estado: str, cliente_retorno: str = "", erro: str = "") -> dict[str, Any]:
    """Troca o codigo pelos tokens, confere o ID token e grava. Devolve
    {"voltar_para", "primeiro_login", "email"}."""
    with _lock:
        pend = _pendentes.pop(estado or "", None)
    if pend is None or time.monotonic() - pend["criado"] > VALIDADE_SEG:
        raise LoginInvalido("Login expirado ou iniciado em outra janela. Clique em Continuar com o ChatGPT de novo.")
    voltar = pend["voltar_para"]
    if erro:
        raise LoginInvalido("Login cancelado na OpenAI." if erro == "access_denied" else f"A OpenAI recusou o login ({erro[:60]}).")
    if not codigo or len(codigo) > 2000:
        raise LoginInvalido("A OpenAI não devolveu o código de autorização.")
    if pend["cliente"] == CLIENTE_DINAMICO:
        if not cliente_retorno or cliente_retorno == CLIENTE_DINAMICO or len(cliente_retorno) > 200:
            raise LoginInvalido("A OpenAI não devolveu o registro do app. Tente de novo.")
        cliente_id = cliente_retorno
    else:
        if cliente_retorno and cliente_retorno != pend["cliente"]:
            raise LoginInvalido("O registro devolvido pela OpenAI não confere com o deste computador.")
        cliente_id = pend["cliente"]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as cliente:
            r = await cliente.post(TOKEN, data={
                "grant_type": "authorization_code", "client_id": cliente_id, "code": codigo,
                "code_verifier": pend["verificador"], "redirect_uri": pend["retorno"], "resource": RECURSO})
            if r.status_code >= 400:
                raise LoginInvalido(f"A OpenAI recusou o login (HTTP {r.status_code}). Tente entrar de novo.")
            tokens = r.json()
            if not isinstance(tokens, dict) or not tokens.get("access_token") or not tokens.get("id_token"):
                raise LoginInvalido("A OpenAI não devolveu as credenciais do login.")
            ident = await _validar_id_token(cliente, tokens["id_token"], cliente_id, pend["nonce"])
    except httpx.HTTPError as e:
        raise LoginInvalido(f"Não foi possível falar com a OpenAI ({type(e).__name__}).") from e
    except ValueError as e:
        if isinstance(e, LoginInvalido):
            raise
        raise LoginInvalido("Resposta inválida da OpenAI no login.") from e
    escopos = str(tokens.get("scope") or "").split()
    if ESCOPO_PLANO not in escopos:
        raise LoginInvalido("O uso do plano do ChatGPT não foi autorizado neste login.")
    with _lock:
        c = carregar()
        if pend["cliente"] != CLIENTE_DINAMICO and c.get("subject") and c["subject"] != ident["sub"]:
            raise LoginInvalido("Entrou com outra conta. Use Entrar com outra conta para trocar de conta.")
        primeiro = not c.get("ja_entrou")
        c.update({
            "email": ident.get("email") or c.get("email"), "issuer": EMISSOR, "subject": ident["sub"],
            "client_id": cliente_id, "id_token": tokens["id_token"], "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token") or "", "token_type": tokens.get("token_type") or "Bearer",
            "expira_em": time.time() + float(tokens.get("expires_in") or 3600), "scopes": escopos,
            "saved_at": datetime.now().isoformat(timespec="seconds"), "ja_entrou": True,
        })
        _gravar(c)
    return {"voltar_para": voltar, "primeiro_login": primeiro, "email": c.get("email")}


async def token_de_acesso() -> str:
    """Access token valido; renova pelo refresh token quando falta pouco para vencer."""
    c = carregar()
    if c.get("access_token") and c.get("expira_em", 0) - time.time() > RENOVAR_ANTES_SEG:
        return c["access_token"]
    laco = id(asyncio.get_running_loop())
    trava = _travas.get(laco)
    if trava is None:
        _travas.clear()  # um laco de eventos por vez no app
        trava = _travas[laco] = asyncio.Lock()
    async with trava:  # refresh token rotativo: uma renovacao por vez
        c = carregar()
        if c.get("access_token") and c.get("expira_em", 0) - time.time() > RENOVAR_ANTES_SEG:
            return c["access_token"]
        if not c.get("refresh_token"):
            raise SessaoExpirada("Entre de novo com o ChatGPT (Configurar provedores).")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as cliente:
                r = await cliente.post(TOKEN, data={
                    "grant_type": "refresh_token", "client_id": c["client_id"],
                    "refresh_token": c["refresh_token"], "resource": RECURSO})
        except httpx.HTTPError as e:
            raise RuntimeError(f"Sem conexão com a OpenAI para renovar o login ({type(e).__name__}).") from e
        if r.status_code >= 400:
            try:
                codigo = str((r.json() or {}).get("error") or "")
            except ValueError:
                codigo = ""
            if r.status_code in (400, 401) and codigo != "invalid_client":
                with _lock:
                    c = carregar()
                    for k in ("access_token", "refresh_token", "id_token", "expira_em"):
                        c.pop(k, None)
                    _gravar(c)
                raise SessaoExpirada("O login do ChatGPT expirou. Entre de novo (Configurar provedores).")
            raise RuntimeError(f"A OpenAI não renovou o login (HTTP {r.status_code}). Tente de novo em instantes.")
        tokens = r.json()
        with _lock:
            c = carregar()
            c.update({"access_token": tokens["access_token"],
                      "refresh_token": tokens.get("refresh_token") or c.get("refresh_token"),
                      "expira_em": time.time() + float(tokens.get("expires_in") or 3600),
                      "scopes": str(tokens.get("scope") or "").split() or c.get("scopes", []),
                      "saved_at": datetime.now().isoformat(timespec="seconds")})
            if tokens.get("id_token"):
                c["id_token"] = tokens["id_token"]
            _gravar(c)
        return c["access_token"]


async def sair() -> bool:
    """Revoga o refresh token e apaga os tokens locais (mantem o registro do app).
    Devolve se a revogacao foi confirmada pela OpenAI."""
    c = carregar()
    confirmada = not c.get("refresh_token")
    if c.get("refresh_token"):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as cliente:
                cfg = await _config_oidc(cliente)
                r = await cliente.post(cfg["revocation_endpoint"], data={
                    "token": c["refresh_token"], "token_type_hint": "refresh_token", "client_id": c["client_id"]})
                confirmada = r.status_code == 200
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.info("revogacao do login do ChatGPT falhou: %s", type(e).__name__)
    with _lock:
        c = carregar()
        for k in ("access_token", "refresh_token", "id_token", "expira_em"):
            c.pop(k, None)
        _gravar(c)
    return confirmada


async def listar_modelos() -> tuple[list[str], dict[str, str]]:
    """Modelos liberados para a conta: (slugs na ordem do servidor, nomes de exibicao)."""
    try:
        token = await token_de_acesso()
        async with httpx.AsyncClient(timeout=10.0) as cliente:
            r = await cliente.get(f"{API}/models", headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            dados = r.json()
    except Exception as e:  # noqa: BLE001 -- sem lista, o usuario ainda pode digitar o modelo
        log.info("lista de modelos do ChatGPT indisponivel: %s", type(e).__name__)
        return [], {}
    slugs: list[str] = []
    nomes: dict[str, str] = {}
    for m in (dados.get("models") or []) if isinstance(dados, dict) else []:
        if isinstance(m, dict) and m.get("visibility") == "list" and m.get("slug"):
            slugs.append(str(m["slug"]))
            if m.get("display_name"):
                nomes[str(m["slug"])] = str(m["display_name"])
    return slugs, nomes
