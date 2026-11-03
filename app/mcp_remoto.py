"""MCP pela rede (Tailscale, Tailscale Funnel ou outro tunel), com chave.

O mesmo servidor MCP do app (app/mcp_server.py) fica disponivel dentro do site,
no mesmo endereco e porta, em Streamable HTTP:

- ``/mcp`` com o cabecalho ``Authorization: Bearer <chave>`` (Claude Code,
  Cursor, VS Code, Claude Desktop via mcp-remote...);
- ``/mcp/k/<chave>`` para clientes que so aceitam um endereco, sem cabecalho
  (conectores do claude.ai e do ChatGPT).

A chave fica em MCP_CHAVE (.env) ou e gerada no primeiro uso em
data/mcp_chave.txt (so o dono le). Pode ser trocada no painel Base de Dados.
Chave errada conta como tentativa de login (limite por IP e auditoria).
Ferramentas que alteram dados so rodam com MCP_PERMITIR_ADMIN=true.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

PREFIXO_CHAVE = "/mcp/k/"
_lock = threading.Lock()
_gerenciador: Any = None  # StreamableHTTPSessionManager do processo (criado no lifespan)
_CHAVE_NA_URL = re.compile(r"/mcp/k/[^/?#\s]+")


# ---------------------------------------------------------------- chave


def _arquivo() -> Path:
    return Path(get_settings().receita_data_dir).expanduser().parent / "mcp_chave.txt"


def chave() -> str:
    configurada = get_settings().mcp_chave.strip()
    if configurada:
        return configurada
    path = _arquivo()
    with _lock:
        try:
            atual = path.read_text(encoding="utf-8").strip()
            if len(atual) >= 24:
                return atual
        except OSError:
            pass
        return _gravar(path)


def _gravar(path: Path) -> str:
    nova = "mcp_" + secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(nova + "\n")
    return nova


def nova_chave() -> str:
    """Troca a chave (a antiga deixa de valer na hora)."""
    if get_settings().mcp_chave.strip():
        raise PermissionError("A chave vem de MCP_CHAVE no .env: troque-a la e reinicie o app.")
    with _lock:
        return _gravar(_arquivo())


def chave_da_env() -> bool:
    return bool(get_settings().mcp_chave.strip())


def esconder_chave(texto: str) -> str:
    """Para logs: /mcp/k/<chave> vira /mcp/k/…"""
    return _CHAVE_NA_URL.sub("/mcp/k/…", texto)


# ---------------------------------------------------------------- servidor


@asynccontextmanager
async def ciclo_de_vida():
    """Sobe o gerenciador de sessoes do MCP junto com o app (um por inicio)."""
    global _gerenciador
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings

    from app import mcp_server

    servidor = mcp_server.configurar()
    gerenciador = StreamableHTTPSessionManager(
        app=servidor._lowlevel_server,
        stateless=True,  # cada pedido se basta: funciona atras de tunel/proxy e com varias abas
        json_response=True,
        # O Host ja e conferido pelo app (localhost, IP, *.ts.net, APP_HOSTS_PERMITIDOS)
        # e todo pedido exige a chave.
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    async with gerenciador.run():
        _gerenciador = gerenciador
        try:
            yield
        finally:
            _gerenciador = None


async def _responder(send, status: int, corpo: dict, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    dados = json.dumps(corpo, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(dados)).encode()),
        *(extra or [])]})
    await send({"type": "http.response.body", "body": dados})


def _chamadas_admin(corpo: bytes) -> list[str]:
    from app.mcp_server import FERRAMENTAS_ADMIN

    try:
        msg = json.loads(corpo or b"null")
    except ValueError:
        return []
    msgs = msg if isinstance(msg, list) else [msg]
    return [m["params"]["name"] for m in msgs
            if isinstance(m, dict) and m.get("method") == "tools/call"
            and isinstance(m.get("params"), dict) and m["params"].get("name") in FERRAMENTAS_ADMIN]


class EndpointMcp:
    """ASGI de /mcp e /mcp/k/<chave>: confere a chave e entrega ao MCP."""

    async def __call__(self, scope, receive, send) -> None:
        from app.receita import jobs
        from app.services import admin_auth

        caminho = scope.get("path", "")
        cabecalhos = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if caminho.startswith(PREFIXO_CHAVE):
            candidata = caminho[len(PREFIXO_CHAVE):].strip("/")
        else:
            auth = cabecalhos.get("authorization", "")
            candidata = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        cliente = scope.get("client") or ("?", 0)
        ip = str(cliente[0])
        if admin_auth.bloqueado(ip):
            await _responder(send, 429, {"erro": "Muitas tentativas com chave errada. Aguarde 5 minutos."})
            return
        if not candidata or not hmac.compare_digest(candidata.encode(), chave().encode()):
            if candidata:
                admin_auth.registrar_falha(ip)
                jobs.auditar("mcp.remoto", resultado="negado", detalhe="chave MCP incorreta", ip=ip)
            await _responder(send, 401, {"erro": "Chave MCP ausente ou incorreta. Use Authorization: Bearer "
                                                 "<chave> ou o endereco /mcp/k/<chave> (painel Base de Dados)."},
                             [(b"www-authenticate", b'Bearer realm="cnpj-consulta"')])
            return
        if _gerenciador is None:
            await _responder(send, 503, {"erro": "Servidor MCP iniciando; tente de novo."})
            return

        # Corpo inteiro (pedido JSON-RPC pequeno): ferramentas que alteram dados so
        # com MCP_PERMITIR_ADMIN (o Chat com IA do app pode te-las ligado no processo).
        corpo = b""
        if scope.get("method") == "POST":
            mais = True
            while mais:
                msg = await receive()
                corpo += msg.get("body", b"")
                mais = msg.get("more_body", False)
                if len(corpo) > 4 * 1024 * 1024:
                    await _responder(send, 413, {"erro": "Pedido grande demais."})
                    return
            admin = _chamadas_admin(corpo)
            if admin and not get_settings().mcp_permitir_admin:
                await _responder(send, 403, {"erro": "Ferramenta administrativa desligada no acesso remoto: "
                                                     f"{', '.join(admin)}. Ligue com MCP_PERMITIR_ADMIN=true."})
                return
        entregue = False

        async def receber():
            nonlocal entregue
            if scope.get("method") == "POST" and not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        escopo = dict(scope, path="/mcp", raw_path=b"/mcp")
        await _gerenciador.handle_request(escopo, receber, send)
