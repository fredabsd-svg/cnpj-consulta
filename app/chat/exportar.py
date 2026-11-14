"""Exportacao das conversas do chat com IA em PDF e Excel.

Exporta a conversa inteira ou um turno (uma pergunta e tudo o que veio depois
dela ate a pergunta seguinte). O markdown das respostas e interpretado aqui do
mesmo jeito que a tela (chat.js): titulos, paragrafos, listas, citacoes, codigo
e tabelas.

- PDF (reportlab): relatorio A4 com cabecalho e rodape em todas as paginas
  ("Pagina X de Y"), pergunta destacada, tabelas com cabecalho repetido a cada
  pagina, colunas numericas alinhadas a direita e paisagem quando ha tabela larga.
- Excel (openpyxl): aba "Conversa" com o texto e uma aba por tabela das
  respostas, com cabecalho congelado, setas de filtro, larguras ajustadas e
  numeros, valores, percentuais e datas como valores de verdade.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from unidecode import unidecode

# Paleta (a mesma do app: teal do tema claro + tinta escura do logo)
TEAL = "#0E7490"
TEAL_ESCURO = "#155E75"
TEAL_CLARO = "#ECF7FA"
TINTA = "#0F172A"
TEXTO = "#1E293B"
MUTED = "#64748B"
FAINT = "#94A3B8"
BORDA = "#E2E8F0"
ZEBRA = "#F8FAFC"
AVISO_FUNDO = "#FFFBEB"
AVISO_TEXTO = "#92400E"
AVISO_BORDA = "#F59E0B"

AVISO_IA = "Conteúdo gerado por IA a partir dos dados do app. Confira as informações nas fontes oficiais."


# ---------------------------------------------------------------- conversa em turnos


@dataclass
class Turno:
    numero: int
    pergunta: str
    itens: list[tuple[str, str]] = field(default_factory=list)  # ("assistente" | "aviso", texto)
    ferramentas: list[str] = field(default_factory=list)

    @property
    def tem_resposta(self) -> bool:
        return any(t == "assistente" and x.strip() for t, x in self.itens)


def turnos(exibicao: list[dict]) -> list[Turno]:
    """Agrupa a exibicao da conversa por pergunta do usuario (numeradas a partir de 1)."""
    lista: list[Turno] = []
    for it in exibicao or []:
        tipo = it.get("tipo")
        if tipo == "usuario":
            lista.append(Turno(len(lista) + 1, str(it.get("texto") or "")))
        elif not lista:
            continue
        elif tipo in ("assistente", "aviso") and str(it.get("texto") or "").strip():
            lista[-1].itens.append((tipo, str(it["texto"])))
        elif tipo == "ferramenta" and it.get("nome") and it["nome"] not in lista[-1].ferramentas:
            lista[-1].ferramentas.append(str(it["nome"]))
    return lista


# ---------------------------------------------------------------- markdown (igual ao chat.js)


@dataclass
class Bloco:
    tipo: str  # titulo | paragrafo | lista | tabela | codigo | citacao | regra
    texto: str = ""
    nivel: int = 0
    ordenada: bool = False
    itens: list[str] = field(default_factory=list)
    cabecalho: list[str] = field(default_factory=list)
    linhas: list[list[str]] = field(default_factory=list)


_RE_CERCA = re.compile(r"^\s*```")
_RE_TAB = re.compile(r"^\s*\|")
_RE_TAB_SEP = re.compile(r"^\s*\|?\s*:?-{2,}")
_RE_TITULO = re.compile(r"^(#{1,6})\s+(.*)$")
_RE_REGRA = re.compile(r"^\s*(---|\*\*\*)\s*$")
_RE_CITACAO = re.compile(r"^\s*>")
_RE_MARCADOR = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_RE_FIM_PARAGRAFO = re.compile(r"^\s*(```|\||#{1,6}\s|>|[-*+]\s|\d+[.)]\s)")


def _celulas(linha: str) -> list[str]:
    s = linha.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") else s
    return [c.strip() for c in s.split("|")]


def blocos(texto: str) -> list[Bloco]:
    linhas = (texto or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[Bloco] = []
    i, n = 0, len(linhas)
    while i < n:
        lin = linhas[i]
        if _RE_CERCA.match(lin):
            corpo = []
            i += 1
            while i < n and not _RE_CERCA.match(linhas[i]):
                corpo.append(linhas[i])
                i += 1
            i += 1
            out.append(Bloco("codigo", "\n".join(corpo)))
            continue
        if _RE_TAB.match(lin) and i + 1 < n and _RE_TAB_SEP.match(linhas[i + 1]):
            cab = _celulas(lin)
            i += 2
            corpo_t: list[list[str]] = []
            while i < n and _RE_TAB.match(linhas[i]):
                cel = _celulas(linhas[i])
                corpo_t.append((cel + [""] * len(cab))[:len(cab)])
                i += 1
            out.append(Bloco("tabela", cabecalho=cab, linhas=corpo_t))
            continue
        m = _RE_TITULO.match(lin)
        if m:
            out.append(Bloco("titulo", m.group(2).strip(), nivel=len(m.group(1))))
            i += 1
            continue
        if _RE_REGRA.match(lin):
            out.append(Bloco("regra"))
            i += 1
            continue
        if _RE_CITACAO.match(lin):
            partes = []
            while i < n and _RE_CITACAO.match(linhas[i]):
                partes.append(re.sub(r"^\s*>\s?", "", linhas[i]))
                i += 1
            out.append(Bloco("citacao", " ".join(partes)))
            continue
        if _RE_MARCADOR.match(lin):
            ordenada = bool(re.match(r"^\s*\d", lin))
            itens = []
            while i < n and _RE_MARCADOR.match(linhas[i]):
                itens.append(_RE_MARCADOR.sub("", linhas[i], count=1))
                i += 1
            out.append(Bloco("lista", ordenada=ordenada, itens=itens))
            continue
        if not lin.strip():
            i += 1
            continue
        partes = []
        while i < n and linhas[i].strip() and not _RE_FIM_PARAGRAFO.match(linhas[i]):
            partes.append(linhas[i])
            i += 1
        if not partes:
            partes.append(linhas[i])
            i += 1
        out.append(Bloco("paragrafo", "\n".join(partes)))
    return out


_INLINE = re.compile(
    r"(`[^`\n]+`)|(\*\*[^*\n]+\*\*|__[^_\n]+__)|(\[[^\]\n]+\]\([^)\s]+\))|(\*[^*\s\n][^*\n]*\*)"
    r"|(https?://[^\s<>()]+[^\s<>().,;:!?'\"])"
)


def texto_puro(texto: str) -> str:
    """Tira as marcas de markdown (negrito, codigo, links) e deixa so o texto."""
    def troca(m: re.Match) -> str:
        code, negrito, link, italico, url = m.groups()
        if code:
            return code[1:-1]
        if negrito:
            return texto_puro(negrito[2:-2])
        if italico:
            return texto_puro(italico[1:-1])
        if link:
            return link[1:link.index("](")]
        return url
    return _INLINE.sub(troca, texto or "")


# ---------------------------------------------------------------- utilitarios


def _agora() -> datetime:
    return datetime.now()


def _slug(texto: str, limite: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", unidecode(texto or "").lower()).strip("-")
    return (s[:limite].rstrip("-")) or "conversa"


def nome_arquivo(conversa: dict, formato: str, turno: int | None = None, quando: datetime | None = None) -> str:
    quando = quando or _agora()
    partes = ["consulta-ia", _slug(conversa.get("titulo") or "")]
    if turno:
        partes.append(f"resposta-{turno}")
    return "_".join(partes) + f"_{quando:%Y-%m-%d_%H%M}.{formato}"


def _nome_provedor(pid: str) -> str:
    from app.chat import provedores

    p = provedores.PRESETS.get(pid)
    return p.nome if p else pid


def _nome_modelo(mid: str) -> str:
    from app.chat import provedores

    return provedores.nome_modelo(mid) or mid


def _selecionar(conversa: dict, turno: int | None) -> list[Turno]:
    todos = [t for t in turnos(conversa.get("exibicao") or []) if t.tem_resposta or t.itens]
    if turno is None:
        if not todos:
            raise ValueError("Esta conversa ainda não tem respostas para exportar.")
        return todos
    escolhido = [t for t in todos if t.numero == turno]
    if not escolhido:
        raise LookupError("Resposta não encontrada nesta conversa.")
    return escolhido


# Colunas que guardam identificadores: ficam como texto mesmo parecendo numero.
_IDENTIFICADORES = {"cnpj", "cpf", "cep", "telefone", "telefones", "fone", "celular", "codigo", "cod", "cnae",
                    "ie", "inscricao", "id", "nire", "matricula", "raiz", "ddd", "whatsapp", "numero", "n", "no"}
_RE_NUM = re.compile(r"^-?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?$")
_RE_DATA = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def _coluna_identificador(cabecalho: str) -> bool:
    tokens = set(re.split(r"[^a-z0-9]+", unidecode(cabecalho or "").lower()))
    return bool(tokens & _IDENTIFICADORES)


def converter_valor(texto: str, cabecalho: str = "") -> tuple[Any, str | None]:
    """Texto da celula -> (valor, formato do Excel). Numeros em pt-BR viram numero."""
    s = (texto or "").strip()
    if not s or _coluna_identificador(cabecalho):
        return s, None
    m = _RE_DATA.match(s)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))), "DD/MM/YYYY"
        except ValueError:
            return s, None
    moeda = s.upper().startswith("R$")
    pct = s.endswith("%")
    nucleo = s[2:].strip() if moeda else s
    nucleo = nucleo[:-1].strip() if pct else nucleo
    if not _RE_NUM.match(nucleo):
        return s, None
    inteiro = nucleo.lstrip("-").split(",")[0].replace(".", "")
    if len(inteiro) > 1 and inteiro.startswith("0"):
        return s, None  # codigo com zero a esquerda
    if "." not in nucleo and "," not in nucleo and len(inteiro) >= 8:
        return s, None  # sequencia longa de digitos: mais provavel identificador
    if len(inteiro) > 15:
        return s, None
    decimais = len(nucleo.split(",")[1]) if "," in nucleo else 0
    valor = float(nucleo.replace(".", "").replace(",", ".")) if decimais else int(nucleo.replace(".", ""))
    if moeda:
        return valor, '"R$" #,##0.00;-"R$" #,##0.00'
    if pct:
        return valor / 100, "0" + ("." + "0" * min(decimais, 4) if decimais else "") + "%"
    if decimais:
        return valor, "#,##0." + "0" * min(decimais, 6)
    return valor, "#,##0"


def _parece_numero(texto: str, cabecalho: str) -> bool:
    v, fmt = converter_valor(texto_puro(texto), cabecalho)
    return fmt is not None and fmt != "DD/MM/YYYY"


def _coluna_numerica(cabecalho: str, valores: list[str]) -> bool:
    cheios = [v for v in valores if v.strip() and v.strip() not in ("-", "—")]
    if not cheios:
        return False
    return sum(_parece_numero(v, cabecalho) for v in cheios) / len(cheios) >= 0.8


# ---------------------------------------------------------------- PDF


@dataclass(frozen=True)
class _Fontes:
    normal: str
    negrito: str
    italico: str
    mono: str
    unicode: bool
    cobertura: frozenset[int] | None = field(repr=False)  # codigos que a fonte desenha (None = Latin-1/cp1252)


def _dirs_fontes() -> list[Path]:
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    return [Path(windir) / "Fonts", Path("/usr/share/fonts/truetype/liberation"),
            Path("/usr/share/fonts/truetype/liberation2"), Path("/usr/share/fonts/liberation-sans"),
            Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu"),
            Path("/System/Library/Fonts/Supplemental"), Path("/Library/Fonts")]


# Familias sem serifa comuns em Windows, Linux e macOS (na ordem de preferencia).
_FAMILIAS = [
    ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf"),
    ("arial.ttf", "arialbd.ttf", "ariali.ttf"),
    ("Arial.ttf", "Arial Bold.ttf", "Arial Italic.ttf"),
    ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf", "LiberationSans-Italic.ttf"),
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf"),
]
_MONO = ["consola.ttf", "LiberationMono-Regular.ttf", "DejaVuSansMono.ttf", "Courier New.ttf", "cour.ttf"]


def _achar(nome: str) -> Path | None:
    for d in _dirs_fontes():
        p = d / nome
        if p.is_file():
            return p
    return None


@lru_cache(maxsize=1)
def _fontes() -> _Fontes:
    """Fonte TrueType do sistema (acentos e simbolos); sem ela, Helvetica embutida."""
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for normal, negrito, italico in _FAMILIAS:
        pn, pb, pi = _achar(normal), _achar(negrito), _achar(italico)
        if not (pn and pb):
            continue
        try:
            fn = TTFont("CCNormal", str(pn))
            pdfmetrics.registerFont(fn)
            pdfmetrics.registerFont(TTFont("CCNegrito", str(pb)))
            pdfmetrics.registerFont(TTFont("CCItalico", str(pi or pn)))
            pdfmetrics.registerFont(TTFont("CCNegritoItalico", str(pb)))
            addMapping("CCNormal", 0, 0, "CCNormal")
            addMapping("CCNormal", 1, 0, "CCNegrito")
            addMapping("CCNormal", 0, 1, "CCItalico")
            addMapping("CCNormal", 1, 1, "CCNegritoItalico")
            mono = "Courier"
            for nome_mono in _MONO:
                pm = _achar(nome_mono)
                if pm:
                    try:
                        pdfmetrics.registerFont(TTFont("CCMono", str(pm)))
                        mono = "CCMono"
                        break
                    except Exception:  # noqa: BLE001 -- fonte quebrada: tenta a proxima
                        continue
            cobertura = frozenset(getattr(fn.face, "charToGlyph", {}).keys()) or None
            return _Fontes("CCNormal", "CCNegrito", "CCItalico", mono, True, cobertura)
        except Exception:  # noqa: BLE001 -- fonte ilegivel: tenta a proxima familia
            continue
    return _Fontes("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Courier", False, None)


_TROCAS = {"→": "->", "←": "<-", "⇒": "=>", "≥": ">=", "≤": "<=", "≠": "!=", "✓": "OK", "✔": "OK",
           "✅": "OK", "❌": "X", "✗": "X", "⚠": "!", "️": "", "\u200b": "", "\u00a0": " "}


def _limpar(texto: str) -> str:
    """Troca caracteres que a fonte do PDF nao desenha (emoji, setas raras)."""
    f = _fontes()
    out = []
    for ch in texto:
        if ch in "\n\t":
            out.append(ch)
            continue
        if f.cobertura is not None:
            if ord(ch) in f.cobertura:
                out.append(ch)
                continue
        else:
            try:
                ch.encode("cp1252")
                out.append(ch)
                continue
            except UnicodeEncodeError:
                pass
        out.append(_TROCAS.get(ch, ""))
    return "".join(out)


_RE_MOEDA = re.compile(r"R\$ (?=-?\d)")


def _inline_pdf(texto: str) -> str:
    """Markdown em linha -> marcacao de paragrafo do reportlab (tudo escapado)."""
    f = _fontes()
    texto = _RE_MOEDA.sub("R$\u00a0", texto)  # "R$ 1.250,00" nunca quebra no meio
    partes: list[str] = []
    ultimo = 0
    for m in _INLINE.finditer(texto):
        if m.start() > ultimo:
            partes.append(escape(_limpar(texto[ultimo:m.start()])))
        code, negrito, link, italico, url = m.groups()
        if code:
            partes.append(f'<font name="{f.mono}" backColor="#F1F5F9">'
                          f'{escape(_limpar(code[1:-1]))}</font>')
        elif negrito:
            partes.append(f"<b>{_inline_pdf(negrito[2:-2])}</b>")
        elif italico:
            partes.append(f"<i>{_inline_pdf(italico[1:-1])}</i>")
        else:
            rotulo, alvo = url, url
            if link:
                k = link.index("](")
                rotulo, alvo = link[1:k], link[k + 2:-1]
            if re.match(r"^https?://[^/\\\s]", alvo or "", re.I):
                partes.append(f'<a href="{escape(alvo, {chr(34): "&quot;"})}" color="{TEAL}">'
                              f"{escape(_limpar(rotulo))}</a>")
            else:
                partes.append(escape(_limpar(rotulo)))
        ultimo = m.end()
    if ultimo < len(texto):
        partes.append(escape(_limpar(texto[ultimo:])))
    return "".join(partes)


def _estilos() -> dict[str, Any]:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle

    f = _fontes()
    c = colors.HexColor
    base = ParagraphStyle("base", fontName=f.normal, fontSize=9.5, leading=14, textColor=c(TEXTO),
                          spaceAfter=5, alignment=TA_LEFT)
    return {
        "base": base,
        "olho": ParagraphStyle("olho", parent=base, fontName=f.negrito, fontSize=7.5, leading=10,
                               textColor=c(TEAL), spaceAfter=3),
        "titulo": ParagraphStyle("titulo", parent=base, fontName=f.negrito, fontSize=19, leading=24,
                                 textColor=c(TINTA), spaceAfter=4),
        "meta": ParagraphStyle("meta", parent=base, fontSize=8.5, leading=12, textColor=c(MUTED), spaceAfter=0),
        "rotulo": ParagraphStyle("rotulo", parent=base, fontName=f.negrito, fontSize=7.5, leading=10,
                                 textColor=c(TEAL), spaceAfter=4, spaceBefore=0),
        "pergunta": ParagraphStyle("pergunta", parent=base, fontName=f.negrito, fontSize=10.5, leading=15,
                                   textColor=c(TINTA), spaceAfter=0),
        "h1": ParagraphStyle("h1", parent=base, fontName=f.negrito, fontSize=13.5, leading=18,
                             textColor=c(TINTA), spaceBefore=8, spaceAfter=5),
        "h2": ParagraphStyle("h2", parent=base, fontName=f.negrito, fontSize=12, leading=16,
                             textColor=c(TINTA), spaceBefore=7, spaceAfter=4),
        "h3": ParagraphStyle("h3", parent=base, fontName=f.negrito, fontSize=10.5, leading=14,
                             textColor=c(TEAL_ESCURO), spaceBefore=6, spaceAfter=3),
        "item": ParagraphStyle("item", parent=base, spaceAfter=2),
        "citacao": ParagraphStyle("citacao", parent=base, fontName=f.italico, textColor=c(MUTED), spaceAfter=0),
        "codigo": ParagraphStyle("codigo", parent=base, fontName=f.mono, fontSize=8, leading=11,
                                 textColor=c(TINTA), spaceAfter=0),
        "aviso": ParagraphStyle("aviso", parent=base, fontSize=8.5, leading=12, textColor=c(AVISO_TEXTO),
                                spaceAfter=0),
        "ferramentas": ParagraphStyle("ferramentas", parent=base, fontSize=7.5, leading=10,
                                      textColor=c(FAINT), spaceBefore=2, spaceAfter=0),
        "cel": ParagraphStyle("cel", parent=base, fontSize=8, leading=10.5, spaceAfter=0),
        "cel_dir": ParagraphStyle("cel_dir", parent=base, fontSize=8, leading=10.5, spaceAfter=0,
                                  alignment=TA_RIGHT),
        "cab": ParagraphStyle("cab", parent=base, fontName=f.negrito, fontSize=8, leading=10.5,
                              textColor=colors.white, spaceAfter=0),
        "cab_dir": ParagraphStyle("cab_dir", parent=base, fontName=f.negrito, fontSize=8, leading=10.5,
                                  textColor=colors.white, spaceAfter=0, alignment=TA_RIGHT),
    }


def _caixa(conteudo: list, largura: float, fundo: str, borda: str, *, pad: float = 8) -> Any:
    """Bloco com fundo claro e filete colorido a esquerda (pergunta, aviso, codigo)."""
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    t = Table([[conteudo]], colWidths=[largura])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(fundo)),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, colors.HexColor(borda)),
        ("LEFTPADDING", (0, 0), (-1, -1), pad + 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), pad),
        ("TOPPADDING", (0, 0), (-1, -1), pad - 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
    ]))
    t.hAlign = "LEFT"
    return t


def _larguras_colunas(b: Bloco, largura: float) -> list[float]:
    """Larguras medidas com a fonte: nenhuma palavra e quebrada no meio (se couber) e
    o espaco que sobra vai para as colunas de texto mais longo."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    f = _fontes()
    pad = 11.0  # 5 + 5 de respiro e 1 de folga
    ncol = len(b.cabecalho)
    natural, minimo = [], []
    for j in range(ncol):
        cab = _limpar(texto_puro(b.cabecalho[j]))
        cels = [_limpar(_RE_MOEDA.sub("R$\u00a0", texto_puro(ln[j]))) for ln in b.linhas[:300]]
        nat = max([stringWidth(cab, f.negrito, 8)] + [stringWidth(c, f.normal, 8) for c in cels])
        pal = max([stringWidth(w, f.negrito, 8) for w in cab.split(" ")] or [0])
        pal = max([pal] + [stringWidth(w, f.normal, 8) for c in cels for w in c.split(" ")])
        natural.append(min(nat, 260.0) + pad)
        minimo.append(min(pal, 130.0) + pad)
    soma_min = sum(minimo)
    if soma_min >= largura:  # tabela larga demais: reparte proporcional ao minimo
        return [largura * m / soma_min for m in minimo]
    if sum(natural) <= largura:  # cabe tudo numa linha: estica proporcionalmente
        return [largura * n / sum(natural) for n in natural]
    extra = [n - m for n, m in zip(natural, minimo, strict=True)]
    sobra = largura - soma_min
    return [m + sobra * e / (sum(extra) or 1) for m, e in zip(minimo, extra, strict=True)]


