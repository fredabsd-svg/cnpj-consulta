"""Regressao do Gauntlet Loop (rodada 1, chat com IA): cada teste reproduziu um
defeito real e hoje guarda a correcao."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.test_chat import _coletar, _env, _sem_chaves, _sse, _sse_anthropic, claude_falso  # noqa: F401

RAIZ = Path(__file__).resolve().parents[1]


def _ok_sse(*pedacos: dict) -> httpx.Response:
    return httpx.Response(200, content=_sse(*pedacos), headers={"content-type": "text/event-stream"})


# ---------------------------------------------------------------- C1 concorrencia


@respx.mock
async def test_c1_duas_mensagens_simultaneas_na_mesma_conversa(db_initialized):
    """A trava _em_andamento e checada antes de varios awaits e so marcada depois:
    duas mensagens ao mesmo tempo passam as duas, e a ultima a gravar apaga a outra."""
    from app.chat import conversas, provedores

    provedores.salvar("deepseek", api_key="sk-deepseek-000000000")

    async def responder(request: httpx.Request):
        await asyncio.sleep(0.05)
        return _ok_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})

    respx.post("https://api.deepseek.com/v1/chat/completions").mock(side_effect=responder)
    conv = conversas.criar("deepseek", "deepseek-chat")
    a, b = await asyncio.gather(_coletar(conv["id"], "primeira"), _coletar(conv["id"], "segunda"))
    salvo = conversas.obter(conv["id"], com_historico=True)
    usuarios = [m["content"] for m in salvo["historico"] if m["role"] == "user"]
    recusadas = [evs for evs in (a, b) if evs and evs[-1]["tipo"] == "erro"]
    # Ou uma das duas e recusada ("Ja ha uma resposta em andamento"), ou as duas ficam no historico.
    assert len(recusadas) == 1 or sorted(usuarios) == ["primeira", "segunda"], (
        f"as duas rodaram e o historico salvo ficou so com {usuarios}")


# ---------------------------------------------------------------- C2 tool_calls sem index (Gemini)


@respx.mock
async def test_c2_tool_calls_paralelas_sem_index_sao_fundidas(db_initialized):
    """Provedores que mandam cada tool_call completa num pedaco proprio, sem `index`
    (compatibilidade OpenAI do Gemini), viram UMA chamada com nome e argumentos colados."""
    from app.chat import conversas, provedores

    provedores.salvar("gemini", api_key="gem-chave-00000000000")
    corpos: list[dict] = []
    respostas = [
        _sse({"choices": [{"delta": {"role": "assistant", "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "listar_listas", "arguments": "{}"}}]}}]},
             {"choices": [{"delta": {"tool_calls": [
                 {"id": "call_b", "type": "function", "function": {"name": "listar_favoritos", "arguments": "{}"}}]},
                 "finish_reason": "tool_calls"}]}),
        _sse({"choices": [{"delta": {"content": "Pronto."}, "finish_reason": "stop"}]}),
    ]

    def responder(request: httpx.Request):
        corpos.append(json.loads(request.content))
        return httpx.Response(200, content=respostas[len(corpos) - 1], headers={"content-type": "text/event-stream"})

    respx.post("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions").mock(side_effect=responder)
    conv = conversas.criar("gemini", "gemini-x")
    evs = await _coletar(conv["id"], "o que tenho salvo?")
    nomes = [e["nome"] for e in evs if e["tipo"] == "ferramenta"]
    assert nomes == ["listar_listas", "listar_favoritos"], nomes


# ---------------------------------------------------------------- C3 /models com "data": null


def test_c3_lista_de_modelos_com_data_null_derruba_a_rota(db_initialized, admin_headers):
    """Servidor compativel que responde /models com {"data": null} (ex.: Ollama sem
    modelos instalados) faz GET /chat/api/provedores dar 500 para TODOS os provedores."""
    from app.main import app

    cliente = TestClient(app, raise_server_exceptions=False)
    with respx.mock(assert_all_called=False) as rx:
        rx.get("http://localhost:11434/v1/models").respond(json={"object": "list", "data": None})
        r = cliente.post("/chat/api/provedores/ollama", headers=admin_headers,
                         json={"base_url": "http://localhost:11434/v1"})
        assert r.status_code == 200, (r.status_code, r.text[:200])
        r = cliente.get("/chat/api/provedores", headers=admin_headers)
        assert r.status_code == 200, (r.status_code, r.text[:200])


# ---------------------------------------------------------------- C4 endereco malformado


ENDERECOS_RUINS = ["http://localhost:114340/v1", "http://[::1/v1"]


@pytest.mark.parametrize("url", ENDERECOS_RUINS)
async def test_c4_endereco_malformado_quebra_o_fluxo_sem_evento_de_erro(db_initialized, url, monkeypatch):
    """Endereco aceito pela validacao (comeca com http://) mas malformado: a excecao
    (OverflowError/ExceptionGroup, httpx.InvalidURL) nao e httpx.HTTPError, escapa do
    adaptador e de responder(); a tela nao recebe `erro` e o historico nao registra nada."""
    from app.chat import conversas, provedores

    with pytest.raises(ValueError):  # a tela recusa o endereco ao salvar...
        provedores.salvar("personalizado", base_url=url, modelos="m")
    monkeypatch.setenv("CHAT_CUSTOM_URL", url)  # ...mas o .env nao passa por essa validacao
    monkeypatch.setenv("CHAT_CUSTOM_MODELOS", "m")
    from app.config import get_settings

    get_settings.cache_clear()
    conv = conversas.criar("personalizado", "m")
    try:
        evs = await _coletar(conv["id"], "oi")
    except BaseException as e:  # noqa: BLE001
        pytest.fail(f"responder() levantou {type(e).__name__}: {e}")
    assert evs[-1]["tipo"] == "erro"


@pytest.mark.parametrize("url", ENDERECOS_RUINS)
def test_c4_endereco_malformado_da_500_nas_rotas_de_provedor(db_initialized, admin_headers, url):
    from app.main import app

    cliente = TestClient(app, raise_server_exceptions=False)
    r = cliente.post("/chat/api/provedores/personalizado", headers=admin_headers, json={"base_url": url})
    assert r.status_code == 422, (r.status_code, r.text[:200])
    r = cliente.get("/chat/api/provedores", headers=admin_headers)
    assert r.status_code != 500, (r.status_code, r.text[:200])


# ---------------------------------------------------------------- C5 resposta vazia do Claude


async def test_c5_resposta_vazia_deixa_assistente_sem_conteudo_no_historico(db_initialized, claude_falso):  # noqa: F811
    """end_turn com content=[] (acontece, sobretudo apos resultados de ferramenta) e
    gravado como {"role": "assistant", "content": []}. A API exige conteudo nao vazio em
    toda mensagem exceto a ultima do assistente: toda mensagem seguinte da conversa
    leva esse bloco no meio e recebe 400 -> conversa quebrada para sempre."""
    from app.chat import conversas

    claude_falso["respostas"] = [
        _sse_anthropic([], "end_turn"),
        _sse_anthropic([{"type": "text", "text": "ok"}], "end_turn"),
    ]
    conv = conversas.criar("anthropic", "claude-opus-5-5")
    await _coletar(conv["id"], "primeira")
    await _coletar(conv["id"], "segunda")
    msgs = claude_falso["pedidos"][1]["corpo"]["messages"]
    vazias = [i for i, m in enumerate(msgs[:-1]) if m["content"] in ([], "")]
    assert not vazias, f"mensagens sem conteudo enviadas no meio do historico: {[msgs[i] for i in vazias]}"


@respx.mock
async def test_c5_openai_resposta_so_com_raciocinio_vira_assistente_vazio(db_initialized):
    from app.chat import conversas, provedores

    provedores.salvar("mistral", api_key="mistral-chave-0000000")
    corpos: list[dict] = []
    respostas = [
        _sse({"choices": [{"delta": {"reasoning_content": "hmm"}, "finish_reason": "stop"}]}),
        _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
    ]

    def responder(request: httpx.Request):
        corpos.append(json.loads(request.content))
        return httpx.Response(200, content=respostas[len(corpos) - 1], headers={"content-type": "text/event-stream"})

    respx.post("https://api.mistral.ai/v1/chat/completions").mock(side_effect=responder)
    conv = conversas.criar("mistral", "mistral-large-latest")
    await _coletar(conv["id"], "primeira")
    await _coletar(conv["id"], "segunda")
    msgs = corpos[1]["messages"]
    assert {"role": "assistant", "content": ""} not in msgs, msgs


# ---------------------------------------------------------------- C6 pedido recusado continua no historico


async def test_c6_pedido_recusado_e_reenviado_na_mensagem_seguinte(db_initialized, claude_falso):  # noqa: F811
    """Apos stop_reason=refusal o app diz "Reformule o pedido", mas o texto recusado fica
    no historico e volta em toda chamada seguinte (a API junta usuarios consecutivos):
    o pedido reformulado carrega o original e tende a ser recusado de novo."""
    from app.chat import conversas

    claude_falso["respostas"] = [_sse_anthropic([], "refusal"),
                                 _sse_anthropic([{"type": "text", "text": "ok"}], "end_turn")]
    conv = conversas.criar("anthropic", "claude-opus-5-5")
    await _coletar(conv["id"], "PEDIDO-RECUSADO")
    await _coletar(conv["id"], "pedido reformulado")
    enviado = json.dumps(claude_falso["pedidos"][1]["corpo"]["messages"], ensure_ascii=False)
    assert "PEDIDO-RECUSADO" not in enviado, enviado


# ---------------------------------------------------------------- C7 retentativa duplica o texto


async def test_c7_retentativa_por_json_ilegivel_duplica_o_texto_na_tela(db_initialized, claude_falso):  # noqa: F811
    """Quando o SDK levanta ValueError (entrada de ferramenta ilegivel, eager_input_streaming),
    a rodada inteira e refeita, mas os deltas de texto da tentativa falha ja foram para a
    tela; a bolha (chat.js nao zera ctx.bolha em `aviso`) mostra o texto duas vezes."""
    from app.chat import conversas

    ruim = _sse_anthropic([{"type": "text", "text": "Vou conferir."},
                           {"type": "tool_use", "id": "toolu_1", "name": "listar_listas", "input": {"x": 1}}],
                          "tool_use")
    ruim = ruim.replace(b'{\\"x\\": 1}', b'{\\"x\\" 1 ]]')
    claude_falso["respostas"] = [ruim, _sse_anthropic([{"type": "text", "text": "Vou conferir."}], "end_turn")]
    conv = conversas.criar("anthropic", "claude-opus-5-5")
    evs = await _coletar(conv["id"], "oi")
    assert any(e["tipo"] == "aviso" and "Refazendo" in e["mensagem"] for e in evs), evs  # caminho da retentativa
    # Simula a tela (chat.js): `reiniciar` descarta o texto da tentativa em curso.
    partes: list[str] = []
    for e in evs:
        if e["tipo"] == "reiniciar":
            partes.clear()
        elif e["tipo"] == "texto":
            partes.append(e["delta"])
    na_tela = "".join(partes)
    salvo = conversas.obter(conv["id"])["exibicao"][-1]["texto"]
    assert na_tela == salvo, f"tela={na_tela!r} salvo={salvo!r}"


# ---------------------------------------------------------------- C8 CHAT_MAX_RODADAS


async def test_c8_max_rodadas_conta_idas_e_fecha_sem_ferramentas(db_initialized, claude_falso, monkeypatch):  # noqa: F811
    """CHAT_MAX_RODADAS conta idas ao modelo com ferramentas (como diz CHAT_IA.md). Esgotado o
    limite, os resultados da ultima rodada nao ficam sem resposta: ha uma ida final sem
    ferramentas (tool_choice none) e o modelo responde com o que reuniu."""
    from app.chat import conversas

    _env(monkeypatch, CHAT_MAX_RODADAS="1")
    claude_falso["respostas"] = [
        _sse_anthropic([{"type": "tool_use", "id": f"t{i}", "name": "listar_listas", "input": {}} for i in range(2)],
                       "tool_use"),
        _sse_anthropic([{"type": "text", "text": "Resumo final."}], "end_turn"),
    ]
    conv = conversas.criar("anthropic", "claude-opus-5-5")
    evs = await _coletar(conv["id"], "laço")
    assert len(claude_falso["pedidos"]) == 2
    assert "tool_choice" not in claude_falso["pedidos"][0]["corpo"]
    assert claude_falso["pedidos"][1]["corpo"]["tool_choice"] == {"type": "none"}
    assert any(e["tipo"] == "aviso" and "rodadas" in e["mensagem"] for e in evs)
    salvo = conversas.obter(conv["id"], com_historico=True)
    assert salvo["historico"][-1]["content"][-1] == {"type": "text", "text": "Resumo final."}
    doc = (RAIZ / "CHAT_IA.md").read_text(encoding="utf-8")
    assert "chamadas de ferramenta por mensagem" not in doc


# ---------------------------------------------------------------- C9 titulo renomeado e sobrescrito


@respx.mock
async def test_c9_renomear_durante_a_primeira_resposta_e_desfeito(db_initialized):
    from app.chat import conversas, provedores

    provedores.salvar("deepseek", api_key="sk-deepseek-000000000")
    conv = conversas.criar("deepseek", "deepseek-chat")
    respostas = [
        _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {
            "name": "listar_listas", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}),
        _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
    ]
    n = {"i": 0}

    def responder(request: httpx.Request):
        n["i"] += 1
        if n["i"] == 2:  # usuario renomeia (PATCH) enquanto a resposta corre
            conversas.renomear(conv["id"], "Meu titulo")
        return httpx.Response(200, content=respostas[n["i"] - 1], headers={"content-type": "text/event-stream"})

    respx.post("https://api.deepseek.com/v1/chat/completions").mock(side_effect=responder)
    await _coletar(conv["id"], "pergunta original")
    assert conversas.obter(conv["id"])["titulo"] == "Meu titulo"


# ---------------------------------------------------------------- C10 link "interno" que sai do app


@pytest.mark.skipif(shutil.which("node") is None, reason="node indisponivel")
def test_c10_link_markdown_com_barra_invertida_sai_do_app():
    """linkSeguro trata "/\\\\host" como caminho interno (abre na mesma aba, sem noopener),
    mas o navegador normaliza a barra invertida: o link leva a outro dominio."""
    js = (RAIZ / "app/static/js/chat.js").read_text(encoding="utf-8")
    ini = js.index("function linkSeguro")
    fim = js.index("\n    }\n", ini) + 6
    script = js[ini:fim] + """
const s = linkSeguro("/\\\\evil.example/login");
console.log(JSON.stringify(s === null ? {rejeitado: true} : {externo: s.externo, host: new URL(s.href, "http://127.0.0.1:8000/chat").host}));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    r = json.loads(out)
    assert r.get("rejeitado") or r["externo"] or r["host"] == "127.0.0.1:8000", r
