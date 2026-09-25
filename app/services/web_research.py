"""Pesquisa na internet sobre a empresa consultada.

Duas camadas:

1. **Atalhos de pesquisa** (sempre disponiveis): links prontos para
   buscadores, reputacao, processos, certidoes oficiais e mapas, montados a
   partir da razao social, do CNPJ e do endereco. Nada e enviado a terceiros
   sem o clique do usuario, e o link abre em nova aba.
2. **Resultados na propria tela** (opcional): consulta um provedor de busca
   configurado no .env -- Brave Search API ou Tavily (chave do usuario) ou
   uma instancia SearXNG propria. So roda quando o usuario pede.

LGPD: as consultas usam somente dados da pessoa juridica. Nomes e documentos
de socios NUNCA entram nas buscas, para nao montar perfil de pessoas.
"""

from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

import httpx

from app import __version__
from app.config import Settings, get_settings
from app.schemas.company import CompanyUnified
from app.services.official_docs import CCC_PORTAL_URL, SINTEGRA_URL, comprovante_url

log = logging.getLogger(__name__)

# Dominios de e-mail gratuitos: nao indicam o site da empresa.
_FREE_MAIL = frozenset(
    {
        "gmail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "yahoo.com.br",
        "bol.com.br", "uol.com.br", "terra.com.br", "ig.com.br", "icloud.com", "msn.com",
        "globo.com", "globomail.com", "zipmail.com.br", "r7.com", "protonmail.com",
        "proton.me", "aol.com", "gmx.com", "hotmail.com.br", "outlook.com.br",
    }
)

PROVIDER_LABELS = {"brave": "Brave Search", "tavily": "Tavily", "searxng": "SearXNG"}
# O credito mensal gratuito da Brave exige atribuicao visivel.
PROVIDER_ATTRIBUTION = {"brave": "Powered by Brave Search", "tavily": "Resultados via Tavily"}


@dataclass(frozen=True, slots=True)
class ResearchLink:
    rotulo: str
    url: str
    dica: str
    # True quando o site oficial nao aceita o CNPJ na URL (captcha/formulario):
    # a interface oferece "copiar CNPJ" ao lado do link.
    manual: bool = False


@dataclass(frozen=True, slots=True)
class ResearchGroup:
    id: str
    titulo: str
    descricao: str
    links: list[ResearchLink] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WebResult:
    titulo: str
    url: str
    trecho: str
    dominio: str


@dataclass(slots=True)
class WebSearchResponse:
    habilitado: bool
    provedor: str | None
    consulta: str
    resultados: list[WebResult] = field(default_factory=list)
    erro: str | None = None
    do_cache: bool = False
    atribuicao: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Atalhos
# ---------------------------------------------------------------------------


def _q(value: str) -> str:
    return quote_plus(value, safe="")


def company_name(company: CompanyUnified) -> str:
    return (company.razao_social or company.nome_fantasia or company.cnpj_formatado).strip()


def default_query(company: CompanyUnified) -> str:
    """Consulta padrao: razao social entre aspas + cidade (ou CNPJ, se faltar)."""
    name = company_name(company)
    e = company.endereco
    local = " ".join(filter(None, [e.municipio if e else None, e.uf if e else None]))
    return f'"{name}" {local or company.cnpj_formatado}'.strip()


def corporate_domain(email: str | None) -> str | None:
    """Dominio do e-mail cadastral, se parecer corporativo (nao gmail/hotmail...)."""
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain) or domain in _FREE_MAIL:
        return None
    return domain


def address_query(company: CompanyUnified) -> str | None:
    e = company.endereco
    if not e or not (e.logradouro or e.cep):
        return None
    parts = [
        " ".join(filter(None, [e.logradouro, e.numero])),
        e.bairro,
        " - ".join(filter(None, [e.municipio, e.uf])),
        e.cep and f"{e.cep[:5]}-{e.cep[5:]}",
    ]
    return ", ".join(p for p in parts if p)