def _tabela_pdf(b: Bloco, largura: float, est: dict) -> Any:
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle

    ncol = max(1, len(b.cabecalho))
    numericas = [_coluna_numerica(b.cabecalho[j], [ln[j] for ln in b.linhas]) for j in range(ncol)]
    larguras = _larguras_colunas(b, largura)

    def cel(txt: str, j: int, cab: bool = False) -> Paragraph:
        nome = ("cab" if cab else "cel") + ("_dir" if numericas[j] else "")
        return Paragraph(_inline_pdf(txt), est[nome])

    dados = [[cel(h, j, True) for j, h in enumerate(b.cabecalho)]]
    dados += [[cel(v, j) for j, v in enumerate(ln)] for ln in b.linhas]
    t = Table(dados, colWidths=larguras, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(TEAL)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor(ZEBRA)]),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor(BORDA)),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5.5),
    ]))
    return t


def _markdown_pdf(texto: str, largura: float, est: dict) -> list:
    from reportlab.lib import colors
    from reportlab.platypus import HRFlowable, ListFlowable, ListItem, Paragraph, Spacer

    f = _fontes()
    out: list = []
    for b in blocos(texto):
        if b.tipo == "titulo":
            out.append(Paragraph(_inline_pdf(b.texto), est["h1" if b.nivel == 1 else "h2" if b.nivel == 2 else "h3"]))
        elif b.tipo == "paragrafo":
            out.append(Paragraph("<br/>".join(_inline_pdf(p) for p in b.texto.split("\n")), est["base"]))
        elif b.tipo == "lista":
            itens = [ListItem(Paragraph(_inline_pdf(x), est["item"]), leftIndent=14) for x in b.itens]
            out.append(ListFlowable(
                itens, bulletType="1" if b.ordenada else "bullet", start="1" if b.ordenada else "•",
                leftIndent=14, bulletFontName=f.negrito if b.ordenada else f.normal,
                bulletFontSize=8.5 if b.ordenada else 9, bulletColor=colors.HexColor(TEAL),
                bulletFormat="%s." if b.ordenada else None, spaceAfter=5))
        elif b.tipo == "tabela":
            out += [Spacer(1, 3), _tabela_pdf(b, largura, est), Spacer(1, 8)]
        elif b.tipo == "codigo":
            linhas = [escape(_limpar(lin)).replace("  ", "&nbsp; ") for lin in b.texto.split("\n")]
            out += [_caixa([Paragraph("<br/>".join(linhas) or "&nbsp;", est["codigo"])], largura, ZEBRA, "#CBD5E1"),
                    Spacer(1, 6)]
        elif b.tipo == "citacao":
            out += [_caixa([Paragraph(_inline_pdf(b.texto), est["citacao"])], largura, "#FFFFFF", "#CBD5E1", pad=4),
                    Spacer(1, 6)]
        elif b.tipo == "regra":
            out.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor(BORDA),
                                  spaceBefore=4, spaceAfter=8))
    return out


