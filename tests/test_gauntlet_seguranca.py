"""Regressao do Gauntlet Loop (rodada 1, seguranca): cada teste afirma o
comportamento correto; todos reproduziram um defeito real antes da correcao."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

CNPJ = "12345678000195"


def _cli() -> TestClient:
    # GAUNTLET_TRACE=1 mostra a excecao original em vez do 500
    import os

    return TestClient(app, raise_server_exceptions=bool(os.environ.get("GAUNTLET_TRACE")))


# ---------------------------------------------------------------- B6: 500 na busca


@pytest.mark.parametrize("valor", ["5000", "nan"])
def test_idade_min_absurda_nao_gera_500_api(base_local, valor):
    r = _cli().get("/api/busca", params={"idade_min": valor})
    assert r.status_code == 400, (r.status_code, r.text[:200])


@pytest.mark.parametrize("valor", ["5000", "nan"])
def test_idade_min_absurda_nao_gera_500_pagina(base_local, valor):
    r = _cli().get("/busca", params={"idade_min": valor})
    assert r.status_code == 400, r.status_code


def test_socio_multiempresas_nan_nao_gera_500(base_local):
    r = _cli().get("/api/busca", params={"socio_multiempresas": "nan"})
    assert r.status_code == 400, (r.status_code, r.text[:200])


def test_arvore_itens_nao_lista_nao_gera_500(base_local):
    r = _cli().get("/api/busca", params={"arvore": json.dumps({"op": "E", "itens": 5})})
    assert r.status_code == 400, (r.status_code, r.text[:200])


def test_arvore_itens_nao_lista_ao_salvar_busca(db_initialized):
    r = _cli().post("/api/buscas-salvas", json={"nome": "x", "filtros": {"arvore": json.dumps({"itens": 5})}})
    assert r.status_code == 400, (r.status_code, r.text[:200])


def test_municipio_digito_unicode_nao_gera_500(base_local):
    r = _cli().get("/busca", params={"municipio": "²"})
    assert r.status_code == 400, r.status_code


# ---------------------------------------------------------------- B6: ids inteiros enormes

GRANDE = "99999999999999999999"  # > 2**63: SQLite levanta OverflowError


@pytest.mark.parametrize("metodo,url", [
    ("get", f"/api/listas/{GRANDE}"),
    ("get", f"/listas/{GRANDE}"),
    ("get", f"/api/exportacoes/{GRANDE}/arquivo"),
    ("get", f"/busca?salva={GRANDE}"),
    ("delete", f"/api/buscas-salvas/{GRANDE}"),
    ("get", f"/chat/api/conversas/{GRANDE}"),
    ("get", f"/api/admin/base/job/{GRANDE}"),
])
def test_id_enorme_nao_gera_500(db_initialized, admin_headers, metodo, url):
    r = getattr(_cli(), metodo)(url, headers=admin_headers)
    assert r.status_code in (404, 422), (metodo, url, r.status_code)


# ---------------------------------------------------------------- B6: cookie de sessao


def test_cookie_admin_nao_ascii_nao_gera_500(db_initialized):
    r = _cli().get("/base", headers=[(b"cookie", "cnpj_admin=1.2.é".encode("latin-1"))], follow_redirects=False)
    assert r.status_code == 303, r.status_code


def test_cookie_admin_nao_ascii_api(db_initialized):
    r = _cli().get("/api/admin/base/status", headers=[(b"cookie", "cnpj_admin=1.2.é".encode("latin-1"))])
    assert r.status_code == 401, r.status_code


# ---------------------------------------------------------------- B6: contato com URL invalida


def test_contato_api_url_ipv6_quebrada_nao_gera_500(db_initialized, admin_headers):
    r = _cli().post(f"/api/admin/contatos/{CNPJ}", headers=admin_headers,
                    json={"tipo": "site", "valor": "http://[empresa.com.br", "fonte": "site oficial"})
    assert r.status_code == 400, (r.status_code, r.text[:200])


def test_contato_api_url_fonte_quebrada_nao_gera_500(db_initialized, admin_headers):
    r = _cli().post(f"/api/admin/contatos/{CNPJ}", headers=admin_headers,
                    json={"tipo": "email", "valor": "a@b.com.br", "fonte": "site oficial",
                          "url_fonte": "http://[x"})
    assert r.status_code == 400, (r.status_code, r.text[:200])


# ---------------------------------------------------------------- LGPD: historico de buscas


def test_historico_nao_guarda_nome_de_socio_vindo_da_arvore(base_local):
    c = _cli()
    arvore = json.dumps({"op": "E", "itens": [{"campo": "socio_nome", "valor": "MARIA DA SILVA"}]})
    assert c.get("/api/busca", params={"arvore": arvore}).status_code == 200
    gravado = json.dumps(c.get("/api/buscas-recentes").json(), ensure_ascii=False)
    assert "MARIA" not in gravado, gravado


def test_historico_nao_guarda_documento_de_socio_excluido(base_local):
    c = _cli()
    assert c.get("/api/busca", params={"uf": "TO", "excluir_socio_documento": "***123456**"}).status_code == 200
    gravado = json.dumps(c.get("/api/buscas-recentes").json(), ensure_ascii=False)
    assert "123456" not in gravado, gravado


# ---------------------------------------------------------------- LGPD: colunas restritas sem finalidade


def test_api_busca_nao_entrega_nomes_de_socios_sem_finalidade(base_local):
    r = _cli().get("/api/busca", params={"uf": "TO", "colunas": ["cnpj", "socios"]})
    nomes = json.dumps(r.json(), ensure_ascii=False)
    # a exportacao exige finalidade para a mesma coluna; a API de busca nao
    assert r.status_code == 400 or "MARIA DA SILVA" not in nomes, nomes[:300]


# ---------------------------------------------------------------- acesso: DNS rebinding


def test_host_estranho_recusado_dns_rebinding(base_local):
    """Pagina em http://atacante.example:8000 que re-resolve para 127.0.0.1: o
    navegador manda Host/Origin do atacante e Sec-Fetch-Site: same-origin."""
    c = _cli()
    h = {"host": "atacante.example:8000", "origin": "http://atacante.example:8000", "sec-fetch-site": "same-origin"}
    r = c.post("/api/listas", json={"nome": "plantada"}, headers=h)
    assert r.status_code in (400, 403, 421), r.status_code
    r = c.get("/api/busca", params={"uf": "TO", "colunas": ["cnpj", "email", "telefone_1"]}, headers=h)
    assert r.status_code in (400, 403, 421), (r.status_code, r.text[:200])


# ---------------------------------------------------------------- acesso: forca bruta no Bearer


def test_bearer_errado_e_limitado_e_auditado(db_initialized, admin_headers):
    c = _cli()
    codigos = [c.get("/api/admin/base/status", headers={"Authorization": f"Bearer chute-{i}"}).status_code
               for i in range(20)]
    # mesma regra do formulario de login: 5 erros na janela -> bloqueia
    assert 429 in codigos, codigos
    # O IP fica bloqueado inclusive para o token certo (como no formulario): le a auditoria direto.
    from app.receita import jobs

    itens = jobs.auditoria()
    assert any(i["resultado"] == "negado" for i in itens), itens


# ---------------------------------------------------------------- bind nao-loopback em producao


def _uvicorn(tmp_path, host: str, **env_extra):
    """Sobe o app como o start.sh sobe (python -m uvicorn app.main:app --host ...)."""
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[1]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k != "APP_HOST"}
    env.update(PYTHONPATH=str(raiz), DATABASE_URL=f"sqlite:///{tmp_path / 'app.db'}",
               RECEITA_DATA_DIR=str(tmp_path / "receita"), **env_extra)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(porta)],
                            cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{porta}"
    for _ in range(80):
        if proc.poll() is not None:
            return proc, None
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1):
                return proc, url
        except OSError:
            time.sleep(0.25)
    return proc, None


def _parar(proc) -> str:
    proc.terminate()
    return proc.communicate(timeout=10)[0].decode(errors="replace")


def test_producao_recusa_uvicorn_em_0000_mesmo_sem_app_host(tmp_path):
    """start.sh/start.ps1 sobem com `uvicorn --host ...`. A trava so olha
    APP_HOST (settings), nao o socket real: `--host 0.0.0.0` passa."""
    proc, url = _uvicorn(tmp_path, "0.0.0.0", APP_ENV="production", APP_DEBUG="false")
    saida = _parar(proc)
    assert url is None, "servidor de producao aceitou bind em 0.0.0.0:\n" + saida[-600:]


# ---------------------------------------------------------------- forca bruta: X-Forwarded-For


def test_limite_de_login_nao_contornavel_por_x_forwarded_for(tmp_path):
    """uvicorn confia em X-Forwarded-For vindo de 127.0.0.1 (padrao) e todo
    cliente local E 127.0.0.1: cada tentativa com um XFF novo vira um 'IP' novo."""
    import httpx

    proc, url = _uvicorn(tmp_path, "127.0.0.1", ADMIN_TOKEN="token-de-teste-123456")
    try:
        assert url, _parar(proc)
        codigos = [httpx.post(f"{url}/admin/entrar", data={"token": f"chute-{i}"},
                              headers={"X-Forwarded-For": f"10.9.{i}.1"}).status_code for i in range(8)]
    finally:
        _parar(proc)
    # O IP local fica bloqueado (inclusive para o token certo): le a auditoria direto do banco.
    import sqlite3

    with sqlite3.connect(tmp_path / "app.db") as conn:
        ips = {r[0] for r in conn.execute("SELECT ip FROM auditoria_admin WHERE acao = 'admin.login'")}
    # 5 erros na janela deveriam bloquear (429) e a auditoria nao deveria gravar IP forjado
    assert 429 in codigos and not any(ip.startswith("10.9.") for ip in ips), (codigos, sorted(ips))


# ---------------------------------------------------------------- chat: links do markdown


def test_chat_link_interno_nao_vira_externo_com_barra_invertida():
    """linkSeguro trata '/\\\\host' como link interno (mesma aba, sem noopener),
    mas o navegador resolve '/\\\\evil.example' como http://evil.example/."""
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node indisponivel")
    js = (Path(__file__).resolve().parents[1] / "app/static/js/chat.js").read_text(encoding="utf-8")
    fn = re.search(r"function linkSeguro\(url\) \{.*?\n    \}", js, re.S).group(0)
    prog = fn + r"""
const s = linkSeguro("/\\evil.example/login");
const destino = s ? new URL(s.href, "http://127.0.0.1:8000/chat").host : null;
console.log(JSON.stringify({s, destino}));
"""
    out = json.loads(subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True).stdout)
    assert out["s"] is None or out["destino"] == "127.0.0.1:8000", out