def build_research_links(company: CompanyUnified) -> list[ResearchGroup]:
    """Monta os grupos de atalhos para a empresa (somente dados de PJ)."""
    name = company_name(company)
    quoted = f'"{name}"'
    cnpj_fmt = company.cnpj_formatado
    cnpj = company.cnpj

    groups = [
        ResearchGroup(
            "buscadores",
            "Buscadores",
            "Razão social entre aspas, para achar a própria empresa e não homônimos.",
            [
                ResearchLink("Google", f"https://www.google.com/search?q={_q(quoted)}&hl=pt-BR", "Busca geral"),
                ResearchLink(
                    "Google Notícias",
                    f"https://news.google.com/search?q={_q(quoted)}&hl=pt-BR&gl=BR&ceid=BR:pt-419",
                    "Notícias recentes",
                ),
                ResearchLink(
                    "Bing", f"https://www.bing.com/search?q={_q(quoted)}&setlang=pt-BR&cc=BR", "Busca geral"
                ),
                ResearchLink(
                    "DuckDuckGo", f"https://duckduckgo.com/?q={_q(quoted)}&kl=br-pt", "Busca sem rastreio"
                ),
                ResearchLink(
                    "Páginas que citam o CNPJ",
                    f"https://www.google.com/search?q={_q(chr(34) + cnpj_fmt + chr(34))}&hl=pt-BR",
                    "Busca pelo número formatado",
                ),
            ],
        ),
        ResearchGroup(
            "reputacao",
            "Reputação e processos",
            "Reclamações de consumidores, processos judiciais e diários oficiais.",
            [
                ResearchLink(
                    "Jusbrasil", f"https://www.jusbrasil.com.br/busca?q={_q(quoted)}", "Processos e diários oficiais"
                ),
                ResearchLink(
                    "Reclame Aqui",
                    f"https://www.reclameaqui.com.br/busca/?q={_q(company.nome_fantasia or name)}",
                    "Reclamações de clientes",
                ),
                ResearchLink(
                    "Execução, falência ou recuperação",
                    "https://www.google.com/search?q="
                    + _q(f"{quoted} (execução OR falência OR \"recuperação judicial\")")
                    + "&hl=pt-BR",
                    "Busca no Google com termos jurídicos",
                ),
            ],
        ),
        ResearchGroup(
            "sancoes",
            "Sanções e transparência",
            "Cadastros públicos de empresas punidas e relação com o governo federal.",
            [
                ResearchLink(
                    "CEIS — inidôneas e suspensas",
                    f"https://portaldatransparencia.gov.br/sancoes/consulta?cadastro=1&cpfCnpj={cnpj}",
                    "Portal da Transparência",
                ),
                ResearchLink(
                    "CNEP — empresas punidas",
                    f"https://portaldatransparencia.gov.br/sancoes/consulta?cadastro=2&cpfCnpj={cnpj}",
                    "Lei Anticorrupção (Lei 12.846/2013)",
                ),
                ResearchLink(
                    "Visão geral no Portal da Transparência",
                    f"https://portaldatransparencia.gov.br/pessoa-juridica/{cnpj}",
                    "Contratos, licitações e sanções",
                ),
            ],
        ),
        ResearchGroup(
            "oficiais",
            "Certidões e cadastros oficiais",
            "Sites do governo com captcha; nos que não aceitam o CNPJ no link, copie e cole.",
            [
                ResearchLink(
                    "Comprovante de CNPJ (Receita)",
                    comprovante_url(cnpj),
                    "Cartão CNPJ oficial · CNPJ já preenchido",
                ),
                ResearchLink(
                    "CND federal (Receita/PGFN)",
                    "https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cnpj",
                    "Débitos federais e dívida ativa",
                    manual=True,
                ),
                ResearchLink(
                    "CRF do FGTS (Caixa)",
                    "https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf",
                    "Regularidade do FGTS",
                    manual=True,
                ),
                ResearchLink(
                    "CNDT (TST)",
                    "https://cndt-certidao.tst.jus.br/gerarCertidao",
                    "Débitos trabalhistas",
                    manual=True,
                ),
                ResearchLink(
                    "Simples Nacional",
                    "https://consopt.www8.receita.fazenda.gov.br/consultaoptantes",
                    "Opção pelo Simples e SIMEI",
                    manual=True,
                ),
                ResearchLink(
                    "Inscrição estadual (CCC)",
                    CCC_PORTAL_URL,
                    "Todas as UFs · exige login gov.br",
                    manual=True,
                ),
                ResearchLink(
                    "Inscrição estadual (Sintegra)",
                    SINTEGRA_URL,
                    "Escolha a UF da empresa",
                    manual=True,
                ),
            ],
        ),
    ]

    presenca: list[ResearchLink] = []
    if addr := address_query(company):
        presenca += [
            ResearchLink(
                "Google Maps",
                f"https://www.google.com/maps/search/?api=1&query={_q(addr)}",
                "Fachada e arredores",
            ),
            ResearchLink(
                "OpenStreetMap", f"https://www.openstreetmap.org/search?query={_q(addr)}", "Mapa aberto"
            ),
        ]
    if domain := corporate_domain(company.email):
        presenca += [
            ResearchLink(f"Site {domain}", f"https://{domain}", "Domínio do e-mail cadastral"),
            ResearchLink(
                "Registro do domínio",
                f"https://registro.br/tecnologia/ferramentas/whois/?search={quote(domain)}",
                "Titular do domínio (.br)",
            )
            if domain.endswith(".br")
            else ResearchLink(
                "Registro do domínio", f"https://lookup.icann.org/pt/lookup?name={quote(domain)}", "WHOIS"
            ),
        ]
    presenca.append(
        ResearchLink(
            "LinkedIn",
            "https://www.google.com/search?q="
            + _q(f'site:linkedin.com/company "{company.nome_fantasia or name}"')
            + "&hl=pt-BR",
            "Página da empresa (sem precisar de login)",
        )
    )
    groups.append(
        ResearchGroup(
            "presenca",
            "Localização e presença digital",
            "Confirme se o endereço existe e se a empresa tem operação visível.",
            presenca,
        )
    )
    return groups


