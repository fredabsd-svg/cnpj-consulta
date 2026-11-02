"""Continuar com o ChatGPT: login oficial da OpenAI (OAuth/OIDC + PKCE) e
respostas pela Responses API com o plano do usuario."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.main import app

TOKEN = "https://auth.openai.com/api/accounts/oauth/token"
OIDC = "https://auth.openai.com/.well-known/openid-configuration"
JWKS = "https://auth.openai.com/.well-known/jwks.json"
REVOGAR = "https://auth.openai.com/oauth/revoke"
CHAVE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(CHAVE.public_key())), "kid": "k1", "alg": "RS256"}


@pytest.fixture(autouse=True)
def _limpo():
    from app.chat import chatgpt, provedores

    chatgpt._pendentes.clear()
    chatgpt._oidc.clear()
    provedores._cache_modelos.clear()
    yield


def _id_token(aud: str, nonce: str, sub: str = "user-1", email: str = "eu@exemplo.com", **extra) -> str:
    agora = int(time.time())
    return jwt.encode({"iss": "https://auth.openai.com", "aud": aud, "sub": sub, "email": email, "nonce": nonce,
                       "iat": agora, "exp": agora + 3600, **extra}, CHAVE, algorithm="RS256", headers={"kid": "k1"})


def _oidc_mock():
    respx.get(OIDC).respond(json={"issuer": "https://auth.openai.com", "jwks_uri": JWKS, "revocation_endpoint": REVOGAR})
    respx.get(JWKS).respond(json={"keys": [JWK]})


def _iniciar(c: TestClient, headers, **params) -> dict[str, str]:
    r = c.get("/chat/login/chatgpt/iniciar" + (("?" + urlencode(params)) if params else ""),
              headers=headers, follow_redirects=False)
    assert r.status_code == 303, r.text
    url = urlparse(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://auth.openai.com/api/accounts/authorize"
    return {k: v[0] for k, v in parse_qs(url.query).items()}


def _login(c: TestClient, headers, *, escopo="openid profile email offline_access resource.invoke "
                                          "chatgpt.tokens.use.direct", cliente="oaiapp_123", sub="user-1"):
    q = _iniciar(c, headers)
    pedidos: list[dict] = []

    def token(request: httpx.Request):
        dados = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        pedidos.append(dados)
        return httpx.Response(200, json={
            "access_token": "at-1", "refresh_token": "rt-1", "token_type": "Bearer", "expires_in": 3600,
            "scope": escopo, "id_token": _id_token(cliente, q["nonce"], sub=sub)})

    respx.post(TOKEN).mock(side_effect=token)
    _oidc_mock()
    params = {"code": "cod-1", "state": q["state"], "scope": escopo}
    if q["client_id"] == "dynamic_agent_client":
        params["client_id"] = cliente
    r = c.get("/chat/login/chatgpt/retorno", params=params, follow_redirects=False)
    return q, pedidos, r


class TestLogin:
    def test_parametros_do_login(self, db_initialized, admin_headers):
        from app.chat import chatgpt

        q = _iniciar(TestClient(app), admin_headers)
        assert q["client_id"] == "dynamic_agent_client" and q["agent_name_hint"] == "CNPJ Consulta"
        assert q["redirect_uri"] == "http://127.0.0.1:80/chat/login/chatgpt/retorno"
        assert set(q["scope"].split()) == {"openid", "profile", "email", "offline_access", "resource.invoke",
                                           "chatgpt.tokens.use.direct"}
        assert q["resource"] == "https://api.openai.com/v1" and q["code_challenge_method"] == "S256"
        assert q["ext_agent_host_id"].startswith("urn:uuid:")
        # o id do computador e gravado antes do primeiro login e reaproveitado
        assert chatgpt.carregar()["ext_agent_host_id"] == q["ext_agent_host_id"]
        assert _iniciar(TestClient(app), admin_headers)["ext_agent_host_id"] == q["ext_agent_host_id"]

    def test_exige_admin_para_iniciar(self, db_initialized):
        r = TestClient(app).get("/chat/login/chatgpt/iniciar", follow_redirects=False)
        assert "/admin/entrar" in r.headers["location"]

    @respx.mock
    def test_login_completo(self, db_initialized, admin_headers):
        from app.chat import chatgpt, provedores
        from app.receita import jobs

        c = TestClient(app)
        q, pedidos, r = _login(c, admin_headers)
        assert r.status_code == 303 and r.headers["location"] == "http://testserver/chat?login_ok=chatgpt&primeiro=1"
        p = pedidos[0]
        assert p["grant_type"] == "authorization_code" and p["client_id"] == "oaiapp_123"
        assert p["redirect_uri"] == q["redirect_uri"] and p["resource"] == "https://api.openai.com/v1"
        desafio = base64.urlsafe_b64encode(hashlib.sha256(p["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        assert desafio == q["code_challenge"]
        cred = chatgpt.carregar()
        assert cred["client_id"] == "oaiapp_123" and cred["subject"] == "user-1" and cred["refresh_token"] == "rt-1"
        assert stat.S_IMODE(os.stat(chatgpt._arquivo()).st_mode) == 0o600
        assert provedores.resolver("chatgpt").configurado
        reg = [a for a in jobs.auditoria() if a["acao"] == "chat.provedor.login"]
        assert reg and "rt-1" not in json.dumps(reg, default=str) and "at-1" not in json.dumps(reg, default=str)
        lista = {x["id"]: x for x in c.get("/chat/api/provedores", headers=admin_headers).json()}
        assert lista["chatgpt"]["conta"] == {"conectado": True, "registrado": True, "email": "eu@exemplo.com",
                                             "gerenciar_uso": "https://chatgpt.com/settings/usage"}
        assert "at-1" not in json.dumps(lista)

        # segundo login: reaproveita o client_id emitido e nao mostra boas-vindas de novo
        q2 = _iniciar(c, admin_headers)
        assert q2["client_id"] == "oaiapp_123" and "agent_name_hint" not in q2
        assert q2["login_hint"] == "eu@exemplo.com"
        assert _iniciar(c, admin_headers, nova_conta="1")["client_id"] == "dynamic_agent_client"

    @respx.mock
    def test_sem_escopo_do_plano_recusa(self, db_initialized, admin_headers):
        from app.chat import chatgpt

        _, _, r = _login(TestClient(app), admin_headers, escopo="openid profile email")
        assert "login_erro=" in r.headers["location"] and not chatgpt.conectado()

    @respx.mock
    def test_id_token_com_audiencia_errada_recusa(self, db_initialized, admin_headers):
        from app.chat import chatgpt

        c = TestClient(app)
        q = _iniciar(c, admin_headers)
        respx.post(TOKEN).respond(json={"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                                        "scope": "chatgpt.tokens.use.direct openid",
                                        "id_token": _id_token("outro_app", q["nonce"])})
        _oidc_mock()
        r = c.get("/chat/login/chatgpt/retorno", params={"code": "x", "state": q["state"], "client_id": "oaiapp_9"},
                  follow_redirects=False)
        assert "login_erro=" in r.headers["location"] and not chatgpt.conectado()

    def test_state_falso_e_cancelamento(self, db_initialized, admin_headers):
        c = TestClient(app)
        r = c.get("/chat/login/chatgpt/retorno", params={"code": "x", "state": "inventado"}, follow_redirects=False)
        assert r.headers["location"].startswith("/chat?login_erro=")
        q = _iniciar(c, admin_headers)
        r = c.get("/chat/login/chatgpt/retorno", params={"error": "access_denied", "state": q["state"]},
                  follow_redirects=False)
        assert "cancelado" in parse_qs(urlparse(r.headers["location"]).query)["login_erro"][0]


class TestSessao:
    @respx.mock
    async def test_renova_token_e_expira(self, db_initialized):
        from app.chat import chatgpt

        chatgpt._gravar({"client_id": "oaiapp_1", "access_token": "velho", "refresh_token": "rt-1",
                         "expira_em": time.time() + 10})
        rota = respx.post(TOKEN).respond(json={"access_token": "novo", "refresh_token": "rt-2", "expires_in": 3600,
                                               "scope": "chatgpt.tokens.use.direct"})
        assert await chatgpt.token_de_acesso() == "novo"
        dados = {k: v[0] for k, v in parse_qs(rota.calls[0].request.content.decode()).items()}
        assert dados == {"grant_type": "refresh_token", "client_id": "oaiapp_1", "refresh_token": "rt-1",
                         "resource": "https://api.openai.com/v1"}
        assert chatgpt.carregar()["refresh_token"] == "rt-2"  # token rotativo guardado
        assert await chatgpt.token_de_acesso() == "novo" and len(rota.calls) == 1  # ainda valido: nao renova

        chatgpt._gravar({**chatgpt.carregar(), "expira_em": 0})
        respx.post(TOKEN).respond(400, json={"error": "invalid_grant"})
        with pytest.raises(chatgpt.SessaoExpirada):
            await chatgpt.token_de_acesso()
        assert not chatgpt.conectado() and chatgpt.carregar()["client_id"] == "oaiapp_1"  # registro mantido

    @respx.mock
    def test_sair_revoga(self, db_initialized, admin_headers):
        from app.chat import chatgpt

        chatgpt._gravar({"client_id": "oaiapp_1", "access_token": "a", "refresh_token": "rt-9",
                         "expira_em": time.time() + 3600})
        _oidc_mock()
        rota = respx.post(REVOGAR).respond(200)
        r = TestClient(app).post("/chat/api/provedores/chatgpt/sair", headers=admin_headers, json={})
        assert r.json() == {"saiu": True, "revogacao_confirmada": True}
        dados = {k: v[0] for k, v in parse_qs(rota.calls[0].request.content.decode()).items()}
        assert dados == {"token": "rt-9", "token_type_hint": "refresh_token", "client_id": "oaiapp_1"}
        assert not chatgpt.conectado()

    @respx.mock
    async def test_lista_de_modelos_da_conta(self, db_initialized):
        from app.chat import chatgpt, provedores

        chatgpt._gravar({"client_id": "c", "access_token": "a", "refresh_token": "r", "expira_em": time.time() + 3600})
        respx.get("https://api.openai.com/v1/models").respond(json={"models": [
            {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"},
            {"slug": "interno", "display_name": "X", "visibility": "hide"}]})
        modelos = await provedores.listar_modelos(provedores.resolver("chatgpt"))
        assert modelos == ["gpt-6.1-sol"] and provedores.nome_modelo("gpt-6.1-sol") == "GPT-6.1 Sol"


def _sse(*eventos: dict) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in eventos)


class TestRespostas:
    @respx.mock
    async def test_ferramenta_e_resposta_pela_responses_api(self, db_initialized):
        from app.chat import chatgpt, conversas, motor
        from app.services import listas

        listas.criar_lista("Clientes Palmas", None)
        chatgpt._gravar({"client_id": "c", "access_token": "tok", "refresh_token": "r", "expira_em": time.time() + 3600})
        corpos: list[dict] = []
        respostas = [
            _sse({"type": "response.output_text.delta", "delta": "Vou ver. "},
                 {"type": "response.output_item.done", "item": {
                     "type": "function_call", "call_id": "call_1", "namespace": "cnpj", "name": "listar_listas",
                     "arguments": "{}"}},
                 {"type": "response.completed", "response": {"usage": {"input_tokens": 10, "output_tokens": 3}}}),
            _sse({"type": "response.output_text.delta", "delta": "Você tem 1 lista."},
                 {"type": "response.completed", "response": {"usage": {"input_tokens": 20, "output_tokens": 5}}}),
        ]

        def responder(request: httpx.Request):
            corpos.append(json.loads(request.content))
            assert request.headers["authorization"] == "Bearer tok"
            return httpx.Response(200, content=respostas[len(corpos) - 1], headers={"content-type": "text/event-stream"})

        respx.post("https://api.openai.com/v1/responses").mock(side_effect=responder)
        conv = conversas.criar("chatgpt", "gpt-6.1-sol")
        evs = [ev async for ev in motor.responder(conv["id"], "Quais listas?")]
        assert "".join(e["delta"] for e in evs if e["tipo"] == "texto") == "Vou ver. Você tem 1 lista."
        primeiro, segundo = corpos
        assert primeiro["store"] is False and primeiro["stream"] is True and "CNPJ Consulta" in primeiro["instructions"]
        assert not any(i.get("role") == "system" for i in primeiro["input"])
        assert primeiro["tools"][0]["type"] == "namespace" and primeiro["tools"][0]["name"] == "cnpj"
        assert any(t["name"] == "consultar_sql_base" for t in primeiro["tools"][0]["tools"])
        for proibido in ("temperature", "max_output_tokens", "previous_response_id", "metadata", "user"):
            assert proibido not in primeiro
        tipos = [i.get("type") or i.get("role") for i in segundo["input"]]
        assert tipos == ["user", "assistant", "function_call", "function_call_output"]
        assert segundo["input"][2] == {"type": "function_call", "call_id": "call_1", "namespace": "cnpj",
                                       "name": "listar_listas", "arguments": "{}"}
        assert "Clientes Palmas" in segundo["input"][3]["output"] and segundo["input"][3]["call_id"] == "call_1"
        assert conversas.obter(conv["id"])["tokens_saida"] == 8

    @respx.mock
    async def test_limite_do_plano_e_stream_interrompido(self, db_initialized):
        from app.chat import chatgpt, conversas, motor

        chatgpt._gravar({"client_id": "c", "access_token": "tok", "refresh_token": "r", "expira_em": time.time() + 3600})
        rota = respx.post("https://api.openai.com/v1/responses")
        rota.respond(200, content=_sse({"type": "response.failed", "response": {
            "error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}}),
            headers={"content-type": "text/event-stream"})
        conv = conversas.criar("chatgpt", "gpt-6.1-sol")
        evs = [ev async for ev in motor.responder(conv["id"], "oi")]
        erro = next(e for e in evs if e["tipo"] == "erro")
        assert "chatgpt.com/settings/usage" in erro["mensagem"]
        rota.respond(200, content=_sse({"type": "response.output_text.delta", "delta": "meia"}),
                     headers={"content-type": "text/event-stream"})
        evs = [ev async for ev in motor.responder(conv["id"], "oi de novo")]
        assert "interrompida" in next(e for e in evs if e["tipo"] == "erro")["mensagem"]


def test_salvar_modelos_extras_do_chatgpt(db_initialized, admin_headers):
    r = TestClient(app).post("/chat/api/provedores/chatgpt", headers=admin_headers, json={"modelos": "gpt-x"})
    assert r.status_code == 200 and "gpt-x" in r.json()["modelos"] and r.json()["configurado"] is False
