"""Acesso pela rede (Tailscale, tunel): login obrigatorio fora do proprio PC e
MCP em /mcp com chave."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.main import app

ACEITA = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INICIAR = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "teste", "version": "1"}}}


@pytest.fixture
def remoto(monkeypatch, db_initialized):
    monkeypatch.setenv("APP_LOGIN_REMOTO", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _rpc(c: TestClient, url: str, corpo: dict, **headers):
    return c.post(url, content=json.dumps(corpo), headers={**ACEITA, **headers})


class TestLoginRemoto:
    def test_pela_rede_exige_login(self, remoto, admin_headers):
        c = TestClient(app, base_url="http://meu-pc.tail1234.ts.net", follow_redirects=False)
        r = c.get("/busca?uf=TO")
        assert r.status_code == 303 and r.headers["location"] == "/admin/entrar?proximo=/busca%3Fuf%3DTO"
        assert c.get("/api/busca/contar").status_code == 401
        assert c.get("/admin/entrar").status_code == 200  # a tela de login abre
        assert c.get("/api/busca/contar", headers=admin_headers).status_code != 401
        # depois do login (cookie) as paginas abrem
        r = c.post("/admin/entrar", data={"token": "token-de-teste-123456", "proximo": "/"})
        assert r.status_code == 303
        assert c.get("/").status_code == 200

    def test_no_proprio_pc_nao_pede_login(self, remoto):
        c = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000), follow_redirects=False)
        assert c.get("/").status_code == 200

    def test_host_localhost_vindo_de_fora_pede_login(self, remoto):
        # alguem na rede manda Host: localhost direto para o IP do PC
        c = TestClient(app, base_url="http://localhost:8000", client=("100.101.102.103", 50000),
                       follow_redirects=False)
        assert c.get("/").status_code == 303

    def test_ip_da_tailnet_pede_login(self, remoto):
        c = TestClient(app, base_url="http://100.64.0.7:8000", client=("100.64.0.9", 50000), follow_redirects=False)
        assert c.get("/").status_code == 303


class TestMcpRemoto:
    def test_chave_gerada_e_trocada(self, db_initialized, tmp_path):
        from app import mcp_remoto

        k = mcp_remoto.chave()
        assert k.startswith("mcp_") and len(k) > 30 and mcp_remoto.chave() == k
        assert (tmp_path / "data" / "mcp_chave.txt").read_text().strip() == k
        nova = mcp_remoto.nova_chave()
        assert nova != k and mcp_remoto.chave() == nova
        assert mcp_remoto.esconder_chave(f"POST /mcp/k/{nova} HTTP/1.1") == "POST /mcp/k/… HTTP/1.1"

    def test_mcp_com_chave_pelo_cabecalho_e_pela_url(self, remoto):
        from app import mcp_remoto
        from app.receita import jobs

        chave = mcp_remoto.chave()
        with TestClient(app, base_url="https://meu-pc.tail1234.ts.net") as c:
            assert _rpc(c, "/mcp", INICIAR).status_code == 401
            r = _rpc(c, "/mcp", INICIAR, Authorization="Bearer errada-errada-errada")
            assert r.status_code == 401
            assert any(a["acao"] == "mcp.remoto" and a["resultado"] == "negado" for a in jobs.auditoria())
            r = _rpc(c, "/mcp", INICIAR, Authorization=f"Bearer {chave}")
            assert r.status_code == 200, r.text
            assert r.json()["result"]["serverInfo"]["name"]
            r = _rpc(c, f"/mcp/k/{chave}", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                     **{"mcp-protocol-version": "2025-06-18"})
            assert r.status_code == 200, r.text
            nomes = {t["name"] for t in r.json()["result"]["tools"]}
            assert {"buscar_empresas", "exportar_busca", "ficha_cnpj"} <= nomes
            assert _rpc(c, "/mcp/k/chave-errada-qualquer", INICIAR).status_code == 401

    def test_ferramenta_admin_bloqueada_sem_permissao(self, remoto):
        from app import mcp_remoto

        chave = mcp_remoto.chave()
        with TestClient(app, base_url="https://meu-pc.tail1234.ts.net") as c:
            r = _rpc(c, "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "atualizar_base", "arguments": {}}},
                     Authorization=f"Bearer {chave}", **{"mcp-protocol-version": "2025-06-18"})
            assert r.status_code == 403 and "MCP_PERMITIR_ADMIN" in r.json()["erro"]

    def test_chamada_de_ferramenta(self, remoto, base_local):
        from app import mcp_remoto

        chave = mcp_remoto.chave()
        with TestClient(app, base_url="https://meu-pc.tail1234.ts.net") as c:
            r = _rpc(c, f"/mcp/k/{chave}", {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                            "params": {"name": "contar_empresas", "arguments": {"filtros": {"uf": ["TO"]}}}},
                     **{"mcp-protocol-version": "2025-06-18"})
            assert r.status_code == 200, r.text
            assert json.loads(r.json()["result"]["content"][0]["text"])["total"] == 3

    def test_log_de_acesso_nao_mostra_a_chave(self):
        from app.main import SemQueryString

        rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', (
            "1.2.3.4", "POST", "/mcp/k/mcp_segredo123?x=1", "1.1", 200), None)
        SemQueryString().filter(rec)
        assert "segredo" not in rec.getMessage()


def test_painel_mostra_endereco_e_chave(remoto, admin_headers):
    from app import mcp_remoto

    c = TestClient(app, base_url="https://meu-pc.tail1234.ts.net")
    html = c.get("/base", headers=admin_headers).text
    assert "https://meu-pc.tail1234.ts.net/mcp" in html and mcp_remoto.chave() in html
