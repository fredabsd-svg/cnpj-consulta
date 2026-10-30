"""Regressao do Gauntlet Loop (rodada 2, seguranca e LGPD): cada teste foi escrito por um
critico independente, reproduziu um defeito real em 811dfe5 e hoje guarda a correcao."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.main import app


def _cli(**kw) -> TestClient:
    return TestClient(app, raise_server_exceptions=bool(os.environ.get("GAUNTLET_TRACE")), **kw)


def _como_uvicorn() -> TestClient:
    """O app como o uvicorn o serve por padrao (proxy_headers=True,
    forwarded_allow_ips=127.0.0.1) e com o cliente local (127.0.0.1), que e
    exatamente o caso do app single-user: todo cliente chega de 127.0.0.1."""
    return TestClient(ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1"),
                      client=("127.0.0.1", 50000), raise_server_exceptions=False)


# ---------------------------------------------------------------- S2-1 limite de consultas x X-Forwarded-For


def test_s2_1_limite_de_consultas_contornavel_por_x_forwarded_for(db_initialized, monkeypatch):
    """O ip_de foi corrigido para ignorar X-Forwarded-For, mas o limite de consultas
    (inbound_limit._client_key) ainda usa request.client.host, que o uvicorn troca
    pelo XFF: cada pedido com XFF novo ganha cota nova."""
    from app.config import get_settings
    from app.core.inbound_limit import reset_inbound_limiter

    monkeypatch.setenv("INBOUND_RATE_LIMIT_PER_MINUTE", "3")
    get_settings.cache_clear()
    reset_inbound_limiter()
    c = _como_uvicorn()
    # controle: sem XFF o limite funciona
    sem_xff = [c.get("/empresa/123").status_code for _ in range(5)]
    assert 429 in sem_xff, sem_xff
    reset_inbound_limiter()
    com_xff = [c.get("/empresa/123", headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code for i in range(10)]
    assert 429 in com_xff, com_xff


# ---------------------------------------------------------------- S2-2 auditoria da exportacao x X-Forwarded-For


def test_s2_2_auditoria_da_exportacao_grava_ip_forjado(base_local):
    """api/busca.py audita a exportacao com request.client.host (nao ip_de): o IP
    da auditoria de exportacao com contatos e o que o cliente quiser."""
    from app.receita import jobs

    r = _como_uvicorn().get("/api/busca/exportar.csv",
                            params={"uf": "TO", "colunas": ["cnpj", "email"], "finalidade": "prospeccao comercial"},
                            headers={"X-Forwarded-For": "203.0.113.66"})
    assert r.status_code == 200, (r.status_code, r.text[:200])
    ips = [i["ip"] for i in jobs.auditoria() if i["acao"] == "busca.exportar"]
    assert ips and "203.0.113.66" not in ips, ips


# ---------------------------------------------------------------- S2-3 exportacao com contatos por GET de outro site


def test_s2_3_exportacao_com_contatos_disparada_por_outro_site(base_local):
    """A exportacao (com contatos + finalidade + auditoria) e um GET: a protecao de
    outra origem so cobre POST/PATCH/DELETE. Um <img src> num site qualquer dispara a
    exportacao e grava na auditoria uma 'finalidade' escolhida pelo atacante."""
    from app.receita import jobs

    h = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image",
         "host": "127.0.0.1:8000"}
    r = _cli().get("/api/busca/exportar.csv",
                   params={"colunas": ["cnpj", "email", "telefone_1", "socios"],
                           "finalidade": "consentimento dado pelo titular"},
                   headers=h)
    registros = [i for i in jobs.auditoria() if i["acao"] == "busca.exportar"]
    assert r.status_code in (400, 403) and not registros, (r.status_code, registros)


# ---------------------------------------------------------------- S2-4 Bearer nas paginas administrativas


def test_s2_4_bearer_errado_nas_paginas_admin_sem_limite_nem_auditoria(db_initialized, admin_headers):
    """O limite/auditoria do Bearer so vale em exigir_admin_api. As paginas admin
    (exigir_admin_pagina -> eh_admin) aceitam o mesmo Bearer sem contar a tentativa:
    oraculo de forca bruta ilimitado e que funciona mesmo com o IP ja bloqueado."""
    from app.receita import jobs

    c = _cli(follow_redirects=False)
    codigos = [c.get("/base", headers={"Authorization": f"Bearer chute-{i}"}).status_code for i in range(20)]
    negados = [i for i in jobs.auditoria() if i["resultado"] == "negado"]
    # depois de 20 erros o IP deveria estar bloqueado: o token certo nao pode passar pela pagina
    certo = c.get("/base", headers=admin_headers).status_code
    assert negados and certo == 429, (codigos[:6], len(negados), certo)


# ---------------------------------------------------------------- S2-5 trava de bind x argumentos do uvicorn


def _subir(tmp_path, args: list[str], **env_extra):
    """Sobe `python -m uvicorn app.main:app <args>` e diz se respondeu."""
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
    env = {k: v for k, v in os.environ.items() if k not in ("APP_HOST", "UVICORN_HOST")}
    env.update(PYTHONPATH=str(raiz), DATABASE_URL=f"sqlite:///{tmp_path / 'app.db'}",
               RECEITA_DATA_DIR=str(tmp_path / "receita"), **env_extra)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", *args, "--port", str(porta)],
                            cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    no_ar = False
    for _ in range(60):
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{porta}/api/health", timeout=1):
                no_ar = True
                break
        except OSError:
            time.sleep(0.25)
    proc.terminate()
    saida = proc.communicate(timeout=15)[0].decode(errors="replace")
    return no_ar, saida


def test_s2_5_host_repetido_engana_a_trava_de_bind(tmp_path):
    """host_de_execucao pega o PRIMEIRO --host; o click/uvicorn usa o ULTIMO.
    `--host 127.0.0.1 --host 0.0.0.0` passa pela trava e escuta em todas as interfaces."""
    no_ar, saida = _subir(tmp_path, ["--host", "127.0.0.1", "--host", "0.0.0.0"],
                          APP_ENV="production", APP_DEBUG="false")
    assert not no_ar, "producao subiu em 0.0.0.0:\n" + saida[-400:]


# ---------------------------------------------------------------- S2-6 historico gravado antes da correcao


def test_s2_6_historico_antigo_com_dado_de_socio_continua_exposto(db_initialized):
    """A correcao so limpa na GRAVACAO (registrar_recente). Linhas gravadas pela versao
    anterior (que so tirava socio_nome/socio_documento/excluir_socio_nome) continuam
    saindo cruas em /api/buscas-recentes e na tela /busca. A lista de exportacoes, ao
    contrario, e limpa na LEITURA."""
    import json

    from app.db import get_session_factory
    from app.models.base_cnpj import BuscaRecente

    # exatamente o que 145e67c gravava para estes filtros
    antigo = {"uf": ["TO"], "excluir_socio_documento": "***123456**",
              "arvore": json.dumps({"op": "E", "itens": [{"campo": "socio_nome", "valor": "MARIA DA SILVA"}]})}
    with get_session_factory()() as s:
        s.add(BuscaRecente(filtros=antigo, resumo="UF: TO", total=1))
        s.commit()
    api = json.dumps(_cli().get("/api/buscas-recentes").json(), ensure_ascii=False)
    tela = _cli().get("/busca").text
    # "123456" sozinho aparece no exemplo do campo CNPJ ("ex.: 12345678"): procura o documento mascarado
    vaza = [x for x in ("MARIA", "***123456**") if x in api or x in tela]
    assert not vaza, (vaza, api[:300])


# ---------------------------------------------------------------- S2-7 CPF do titular no historico de consultas


def test_s2_7_cpf_do_empresario_individual_no_historico_de_consultas(base_local):
    """web_research tira o CPF do titular da razao social de EI/MEI ('JOSE PEREIRA
    12345678900') por ser dado pessoal, mas o historico de consultas grava e lista a
    razao social com o CPF inteiro (/api/history e /historico)."""
    import respx

    c = _cli()
    with respx.mock(assert_all_called=False):
        assert c.get("/api/companies/11222333000181").status_code == 200
    hist = c.get("/api/history").text + c.get("/historico").text
    assert "12345678900" not in hist, hist[hist.find("JOSE"):hist.find("JOSE") + 60]


# ---------------------------------------------------------------- S2-8 contatos em massa pela API de busca


def test_s2_8_api_de_busca_entrega_contatos_sem_finalidade_nem_auditoria(base_local):
    """COLUNAS marca telefone_1/telefone_2/email como restritas (exportar exige finalidade
    e audita), mas COLUNAS_TELA as inclui: /api/busca?colunas=email&por_pagina=100&pagina=N
    pagina a base inteira de contatos sem finalidade e sem registro na auditoria."""
    from app.receita import jobs

    c = _cli()
    exp = c.get("/api/busca/exportar.csv", params={"uf": "TO", "colunas": ["cnpj", "email"]})
    assert exp.status_code == 400  # a exportacao exige finalidade
    r = c.get("/api/busca", params={"uf": "TO", "colunas": ["cnpj", "email", "telefone_1"], "por_pagina": 100})
    com_email = [i for i in r.json().get("itens", []) if i.get("email") or i.get("telefone_1")]
    auditado = [i for i in jobs.auditoria() if i["acao"].startswith("busca")]
    assert not com_email or auditado, (len(com_email), com_email[:2])
