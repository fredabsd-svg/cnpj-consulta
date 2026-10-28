"""Descoberta da versao e dos arquivos no portal OFICIAL da Receita Federal.

Desde 2025 os dados abertos do CNPJ ficam num compartilhamento publico do
portal de arquivos da Receita (Nextcloud), em pastas AAAA-MM. Nada aqui e
fixo alem do endereco do portal (configuravel em RECEITA_PORTAL_URL):

1. O portal redireciona para o compartilhamento publico (/s/<token>); o token
   e lido do proprio redirecionamento.
2. A listagem usa o WebDAV publico do Nextcloud (PROPFIND), o mesmo que o
   navegador usa para mostrar a pasta, autenticando com o token publico como
   usuario e senha vazia -- e o mecanismo documentado de acesso a links
   publicos, nao um contorno de controle de acesso.
3. A pasta do CNPJ (RECEITA_PASTA_CNPJ, padrao Dados/Cadastros/CNPJ) e
   validada; se tiver mudado, procura-se uma pasta "CNPJ" com subpastas AAAA-MM.
4. A versao mais recente e a maior pasta AAAA-MM que tenha TODOS os grupos de
   arquivos (uma pasta ainda em publicacao fica de fora, com aviso).

Para cada arquivo o servidor informa tamanho, ETag e data; o ETag identifica
a versao do arquivo (nao e hash do conteudo) e e usado no If-Range para que
um download retomado nunca misture duas versoes do mesmo arquivo.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from app.config import Settings, get_settings
from app.receita.layout import MES_RE, grupo_do_arquivo, grupos_faltando

log = logging.getLogger(__name__)

_DAV = "{DAV:}"
_PROPFIND_BODY = (
    '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:getcontentlength/><d:getetag/><d:getlastmodified/><d:resourcetype/>"
    "<d:getcontenttype/></d:prop></d:propfind>"
)
_MAX_XML = 5 * 1024 * 1024  # listagem de pasta; bem acima do real (~20 KB)
_TOKEN_RE = re.compile(r"/s/([A-Za-z0-9]{8,64})(?:[/?#]|$)")


class FonteError(RuntimeError):
    """Falha ao consultar o portal oficial (mensagem pronta para o usuario)."""


@dataclass
class ArquivoRemoto:
    nome: str
    url: str
    tamanho: int
    etag: str
    modificado: str  # ISO 8601
    grupo: str


@dataclass
class VersaoRemota:
    mes: str
    modificado: str
    completa: bool = True
    faltando: list[str] = field(default_factory=list)
    arquivos: list[ArquivoRemoto] = field(default_factory=list)
    ignorados: list[str] = field(default_factory=list)

    @property
    def tamanho_total(self) -> int:
        return sum(a.tamanho for a in self.arquivos)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["tamanho_total"] = self.tamanho_total
        return d


@dataclass
class _Entrada:
    href: str
    nome: str
    pasta: bool
    tamanho: int
    etag: str
    modificado: str


def _iso(http_date: str | None) -> str:
    if not http_date:
        return ""
    try:
        return parsedate_to_datetime(http_date).isoformat()
    except (TypeError, ValueError):
        return ""


def parse_propfind(xml_text: str | bytes) -> list[_Entrada]:
    """Entradas de uma resposta PROPFIND (a primeira e a propria pasta)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise FonteError(f"Resposta inesperada do portal da Receita (XML invalido: {e})") from e
    out: list[_Entrada] = []
    for resp in root.iter(f"{_DAV}response"):
        href = (resp.findtext(f"{_DAV}href") or "").strip()
        if not href:
            continue
        props: dict[str, ET.Element] = {}
        for ps in resp.iter(f"{_DAV}propstat"):
            if "200" not in (ps.findtext(f"{_DAV}status") or ""):
                continue
            prop = ps.find(f"{_DAV}prop")
            if prop is not None:
                for child in prop:
                    props[child.tag.removeprefix(_DAV)] = child
        rt = props.get("resourcetype")
        pasta = rt is not None and rt.find(f"{_DAV}collection") is not None
        try:
            tamanho = int((props["getcontentlength"].text or "0").strip()) if "getcontentlength" in props else 0
        except ValueError:
            tamanho = 0
        etag = (props["getetag"].text or "").strip().strip('"') if "getetag" in props else ""
        mod = _iso(props["getlastmodified"].text) if "getlastmodified" in props else ""
        nome = unquote(href.rstrip("/").rsplit("/", 1)[-1])
        out.append(_Entrada(href=href, nome=nome, pasta=pasta, tamanho=tamanho, etag=etag, modificado=mod))
    return out


