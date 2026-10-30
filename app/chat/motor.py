"""Laco do chat: manda a conversa ao modelo, executa as ferramentas pedidas e
repete ate o modelo responder em texto.

Cada provedor tem um adaptador que fala o formato nativo dele; o historico e
gravado nesse formato (sem reescrever mensagens anteriores) e a tela recebe
eventos neutros:

    texto {delta} | pensando | ferramenta {id, nome, entrada}
    resultado {id, nome, ok, resumo} | aviso {mensagem} | erro {mensagem}
    fim {uso: {entrada, saida}}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.chat import conversas, ferramentas
from app.chat.provedores import Provedor, resolver, url_api
from app.config import get_settings
from app.mcp_server import INSTRUCOES

log = logging.getLogger(__name__)

SISTEMA = """\
Voce e o assistente de IA do CNPJ Consulta, aplicativo instalado no computador do
usuario (um escritorio de contabilidade/assessoria) para consultar empresas
brasileiras na base oficial da Receita Federal e em fontes publicas.

Voce tem acesso a todas as funcoes do app pelas ferramentas: consulta e ficha de
CNPJ, busca avancada e contagens, exportacao de planilhas, socios, consulta em
lote, sancoes, inscricao estadual, pesquisa na internet, buscas salvas, listas,
historico e favoritos, situacao e atualizacao da base local, contatos
complementares, e SQL somente-leitura na base da Receita e no banco do app.

Como trabalhar:
- Responda em portugues do Brasil, de forma direta. Busque os dados com as
  ferramentas; nunca responda de memoria sobre uma empresa.
- Prefira as ferramentas prontas (ja aplicam as regras de negocio). Use SQL para
  o que elas nao cobrem (rankings, agregacoes, cruzamentos); chame
  `descrever_bancos` antes da primeira consulta SQL.
- Encadeie quantas ferramentas forem necessarias e rode em paralelo as que forem
  independentes.
- Antes de algo que altera dados ou demora (atualizar a base, cancelar tarefa,
  apagar lista, cadastrar ou remover contato), confirme com o usuario, a menos que
  ele ja tenha pedido isso explicitamente.
- Empresas em tabela markdown, com CNPJ formatado (00.000.000/0000-00). Para a
  ficha de uma empresa, de o link [ficha](/empresa/CNPJ_SO_DIGITOS). Quando uma
  exportacao devolver `link_download`, entregue-o como link markdown.
- Se uma ferramenta falhar, explique o motivo e o que o usuario pode fazer.