# ---------------------------------------------------------------------------
# Resultados na tela (provedor opcional)
# ---------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_CACHE_MAX = 256
_cache: dict[tuple[str, str], tuple[float, list[WebResult]]] = {}


def _clean(text: Any, limit: int) -> str:
    s = html.unescape(_TAG.sub("", str(text or "")))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _safe_result(title: Any, url: Any, snippet: Any) -> WebResult | None:
    """Descarta URLs que nao sejam http(s) (javascript:, data:...)."""
    url = str(url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    if not parts.hostname:
        return None
    # hostname, nao netloc: "https://google.com@evil.example" mostra evil.example
    domain = parts.hostname.lower().removeprefix("www.")
    return WebResult(
        titulo=_clean(title, 160) or domain, url=url, trecho=_clean(snippet, 320), dominio=domain
    )


def is_enabled(settings: Settings | None = None) -> bool:
    cfg = settings or get_settings()
    if cfg.web_search_provider in ("brave", "tavily"):
        return bool(cfg.web_search_api_key)
    if cfg.web_search_provider == "searxng":
        return bool(cfg.web_search_url)
    return False


def provider_label(settings: Settings | None = None) -> str | None:
    cfg = settings or get_settings()
    return PROVIDER_LABELS.get(cfg.web_search_provider) if is_enabled(cfg) else None


async def _brave(client: httpx.AsyncClient, cfg: Settings, query: str) -> list[WebResult]:
    resp = await client.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={
            "q": query,
            "count": max(1, min(cfg.web_search_max_results, 20)),
            "country": "BR",
            "search_lang": "pt-br",
            "safesearch": "moderate",
        },
        headers={"Accept": "application/json", "X-Subscription-Token": cfg.web_search_api_key},
    )
    resp.raise_for_status()
    items = ((resp.json() or {}).get("web") or {}).get("results") or []
    return [r for i in items if (r := _safe_result(i.get("title"), i.get("url"), i.get("description")))]