def _desenhar_marca(canv: Any, x: float, y: float, tam: float) -> None:
    """Logo do app (lupa com o visto) desenhado em vetor: nitido em qualquer zoom."""
    from reportlab.lib import colors

    k = tam / 64.0
    canv.saveState()
    canv.setFillColor(colors.HexColor("#0F1424"))
    canv.roundRect(x, y, tam, tam, 16 * k, stroke=0, fill=1)
    canv.setLineCap(1)
    canv.setLineJoin(1)
    canv.setStrokeColor(colors.HexColor("#22D3EE"))
    canv.setLineWidth(6 * k)
    canv.circle(x + 36 * k, y + (64 - 28) * k, 14 * k, stroke=1, fill=0)
    canv.setStrokeColor(colors.HexColor("#6D8CF5"))
    canv.setLineWidth(7 * k)
    canv.line(x + 25.5 * k, y + (64 - 38.5) * k, x + 14.5 * k, y + (64 - 49.5) * k)
    canv.setStrokeColor(colors.HexColor("#34D399"))
    canv.setLineWidth(3.6 * k)
    p = canv.beginPath()
    p.moveTo(x + 29.6 * k, y + (64 - 28.2) * k)
    p.lineTo(x + 34.2 * k, y + (64 - 32.8) * k)
    p.lineTo(x + 42.4 * k, y + (64 - 23.2) * k)
    canv.drawPath(p, stroke=1, fill=0)
    canv.restoreState()


