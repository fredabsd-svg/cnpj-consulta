"""Entrar no provedor de IA pelo login dele, sem copiar e colar chave de API.

So entram aqui provedores que oferecem, OFICIALMENTE, login para aplicativos de
terceiros. Hoje:

- OpenRouter: OAuth com PKCE, sem cadastro de aplicativo
  (https://openrouter.ai/docs/use-cases/oauth-pkce). O usuario entra na conta
  do OpenRouter, autoriza, e o OpenRouter devolve uma chave da conta dele, que o
  app guarda como se tivesse sido colada. Pelo OpenRouter ficam disponiveis
  modelos de varios fabricantes (Grok/xAI, Claude, GPT, Gemini, Llama...).

Fora daqui, de proposito: Anthropic (proibe login do Claude.ai em apps de
terceiros), xAI/Grok (o login por assinatura so existe para parceiros escolhidos
pela xAI, sem programa aberto), Groq, Mistral e DeepSeek (so chave de API).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

VALIDADE_SEG = 600.0  # o codigo do OpenRouter vale 10 minutos


@dataclass(frozen=True)
class Login:
    provedor: str
    nome: str
    autorizar: str
    trocar: str


LOGINS: dict[str, Login] = {
    "openrouter": Login("openrouter", "OpenRouter", "https://openrouter.ai/auth",
                        "https://openrouter.ai/api/v1/auth/keys"),
}

_pendentes: dict[str, tuple[str, str, float]] = {}  # state -> (provedor, code_verifier, criado)
_lock = threading.Lock()


class LoginInvalido(ValueError):
    pass


def disponivel(pid: str) -> bool:
    return pid in LOGINS


def _desafio(verificador: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest()).rstrip(b"=").decode()


def iniciar(pid: str, retorno: str) -> str:
    """URL de login do provedor. `retorno` e o endereco /chat/login/{pid}/retorno do app."""
    login = LOGINS.get(pid)
    if login is None:
        raise LookupError(pid)
    estado = secrets.token_urlsafe(24)
    verificador = secrets.token_urlsafe(48)
    agora = time.monotonic()
    with _lock:
        for k in [k for k, v in _pendentes.items() if agora - v[2] > VALIDADE_SEG]:
            del _pendentes[k]
        if len(_pendentes) > 50:  # cliques repetidos: guarda so os mais novos
            for k in sorted(_pendentes, key=lambda k: _pendentes[k][2])[:-50]:
                del _pendentes[k]
        _pendentes[estado] = (pid, verificador, agora)
    return login.autorizar + "?" + urlencode({
        "callback_url": retorno, "code_challenge": _desafio(verificador), "code_challenge_method": "S256",
        "state": estado, "key_label": "CNPJ Consulta (chat)",
    })


def _verificador(pid: str, estado: str) -> str:
    with _lock:
        pendente = _pendentes.pop(estado or "", None)
    if pendente is None or pendente[0] != pid or time.monotonic() - pendente[2] > VALIDADE_SEG:
        raise LoginInvalido("Login expirado ou iniciado em outra janela. Clique em Entrar de novo.")
    return pendente[1]


async def concluir(pid: str, codigo: str, estado: str) -> str:
    """Troca o codigo pela chave da conta do usuario. Devolve a chave (o chamador a guarda)."""
    login = LOGINS.get(pid)
    if login is None:
        raise LookupError(pid)
    verificador = _verificador(pid, estado)  # sempre consome o estado, mesmo com codigo ruim
    if not codigo or len(codigo) > 500:
        raise LoginInvalido("O provedor não devolveu o código de autorização.")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as cliente:
            r = await cliente.post(login.trocar, json={
                "code": codigo, "code_verifier": verificador, "code_challenge_method": "S256"})
    except httpx.HTTPError as e:
        raise LoginInvalido(f"Não foi possível falar com {login.nome} ({type(e).__name__}).") from e
    if r.status_code >= 400:
        raise LoginInvalido(f"{login.nome} recusou o login (HTTP {r.status_code}). Tente entrar de novo.")
    try:
        chave = r.json().get("key")
    except (ValueError, AttributeError):
        chave = None
    if not isinstance(chave, str) or not chave.strip():
        raise LoginInvalido(f"{login.nome} não devolveu a chave da conta.")
    return chave.strip()