Regras do app (valem tambem aqui):
""" + INSTRUCOES


def sistema() -> str:
    hoje = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y")
    return f"{SISTEMA}\nData de hoje: {hoje}."


class ErroProvedor(Exception):
    pass


@dataclass
class Chamada:
    id: str
    nome: str
    entrada: Any
    erro_entrada: str | None = None
    extra: dict | None = None  # campos do provedor na chamada (ex.: thought_signature do Gemini)


@dataclass
class Rodada:
    """Resultado de uma ida ao modelo."""

    mensagens: list[dict] = field(default_factory=list)  # a anexar ao historico (assistente)
    texto: str = ""
    chamadas: list[Chamada] = field(default_factory=list)
    parada: str = "fim"  # fim | ferramentas | pausa | recusa | limite
    aviso: str | None = None
    entrada_tokens: int = 0
    saida_tokens: int = 0


# ---------------------------------------------------------------- Anthropic (SDK oficial)

# Modelos com raciocinio adaptativo e controle de esforco.
_ADAPTATIVOS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "claude-fable-5",
                "claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
                "claude-sonnet-4-6"}
# Fallback automatico do servidor quando o filtro de seguranca recusa um pedido.
_COM_FALLBACK = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5", "claude-opus-5"}
_BETA_FALLBACK = "server-side-fallback-2026-07-01"
_ESFORCOS = {"low", "medium", "high", "xhigh", "max"}
_PENSAMENTO = {"thinking", "redacted_thinking"}

_clientes_anthropic: dict[str, Any] = {}


class AdaptadorAnthropic:
    tipo = "anthropic"

    def __init__(self, prov: Provedor, modelo: str) -> None:
        self.prov, self.modelo = prov, modelo

    def _cliente(self):
        from anthropic import AsyncAnthropic

        chave = self.prov.api_key
        if chave not in _clientes_anthropic:
            _clientes_anthropic[chave] = AsyncAnthropic(api_key=chave or None, max_retries=2)
        return _clientes_anthropic[chave]

    @staticmethod
    def ferramentas(catalogo: list[dict]) -> list[dict]:
        # eager_input_streaming: a entrada da ferramenta chega enquanto e gerada;
        # a validacao fica com o app (call_tool valida pelo esquema).
        return [{"name": t["nome"], "description": t["descricao"][:4000], "input_schema": t["esquema"],
                 "eager_input_streaming": True} for t in catalogo]

    @staticmethod
    def msg_usuario(texto: str) -> dict:
        return {"role": "user", "content": texto}

    @staticmethod
    def msg_assistente_texto(texto: str) -> dict:
        return {"role": "assistant", "content": [{"type": "text", "text": texto}]}

    @staticmethod
    def msgs_resultados(resultados: list[tuple[Chamada, bool, str]]) -> list[dict]:
        # Todos os resultados numa unica mensagem (preserva chamadas em paralelo).
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": texto, **({} if ok else {"is_error": True})}
            for c, ok, texto in resultados]}]

    def _parametros(self, historico: list[dict], tools: list[dict], sem_ferramentas: bool = False) -> dict:
        cfg = get_settings()
        kw: dict[str, Any] = {
            "model": self.modelo, "max_tokens": cfg.chat_max_tokens, "system": sistema(),
            "messages": historico, "tools": tools, "cache_control": {"type": "ephemeral"},
        }
        if self.modelo in _ADAPTATIVOS:
            kw["thinking"] = {"type": "adaptive"}
            esforco = cfg.chat_esforco if cfg.chat_esforco in _ESFORCOS else "medium"
            kw["output_config"] = {"effort": esforco}
        if sem_ferramentas:
            kw["tool_choice"] = {"type": "none"}
        if self.modelo in _COM_FALLBACK:
            kw["betas"] = [_BETA_FALLBACK]
            kw["fallbacks"] = "default"
        return kw

    async def rodada(self, historico: list[dict], tools: list[dict], *,
                     sem_ferramentas: bool = False) -> AsyncIterator[dict]:
        import anthropic

        kw = self._parametros(historico, tools, sem_ferramentas)
        for tentativa in range(3):
            pensando = False
            try:
                async with self._cliente().beta.messages.stream(**kw) as stream:
                    async for ev in stream:
                        if ev.type == "text":
                            yield {"tipo": "texto", "delta": ev.text}
                        elif ev.type == "content_block_start" and ev.content_block.type in _PENSAMENTO and not pensando:
                            pensando = True
                            yield {"tipo": "pensando"}
                    final = await stream.get_final_message()
                break
            except ValueError:
                # JSON de entrada de ferramenta que nao deu para ler: refaz a rodada.
                if tentativa == 2:
                    raise ErroProvedor("O modelo gerou parâmetros de ferramenta ilegíveis. Tente de novo.") from None
                # A tela descarta o texto ja mostrado desta tentativa.
                yield {"tipo": "reiniciar"}
                yield {"tipo": "aviso", "mensagem": "Refazendo a resposta (parâmetros ilegíveis)…"}
            except anthropic.AuthenticationError as e:
                raise ErroProvedor("Chave da Anthropic inválida. Confira em Configurar provedores.") from e
            except anthropic.PermissionDeniedError as e:
                raise ErroProvedor("A chave da Anthropic não tem permissão para este modelo.") from e
            except anthropic.NotFoundError as e:
                raise ErroProvedor(f"Modelo não encontrado: {self.modelo}.") from e
            except anthropic.RateLimitError as e:
                raise ErroProvedor("Limite de uso da Anthropic atingido. Aguarde um pouco e tente de novo.") from e
            except anthropic.BadRequestError as e:
                raise ErroProvedor(f"Pedido recusado pela API da Anthropic: {e.message}") from e
            except anthropic.APIStatusError as e:
                raise ErroProvedor(f"Erro da API da Anthropic (HTTP {e.status_code}). Tente de novo.") from e
            except anthropic.APIConnectionError as e:
                raise ErroProvedor("Sem conexão com a API da Anthropic.") from e

        blocos = _sem_tentativa_recusada([b.to_dict(mode="json") for b in final.content])
        r = Rodada()
        u = final.usage
        r.entrada_tokens = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        r.saida_tokens = u.output_tokens or 0
        r.texto = "".join(b.get("text", "") for b in blocos if b.get("type") == "text")
        r.chamadas = [Chamada(b["id"], b["name"], b.get("input")) for b in blocos if b.get("type") == "tool_use"]
        motivo = final.stop_reason
        if motivo == "refusal":
            r.parada = "recusa"
            cat = getattr(final.stop_details, "category", None) if final.stop_details else None
            r.aviso = "O modelo recusou este pedido" + (f" (categoria: {cat})" if cat else "") + \
                ". Reformule o pedido ou escolha outro modelo."
        elif motivo == "max_tokens" and r.chamadas:
            r.parada = "limite"
            r.aviso = "A resposta passou do limite de tamanho no meio de uma chamada de ferramenta. Peça algo menor."
        elif not r.texto.strip() and not r.chamadas and motivo != "pause_turn":
            # Resposta sem texto nem ferramenta: nao entra no historico (a API
            # recusa mensagem de assistente vazia no meio da conversa).
            r.aviso = "O modelo não devolveu resposta. Tente reformular ou peça para continuar."
        else:
            r.mensagens = [{"role": "assistant", "content": blocos}]
            if motivo == "pause_turn":
                r.parada = "pausa"
            elif r.chamadas:
                r.parada = "ferramentas"
            elif motivo == "max_tokens":
                r.aviso = "Resposta cortada por tamanho. Peça para continuar."
        yield {"tipo": "_rodada", "rodada": r}


def _sem_tentativa_recusada(blocos: list[dict]) -> list[dict]:
    """Depois de um fallback no meio da resposta, raciocinio e chamadas de
    ferramenta da tentativa recusada nao voltam ao historico (texto volta)."""
    marcas = [i for i, b in enumerate(blocos) if b.get("type") == "fallback"]
    if not marcas:
        return blocos
    corte = marcas[-1]
    descartar = {"thinking", "redacted_thinking", "tool_use", "server_tool_use", "fallback"}
    return [b for i, b in enumerate(blocos) if not (i <= corte and b.get("type") in descartar)]


# ---------------------------------------------------------------- compativeis com a API da OpenAI


def _esquema_simples(esq: Any) -> Any:
    """Esquema JSON aceito pelos provedores mais exigentes (Gemini, modelos locais)."""
    if isinstance(esq, list):
        return [_esquema_simples(x) for x in esq]
    if not isinstance(esq, dict):
        return esq
    opcoes = esq.get("anyOf")
    if isinstance(opcoes, list):
        sem_nulo = [o for o in opcoes if not (isinstance(o, dict) and o.get("type") == "null")]
        if len(sem_nulo) == 1:
            junto = {**{k: v for k, v in esq.items() if k != "anyOf"}, **sem_nulo[0]}
            return _esquema_simples(junto)
    out = {}
    for k, v in esq.items():
        if k in ("title", "additionalProperties") or (k == "default" and v is None):
            continue
        out[k] = _esquema_simples(v) if k != "properties" else {p: _esquema_simples(s) for p, s in v.items()}
    return out


class AdaptadorOpenAI:
    tipo = "openai"

    def __init__(self, prov: Provedor, modelo: str) -> None:
        self.prov, self.modelo = prov, modelo

    @staticmethod
    def ferramentas(catalogo: list[dict]) -> list[dict]:
        out = []
        for t in catalogo:
            params = _esquema_simples(t["esquema"]) or {}
            params.setdefault("type", "object")
            params.setdefault("properties", {})
            out.append({"type": "function", "function": {
                "name": t["nome"], "description": t["descricao"][:1024], "parameters": params}})
        return out

    @staticmethod
    def msg_usuario(texto: str) -> dict:
        return {"role": "user", "content": texto}

    @staticmethod
    def msg_assistente_texto(texto: str) -> dict:
        return {"role": "assistant", "content": texto}

    @staticmethod
    def msgs_resultados(resultados: list[tuple[Chamada, bool, str]]) -> list[dict]:
        return [{"role": "tool", "tool_call_id": c.id, "content": texto if ok else f"ERRO: {texto}"}
                for c, ok, texto in resultados]

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.prov.api_key:
            if eh_azure(self.prov.base_url):
                h["api-key"] = self.prov.api_key  # Azure OpenAI usa este cabecalho, nao Bearer
            else:
                h["Authorization"] = f"Bearer {self.prov.api_key}"
        if self.prov.id == "openrouter":
            h["X-Title"] = "CNPJ Consulta"
        return h

    async def rodada(self, historico: list[dict], tools: list[dict], *,
                     sem_ferramentas: bool = False) -> AsyncIterator[dict]:
        corpo = {"model": self.modelo, "stream": True, "tools": tools,
                 "messages": [{"role": "system", "content": sistema()}, *_para_envio(historico, self.prov.id)]}
        if sem_ferramentas:
            corpo["tool_choice"] = "none"
        texto: list[str] = []
        raciocinio: list[str] = []
        parciais: dict[int, dict] = {}
        atual: int | None = None  # chamada em montagem quando o provedor nao manda `index`
        motivo = None
        uso: dict = {}
        pensando = False
        timeout = httpx.Timeout(300.0, connect=15.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as cliente, cliente.stream(
                    "POST", url_api(self.prov.base_url, "/chat/completions"), json=corpo, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    raise ErroProvedor(_erro_http(self.prov, resp.status_code, (await resp.aread())[:2000]))
                async for linha in resp.aiter_lines():
                    if not linha.startswith("data:"):
                        continue
                    dado = linha[5:].strip()
                    if dado == "[DONE]":
                        break
                    try:
                        pedaco = json.loads(dado)
                    except ValueError:
                        continue
                    if pedaco.get("error"):
                        raise ErroProvedor(f"{self.prov.nome}: {_msg_erro(pedaco['error'])}")
                    uso = pedaco.get("usage") or uso
                    for escolha in pedaco.get("choices") or []:
                        delta = escolha.get("delta") or {}
                        if isinstance(delta.get("reasoning_content"), str):
                            raciocinio.append(delta["reasoning_content"])  # DeepSeek exige de volta
                        if (delta.get("reasoning_content") or delta.get("reasoning")) and not pensando:
                            pensando = True
                            yield {"tipo": "pensando"}
                        conteudo = delta.get("content")
                        if isinstance(conteudo, list):
                            # Partes tipadas (ex.: modelos de raciocinio da Mistral)
                            if any(isinstance(p, dict) and p.get("type") == "thinking" for p in conteudo) \
                                    and not pensando:
                                pensando = True
                                yield {"tipo": "pensando"}
                            conteudo = "".join(str(p.get("text") or "") for p in conteudo
                                               if isinstance(p, dict) and p.get("type") == "text")
                        if isinstance(conteudo, str) and conteudo:
                            texto.append(conteudo)
                            yield {"tipo": "texto", "delta": conteudo}
                        for tc in delta.get("tool_calls") or []:
                            if isinstance(tc.get("index"), int):
                                idx = tc["index"]
                            else:
                                # Sem `index` (ex.: Gemini manda cada chamada inteira num
                                # pedaco): um `id` novo abre outra chamada.
                                novo_id = tc.get("id")
                                cur = parciais.get(atual) if atual is not None else None
                                mesma = bool(novo_id) and cur is not None and cur["id"] == novo_id
                                if (cur is None
                                        or (novo_id and cur["id"] and not mesma)
                                        # outro nome chegando para uma chamada ja nomeada = outra chamada
                                        or ((tc.get("function") or {}).get("name") and cur["nome"] and not mesma)):
                                    atual = max(parciais, default=-1) + 1
                                idx = atual
                            p = parciais.setdefault(idx, {"id": "", "nome": "", "args": "", "extra": None})
                            p["id"] = tc.get("id") or p["id"]
                            if isinstance(tc.get("extra_content"), dict):
                                # Gemini 3: thought_signature tem de voltar intacta na proxima ida
                                p["extra"] = {**(p["extra"] or {}), **tc["extra_content"]}
                            fn = tc.get("function") or {}
                            p["nome"] += fn.get("name") or ""
                            p["args"] += fn.get("arguments") or ""
                        motivo = escolha.get("finish_reason") or motivo
        except httpx.TimeoutException as e:
            raise ErroProvedor(f"{self.prov.nome} demorou demais para responder.") from e
        except (httpx.HTTPError, httpx.InvalidURL, OverflowError, ValueError) as e:
            raise ErroProvedor(f"Sem conexão com {self.prov.nome} ({self.prov.base_url}).") from e

        r = Rodada(texto="".join(texto))
        r.entrada_tokens = int(uso.get("prompt_tokens") or 0)
        r.saida_tokens = int(uso.get("completion_tokens") or 0)
        for idx in sorted(parciais):
            p = parciais[idx]
            if not p["nome"]:
                continue
            cid = p["id"] or f"call{uuid.uuid4().hex[:5]}"  # 9 caracteres alfanumericos (aceito pela Mistral)
            try:
                entrada = json.loads(p["args"]) if p["args"].strip() else {}
                c = Chamada(cid, p["nome"], entrada)
            except ValueError:
                c = Chamada(cid, p["nome"], p["args"],
                            erro_entrada="Argumentos JSON inválidos; gere a chamada de novo.")
            c.extra = p["extra"]
            r.chamadas.append(c)
        if motivo == "content_filter":
            r.parada, r.aviso = "recusa", "O provedor bloqueou a resposta (filtro de conteúdo)."
        elif motivo == "length" and r.chamadas:
            r.parada = "limite"
            r.aviso = "A resposta passou do limite de tamanho no meio de uma chamada de ferramenta. Peça algo menor."
        elif not r.texto.strip() and not r.chamadas:
            r.aviso = "O modelo não devolveu resposta. Tente reformular ou peça para continuar."
        else:
            msg: dict[str, Any] = {"role": "assistant", "content": r.texto}
            if raciocinio and "".join(raciocinio):
                msg["reasoning_content"] = "".join(raciocinio)
            if r.chamadas:
                msg["tool_calls"] = []
                for c in r.chamadas:
                    tc: dict[str, Any] = {"id": c.id, "type": "function", "function": {
                        "name": c.nome,
                        # Argumentos ilegiveis nao vao para o historico (alguns servidores
                        # recusam a conversa inteira); o erro fica no resultado da ferramenta.
                        "arguments": "{}" if c.erro_entrada else json.dumps(c.entrada, ensure_ascii=False),
                    }}
                    if c.extra:
                        tc["extra_content"] = c.extra
                    msg["tool_calls"].append(tc)
                r.parada = "ferramentas"
            if "reasoning_content" in msg or any("extra_content" in t for t in msg.get("tool_calls", [])):
                msg["_origem"] = self.prov.id  # campos proprios deste provedor (ver _para_envio)
            elif motivo == "length":
                r.aviso = "Resposta cortada por tamanho. Peça para continuar."
            r.mensagens = [msg]
        yield {"tipo": "_rodada", "rodada": r}


def eh_azure(base_url: str) -> bool:
    try:
        host = (httpx.URL(base_url).host or "").lower()
    except (httpx.InvalidURL, ValueError, TypeError):
        return False
    return host.endswith(".azure.com") or "/openai/deployments/" in base_url


_ID_MISTRAL = re.compile(r"[a-zA-Z0-9]{9}")


def _id_mistral(cid: str) -> str:
    """A Mistral so aceita ids de chamada com 9 caracteres alfanumericos: converte de
    forma estavel (o mesmo id original vira sempre o mesmo)."""
    if _ID_MISTRAL.fullmatch(cid or ""):
        return cid
    return "c" + hashlib.sha256((cid or "").encode()).hexdigest()[:8]


def _para_envio(historico: list[dict], provedor: str) -> list[dict]:
    """Historico como o provedor atual aceita, sem alterar o que esta gravado.

    Campos proprios de um provedor (raciocinio do DeepSeek, assinatura do Gemini)
    voltam so para ele; se a conversa trocou de provedor, ficam de fora.
    """
    out = []
    for m in historico:
        m = dict(m)
        origem = m.pop("_origem", None)
        if m.get("role") == "assistant" and origem != provedor:
            m.pop("reasoning_content", None)
            if m.get("tool_calls"):
                m["tool_calls"] = [{k: v for k, v in t.items() if k != "extra_content"} for t in m["tool_calls"]]
        if provedor == "mistral":
            if m.get("tool_calls"):
                m["tool_calls"] = [{**t, "id": _id_mistral(t.get("id", ""))} for t in m["tool_calls"]]
            if m.get("role") == "tool":
                m["tool_call_id"] = _id_mistral(m.get("tool_call_id", ""))
        out.append(m)
    return out


def _msg_erro(err: Any) -> str:
    if isinstance(err, dict):
        return str(err.get("message") or err.get("error") or err)[:500]
    return str(err)[:500]


def _erro_http(prov: Provedor, status: int, corpo: bytes) -> str:
    try:
        detalhe = _msg_erro(json.loads(corpo).get("error") or json.loads(corpo))
    except (ValueError, AttributeError):
        detalhe = corpo.decode("utf-8", "replace")[:300]
    if status in (401, 403):
        return f"Chave de {prov.nome} inválida ou sem permissão. Confira em Configurar provedores."
    if status == 404:
        return f"{prov.nome}: modelo ou endereço não encontrado ({detalhe})."
    if status == 429:
        return f"Limite de uso de {prov.nome} atingido. Aguarde e tente de novo."
    return f"{prov.nome} respondeu HTTP {status}: {detalhe}"


def adaptador(prov: Provedor, modelo: str):
    return AdaptadorAnthropic(prov, modelo) if prov.tipo == "anthropic" else AdaptadorOpenAI(prov, modelo)


# ---------------------------------------------------------------- conversa

_em_andamento: set[int] = set()


def _resumo(texto: str, n: int = 600) -> str:
    texto = texto.strip()
    return texto if len(texto) <= n else texto[:n] + "…"


def _entrada_exibicao(entrada: Any) -> Any:
    bruto = json.dumps(entrada, ensure_ascii=False, default=str) if not isinstance(entrada, str) else entrada
    return entrada if len(bruto) <= 2000 else bruto[:2000] + "…"


async def responder(conversa_id: int, texto_usuario: str) -> AsyncIterator[dict]:
    """Processa uma mensagem do usuario e emite os eventos da resposta."""
    # Marca a conversa ANTES de qualquer await: duas mensagens simultaneas nao
    # podem ler o mesmo historico e uma apagar a outra ao gravar.
    if conversa_id in _em_andamento:
        yield {"tipo": "erro", "mensagem": "Já há uma resposta em andamento nesta conversa."}
        return
    _em_andamento.add(conversa_id)
    try:
        async for ev in _responder(conversa_id, texto_usuario):
            yield ev
    finally:
        _em_andamento.discard(conversa_id)


async def _responder(conversa_id: int, texto_usuario: str) -> AsyncIterator[dict]:
    conv = await asyncio.to_thread(conversas.obter, conversa_id, com_historico=True)
    if conv is None:
        yield {"tipo": "erro", "mensagem": "Conversa não encontrada."}
        return
    try:
        prov = await asyncio.to_thread(resolver, conv["provedor"])
    except LookupError:
        yield {"tipo": "erro", "mensagem": "Provedor desta conversa não existe mais. Comece outra conversa."}
        return
    if not prov.configurado:
        yield {"tipo": "erro", "mensagem": f"{prov.nome} não está configurado. Cadastre a chave em Configurar provedores."}
        return

    cfg = get_settings()
    ad = adaptador(prov, conv["modelo"])
    tools = ad.ferramentas(await ferramentas.catalogo())
    historico: list[dict] = conv["historico"]
    exibicao: list[dict] = conv["exibicao"]
    titulo = None if exibicao else " ".join(texto_usuario.split())[:70]
    inicio_turno = len(historico)
    historico.append(ad.msg_usuario(texto_usuario))
    exibicao.append({"tipo": "usuario", "texto": texto_usuario})
    tok_e = tok_s = 0

    async def gravar() -> None:
        nonlocal tok_e, tok_s, titulo
        await asyncio.to_thread(conversas.salvar, conversa_id, historico=historico, exibicao=exibicao,
                                titulo=titulo, tokens_entrada=tok_e, tokens_saida=tok_s)
        tok_e = tok_s = 0
        titulo = None  # so na primeira gravacao: nao desfaz um "renomear" feito durante a resposta

    try:
        await gravar()
        yield {"tipo": "inicio", "provedor": prov.id, "modelo": conv["modelo"]}
        total_e = total_s = 0
        limite = max(1, cfg.chat_max_rodadas)
        rodada_n = 0
        while True:
            rodada_n += 1
            # Esgotadas as rodadas com ferramentas, uma ultima ida sem ferramentas
            # para o modelo responder com o que ja reuniu.
            ultima = rodada_n > limite
            r: Rodada | None = None
            async for ev in ad.rodada(historico, tools, sem_ferramentas=ultima):
                if ev["tipo"] == "_rodada":
                    r = ev["rodada"]
                else:
                    yield ev
            assert r is not None
            tok_e += r.entrada_tokens
            tok_s += r.saida_tokens
            total_e += r.entrada_tokens
            total_s += r.saida_tokens
            if ultima and r.chamadas:
                # O modelo insistiu em ferramentas na ida final: fica so o texto.
                r.parada, r.chamadas = "fim", []
                r.mensagens = [ad.msg_assistente_texto(r.texto)] if r.texto.strip() else []
            if r.aviso:
                yield {"tipo": "aviso", "mensagem": r.aviso}
            if r.parada == "recusa":
                # O pedido recusado sai do historico enviado ao modelo (continua na
                # tela): senao ele volta junto com o pedido reformulado.
                del historico[inicio_turno:]
                exibicao.append({"tipo": "aviso", "texto": r.aviso or ""})
                await gravar()
                break
            if r.parada == "limite":
                if r.texto.strip():  # ja apareceu na tela: fica tambem na conversa salva
                    exibicao.append({"tipo": "assistente", "texto": r.texto})
                exibicao.append({"tipo": "aviso", "texto": r.aviso or ""})
                await gravar()
                break
            if r.texto.strip():
                exibicao.append({"tipo": "assistente", "texto": r.texto})
            if r.parada == "pausa":
                historico.extend(r.mensagens)
                await gravar()
                continue
            if r.parada != "ferramentas":
                historico.extend(r.mensagens)
                if r.aviso:
                    exibicao.append({"tipo": "aviso", "texto": r.aviso})
                if ultima:
                    aviso = (f"Parei após {limite} rodadas de ferramentas. "
                             "Peça para continuar se quiser que eu siga.")
                    exibicao.append({"tipo": "aviso", "texto": aviso})
                    yield {"tipo": "aviso", "mensagem": aviso}
                await gravar()
                break

            for c in r.chamadas:
                yield {"tipo": "ferramenta", "id": c.id, "nome": c.nome, "entrada": _entrada_exibicao(c.entrada)}

            async def rodar(c: Chamada) -> tuple[Chamada, bool, str]:
                if c.erro_entrada:
                    return c, False, c.erro_entrada
                ok, txt = await ferramentas.executar(c.nome, c.entrada, max_chars=cfg.chat_resultado_max_chars)
                return c, ok, txt

            resultados = await asyncio.gather(*(rodar(c) for c in r.chamadas))
            for c, ok, txt in resultados:
                yield {"tipo": "resultado", "id": c.id, "nome": c.nome, "ok": ok, "resumo": _resumo(txt)}
                exibicao.append({"tipo": "ferramenta", "nome": c.nome, "entrada": _entrada_exibicao(c.entrada),
                                 "ok": ok, "resumo": _resumo(txt, 300)})
            # Pedido do modelo e resultados entram juntos: o historico nunca fica
            # com chamada sem resposta, mesmo se a conexao cair aqui.
            historico.extend(r.mensagens)
            historico.extend(ad.msgs_resultados(list(resultados)))
            await gravar()
        yield {"tipo": "fim", "uso": {"entrada": total_e, "saida": total_s}}
    except Exception as e:  # noqa: BLE001 -- qualquer falha vira evento de erro, nunca fluxo quebrado
        msg = str(e) if isinstance(e, ErroProvedor) else \
            f"Erro inesperado ao falar com {prov.nome}: {type(e).__name__}. Confira o endereço e tente de novo."
        if not isinstance(e, ErroProvedor):
            log.exception("chat: falha inesperada na conversa %s", conversa_id)
        exibicao.append({"tipo": "aviso", "texto": msg})
        await gravar()
        yield {"tipo": "erro", "mensagem": msg}