async def _tavily(client: httpx.AsyncClient, cfg: Settings, query: str) -> list[WebResult]:
    resp = await client.post(
        "https://api.tavily.com/search",
        json={
            "query": query,
            "max_results": max(1, min(cfg.web_search_max_results, 20)),
            "search_depth": "basic",
            "topic": "general",
        },
        headers={"Accept": "application/json", "Authorization": f"Bearer {cfg.web_search_api_key}"},
    )
    resp.raise_for_status()
    items = (resp.json() or {}).get("results") or []
    return [r for i in items if (r := _safe_result(i.get("title"), i.get("url"), i.get("content")))]


async def _searxng(client: httpx.AsyncClient, cfg: Settings, query: str) -> list[WebResult]:
    resp = await client.get(
        cfg.web_search_url.rstrip("/") + "/search",
        params={"q": query, "format": "json", "language": "pt-BR", "safesearch": 1},
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    items = (resp.json() or {}).get("results") or []
    return [r for i in items if (r := _safe_result(i.get("title"), i.get("url"), i.get("content")))]


def _error_message(exc: Exception, provider: str) -> str:
    label = PROVIDER_LABELS.get(provider, provider)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403, 422) and provider != "searxng":
            return f"{label} recusou a chave (HTTP {code}). Confira WEB_SEARCH_API_KEY no .env."
        if code == 403:
            return f"{label} recusou o formato JSON (HTTP 403). Habilite 'json' em search.formats."
        if code in (429, 432, 433):  # 432/433: cota do plano Tavily esgotada
            return f"{label}: limite de buscas do plano atingido (HTTP {code}). Tente mais tarde."
        return f"{label} indisponível (HTTP {code})."
    if isinstance(exc, httpx.TimeoutException):
        return f"{label} não respondeu a tempo."
    if isinstance(exc, httpx.HTTPError):
        return f"Falha de conexão com {label}."
    if isinstance(exc, ValueError):
        return f"{label} respondeu em formato inesperado (a saída JSON está habilitada?)."
    return f"Erro inesperado em {label}."


async def search_web(
    query: str,
    *,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> WebSearchResponse:
    """Busca `query` no provedor configurado. Nunca levanta excecao."""
    cfg = settings or get_settings()
    query = " ".join(query.split())[:300]
    if not is_enabled(cfg):
        return WebSearchResponse(habilitado=False, provedor=None, consulta=query)
    provider = cfg.web_search_provider
    resp = WebSearchResponse(
        habilitado=True,
        provedor=PROVIDER_LABELS[provider],
        consulta=query,
        atribuicao=PROVIDER_ATTRIBUTION.get(provider),
    )
    if not query:
        resp.erro = "Informe o que pesquisar."
        return resp

    key = (provider, query.lower())
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < cfg.web_search_cache_seconds:
        resp.resultados, resp.do_cache = hit[1], True
        return resp

    owned = client is None
    client = client or httpx.AsyncClient(
        timeout=float(cfg.request_timeout_seconds),
        follow_redirects=True,
        headers={"User-Agent": f"cnpj-consulta/{__version__}"},
    )
    try:
        fetch = {"brave": _brave, "tavily": _tavily}.get(provider, _searxng)
        results = (await fetch(client, cfg, query))[: cfg.web_search_max_results]
    except Exception as e:  # noqa: BLE001 -- vira mensagem para o usuario
        log.warning("pesquisa na internet falhou (%s): %s", provider, type(e).__name__)
        resp.erro = _error_message(e, provider)
        return resp
    finally:
        if owned:
            await client.aclose()
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))  # descarta a entrada mais antiga
    _cache[key] = (time.monotonic(), results)
    resp.resultados = results
    return resp


def clear_web_cache() -> None:
    _cache.clear()
