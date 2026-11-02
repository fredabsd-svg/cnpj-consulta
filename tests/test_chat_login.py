"""Login no provedor de IA sem chave (OAuth com PKCE): hoje, OpenRouter."""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from fastapi.testclient import TestClient

from app.main import app

TROCA = "https://openrouter.ai/api/v1/auth/keys"


def _iniciar(c: TestClient, headers) -> dict[str, str]:
    r = c.get("/chat/login/openrouter/iniciar", headers=headers, follow_redirects=False)
    assert r.status_code == 303
    url = urlparse(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://openrouter.ai/auth"
    return {k: v[0] for k, v in parse_qs(url.query).items()}


def test_login_exige_admin(db_initialized):
    r = TestClient(app).get("/chat/login/openrouter/iniciar", follow_redirects=False)
    assert r.status_code == 303 and "/admin/entrar" in r.headers["location"]


def test_provedor_sem_login_oficial(db_initialized, admin_headers):
    c = TestClient(app)
    for pid in ("xai", "anthropic", "groq"):
        assert c.get(f"/chat/login/{pid}/iniciar", headers=admin_headers, follow_redirects=False).status_code == 404
    provs = {p["id"]: p["login"] for p in c.get("/chat/api/provedores", headers=admin_headers).json()}
    assert provs["openrouter"] is True and provs["xai"] is False and provs["anthropic"] is False


@respx.mock
def test_login_openrouter_guarda_a_chave(db_initialized, admin_headers):
    from app.chat import provedores
    from app.receita import jobs

    c = TestClient(app)
    q = _iniciar(c, admin_headers)
    assert q["callback_url"] == "http://testserver/chat/login/openrouter/retorno"
    assert q["code_challenge_method"] == "S256" and len(q["state"]) > 20

    pedidos: list[dict] = []

    def trocar(request: httpx.Request):
        pedidos.append(json.loads(request.content))
        return httpx.Response(200, json={"key": "sk-or-v1-chave-da-conta-0000abcd"})

    respx.post(TROCA).mock(side_effect=trocar)
    r = c.get("/chat/login/openrouter/retorno", params={"code": "codigo-1", "state": q["state"]},
              headers=admin_headers, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/chat?login_ok=openrouter"
    corpo = pedidos[0]
    desafio = base64.urlsafe_b64encode(hashlib.sha256(corpo["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert corpo["code"] == "codigo-1" and desafio == q["code_challenge"] and corpo["code_challenge_method"] == "S256"
    p = provedores.resolver("openrouter")
    assert p.configurado and p.api_key == "sk-or-v1-chave-da-conta-0000abcd"
    reg = [a for a in jobs.auditoria() if a["acao"] == "chat.provedor.login"]
    assert reg and "ok=True" in reg[0]["detalhe"] and "sk-or" not in json.dumps(reg, default=str)

    # o mesmo state nao vale duas vezes
    r = c.get("/chat/login/openrouter/retorno", params={"code": "codigo-1", "state": q["state"]},
              headers=admin_headers, follow_redirects=False)
    assert "login_erro=" in r.headers["location"]
    assert len(pedidos) == 1


@respx.mock
def test_login_recusado_ou_state_falso(db_initialized, admin_headers):
    from app.chat import provedores

    c = TestClient(app)
    respx.post(TROCA).respond(403, json={"error": {"message": "Invalid code or code_verifier"}})
    r = c.get("/chat/login/openrouter/retorno", params={"code": "x", "state": "inventado"},
              headers=admin_headers, follow_redirects=False)
    assert "login_erro=" in r.headers["location"] and not respx.calls  # state falso nem chega ao provedor
    q = _iniciar(c, admin_headers)
    r = c.get("/chat/login/openrouter/retorno", params={"code": "x", "state": q["state"]},
              headers=admin_headers, follow_redirects=False)
    assert "login_erro=" in r.headers["location"] and "403" in r.headers["location"]
    assert not provedores.resolver("openrouter").configurado