class FonteReceita:
    """Cliente do compartilhamento publico oficial (somente leitura)."""

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._own_client = client is None
        self.token: str = self.settings.receita_share_token.strip()
        self.origem = ""
        self.webdav_base = ""  # .../public.php/webdav/
        self.pasta_cnpj = ""  # caminho relativo confirmado

    # ---------- infraestrutura ----------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(30, read=120),
                headers={"User-Agent": "cnpj-consulta (atualizacao da base local)"},
            )
        return self._client

    def close(self) -> None:
        if self._own_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> FonteReceita:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def auth(self) -> tuple[str, str]:
        return (self.token, "")

    # ---------- descoberta ----------

    def descobrir_compartilhamento(self) -> None:
        """Le o token do redirecionamento do portal oficial."""
        portal = self.settings.receita_portal_url.strip()
        parts = urlsplit(portal)
        if parts.scheme != "https" or not parts.netloc.endswith(".gov.br"):
            raise FonteError(
                f"RECEITA_PORTAL_URL={portal!r} precisa ser um endereco https oficial (.gov.br)."
            )
        self.origem = f"{parts.scheme}://{parts.netloc}"
        if not self.token:
            try:
                resp = self.client.get(portal, follow_redirects=False)
            except httpx.HTTPError as e:
                raise FonteError(f"Portal da Receita indisponivel ({type(e).__name__}): {e}") from e
            candidatos = [resp.headers.get("location", "")]
            if resp.status_code == 200:
                candidatos.append(resp.text[:200_000])
            for texto in candidatos:
                if texto and (m := _TOKEN_RE.search(texto)):
                    self.token = m.group(1)
                    break
            if not self.token:
                raise FonteError(
                    "Nao foi possivel localizar o compartilhamento de dados abertos no portal "
                    f"{portal} (HTTP {resp.status_code}). Verifique o endereco no site da Receita."
                )
        self.webdav_base = f"{self.origem}/public.php/webdav/"

    def _propfind(self, rel: str, depth: str = "1") -> list[_Entrada]:
        url = urljoin(self.webdav_base, quote(rel.strip("/") + "/" if rel.strip("/") else ""))
        try:
            with self.client.stream(
                "PROPFIND",
                url,
                auth=self.auth,
                headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
                content=_PROPFIND_BODY,
            ) as resp:
                if resp.status_code == 404:
                    return []
                if resp.status_code in (401, 403):
                    raise FonteError(
                        f"O portal recusou a listagem (HTTP {resp.status_code}). O link publico pode "
                        "ter mudado; confira RECEITA_PORTAL_URL ou RECEITA_SHARE_TOKEN."
                    )
                if resp.status_code != 207:
                    raise FonteError(f"Listagem do portal falhou: HTTP {resp.status_code} em {rel or '/'}")
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > _MAX_XML:
                        raise FonteError("Listagem do portal grande demais; abortado por seguranca.")
        except httpx.HTTPError as e:
            raise FonteError(f"Falha de conexao com o portal da Receita ({type(e).__name__}): {e}") from e
        return parse_propfind(body)[1:]  # [0] e a propria pasta

    def _tem_meses(self, rel: str) -> bool:
        return any(e.pasta and MES_RE.match(e.nome) for e in self._propfind(rel))

    def localizar_pasta_cnpj(self) -> str:
        if not self.webdav_base:
            self.descobrir_compartilhamento()
        preferida = self.settings.receita_pasta_cnpj.strip("/")
        if preferida and self._tem_meses(preferida):
            self.pasta_cnpj = preferida
            return preferida
        # Busca em largura limitada (3 niveis) por uma pasta CNPJ com AAAA-MM.
        fila, visitados = [""], 0
        while fila and visitados < 60:
            rel = fila.pop(0)
            visitados += 1
            for e in self._propfind(rel):
                if not e.pasta:
                    continue
                filho = f"{rel}/{e.nome}".strip("/")
                if e.nome.upper() == "CNPJ" and self._tem_meses(filho):
                    log.warning("pasta do CNPJ mudou para %s (configurada: %s)", filho, preferida)
                    self.pasta_cnpj = filho
                    return filho
                if filho.count("/") < 2:
                    fila.append(filho)
        raise FonteError(
            "Pasta dos dados abertos do CNPJ nao encontrada no portal da Receita. "
            "Ajuste RECEITA_PASTA_CNPJ conforme o site oficial."
        )

    # ---------- versoes e arquivos ----------

    def listar_meses(self) -> list[tuple[str, str]]:
        """[(AAAA-MM, modificado ISO)] em ordem crescente."""
        if not self.pasta_cnpj:
            self.localizar_pasta_cnpj()
        meses = [(e.nome, e.modificado) for e in self._propfind(self.pasta_cnpj) if e.pasta and MES_RE.match(e.nome)]
        return sorted(meses)

    def listar_versao(self, mes: str) -> VersaoRemota:
        if not MES_RE.match(mes):
            raise ValueError("Mes deve estar no formato AAAA-MM (ex.: 2026-10)")
        if not self.pasta_cnpj:
            self.localizar_pasta_cnpj()
        entradas = self._propfind(f"{self.pasta_cnpj}/{mes}")
        if not entradas:
            raise FonteError(f"A versao {mes} nao existe (ainda) no portal da Receita.")
        arquivos, ignorados = [], []
        for e in entradas:
            if e.pasta:
                continue
            g = grupo_do_arquivo(e.nome)
            if g is None:
                ignorados.append(e.nome)
                continue
            arquivos.append(
                ArquivoRemoto(
                    nome=e.nome,
                    url=urljoin(self.origem, e.href),
                    tamanho=e.tamanho,
                    etag=e.etag,
                    modificado=e.modificado,
                    grupo=g.nome,
                )
            )
        faltando = grupos_faltando([a.nome for a in arquivos])
        mod = max((a.modificado for a in arquivos), default="")
        return VersaoRemota(
            mes=mes,
            modificado=mod,
            completa=not faltando,
            faltando=faltando,
            arquivos=sorted(arquivos, key=lambda a: a.nome),
            ignorados=sorted(ignorados),
        )

    def versao_mais_recente(self) -> VersaoRemota:
        """Maior AAAA-MM com todos os grupos (pasta em publicacao fica de fora)."""
        meses = self.listar_meses()
        if not meses:
            raise FonteError("Nenhuma versao AAAA-MM encontrada no portal da Receita.")
        incompletas: list[str] = []
        for mes, _ in reversed(meses[-3:]):
            v = self.listar_versao(mes)
            if v.completa:
                if incompletas:
                    log.warning("versoes incompletas ignoradas: %s", ", ".join(incompletas))
                return v
            incompletas.append(mes)
        raise FonteError(
            "As versoes mais recentes do portal estao incompletas: " + ", ".join(incompletas)
        )
