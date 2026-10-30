"""Chat com IA: SQL somente-leitura, ferramentas, provedores, laco e rotas."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

CHAVES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
          "GROQ_API_KEY", "MISTRAL_API_KEY", "XAI_API_KEY", "OLLAMA_URL", "LMSTUDIO_URL", "CHAT_CUSTOM_URL",
          "CHAT_CUSTOM_API_KEY", "CHAT_CUSTOM_MODELOS", "CHAT_MODELO_PADRAO", "CHAT_PROVEDOR_PADRAO")


@pytest.fixture(autouse=True)
def _sem_chaves(monkeypatch: pytest.MonkeyPatch):
    for k in CHAVES:
        monkeypatch.delenv(k, raising=False)
    from app.chat import motor, provedores
    from app.config import get_settings

    get_settings.cache_clear()
    provedores._cache_modelos.clear()
    motor._clientes_anthropic.clear()
    motor._em_andamento.clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def cliente(db_initialized) -> TestClient:
    from app.main import app

    return TestClient(app)


def _env(monkeypatch, **valores):
    from app.config import get_settings

    for k, v in valores.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


# ---------------------------------------------------------------- SQL somente-leitura


class TestSqlBase:
    def test_select_e_agregacao(self, base_local):
        from app.chat import sql

        r = sql.consultar_base("SELECT uf, count(*) AS n FROM estabelecimentos GROUP BY uf ORDER BY uf")
        assert r["colunas"] == ["uf", "n"]
        assert {"GO", "TO"} <= {linha[0] for linha in r["linhas"]}
        assert r["truncado"] is False

    def test_limite_de_linhas(self, base_local):
        from app.chat import sql

        r = sql.consultar_base("SELECT cnpj_completo FROM estabelecimentos", limite=1)
        assert r["quantidade"] == 1 and r["truncado"] is True

    @pytest.mark.parametrize("consulta", [
        "SELECT 1; SELECT 2",
        "DELETE FROM estabelecimentos",
        "COPY estabelecimentos TO 'saida.csv'",
        "ATTACH 'outro.db'",
        "SET enable_external_access = true",
        "INSTALL httpfs",
    ])
    def test_recusa_o_que_nao_e_um_select(self, base_local, consulta):
        from app.chat import sql

        with pytest.raises(sql.ConsultaInvalida):
            sql.consultar_base(consulta)

    def test_select_nao_le_arquivos_do_computador(self, base_local, tmp_path):
        from app.chat import sql

        segredo = tmp_path / "segredo.csv"
        segredo.write_text("a\n1\n")
        with pytest.raises(sql.ConsultaInvalida, match="(?i)permission|erro"):
            sql.consultar_base(f"SELECT * FROM read_csv('{segredo}')")

    def test_esquema(self, base_local):
        from app.chat import sql

        e = sql.esquema_base()
        assert "estabelecimentos" in e["tabelas"]
        assert e["metadados"]["mes_referencia"] == "2026-10"


class TestSqlApp:
    def test_le_tabelas_do_app(self, db_initialized):
        from app.chat import sql
        from app.services import listas

        listas.criar_lista("Prospecção", "teste")
        r = sql.consultar_app("SELECT nome FROM lista_empresas")
        assert r["linhas"] == [["Prospecção"]]

    def test_chaves_dos_provedores_sao_inacessiveis(self, db_initialized):
        from app.chat import provedores, sql

        provedores.salvar("openai", api_key="sk-segredo-1234567890")
        with pytest.raises(sql.ConsultaInvalida, match="não permitida"):
            sql.consultar_app("SELECT api_key FROM chat_provedor")
        with pytest.raises(sql.ConsultaInvalida):
            sql.consultar_app("SELECT * FROM lista_empresas JOIN chat_provedor ON 1=1")
        assert "chat_provedor" not in sql.esquema_app()

    @pytest.mark.parametrize("consulta", [
        "DELETE FROM lista_empresas",
        "WITH x AS (SELECT 1) DELETE FROM lista_empresas",
        "SELECT 1; DELETE FROM lista_empresas",
        "PRAGMA table_info(lista_empresas)",
    ])
    def test_somente_leitura(self, db_initialized, consulta):
        from app.chat import sql
        from app.services import listas

        listas.criar_lista("Fica", None)
        with pytest.raises(sql.ConsultaInvalida):
            sql.consultar_app(consulta)
        assert sql.consultar_app("SELECT count(*) FROM lista_empresas")["linhas"] == [[1]]


# ---------------------------------------------------------------- ferramentas


class TestFerramentas:
    async def test_catalogo_tem_mcp_admin_e_sql(self, db_initialized):
        from app.chat import ferramentas

        nomes = {t["nome"] for t in await ferramentas.catalogo()}
        assert {"ficha_cnpj", "buscar_empresas", "exportar_busca", "historico_de_consultas", "favoritar",
                "remover_lista", "atualizar_base", "cadastrar_contato_complementar",
                "consultar_sql_base", "consultar_sql_app", "descrever_bancos"} <= nomes

    async def test_erro_vira_resultado_legivel(self, db_initialized):
        from app.chat import ferramentas

        ok, txt = await ferramentas.executar("ficha_cnpj", {"cnpj": "123"}, max_chars=1000)
        assert ok is False and txt.startswith("CNPJ inválido")
        ok, txt = await ferramentas.executar("nao_existe", {}, max_chars=1000)
        assert ok is False
        ok, txt = await ferramentas.executar("ficha_cnpj", "nao e objeto", max_chars=1000)
        assert ok is False

    async def test_resultado_grande_e_cortado(self, db_initialized):
        from app.chat import ferramentas

        ok, txt = await ferramentas.executar("descrever_filtros", {}, max_chars=200)
        assert ok and "resultado cortado" in txt

    async def test_alteracao_audita_com_origem_chat(self, db_initialized):
        from app.chat import ferramentas
        from app.receita import jobs
        from app.services import listas

        lst = listas.criar_lista("Apagar", None)
        ok, _ = await ferramentas.executar("remover_lista", {"lista_id": lst["id"]}, max_chars=1000)
        assert ok
        reg = [a for a in jobs.auditoria() if a["acao"] == "lista.remover"]
        assert reg and "origem=chat" in reg[0]["detalhe"]

    async def test_exportacao_ganha_link_de_download(self, base_local):
        from app.chat import ferramentas

        ok, txt = await ferramentas.executar("exportar_busca", {"filtros": {"uf": ["TO"]}, "formato": "csv"},
                                             max_chars=10_000)
        assert ok, txt
        dados = json.loads(txt)
        assert dados["link_download"].startswith("/chat/arquivos/chat-")


# ---------------------------------------------------------------- provedores


class TestProvedores:
    def test_sem_chave_nada_configurado(self, db_initialized):
        from app.chat import provedores

        assert provedores.configurados() == []

    def test_chave_do_env(self, db_initialized, monkeypatch):
        from app.chat import provedores

        _env(monkeypatch, ANTHROPIC_API_KEY="sk-ant-env-0000000000abcd")
        p = provedores.resolver("anthropic")
        assert p.configurado and p.origem == "env"
        assert p.modelos[0] == "claude-opus-5-5"
        pub = p.publico()
        assert "sk-ant" not in json.dumps(pub) and pub["chave_final"] == "…abcd"

    def test_cadastro_pela_tela_e_locais(self, db_initialized):
        from app.chat import provedores

        p = provedores.salvar("groq", api_key="gsk_chave_teste_123456", modelos="modelo-x, modelo-y")
        assert p.configurado and p.origem == "tela" and p.modelos == ["modelo-x", "modelo-y"]
        assert provedores.salvar("groq", api_key=None).api_key == "gsk_chave_teste_123456"  # mantem
        assert not provedores.salvar("groq", apagar_chave=True).configurado
        assert not provedores.resolver("ollama").configurado  # local so com endereco
        o = provedores.salvar("ollama", base_url="http://localhost:11434/v1/")
        assert o.configurado and o.base_url == "http://localhost:11434/v1"
        with pytest.raises(ValueError):
            provedores.salvar("personalizado", base_url="ftp://x")

    @respx.mock
    async def test_lista_modelos_do_provedor(self, db_initialized):
        from app.chat import provedores

        respx.get("https://api.openai.com/v1/models").respond(json={"data": [
            {"id": "modelo-chat"}, {"id": "text-embedding-3"}, {"id": "whisper-1"}]})
        p = provedores.salvar("openai", api_key="sk-teste-0000000000")
        assert await provedores.listar_modelos(p) == ["modelo-chat"]


def test_esquema_simplificado_para_openai():
    from app.chat.motor import _esquema_simples

    esq = {"type": "object", "title": "X", "properties": {
        "uf": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Uf"},
        "filtros": {"type": "object", "additionalProperties": True},
    }, "required": []}
    assert _esquema_simples(esq) == {"type": "object", "properties": {
        "uf": {"type": "string"}, "filtros": {"type": "object"}}, "required": []}


def test_fallback_descarta_tentativa_recusada():
    from app.chat.motor import _sem_tentativa_recusada

    blocos = [{"type": "thinking", "thinking": "", "signature": "a"}, {"type": "text", "text": "parcial"},
              {"type": "tool_use", "id": "t0", "name": "x", "input": {}}, {"type": "fallback"},
              {"type": "thinking", "thinking": "", "signature": "b"}, {"type": "text", "text": "final"}]
    assert [b.get("signature") or b.get("text") or b.get("id") for b in _sem_tentativa_recusada(blocos)] == \
        ["parcial", "b", "final"]


# ---------------------------------------------------------------- laco com provedor compativel com OpenAI


def _sse(*pedacos: dict) -> bytes:
    return b"".join(f"data: {json.dumps(p)}\n\n".encode() for p in pedacos) + b"data: [DONE]\n\n"


async def _coletar(conversa_id: int, texto: str) -> list[dict]:
    from app.chat import motor

    return [ev async for ev in motor.responder(conversa_id, texto)]


class TestLacoOpenAI:
    @respx.mock
    async def test_chama_ferramenta_e_responde(self, db_initialized):
        from app.chat import conversas, provedores
        from app.services import listas

        listas.criar_lista("Clientes Palmas", None)
        provedores.salvar("deepseek", api_key="sk-deepseek-000000000")
        corpos: list[dict] = []
        respostas = [
            _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {
                "name": "listar_listas", "arguments": ""}}]}}]},
                 {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]},
                               "finish_reason": "tool_calls"}]}),
            _sse({"choices": [{"delta": {"content": "Você tem 1 lista: "}}]},
                 {"choices": [{"delta": {"content": "Clientes Palmas."}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 50, "completion_tokens": 8}}),
        ]

        def responder(request: httpx.Request):
            corpos.append(json.loads(request.content))
            return httpx.Response(200, content=respostas[len(corpos) - 1],
                                  headers={"content-type": "text/event-stream"})

        respx.post("https://api.deepseek.com/v1/chat/completions").mock(side_effect=responder)
        conv = conversas.criar("deepseek", "deepseek-chat")
        evs = await _coletar(conv["id"], "Quais listas eu tenho?")
        tipos = [e["tipo"] for e in evs]
        assert tipos[0] == "inicio" and tipos[-1] == "fim"
        res = next(e for e in evs if e["tipo"] == "resultado")
        assert res["ok"] and "Clientes Palmas" in res["resumo"]
        assert "".join(e["delta"] for e in evs if e["tipo"] == "texto") == "Você tem 1 lista: Clientes Palmas."

        # 2a ida ao modelo leva o pedido da ferramenta e o resultado; sistema vai na frente
        hist = corpos[1]["messages"]
        assert hist[0]["role"] == "system" and "CNPJ Consulta" in hist[0]["content"]
        assert hist[2]["tool_calls"][0]["function"]["name"] == "listar_listas"
        assert hist[3]["role"] == "tool" and hist[3]["tool_call_id"] == "call_1"
        assert any(t["function"]["name"] == "consultar_sql_base" for t in corpos[0]["tools"])

        salvo = conversas.obter(conv["id"], com_historico=True)
        assert [i["tipo"] for i in salvo["exibicao"]] == ["usuario", "ferramenta", "assistente"]
        assert salvo["titulo"] == "Quais listas eu tenho?"
        assert salvo["tokens_saida"] == 8
        assert salvo["historico"][-1] == {"role": "assistant", "content": "Você tem 1 lista: Clientes Palmas."}

    @respx.mock
    async def test_chave_invalida(self, db_initialized):
        from app.chat import conversas, provedores

        provedores.salvar("openai", api_key="sk-errada-000000000")
        respx.post("https://api.openai.com/v1/chat/completions").respond(401, json={"error": {"message": "bad"}})
        conv = conversas.criar("openai", "gpt-x")
        evs = await _coletar(conv["id"], "oi")
        assert evs[-1]["tipo"] == "erro" and "inválida" in evs[-1]["mensagem"]

    async def test_provedor_nao_configurado(self, db_initialized):
        from app.chat import conversas

        conv = conversas.criar("openai", "gpt-x")
        evs = await _coletar(conv["id"], "oi")
        assert evs == [{"tipo": "erro", "mensagem": "OpenAI não está configurado. Cadastre a chave em Configurar provedores."}]


# ---------------------------------------------------------------- laco com Claude (SDK oficial, HTTP simulado)


def _sse_anthropic(blocos: list[dict], stop_reason: str, extra_delta: dict | None = None) -> bytes:
    evs: list[tuple[str, dict]] = [("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 1}}})]
    for i, b in enumerate(blocos):
        if b["type"] == "text":
            evs.append(("content_block_start", {"type": "content_block_start", "index": i,
                                                "content_block": {"type": "text", "text": ""}}))
            evs.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                                "delta": {"type": "text_delta", "text": b["text"]}}))
        elif b["type"] == "tool_use":
            evs.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}))
            evs.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(b["input"])}}))
        elif b["type"] == "thinking":
            evs.append(("content_block_start", {"type": "content_block_start", "index": i,
                                                "content_block": {"type": "thinking", "thinking": "", "signature": ""}}))
            evs.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                                "delta": {"type": "signature_delta", "signature": "assinatura"}}))
        evs.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    evs.append(("message_delta", {"type": "message_delta", "delta": {
        "stop_reason": stop_reason, "stop_sequence": None, **(extra_delta or {})}, "usage": {"output_tokens": 30}}))
    evs.append(("message_stop", {"type": "message_stop"}))
    return b"".join(f"event: {n}\ndata: {json.dumps(d)}\n\n".encode() for n, d in evs)


@pytest.fixture
def claude_falso(monkeypatch):
    """Cliente oficial da Anthropic com transporte HTTP simulado."""
    import anthropic
    import httpx2

    from app.chat import motor

    estado: dict[str, Any] = {"pedidos": [], "respostas": []}

    def handler(request: httpx2.Request) -> httpx2.Response:
        estado["pedidos"].append({"headers": dict(request.headers), "corpo": json.loads(request.content)})
        corpo = estado["respostas"].pop(0)
        return httpx2.Response(200, content=corpo, headers={"content-type": "text/event-stream"})

    cliente = anthropic.AsyncAnthropic(
        api_key="sk-ant-teste", max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
    monkeypatch.setattr(motor.AdaptadorAnthropic, "_cliente", lambda self: cliente)
    _env(monkeypatch, ANTHROPIC_API_KEY="sk-ant-teste-000000000000")
    return estado


class TestLacoClaude:
    async def test_ferramenta_em_paralelo_e_parametros(self, db_initialized, claude_falso):
        from app.chat import conversas

        claude_falso["respostas"] = [
            _sse_anthropic([{"type": "thinking"}, {"type": "text", "text": "Vou conferir."},
                            {"type": "tool_use", "id": "toolu_1", "name": "listar_listas", "input": {}},
                            {"type": "tool_use", "id": "toolu_2", "name": "listar_favoritos", "input": {}}],
                           "tool_use"),
            _sse_anthropic([{"type": "text", "text": "Nenhuma lista nem favorito."}], "end_turn"),
        ]
        conv = conversas.criar("anthropic", "claude-opus-5-5")
        evs = await _coletar(conv["id"], "O que tenho salvo?")
        assert [e["nome"] for e in evs if e["tipo"] == "resultado"] == ["listar_listas", "listar_favoritos"]
        assert any(e["tipo"] == "pensando" for e in evs)

        p1 = claude_falso["pedidos"][0]
        corpo = p1["corpo"]
        assert corpo["model"] == "claude-opus-5-5"
        assert corpo["thinking"] == {"type": "adaptive"}
        assert corpo["output_config"] == {"effort": "medium"}
        assert corpo["fallbacks"] == "default"
        assert "server-side-fallback-2026-07-01" in p1["headers"].get("anthropic-beta", "")
        assert corpo["cache_control"] == {"type": "ephemeral"}
        assert corpo["stream"] is True
        assert all(t["eager_input_streaming"] is True for t in corpo["tools"])

        # 2a ida: bloco de raciocinio devolvido intacto, resultados juntos numa mensagem
        msgs = claude_falso["pedidos"][1]["corpo"]["messages"]
        assistente = msgs[1]["content"]
        assert assistente[0] == {"type": "thinking", "thinking": "", "signature": "assinatura"}
        resultados = msgs[2]["content"]
        assert [r["tool_use_id"] for r in resultados] == ["toolu_1", "toolu_2"]
        assert all(r["type"] == "tool_result" for r in resultados)

    async def test_haiku_sem_fallback(self, db_initialized, claude_falso):
        from app.chat import conversas

        claude_falso["respostas"] = [_sse_anthropic([{"type": "text", "text": "oi"}], "end_turn")]
        conv = conversas.criar("anthropic", "claude-haiku-5-5")
        await _coletar(conv["id"], "oi")
        corpo = claude_falso["pedidos"][0]["corpo"]
        assert "fallbacks" not in corpo and corpo["thinking"] == {"type": "adaptive"}

    async def test_recusa_nao_entra_no_historico(self, db_initialized, claude_falso):
        from app.chat import conversas

        claude_falso["respostas"] = [_sse_anthropic([], "refusal")]
        conv = conversas.criar("anthropic", "claude-opus-5-5")
        evs = await _coletar(conv["id"], "algo")
        assert any(e["tipo"] == "aviso" and "recusou" in e["mensagem"] for e in evs)
        salvo = conversas.obter(conv["id"], com_historico=True)
        assert salvo["historico"] == []  # o pedido recusado nao volta na proxima mensagem
        assert [i["tipo"] for i in salvo["exibicao"]] == ["usuario", "aviso"]

    async def test_limite_de_rodadas(self, db_initialized, claude_falso, monkeypatch):
        from app.chat import conversas

        _env(monkeypatch, CHAT_MAX_RODADAS="2")
        rodada = _sse_anthropic([{"type": "tool_use", "id": "t", "name": "listar_listas", "input": {}}], "tool_use")
        final = _sse_anthropic([{"type": "text", "text": "Até aqui achei isto."}], "end_turn")
        claude_falso["respostas"] = [rodada, rodada, final]
        conv = conversas.criar("anthropic", "claude-opus-5-5")
        evs = await _coletar(conv["id"], "laço")
        assert any(e["tipo"] == "aviso" and "rodadas" in e["mensagem"] for e in evs)
        salvo = conversas.obter(conv["id"], com_historico=True)
        assert [m["role"] for m in salvo["historico"]] == ["user", "assistant", "user", "assistant", "user", "assistant"]


# ---------------------------------------------------------------- rotas


class TestRotas:
    def test_pagina_exige_admin(self, cliente):
        r = cliente.get("/chat", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/admin/entrar")
        assert cliente.get("/chat/api/conversas").status_code == 401

    def test_pagina_e_provedores_sem_vazar_chave(self, cliente, admin_headers):
        r = cliente.post("/chat/api/provedores/mistral", headers=admin_headers,
                         json={"api_key": "chave-mistral-secreta-9876"})
        assert r.status_code == 200 and r.json()["configurado"]
        r = cliente.get("/chat", headers=admin_headers)
        assert r.status_code == 200 and "Chat com IA" in r.text
        lista = cliente.get("/chat/api/provedores", headers=admin_headers)
        assert "chave-mistral-secreta" not in lista.text
        assert next(p for p in lista.json() if p["id"] == "mistral")["chave_final"] == "…9876"

    def test_conversa_ponta_a_ponta(self, cliente, admin_headers, claude_falso):
        claude_falso["respostas"] = [_sse_anthropic([{"type": "text", "text": "Olá! Posso ajudar."}], "end_turn")]
        r = cliente.post("/chat/api/conversas", headers=admin_headers, json={"provedor": "anthropic"})
        assert r.status_code == 201
        cid = r.json()["id"]
        assert r.json()["modelo"] == "claude-opus-5-5"
        with cliente.stream("POST", f"/chat/api/conversas/{cid}/mensagens", headers=admin_headers,
                            json={"texto": "oi"}) as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            evs = [json.loads(linha[5:]) for linha in resp.iter_lines() if linha.startswith("data:")]
        assert evs[-1]["tipo"] == "fim"
        c = cliente.get(f"/chat/api/conversas/{cid}", headers=admin_headers).json()
        assert c["exibicao"][-1] == {"tipo": "assistente", "texto": "Olá! Posso ajudar."}
        assert "historico" not in c
        from app.receita import jobs

        reg = [a for a in jobs.auditoria() if a["acao"] == "chat.mensagem"]
        assert reg and reg[0]["detalhe"] == f"origem=chat conversa={cid} provedor=anthropic modelo=claude-opus-5-5"

    def test_trocar_de_tipo_exige_nova_conversa(self, cliente, admin_headers, claude_falso):
        claude_falso["respostas"] = [_sse_anthropic([{"type": "text", "text": "ok"}], "end_turn")]
        cliente.post("/chat/api/provedores/groq", headers=admin_headers, json={"api_key": "gsk-000000000000"})
        cid = cliente.post("/chat/api/conversas", headers=admin_headers, json={"provedor": "anthropic"}).json()["id"]
        r = cliente.patch(f"/chat/api/conversas/{cid}", headers=admin_headers,
                          json={"provedor": "anthropic", "modelo": "claude-sonnet-5-5"})
        assert r.status_code == 200 and r.json()["modelo"] == "claude-sonnet-5-5"
        with cliente.stream("POST", f"/chat/api/conversas/{cid}/mensagens", headers=admin_headers,
                            json={"texto": "oi"}) as resp:
            list(resp.iter_lines())
        r = cliente.patch(f"/chat/api/conversas/{cid}", headers=admin_headers,
                          json={"provedor": "groq", "modelo": "x"})
        assert r.status_code == 409

    def test_download_so_de_arquivos_exportados(self, cliente, admin_headers):
        from app.receita.armazenamento import exportacoes_dir

        pasta = exportacoes_dir()
        pasta.mkdir(parents=True, exist_ok=True)
        (pasta / "chat-20261030-101010.csv").write_text("cnpj\n1\n")
        assert cliente.get("/chat/arquivos/chat-20261030-101010.csv", headers=admin_headers).status_code == 200
        assert cliente.get("/chat/arquivos/chat-20261030-101010.csv").status_code == 401
        assert cliente.get("/chat/arquivos/..%2Fapp.db", headers=admin_headers).status_code == 404
        assert cliente.get("/chat/arquivos/outro.csv", headers=admin_headers).status_code == 404

    def test_chat_desligado(self, cliente, admin_headers, monkeypatch):
        _env(monkeypatch, CHAT_HABILITADO="false")
        assert cliente.get("/chat", headers=admin_headers).status_code == 404