def gerar_pdf(conversa: dict, turno: int | None = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        HRFlowable,
        KeepTogether,
        PageTemplate,
        Paragraph,
        Spacer,
    )

    selecionados = _selecionar(conversa, turno)
    f = _fontes()
    est = _estilos()
    quando = _agora()
    titulo = (conversa.get("titulo") or "Conversa").strip()
    modelo = _nome_modelo(conversa.get("modelo") or "")
    provedor = _nome_provedor(conversa.get("provedor") or "")

    larga = any(len(b.cabecalho) > 7 for t in selecionados for tipo, x in t.itens if tipo == "assistente"
                for b in blocos(x) if b.tipo == "tabela")
    pagina = landscape(A4) if larga else A4
    margem_x, margem_topo, margem_base = 18 * mm, 24 * mm, 20 * mm
    largura = pagina[0] - 2 * margem_x

    rodape_esq = _limpar(f"Gerado em {quando:%d/%m/%Y às %H:%M} · {AVISO_IA}")
    cab_dir = _limpar(titulo if len(titulo) <= 90 else titulo[:87] + "…")

    class _CanvasNumerado(Canvas):
        """Segunda passada no fim: escreve "Pagina X de Y" em todas as paginas."""

        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            self._paginas: list[dict] = []

        def showPage(self) -> None:  # noqa: N802 -- API do reportlab
            self._paginas.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total = len(self._paginas)
            for estado_pag in self._paginas:
                self.__dict__.update(estado_pag)
                self._moldura(total)
                super().showPage()
            super().save()

        def _moldura(self, total: int) -> None:
            w, h = pagina
            topo = h - 13 * mm
            self.saveState()
            _desenhar_marca(self, margem_x, topo - 1.2 * mm, 5.2 * mm)
            self.setFont(f.negrito, 8.5)
            self.setFillColor(colors.HexColor(TINTA))
            self.drawString(margem_x + 7.2 * mm, topo + 0.6 * mm, "CNPJ Consulta")
            self.setFont(f.normal, 7.5)
            self.setFillColor(colors.HexColor(MUTED))
            self.drawRightString(w - margem_x, topo + 0.6 * mm, cab_dir)
            self.setStrokeColor(colors.HexColor(BORDA))
            self.setLineWidth(0.6)
            self.line(margem_x, topo - 3 * mm, w - margem_x, topo - 3 * mm)
            base = 11 * mm
            self.line(margem_x, base + 4 * mm, w - margem_x, base + 4 * mm)
            self.setFont(f.normal, 7)
            self.setFillColor(colors.HexColor(FAINT))
            self.drawString(margem_x, base, rodape_esq)
            self.setFont(f.negrito, 7.5)
            self.setFillColor(colors.HexColor(MUTED))
            self.drawRightString(w - margem_x, base, f"Página {self._pageNumber} de {total}")
            self.restoreState()

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=pagina, leftMargin=margem_x, rightMargin=margem_x,
                          topMargin=margem_topo, bottomMargin=margem_base, title=_limpar(titulo),
                          author="CNPJ Consulta", subject="Consulta com IA", creator="CNPJ Consulta")
    frame = Frame(margem_x, margem_base, largura, pagina[1] - margem_topo - margem_base, id="corpo",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="pagina", frames=[frame])])

    unico = len(selecionados) == 1
    meta = [f"Modelo: <b>{escape(_limpar(modelo))}</b>", f"Provedor: {escape(_limpar(provedor))}",
            f"Gerado em {quando:%d/%m/%Y às %H:%M}"]
    if not unico:
        meta.insert(0, f"{len(selecionados)} perguntas")
    hist: list = [
        Paragraph("CONSULTA COM IA" + (f" · RESPOSTA {selecionados[0].numero}" if turno else ""), est["olho"]),
        Paragraph(escape(_limpar(titulo)), est["titulo"]),
        Paragraph(" &nbsp;·&nbsp; ".join(meta), est["meta"]),
        Spacer(1, 14),
    ]
    for k, t in enumerate(selecionados):
        if k:
            hist += [Spacer(1, 6), HRFlowable(width="100%", thickness=0.6, color=colors.HexColor(BORDA),
                                              spaceBefore=4, spaceAfter=14)]
        rotulo = "PERGUNTA" if unico else f"PERGUNTA {t.numero}"
        pergunta = "<br/>".join(escape(_limpar(lin)) for lin in t.pergunta.strip().split("\n"))
        caixa = _caixa([Paragraph(rotulo, est["rotulo"]), Paragraph(pergunta, est["pergunta"])],
                       largura, TEAL_CLARO, TEAL)
        corpo: list = []
        for tipo, x in t.itens:
            if tipo == "assistente":
                corpo += _markdown_pdf(x, largura, est)
            else:
                corpo += [Spacer(1, 2), _caixa([Paragraph(_inline_pdf(x), est["aviso"])], largura,
                                               AVISO_FUNDO, AVISO_BORDA, pad=6), Spacer(1, 6)]
        resposta = [Paragraph("RESPOSTA", est["rotulo"])]
        # Pergunta, rotulo e o primeiro bloco da resposta nunca ficam sozinhos no pe da pagina.
        hist.append(KeepTogether([caixa, Spacer(1, 12), *resposta, *corpo[:1]]))
        hist += corpo[1:]
        if t.ferramentas:
            hist.append(Paragraph("Ferramentas consultadas: " + escape(", ".join(t.ferramentas)),
                                  est["ferramentas"]))
    doc.build(hist, canvasmaker=_CanvasNumerado)
    return buf.getvalue()


