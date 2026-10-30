"""Provedores de modelo do chat.

Dois tipos de integracao:

- "anthropic": modelos Claude pelo SDK oficial da Anthropic.
- "openai": qualquer servidor que fale a API de chat da OpenAI
  (/chat/completions com ferramentas): OpenAI, Google Gemini, OpenRouter (centenas
  de modelos), DeepSeek, Groq, Mistral, xAI, Ollama e LM Studio locais, ou um
  endereco personalizado.

A chave vem do .env (prioridade) ou do cadastro feito na tela do chat. As chaves
ficam so no servidor: a tela recebe apenas se ha chave e os 4 ultimos caracteres.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

from app.config import get_settings
from app.db import get_session_factory, init_database
from app.models.chat import ChatProvedor

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Preset:
    id: str
    nome: str
    tipo: str  # anthropic | openai
    base_url: str = ""
    chave_cfg: str = ""  # atributo de Settings com a chave
    url_cfg: str = ""  # atributo de Settings com o endereco (locais/personalizado)
    modelos: tuple[str, ...] = ()
    precisa_chave: bool = True
    local: bool = False
    onde_obter: str = ""


# Modelos Claude atuais (o primeiro e o padrao).
MODELOS_CLAUDE = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1")
NOMES_MODELOS = {
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-5-5": "Claude Haiku 5.5",
    "claude-fable-5-1": "Claude Fable 5.1",
}

PRESETS: dict[str, Preset] = {p.id: p for p in (
    Preset("anthropic", "Anthropic (Claude)", "anthropic", chave_cfg="anthropic_api_key",
           modelos=MODELOS_CLAUDE, onde_obter="https://platform.claude.com/settings/keys"),
    Preset("openai", "OpenAI", "openai", "https://api.openai.com/v1", "openai_api_key",
           onde_obter="https://platform.openai.com/api-keys"),
    Preset("gemini", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
           "gemini_api_key", onde_obter="https://aistudio.google.com/apikey"),
    Preset("openrouter", "OpenRouter (vários provedores)", "openai", "https://openrouter.ai/api/v1",
           "openrouter_api_key", onde_obter="https://openrouter.ai/keys"),
    Preset("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", "deepseek_api_key",
           modelos=("deepseek-chat",), onde_obter="https://platform.deepseek.com/api_keys"),
    Preset("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "groq_api_key",
           onde_obter="https://console.groq.com/keys"),
    Preset("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", "mistral_api_key",
           modelos=("mistral-large-latest",), onde_obter="https://console.mistral.ai/api-keys"),
    Preset("xai", "xAI (Grok)", "openai", "https://api.x.ai/v1", "xai_api_key",
           onde_obter="https://console.x.ai"),
    Preset("ollama", "Ollama (local)", "openai", "http://localhost:11434/v1", url_cfg="ollama_url",
           precisa_chave=False, local=True, onde_obter="https://ollama.com"),
    Preset("lmstudio", "LM Studio (local)", "openai", "http://localhost:1234/v1", url_cfg="lmstudio_url",
           precisa_chave=False, local=True, onde_obter="https://lmstudio.ai"),
    Preset("personalizado", "Personalizado (compatível com OpenAI)", "openai", chave_cfg="chat_custom_api_key",
           url_cfg="chat_custom_url", precisa_chave=False),
)}


@dataclass
class Provedor:
    id: str
    nome: str
    tipo: str
    base_url: str
    api_key: str
    modelos: list[str] = field(default_factory=list)
    configurado: bool = False
    origem: str = ""  # env | tela | ""
    local: bool = False
    precisa_chave: bool = True
    onde_obter: str = ""

    def publico(self) -> dict:
        """Versao para a tela: nunca inclui a chave."""
        return {
            "id": self.id, "nome": self.nome, "tipo": self.tipo, "base_url": self.base_url,
            "configurado": self.configurado, "origem": self.origem, "local": self.local,
            "precisa_chave": self.precisa_chave, "tem_chave": bool(self.api_key),
            "chave_final": mascarar(self.api_key), "modelos": self.modelos, "onde_obter": self.onde_obter,
        }


def mascarar(chave: str) -> str:
    return f"…{chave[-4:]}" if len(chave) >= 12 else ("…" if chave else "")


def _lista(texto: str | None) -> list[str]:
    return [m.strip() for m in (texto or "").split(",") if m.strip()]


def _salvos() -> dict[str, ChatProvedor]:
    init_database()
    with get_session_factory()() as s:
        out = {}
        for r in s.query(ChatProvedor).all():
            s.expunge(r)
            out[r.provedor] = r
        return out


def resolver(pid: str, salvos: dict[str, ChatProvedor] | None = None) -> Provedor:
    p = PRESETS.get(pid)
    if p is None:
        raise LookupError(f"Provedor desconhecido: {pid}")
    cfg = get_settings()
    salvo = (_salvos() if salvos is None else salvos).get(pid)
    chave_env = getattr(cfg, p.chave_cfg, "") if p.chave_cfg else ""
    url_env = getattr(cfg, p.url_cfg, "") if p.url_cfg else ""
    chave = (chave_env or (salvo.api_key if salvo else "") or "").strip()
    url = (url_env or (salvo.base_url if salvo else "") or "").strip()
    habilitado = salvo.habilitado if salvo is not None else True
    origem = "env" if (chave_env or url_env) else ("tela" if salvo and (salvo.api_key or salvo.base_url) else "")
    if p.local or p.id == "personalizado":
        # Locais e personalizado so contam com endereco informado.
        configurado = bool(url) and habilitado
        base = url or p.base_url
    else:
        configurado = bool(chave) and habilitado
        base = p.base_url
    modelos = list(dict.fromkeys([
        *p.modelos,
        *(_lista(cfg.chat_custom_modelos) if p.id == "personalizado" else []),
        *_lista(salvo.modelos if salvo else ""),
    ]))
    return Provedor(p.id, p.nome, p.tipo, base.rstrip("/"), chave, modelos, configurado, origem,
                    p.local, p.precisa_chave, p.onde_obter)


def provedores() -> list[Provedor]:
    salvos = _salvos()
    return [resolver(pid, salvos) for pid in PRESETS]


def configurados() -> list[Provedor]:
    return [p for p in provedores() if p.configurado]


def salvar(pid: str, *, api_key: str | None = None, base_url: str | None = None, modelos: str | None = None,
           habilitado: bool = True, apagar_chave: bool = False) -> Provedor:
    """Cadastra/atualiza um provedor. api_key=None mantem a chave atual."""
    if pid not in PRESETS:
        raise LookupError(pid)
    base_url = (base_url or "").strip()
    if base_url:
        validar_endereco(base_url)
    init_database()
    with get_session_factory()() as s:
        r = s.get(ChatProvedor, pid) or ChatProvedor(provedor=pid)
        if apagar_chave:
            r.api_key = None
        elif api_key is not None and api_key.strip():
            r.api_key = api_key.strip()
        if base_url or PRESETS[pid].local or pid == "personalizado":
            r.base_url = base_url or None
        if modelos is not None:
            r.modelos = ",".join(_lista(modelos)) or None
        r.habilitado = habilitado
        s.add(r)
        s.commit()
    _cache_modelos.pop(pid, None)
    return resolver(pid)


def url_api(base: str, caminho: str) -> str:
    """base + caminho preservando a query (ex.: Azure OpenAI exige ?api-version=)."""
    u = httpx.URL(base)
    return str(u.copy_with(path=u.path.rstrip("/") + caminho))


def validar_endereco(url: str) -> None:
    """Recusa endereco que o cliente HTTP nao conseguiria usar (porta invalida, IPv6 mal fechado)."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("O endereço deve começar com http:// ou https://")
    try:
        u = httpx.URL(url)
        porta = u.port
    except (httpx.InvalidURL, ValueError, TypeError) as e:
        raise ValueError(f"Endereço inválido: {url}") from e
    if not u.host or (porta is not None and not 0 < porta < 65536):
        raise ValueError(f"Endereço inválido: {url}")


