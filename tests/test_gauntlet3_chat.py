"""Regressao do Gauntlet Loop (rodada 3, chat com IA e MCP): cada teste foi escrito por um
critico independente, reproduziu um defeito real em b5704d8 e hoje guarda a correcao."""

from __future__ import annotations

import asyncio
import json

import httpx
import respx

from tests.test_chat import _coletar, _env, _sem_chaves, _sse  # noqa: F401


def _resp(corpo: bytes) -> httpx.Response:
    return httpx.Response(200, content=corpo, headers={"content-type": "text/event-stream"})


async def _coletar_com_prazo(conversa_id: int, texto: str, prazo: float = 30.0) -> list[dict]:
    return await asyncio.wait_for(_coletar(conversa_id, texto), prazo)


# ---------------------------------------------------------------- C3-1 Gemini 3: thought_signature descartada


@respx.mock
async def test_c3_1_gemini_thought_signature_da_chamada_some_do_historico(db_initialized):
    """Gemini 3 (OpenAI compat) manda `extra_content.google.thought_signature` em cada
    tool_call e recusa (HTTP 400) a ida seguinte se a assinatura nao voltar. O adaptador
    remonta tool_calls so com id/nome/argumentos: toda resposta que usa ferramenta quebra."""
    from app.chat import conversas, provedores

    provedores.salvar("gemini", api_key="AIza-chave-de-teste-0000")
    corpos: list[dict] = []

    def responder(request: httpx.Request):
        corpos.append(json.loads(request.content))
        if len(corpos) == 1:
            return _resp(_sse({"choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {
                "role": "assistant", "tool_calls": [{
                    "id": "function-call-1", "type": "function",
                    "function": {"name": "listar_listas", "arguments": "{}"},
                    "extra_content": {"google": {"thought_signature": "SIG-abc"}}}]}}]}))
        chamadas = [tc for m in corpos[-1]["messages"] if m.get("role") == "assistant"
                    for tc in m.get("tool_calls") or []]
        if any(((tc.get("extra_content") or {}).get("google") or {}).get("thought_signature") != "SIG-abc"
               for tc in chamadas):
            return httpx.Response(400, json={"error": {"message": "Function call is missing a thought_signature "
                                                                  "in functionCall parts."}})
        return _resp(_sse({"choices": [{"index": 0, "delta": {"content": "Nenhuma lista."},
                                        "finish_reason": "stop"}]}))

    respx.post("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions").mock(
        side_effect=responder)
    conv = conversas.criar("gemini", "gemini-3-pro-preview")
    evs = await _coletar_com_prazo(conv["id"], "quais listas eu tenho?")
    erros = [e["mensagem"] for e in evs if e["tipo"] == "erro"]
    assert not erros, erros


# ---------------------------------------------------------------- C3-2 DeepSeek: reasoning_content descartado


@respx.mock
async def test_c3_2_deepseek_reasoning_content_nao_volta_na_mesma_rodada(db_initialized):
    """deepseek-reasoner (modo de raciocinio com ferramentas): dentro do mesmo turno o
    `reasoning_content` da mensagem do assistente tem de voltar na ida seguinte, senao a API
    responde 400. O adaptador so marca "pensando" e joga o raciocinio fora."""
    from app.chat import conversas, provedores

    provedores.salvar("deepseek", api_key="sk-deepseek-000000000")
    corpos: list[dict] = []

    def responder(request: httpx.Request):
        corpos.append(json.loads(request.content))
        if len(corpos) == 1:
            return _resp(_sse(
                {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "Vou listar."}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                                    "function": {"name": "listar_listas",
                                                                                 "arguments": "{}"}}]},
                              "finish_reason": "tool_calls"}]}))
        msgs = corpos[-1]["messages"]
        ultimo_usuario = max(i for i, m in enumerate(msgs) if m["role"] == "user")
        for i, m in enumerate(msgs[ultimo_usuario:], ultimo_usuario):
            if m["role"] == "assistant" and "reasoning_content" not in m:
                return httpx.Response(400, json={"error": {
                    "message": f"Missing `reasoning_content` field in the assistant message at message index {i}."}})
        return _resp(_sse({"choices": [{"index": 0, "delta": {"content": "Nenhuma."}, "finish_reason": "stop"}]}))

    respx.post("https://api.deepseek.com/v1/chat/completions").mock(side_effect=responder)
    conv = conversas.criar("deepseek", "deepseek-reasoner")
    evs = await _coletar_com_prazo(conv["id"], "quais listas eu tenho?")
    erros = [e["mensagem"] for e in evs if e["tipo"] == "erro"]
    assert not erros, erros