# ---------------------------------------------------------------- Excel


def _fonte(nome: str = "Calibri", **kw: Any) -> Any:
    from openpyxl.styles import Font

    return Font(name=nome, **kw)


def _nome_aba(base: str, usados: set[str]) -> str:
    limpo = re.sub(r"[\[\]:*?/\\']", " ", texto_puro(base)).strip() or "Tabela"
    limpo = re.sub(r"\s+", " ", limpo)[:28].strip()
    nome, n = limpo, 2
    while nome.lower() in usados:
        sufixo = f" ({n})"
        nome = limpo[:31 - len(sufixo)].strip() + sufixo
        n += 1
    usados.add(nome.lower())
    return nome


def _texto_seguro(c: Any) -> None:
    """Texto que comeca com "=" fica como texto (sem formula injetada)."""
    if isinstance(c.value, str) and c.value.startswith("="):
        c.data_type = "s"


def gerar_xlsx(conversa: dict, turno: int | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, PatternFill
    from openpyxl.worksheet.hyperlink import Hyperlink
    from openpyxl.worksheet.properties import PageSetupProperties

    selecionados = _selecionar(conversa, turno)
    quando = _agora()
    titulo = (conversa.get("titulo") or "Conversa").strip()
    modelo = _nome_modelo(conversa.get("modelo") or "")
    provedor = _nome_provedor(conversa.get("provedor") or "")
    hexa = lambda h: h.lstrip("#").upper()  # noqa: E731

    f_olho = _fonte(bold=True, size=9, color=hexa(TEAL))
    f_titulo = _fonte(bold=True, size=16, color=hexa(TINTA))
    f_meta = _fonte(size=9, color=hexa(MUTED))
    f_rotulo = _fonte(bold=True, size=9, color="FFFFFF")
    f_rotulo_resp = _fonte(bold=True, size=9, color=hexa(TEAL))
    f_pergunta = _fonte(bold=True, size=11, color=hexa(TINTA))
    f_texto = _fonte(size=10, color=hexa(TEXTO))
    f_h = _fonte(bold=True, size=11, color=hexa(TINTA))
    f_link = _fonte(size=10, color=hexa(TEAL), underline="single")
    f_mono = _fonte(nome="Consolas", size=9, color=hexa(TINTA))
    f_aviso = _fonte(size=9, color=hexa(AVISO_TEXTO))
    f_ferr = _fonte(italic=True, size=8, color=hexa(FAINT))
    fundo_teal = PatternFill("solid", fgColor=hexa(TEAL))
    fundo_pergunta = PatternFill("solid", fgColor=hexa(TEAL_CLARO))
    fundo_aviso = PatternFill("solid", fgColor=hexa(AVISO_FUNDO))
    fundo_codigo = PatternFill("solid", fgColor=hexa(ZEBRA))
    quebra = Alignment(wrap_text=True, vertical="top", indent=1)

    wb = Workbook()
    ws = wb.active
    ws.title = "Conversa"
    usados = {"conversa"}
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 15
    ws.column_dimensions["B"].width = 110
    ws["A1"] = "CNPJ CONSULTA · CONSULTA COM IA" + (f" · RESPOSTA {selecionados[0].numero}" if turno else "")
    ws["A1"].font = f_olho
    ws["A2"] = titulo
    ws["A2"].font = f_titulo
    _texto_seguro(ws["A2"])
    ws.row_dimensions[2].height = 26
    ws["A3"] = f"Modelo: {modelo}  ·  Provedor: {provedor}  ·  Gerado em {quando:%d/%m/%Y às %H:%M}"
    ws["A3"].font = f_meta
    ws["A4"] = AVISO_IA
    ws["A4"].font = _fonte(italic=True, size=8, color=hexa(FAINT))

    linha = 6
    unico = len(selecionados) == 1

    def escrever(rotulo: str, texto: str, fonte: Any, *, fundo: PatternFill | None = None,
                 fonte_rotulo: Any = None, fundo_rotulo: PatternFill | None = None) -> Any:
        nonlocal linha
        a, b = ws.cell(linha, 1, rotulo or None), ws.cell(linha, 2, texto)
        a.font = fonte_rotulo or f_rotulo_resp
        a.alignment = Alignment(vertical="top")
        if fundo_rotulo:
            a.fill = fundo_rotulo
        b.font, b.alignment = fonte, quebra
        if fundo:
            b.fill = fundo
        _texto_seguro(b)
        linha += 1
        return b

    n_tabela = 0
    for t in selecionados:
        escrever("PERGUNTA" if unico else f"PERGUNTA {t.numero}", t.pergunta.strip(), f_pergunta,
                 fundo=fundo_pergunta, fonte_rotulo=f_rotulo, fundo_rotulo=fundo_teal)
        ws.cell(linha - 1, 1).alignment = Alignment(vertical="center", horizontal="center")
        ws.row_dimensions[linha - 1].height = max(22, 15 * (1 + len(t.pergunta) // 120 + t.pergunta.count("\n")))
        rotulo = "RESPOSTA"
        ultimo_titulo = ""
        for tipo, x in t.itens:
            if tipo == "aviso":
                escrever(rotulo, "⚠ " + texto_puro(x), f_aviso, fundo=fundo_aviso)
                rotulo = ""
                continue
            for b in blocos(x):
                if b.tipo == "titulo":
                    ultimo_titulo = texto_puro(b.texto)
                    escrever(rotulo, ultimo_titulo, f_h)
                elif b.tipo == "paragrafo":
                    escrever(rotulo, texto_puro(b.texto), f_texto)
                elif b.tipo == "citacao":
                    escrever(rotulo, "“" + texto_puro(b.texto) + "”", _fonte(italic=True, size=10, color=hexa(MUTED)))
                elif b.tipo == "codigo":
                    escrever(rotulo, b.texto, f_mono, fundo=fundo_codigo)
                elif b.tipo == "lista":
                    for k, item in enumerate(b.itens, 1):
                        escrever(rotulo, (f"{k}. " if b.ordenada else "•  ") + texto_puro(item), f_texto)
                        rotulo = ""
                elif b.tipo == "tabela":
                    n_tabela += 1
                    aba = _nome_aba(ultimo_titulo or f"Tabela {n_tabela}", usados)
                    _aba_tabela(wb, aba, b, ultimo_titulo or f"Tabela {n_tabela}", t, unico)
                    cel = escrever(rotulo, f"▦  Tabela com {len(b.linhas)} linha(s) e {len(b.cabecalho)} "
                                           f"coluna(s): abrir a aba “{aba}”", f_link)
                    cel.hyperlink = Hyperlink(ref=cel.coordinate, location=f"'{aba}'!A1",
                                              tooltip=f"Abrir a aba {aba}")
                    ultimo_titulo = ""
                rotulo = "" if b.tipo != "regra" else rotulo
        if t.ferramentas:
            escrever("", "Ferramentas consultadas: " + ", ".join(t.ferramentas), f_ferr)
        linha += 1

    ws.page_setup.orientation = "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.print_options.horizontalCentered = True
    ws.oddFooter.left.text = "CNPJ Consulta · &D"
    ws.oddFooter.right.text = "Página &P de &N"

    wb.properties.title = titulo
    wb.properties.creator = "CNPJ Consulta"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _aba_tabela(wb: Any, nome: str, b: Bloco, titulo: str, t: Turno, unico: bool) -> None:
    """Uma aba por tabela da resposta: cabecalho congelado, filtro, numeros de verdade."""
    from openpyxl.styles import Alignment, Border, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties

    ws = wb.create_sheet(nome)
    ws.sheet_view.showGridLines = False
    ncol = max(1, len(b.cabecalho))
    ws.cell(1, 1, texto_puro(titulo)).font = _fonte(bold=True, size=14, color=TINTA.lstrip("#"))
    ws.row_dimensions[1].height = 22
    _texto_seguro(ws.cell(1, 1))
    pergunta = " ".join(t.pergunta.split())
    pergunta = pergunta if len(pergunta) <= 160 else pergunta[:157] + "…"
    sub = ws.cell(2, 1, ("Pergunta: " if unico else f"Pergunta {t.numero}: ") + pergunta)
    sub.font = _fonte(size=9, color=MUTED.lstrip("#"))
    _texto_seguro(sub)

    cab = 4
    numericas = [_coluna_numerica(b.cabecalho[j], [ln[j] for ln in b.linhas]) for j in range(ncol)]
    for j, h in enumerate(b.cabecalho, 1):
        c = ws.cell(cab, j, texto_puro(h))
        c.font = _fonte(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=TEAL.lstrip("#"))
        c.alignment = Alignment(vertical="center", wrap_text=True,
                                horizontal="right" if numericas[j - 1] else "left", indent=0)
        _texto_seguro(c)
    ws.row_dimensions[cab].height = 30
    larguras = [len(texto_puro(h)) + 4 for h in b.cabecalho]
    for i, ln in enumerate(b.linhas, cab + 1):
        for j, v in enumerate(ln, 1):
            puro = texto_puro(v)
            valor, fmt = converter_valor(puro, b.cabecalho[j - 1]) if numericas[j - 1] or \
                _RE_DATA.match(puro.strip()) else (puro, None)
            c = ws.cell(i, j, valor)
            c.border = Border(bottom=Side(style="thin", color=BORDA.lstrip("#")))
            c.alignment = Alignment(vertical="top", wrap_text=len(puro) > 60)
            if fmt:
                c.number_format = fmt
            _texto_seguro(c)
            larguras[j - 1] = max(larguras[j - 1], min(len(puro), 60) + 2)
    for j, w in enumerate(larguras, 1):
        ws.column_dimensions[get_column_letter(j)].width = max(9, min(w * 1.1, 62))
    ultima = cab + max(len(b.linhas), 1)
    ws.auto_filter.ref = f"A{cab}:{get_column_letter(ncol)}{ultima}"
    ws.freeze_panes = ws.cell(cab + 1, 1)
    ws.page_setup.orientation = "landscape" if ncol > 5 else "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.print_title_rows = f"{cab}:{cab}"
    ws.oddFooter.left.text = "CNPJ Consulta · &A"
    ws.oddFooter.right.text = "Página &P de &N"