# ---------------------------------------------------------------- lista de modelos

_IGNORAR = ("embed", "tts", "whisper", "dall-e", "moderation", "audio", "transcribe", "image",
            "realtime", "rerank", "guard", "davinci", "babbage", "similarity", "search-preview")
_cache_modelos: dict[str, tuple[float, list[str]]] = {}
_CACHE_SEG = 600.0


async def listar_modelos(prov: Provedor) -> list[str]:
    """Modelos sugeridos + os que o provedor informa em /models (cache de 10 min)."""
    if prov.tipo != "openai" or not prov.configurado:
        return prov.modelos
    agora = time.monotonic()
    cache = _cache_modelos.get(prov.id)
    if cache and agora - cache[0] < _CACHE_SEG:
        remotos = cache[1]
    else:
        remotos = await _modelos_remotos(prov)
        if remotos:  # falha (ex.: Ollama ainda fechado) nao fica em cache
            _cache_modelos[prov.id] = (agora, remotos)
    return list(dict.fromkeys([*prov.modelos, *remotos]))


async def _modelos_remotos(prov: Provedor) -> list[str]:
    from app.chat.motor import eh_azure

    if not prov.api_key:
        headers = {}
    elif eh_azure(prov.base_url):
        headers = {"api-key": prov.api_key}
    else:
        headers = {"Authorization": f"Bearer {prov.api_key}"}
    try:
        validar_endereco(prov.base_url)
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(url_api(prov.base_url, "/models"), headers=headers)
            r.raise_for_status()
            dados = r.json()
    except Exception as e:  # noqa: BLE001 -- um provedor com problema nao derruba a lista dos outros
        log.info("lista de modelos de %s indisponivel: %s", prov.id, type(e).__name__)
        return []
    itens = (dados.get("data") or dados.get("models") or []) if isinstance(dados, dict) else dados
    if not isinstance(itens, list):
        return []
    ids = []
    for it in itens:
        mid = str((it.get("id") or it.get("name") or "") if isinstance(it, dict) else it)
        mid = mid.removeprefix("models/")
        if mid and not any(x in mid.lower() for x in _IGNORAR):
            ids.append(mid)
    return sorted(set(ids))