# ---------------------------------------------------------------- C3-3 argumentos invalidos ficam no historico


@respx.mock
async def test_c3_3_argumentos_json_invalidos_ficam_no_historico_e_travam_o_ollama(db_initialized):
    """Modelo local gera argumentos com JSON quebrado: o adaptador devolve erro para a
    ferramenta, mas grava `arguments` (texto invalido) no historico. O Ollama faz
    json.Unmarshal dos argumentos de cada mensagem do assistente e responde 400 "invalid tool
    call arguments": a ida seguinte e TODAS as mensagens futuras da conversa falham."""
    from app.chat import conversas, provedores

    provedores.salvar("ollama", base_url="http://localhost:11434/v1", modelos="qwen3:8b")
    corpos: list[dict] = []

    def responder(request: httpx.Request):
        corpo = json.loads(request.content)
        corpos.append(corpo)
        for m in corpo["messages"]:
            for tc in m.get("tool_calls") or []:
                try:
                    json.loads(tc["function"]["arguments"])
                except ValueError:
                    return httpx.Response(400, json={"error": {"message": "invalid tool call arguments"}})
        if len(corpos) == 1:
            return _resp(_sse({"choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {"tool_calls": [{
                "index": 0, "id": "call_x", "type": "function",
                "function": {"name": "ver_lista", "arguments": '{"lista_id": 1'}}]}}]}))
        return _resp(_sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    respx.post("http://localhost:11434/v1/chat/completions").mock(side_effect=responder)
    conv = conversas.criar("ollama", "qwen3:8b")
    evs1 = await _coletar_com_prazo(conv["id"], "abre a lista 1")
    evs2 = await _coletar_com_prazo(conv["id"], "e agora, tudo bem?")
    erros = [e["mensagem"] for e in evs1 + evs2 if e["tipo"] == "erro"]
    assert not erros, erros


# ---------------------------------------------------------------- C3-4 banco antigo continua reaproveitando ids


def test_c3_4_banco_criado_antes_da_correcao_continua_reaproveitando_id(tmp_path):
    """`sqlite_autoincrement` so vale para tabela NOVA: init_database() usa create_all,
    que nao altera a tabela chat_conversa ja existente (criada pelas versoes anteriores, sem
    AUTOINCREMENT). Em toda instalacao atualizada o id da conversa apagada volta a ser usado."""
    from sqlalchemy.schema import CreateTable

    from app.chat import conversas
    from app.db import get_engine, init_database
    from app.models.chat import ChatConversa

    ddl_antiga = str(CreateTable(ChatConversa.__table__).compile(get_engine())).replace(" AUTOINCREMENT", "")
    with get_engine().begin() as c:
        c.exec_driver_sql(ddl_antiga)  # tabela como as versoes ate 811dfe5 criavam
    init_database()  # inicializacao da versao nova
    velha = conversas.criar("anthropic", "claude-opus-5-5")
    assert conversas.remover(velha["id"])
    nova = conversas.criar("anthropic", "claude-opus-5-5")
    assert nova["id"] != velha["id"], f"id {velha['id']} reaproveitado pela conversa nova"


# ---------------------------------------------------------------- C3-5 alteracoes pelo chat sem auditoria


async def test_c3_5_alteracoes_feitas_pelo_chat_nao_gravam_auditoria(db_initialized):
    """CHAT_IA.md: "Alteracoes (listas, favoritos, ...) so por ferramentas do app, que ...
    gravam auditoria com origem=chat". So remover_lista/exportar/admin auditam: remover_da_lista,
    remover_busca_salva (destrutiva), editar_lista, favoritar, criar/adicionar nao deixam rastro."""
    from app.chat import ferramentas
    from app.receita import jobs
    from app.services import listas

    lst = listas.criar_lista("Clientes", "descricao")
    listas.adicionar(lst["id"], [{"cnpj": "19131243000197"}])
    busca = listas.salvar_busca("Contabilidades TO", {"uf": ["TO"]})
    chamadas = [
        ("remover_da_lista", {"lista_id": lst["id"], "cnpjs": ["19131243000197"]}),
        ("remover_busca_salva", {"busca_id": busca["id"]}),
        ("editar_lista", {"lista_id": lst["id"], "nome": "Ex-clientes", "descricao": ""}),
        ("favoritar", {"cnpj": "19131243000197"}),
    ]
    for nome, entrada in chamadas:
        ok, txt = await ferramentas.executar(nome, entrada, max_chars=10_000)
        assert ok, (nome, txt)
    auditadas = [a for a in jobs.auditoria() if "origem=chat" in (a["detalhe"] or "")]
    assert len(auditadas) >= len(chamadas), (
        f"{len(chamadas)} alteracoes feitas pelo chat, {len(auditadas)} na auditoria: {auditadas}")


# ---------------------------------------------------------------- C3-6 remover_da_lista com CNPJ repetido


async def test_c3_6_remover_da_lista_diz_que_cnpj_removido_nao_estava_na_lista(db_initialized):
    """O mesmo CNPJ duas vezes (ex.: com e sem mascara): sai na 1a, e a 2a cai em
    `nao_estavam_na_lista`. A resposta afirma ao mesmo tempo que foi removido e que nao estava."""
    from app.chat import ferramentas
    from app.services import listas

    lst = listas.criar_lista("Clientes", None)
    listas.adicionar(lst["id"], [{"cnpj": "19131243000197"}])
    ok, txt = await ferramentas.executar("remover_da_lista", {
        "lista_id": lst["id"], "cnpjs": ["19.131.243/0001-97", "19131243000197"]}, max_chars=10_000)
    assert ok, txt
    r = json.loads(txt)
    assert not set(r["removidos"]) & set(r["nao_estavam_na_lista"]), r


# ---------------------------------------------------------------- C3-7 Azure em cognitiveservices.azure.com


@respx.mock
async def test_c3_7_azure_em_cognitiveservices_nao_recebe_cabecalho_api_key(db_initialized):
    """O cabecalho `api-key` do Azure OpenAI so e mandado se o endereco contem
    `.openai.azure.com`. Recursos Azure AI Services/Foundry usam
    `RECURSO.cognitiveservices.azure.com/openai/deployments/...?api-version=` com a mesma
    autenticacao por `api-key`: o pedido sai so com `Authorization: Bearer` e da 401."""
    from app.chat import conversas, provedores

    base = "https://recurso.cognitiveservices.azure.com/openai/deployments/gpt?api-version=2024-10-21"
    provedores.salvar("personalizado", base_url=base, api_key="azure-chave-000000000", modelos="gpt")
    pedidos: list[httpx.Request] = []

    def responder(request: httpx.Request):
        pedidos.append(request)
        if request.headers.get("api-key") != "azure-chave-000000000":
            return httpx.Response(401, json={"error": {"code": "401", "message": "Access denied due to invalid "
                                                       "subscription key or wrong API endpoint."}})
        return _resp(_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    respx.route(host="recurso.cognitiveservices.azure.com").mock(side_effect=responder)
    conv = conversas.criar("personalizado", "gpt")
    evs = await _coletar_com_prazo(conv["id"], "oi")
    assert pedidos
    assert pedidos[0].headers.get("api-key") == "azure-chave-000000000", [e for e in evs if e["tipo"] == "erro"]


# ---------------------------------------------------------------- C3-8 trocar para Mistral no meio da conversa


@respx.mock
async def test_c3_8_trocar_para_mistral_no_meio_da_conversa_quebra_pelo_id_da_chamada(db_initialized):
    """CHAT_IA.md: "O modelo pode ser trocado no meio da conversa ... entre os compativeis
    com OpenAI". A Mistral exige tool_call id com 9 caracteres [a-zA-Z0-9]; o historico guarda o
    id do provedor anterior (DeepSeek: "call_00_...") ou o gerado pelo app ("call_" + 12 hex) e
    e reenviado sem conversao: toda mensagem depois da troca da 400."""
    import re

    from app.chat import conversas, provedores

    provedores.salvar("deepseek", api_key="sk-deepseek-000000000")
    provedores.salvar("mistral", api_key="mistral-chave-0000000")
    n = {"deepseek": 0}

    def deepseek(request: httpx.Request):
        n["deepseek"] += 1
        if n["deepseek"] == 1:
            return _resp(_sse({"choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {"tool_calls": [{
                "index": 0, "id": "call_00_8Hk2mQ", "type": "function",
                "function": {"name": "listar_listas", "arguments": "{}"}}]}}]}))
        return _resp(_sse({"choices": [{"index": 0, "delta": {"content": "Nenhuma."}, "finish_reason": "stop"}]}))

    def mistral(request: httpx.Request):
        for m in json.loads(request.content)["messages"]:
            ids = [tc["id"] for tc in m.get("tool_calls") or []] + ([m["tool_call_id"]] if "tool_call_id" in m else [])
            for i in ids:
                if not re.fullmatch(r"[a-zA-Z0-9]{9}", i):
                    return httpx.Response(400, json={"object": "error", "message": f"Tool call id was {i} but "
                                                     "must be a-z, A-Z, 0-9, with a length of 9."})
        return _resp(_sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    respx.post("https://api.deepseek.com/v1/chat/completions").mock(side_effect=deepseek)
    respx.post("https://api.mistral.ai/v1/chat/completions").mock(side_effect=mistral)
    conv = conversas.criar("deepseek", "deepseek-chat")
    await _coletar_com_prazo(conv["id"], "quais listas eu tenho?")
    conversas.trocar_modelo(conv["id"], "mistral", "mistral-large-latest")  # o mesmo que o PATCH faz
    evs = await _coletar_com_prazo(conv["id"], "e favoritos?")
    erros = [e["mensagem"] for e in evs if e["tipo"] == "erro"]
    assert not erros, erros


def test_c3_4_migracao_com_indice_e_dados(tmp_path, monkeypatch):
    """Tabela criada pela versao anterior (sem AUTOINCREMENT, com indice e dados) e
    migrada na inicializacao sem perder conversas nem colidir o nome do indice."""
    import sqlite3

    import app.db as db
    from app.config import get_settings

    caminho = tmp_path / "m.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{caminho}")
    get_settings.cache_clear()
    db._engine = None
    db._SessionLocal = None
    db.init_database()
    con = sqlite3.connect(caminho)
    ddl = con.execute("SELECT sql FROM sqlite_master WHERE name = 'chat_conversa'").fetchone()[0]
    con.execute("DROP TABLE chat_conversa")
    con.execute(ddl.replace(" AUTOINCREMENT", ""))
    con.execute("CREATE INDEX ix_chat_conversa_atualizado_em ON chat_conversa (atualizado_em)")
    con.execute("INSERT INTO chat_conversa (titulo, provedor, modelo, historico, exibicao, tokens_entrada, "
                "tokens_saida, criado_em, atualizado_em) VALUES ('x', 'a', 'm', '[]', '[]', 0, 0, "
                "'2026-01-01', '2026-01-01')")
    con.commit()
    con.close()
    db._engine = None
    db._SessionLocal = None
    db.init_database()
    con = sqlite3.connect(caminho)
    assert "AUTOINCREMENT" in con.execute("SELECT sql FROM sqlite_master WHERE name = 'chat_conversa'").fetchone()[0]
    assert con.execute("SELECT titulo FROM chat_conversa").fetchall() == [("x",)]
    assert con.execute("SELECT count(*) FROM sqlite_master WHERE name = 'ix_chat_conversa_atualizado_em'").fetchone()[0] == 1
    con.close()
