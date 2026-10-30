"""Regressao do Gauntlet Loop (rodada 2, chat com IA e MCP): cada teste foi escrito por um
critico independente, reproduziu um defeito real em 811dfe5 e hoje guarda a correcao."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime as _dt

import httpx
import respx
from fastapi.testclient import TestClient

from tests.test_chat import _coletar, _env, _sem_chaves, _sse, _sse_anthropic, claude_falso  # noqa: F401


def _resp(corpo: bytes) -> httpx.Response:
    return httpx.Response(200, content=corpo, headers={"content-type": "text/event-stream"})


# ---------------------------------------------------------------- exportacoes com o mesmo nome


async def test_g1_duas_exportacoes_no_mesmo_segundo_se_sobrescrevem(base_local, monkeypatch):
    """O nome do arquivo e `chat-AAAAMMDD-HHMMSS.ext`: duas exportacoes no mesmo
    segundo (o chat roda ferramentas em paralelo) gravam no MESMO arquivo; a
    segunda apaga a primeira e os dois links baixam a mesma planilha."""
    from app import mcp_server
    from app.chat import ferramentas

    class Fixo(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt(2026, 10, 30, 10, 10, 10)

    monkeypatch.setattr(mcp_server, "datetime", Fixo)
    a, b = await asyncio.gather(
        ferramentas.executar("exportar_busca", {"filtros": {"uf": ["TO"]}, "formato": "csv"}, max_chars=10_000),
        ferramentas.executar("exportar_busca", {"filtros": {"uf": ["GO"]}, "formato": "csv"}, max_chars=10_000),
    )
    assert a[0] and b[0], (a, b)
    arq_a, arq_b = json.loads(a[1])["arquivo"], json.loads(b[1])["arquivo"]
    assert arq_a != arq_b, f"as duas exportacoes (TO e GO) foram gravadas no mesmo arquivo {arq_a}"


# ---------------------------------------------------------------- editar_lista apaga a descricao


async def test_g2_editar_lista_so_o_nome_apaga_a_descricao(db_initialized):
    """MCP.md/docstring: `editar_lista` renomeia "e/ou" troca a descricao. Pedir so o
    novo nome (descricao no padrao "") apaga a descricao que a lista tinha."""
    from app.chat import ferramentas
    from app.services import listas

    lst = listas.criar_lista("Clientes", "Escritorios de Palmas visitados em outubro")
    ok, txt = await ferramentas.executar("editar_lista", {"lista_id": lst["id"], "nome": "Clientes TO"},
                                         max_chars=10_000)
    assert ok, txt
    assert listas.obter_lista(lst["id"])["descricao"] == "Escritorios de Palmas visitados em outubro", txt


# ---------------------------------------------------------------- PATCH com titulo em branco


def test_g3_renomear_conversa_com_titulo_em_branco_da_500(db_initialized, admin_headers):
    """PATCH {"titulo": "   "}: `dados.get("titulo")` e verdadeiro, renomear() levanta
    ValueError("Informe um titulo.") que a rota nao trata -> HTTP 500."""
    from app.chat import conversas
    from app.main import app

    conv = conversas.criar("anthropic", "claude-opus-5-5")
    cli = TestClient(app, raise_server_exceptions=False)
    r = cli.patch(f"/chat/api/conversas/{conv['id']}", headers=admin_headers, json={"titulo": "   "})
    assert r.status_code == 422, (r.status_code, r.text[:200])


# ---------------------------------------------------------------- id de conversa reaproveitado


@respx.mock
async def test_g4_resposta_de_conversa_apagada_grava_na_conversa_nova_com_o_mesmo_id(db_initialized):
    """chat_conversa nao usa AUTOINCREMENT: apagada a ultima conversa, a proxima criada
    recebe o MESMO id. A resposta ainda em curso da conversa apagada (a tela deixa
    apagar durante o envio) grava o historico dela na conversa nova, e a trava por id
    bloqueia a primeira mensagem da conversa nova."""
    from app.chat import conversas, provedores

    provedores.salvar("deepseek", api_key="sk-deepseek-000000000")
    chegou, liberar = asyncio.Event(), asyncio.Event()
    chamadas = {"n": 0}

    async def responder(request: httpx.Request):
        chamadas["n"] += 1
        if chamadas["n"] == 1:  # so a resposta da conversa antiga fica pendurada
            chegou.set()
            await liberar.wait()
            return _resp(_sse({"choices": [{"delta": {"content": "RESPOSTA-ANTIGA"}, "finish_reason": "stop"}]}))
        return _resp(_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    respx.post("https://api.deepseek.com/v1/chat/completions").mock(side_effect=responder)
    velha = conversas.criar("deepseek", "deepseek-chat")
    tarefa = asyncio.create_task(_coletar(velha["id"], "PERGUNTA-DA-CONVERSA-APAGADA"))
    await chegou.wait()
    assert conversas.remover(velha["id"])
    nova = conversas.criar("deepseek", "deepseek-chat")
    bloqueio = await _coletar(nova["id"], "primeira da conversa nova")
    liberar.set()
    await tarefa
    salvo = conversas.obter(nova["id"], com_historico=True)
    problemas = []
    if "PERGUNTA-DA-CONVERSA-APAGADA" in json.dumps(salvo, ensure_ascii=False):
        problemas.append(f"conversa nova (id {nova['id']}) recebeu o historico da apagada (id {velha['id']}): "
                         f"titulo={salvo['titulo']!r} exibicao={salvo['exibicao']}")
    if bloqueio and bloqueio[-1]["tipo"] == "erro":
        problemas.append(f"primeira mensagem da conversa nova recusada: {bloqueio[-1]['mensagem']!r}")
    assert not problemas, problemas


# ---------------------------------------------------------------- titulo escolhido antes da 1a mensagem


@respx.mock
async def test_g6_titulo_dado_antes_da_primeira_mensagem_e_sobrescrito(db_initialized):
    """PATCH titulo numa conversa recem-criada (sem mensagens) e depois a 1a mensagem:
    `titulo = None if exibicao else ...` decide pela exibicao, nao por o titulo ainda
    ser o padrao, e troca o nome escolhido pelo texto da mensagem."""
    from app.chat import conversas, provedores

    provedores.salvar("deepseek", api_key="sk-deepseek-000000000")
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(return_value=_resp(
        _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})))
    conv = conversas.criar("deepseek", "deepseek-chat")
    conversas.renomear(conv["id"], "Prospeccao Palmas")
    await _coletar(conv["id"], "quantas contabilidades ativas em Palmas?")
    assert conversas.obter(conv["id"])["titulo"] == "Prospeccao Palmas"


# ---------------------------------------------------------------- remover_da_lista parcial


async def test_g7_remover_da_lista_remove_e_responde_erro(db_initialized):
    """Um CNPJ invalido no meio da lista faz `remover_da_lista` levantar ToolError DEPOIS
    de ja ter removido os anteriores: o modelo recebe "falhou" (e diz ao usuario que nada
    mudou), mas a empresa saiu da lista."""
    from app.chat import ferramentas
    from app.services import listas

    lst = listas.criar_lista("Clientes", None)
    listas.adicionar(lst["id"], [{"cnpj": "19131243000197"}])
    ok, txt = await ferramentas.executar("remover_da_lista", {"lista_id": lst["id"],
                                                             "cnpjs": ["19131243000197", "12345"]},
                                         max_chars=10_000)
    restantes = [i["cnpj"] for i in listas.obter_lista(lst["id"])["itens"]]
    assert ok or restantes == ["19131243000197"], (
        f"ferramenta respondeu ok={ok} {txt!r}, mas a lista ficou {restantes} (a remocao aconteceu)")


# ---------------------------------------------------------------- controlar_tarefa com id inexistente


async def test_g8_controlar_tarefa_inexistente_devolve_so_o_numero(db_initialized):
    """`str(LookupError(7)) or "Tarefa nao encontrada."` vale "7": o modelo recebe
    apenas o numero como mensagem de erro."""
    from app.chat import ferramentas

    ok, txt = await ferramentas.executar("controlar_tarefa", {"job_id": 7, "acao": "pausar"}, max_chars=10_000)
    assert not ok
    assert "encontrada" in txt, f"mensagem de erro devolvida ao modelo: {txt!r}"


# ---------------------------------------------------------------- tool_calls sem index e sem id


@respx.mock
async def test_g5_duas_tool_calls_sem_index_e_sem_id_no_mesmo_pedaco_viram_uma(db_initialized):
    """Regressao do C2: antes, sem `index`, valia a posicao no pedaco. Agora, sem `id`
    (servidores locais/compativeis que omitem os dois), duas chamadas no MESMO pedaco
    caem na mesma montagem: nome "listar_listaslistar_favoritos" e argumentos "{}{}"."""
    from app.chat import conversas, provedores

    provedores.salvar("personalizado", base_url="http://127.0.0.1:9999/v1", modelos="local")
    respostas = [
        _sse({"choices": [{"delta": {"role": "assistant", "tool_calls": [
            {"type": "function", "function": {"name": "listar_listas", "arguments": "{}"}},
            {"type": "function", "function": {"name": "listar_favoritos", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]}),
        _sse({"choices": [{"delta": {"content": "Pronto."}, "finish_reason": "stop"}]}),
    ]
    n = {"i": 0}

    def responder(request: httpx.Request):
        n["i"] += 1
        return _resp(respostas[n["i"] - 1])

    respx.post("http://127.0.0.1:9999/v1/chat/completions").mock(side_effect=responder)
    conv = conversas.criar("personalizado", "local")
    evs = await _coletar(conv["id"], "o que tenho salvo?")
    nomes = [e["nome"] for e in evs if e["tipo"] == "ferramenta"]
    assert nomes == ["listar_listas", "listar_favoritos"], nomes


# ---------------------------------------------------------------- endereco com query (Azure OpenAI)


@respx.mock
async def test_g9_endereco_com_query_string_manda_para_o_caminho_errado(db_initialized):
    """CHAT_IA.md cita Azure OpenAI no Personalizado; o Azure exige `?api-version=`. A
    validacao aceita o endereco com query, mas o adaptador monta f"{base}/chat/completions"
    e o caminho vira parte da query: o POST vai para .../deployments/gpt (sem /chat/completions)."""
    from app.chat import conversas, provedores

    base = "https://recurso.openai.azure.com/openai/deployments/gpt?api-version=2024-10-21"
    provedores.salvar("personalizado", base_url=base, api_key="azure-chave-000000000", modelos="gpt")
    pedidos: list[httpx.Request] = []

    def responder(request: httpx.Request):
        pedidos.append(request)
        return _resp(_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    respx.route(host="recurso.openai.azure.com").mock(side_effect=responder)
    conv = conversas.criar("personalizado", "gpt")
    await _coletar(conv["id"], "oi")
    assert pedidos, "nenhum pedido"
    url = pedidos[0].url
    assert url.path.endswith("/chat/completions") and url.params.get("api-version") == "2024-10-21", str(url)


# ---------------------------------------------------------------- content em lista (Mistral/Magistral)


@respx.mock
async def test_g10_delta_content_em_lista_derruba_a_resposta(db_initialized):
    """Provedores compativeis que mandam `delta.content` como lista de partes
    (ex.: modelos de raciocinio da Mistral: [{"type": "text", "text": ...}]): o adaptador
    faz texto.append(lista) e "".join() levanta TypeError -> a conversa so recebe
    "Erro inesperado ... TypeError", e o delta (uma lista) ainda vai para a tela."""
    from app.chat import conversas, provedores

    provedores.salvar("mistral", api_key="mistral-chave-0000000")
    respx.post("https://api.mistral.ai/v1/chat/completions").mock(return_value=_resp(_sse(
        {"choices": [{"delta": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": [{"type": "text", "text": "pensando"}]}]}}]},
        {"choices": [{"delta": {"content": [{"type": "text", "text": "Olá!"}]}, "finish_reason": "stop"}]},
    )))
    conv = conversas.criar("mistral", "magistral-medium-latest")
    evs = await _coletar(conv["id"], "oi")
    erros = [e for e in evs if e["tipo"] == "erro"]
    deltas_ruins = [e["delta"] for e in evs if e["tipo"] == "texto" and not isinstance(e["delta"], str)]
    assert not erros and not deltas_ruins, (erros, deltas_ruins)
    assert conversas.obter(conv["id"])["exibicao"][-1] == {"tipo": "assistente", "texto": "Olá!"}


# ---------------------------------------------------------------- falha do /models fica em cache


async def test_g11_falha_ao_listar_modelos_fica_em_cache_por_10_minutos(db_initialized):
    """Ollama fechado na primeira abertura da tela: a lista vazia entra no cache de 10 min
    e, mesmo com o Ollama ja aberto, a tela continua sem modelos ate o cache vencer."""
    from app.chat import provedores

    p = provedores.salvar("ollama", base_url="http://localhost:11434/v1")
    with respx.mock(assert_all_called=False) as rx:
        rota = rx.get("http://localhost:11434/v1/models")
        rota.mock(side_effect=httpx.ConnectError("recusada"))
        assert await provedores.listar_modelos(p) == []
        rota.mock(return_value=httpx.Response(200, json={"data": [{"id": "qwen3:8b"}]}))
        assert await provedores.listar_modelos(p) == ["qwen3:8b"], "lista vazia servida do cache apos a falha"


# ---------------------------------------------------------------- texto mostrado e perdido no "limite"


async def test_g12_texto_mostrado_antes_do_limite_some_da_conversa(db_initialized, claude_falso):  # noqa: F811
    """max_tokens no meio de uma chamada de ferramenta: o texto ja foi para a tela em
    fluxo, mas `limite` faz break antes de gravar a exibicao: ao reabrir a conversa
    o texto sumiu (so resta o aviso)."""
    from app.chat import conversas

    claude_falso["respostas"] = [_sse_anthropic([
        {"type": "text", "text": "Encontrei 3 empresas: A, B e C. Vou montar a lista."},
        {"type": "tool_use", "id": "toolu_1", "name": "criar_lista", "input": {"nome": "x"}}], "max_tokens")]
    conv = conversas.criar("anthropic", "claude-opus-5-5")
    evs = await _coletar(conv["id"], "monte a lista")
    na_tela = "".join(e["delta"] for e in evs if e["tipo"] == "texto")
    assert "Encontrei 3 empresas" in na_tela
    exib = conversas.obter(conv["id"])["exibicao"]
    assert any(i["tipo"] == "assistente" and "Encontrei 3 empresas" in i["texto"] for i in exib), exib


def test_g4_rota_nao_apaga_conversa_que_ainda_responde(db_initialized, admin_headers):
    from app.chat import conversas, motor
    from app.main import app

    conv = conversas.criar("anthropic", "claude-opus-5-5")
    motor._em_andamento.add(conv["id"])
    try:
        r = TestClient(app).delete(f"/chat/api/conversas/{conv['id']}", headers=admin_headers)
        assert r.status_code == 409
        assert conversas.obter(conv["id"]) is not None
    finally:
        motor._em_andamento.discard(conv["id"])
