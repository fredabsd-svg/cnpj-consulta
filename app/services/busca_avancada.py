"""Busca avancada e prospeccao sobre a base local (DuckDB).

Toda a filtragem acontece no banco, com SQL PARAMETRIZADO: valores do usuario
nunca entram no texto do SQL. Os nomes de campos, operadores e ordenacoes vem
de listas fechadas (CAMPOS, ORDENACOES, COLUNAS); qualquer coisa fora delas e
recusada.

Criterios:
- formulario: cada campo preenchido vira uma condicao; valores multiplos do
  mesmo campo sao OU (ex.: Palmas OU Araguaina); campos diferentes sao E.
  `excluir_<campo>` nega o criterio (NAO).
- arvore (construtor avancado): {"op": "E"|"OU", "negar": bool, "itens": [...]}
  com folhas {"campo": ..., "valor": ..., "negar": bool}. Junta-se ao
  formulario com E.

Contatos: os oficiais vem da base da Receita (declarados pela empresa); os
complementares vem do SQLite (cadastrados com fonte) e entram na consulta como
lista de CNPJs. Celular/fixo seguem o plano de numeracao da Anatel.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import duckdb

from app.config import get_settings
from app.core.normalizer import normalize_name_for_search
from app.receita.armazenamento import base_ativa
from app.receita.duck import SCHEMA_VERSAO, Vigia, conectar_leitura, metadados
from app.receita.layout import (
    MATRIZ_FILIAL,
    PORTES_CURTO,
    QUALIFICACOES_ADMINISTRACAO,
    SITUACOES,
    TIPOS_SOCIO,
)
from app.services import contatos as contatos_srv
from app.services.cnae_palavras import PALAVRAS_CHAVE, faixas

MAX_PROFUNDIDADE = 4
MAX_ITENS_ARVORE = 40
MAX_VALORES = 500


class FiltroInvalido(ValueError):
    pass


class BuscaCancelada(RuntimeError):
    pass


class BuscaIndisponivel(RuntimeError):
    pass


# ---------------------------------------------------------------- valores


def _lista(valor: Any) -> list[str]:
    if valor is None:
        return []
    itens = valor if isinstance(valor, list | tuple) else [valor]
    out: list[str] = []
    for v in itens:
        for parte in re.split(r"[,;\n]", str(v)):
            if parte.strip():
                out.append(parte.strip())
    if len(out) > MAX_VALORES:
        raise FiltroInvalido(f"Valores demais em um filtro (máximo {MAX_VALORES}).")
    return out


def _ints(valor: Any, nome: str) -> list[int]:
    out = []
    for v in _lista(valor):
        if not re.fullmatch(r"\d{1,7}", v):
            raise FiltroInvalido(f"Valor inválido em {nome}: use apenas códigos numéricos.")
        out.append(int(v))
    return out


def _bool(valor: Any) -> bool | None:
    if valor in (None, "", []):
        return None
    if isinstance(valor, list):
        valor = valor[-1]
    v = str(valor).strip().lower()
    if v in ("1", "sim", "true", "s", "on"):
        return True
    if v in ("0", "nao", "não", "false", "n", "off"):
        return False
    raise FiltroInvalido("Use sim ou não.")


def _data(valor: Any, nome: str) -> date | None:
    if valor in (None, "", []):
        return None
    if isinstance(valor, list):
        valor = valor[-1]
    if isinstance(valor, date):
        return valor
    v = str(valor).strip()
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", v)
    try:
        return date(int(m[3]), int(m[2]), int(m[1])) if m else date.fromisoformat(v)
    except ValueError as e:
        raise FiltroInvalido(f"Data inválida em {nome}.") from e


def _num(valor: Any, nome: str) -> float | None:
    if valor in (None, "", []):
        return None
    if isinstance(valor, list):
        valor = valor[-1]
    v = str(valor).strip()
    if "," in v or re.fullmatch(r"\d{1,3}(\.\d{3})+", v):  # 1.234,56 ou 100.000 (milhar)
        v = v.replace(".", "").replace(",", ".")
    try:
        n = float(v)
    except ValueError as e:
        raise FiltroInvalido(f"Número inválido em {nome}.") from e
    if not math.isfinite(n) or n < 0 or n > 1e15:
        raise FiltroInvalido(f"Número fora do intervalo em {nome}.")
    return n


def sem_dados_de_socio(params: dict[str, Any]) -> dict[str, Any]:
    """Copia dos filtros sem nome/documento/chave de socio (inclusive `excluir_*`
    e folhas do construtor E/OU/NAO), para guardar em historico ou mostrar em
    listas publicas sem dado pessoal (LGPD)."""
    def eh_socio(nome: str) -> bool:
        nome = nome.removeprefix("excluir_")
        c = CAMPOS.get(nome)
        return nome.startswith("socio_") or bool(c and c.socio)

    def podar(no: Any) -> Any:
        if not isinstance(no, dict):
            return no
        if "campo" in no:
            return None if eh_socio(str(no["campo"])) else no
        itens = [x for x in (podar(i) for i in (no.get("itens") or []) if isinstance(no.get("itens"), list)) if x]
        return {**no, "itens": itens} if itens else None

    out: dict[str, Any] = {}
    for k, v in params.items():
        if eh_socio(k):
            continue
        if k == "arvore":
            bruto = v[-1] if isinstance(v, list) and v else v
            try:
                arv = json.loads(bruto) if isinstance(bruto, str) else bruto
            except ValueError:
                continue
            arv = podar(arv)
            if arv:
                out[k] = json.dumps(arv, ensure_ascii=False)
            continue
        out[k] = v
    return out


def _palavras(valor: Any) -> list[str]:
    texto = " ".join(_lista(valor)) if isinstance(valor, list) else str(valor or "")
    norm = normalize_name_for_search(texto) or ""
    palavras = [p for p in norm.split() if p]
    if len(palavras) > 10:
        raise FiltroInvalido("Use no máximo 10 palavras.")
    return palavras


def _idade(valor: Any) -> float | None:
    n = _num(valor, "idade")
    if n is not None and n > 200:
        raise FiltroInvalido("Idade: informe até 200 anos.")
    return n


def _anos_atras(anos: float) -> date:
    hoje = date.today()
    inteiros = int(anos)
    try:
        d = hoje.replace(year=hoje.year - inteiros)
    except ValueError:  # 29/02
        d = hoje.replace(year=hoje.year - inteiros, day=28)
    return d


def _ph(n: int) -> str:
    return ", ".join("?" * n)


# ---------------------------------------------------------------- campos

Sql = tuple[str, list[Any]]


@dataclass(frozen=True)
class Campo:
    nome: str
    rotulo: str
    grupo: str
    tipo: str  # texto | multi | bool | data | numero | enum
    compilar: Callable[[Any], Sql | None]
    opcoes: tuple[tuple[str, str], ...] = ()
    ajuda: str = ""
    socio: bool = False  # condicao sobre a tabela socios (alias s)
    complementar: bool = False  # vira lista de CNPJs do SQLite


def _texto_palavras(col: str) -> Callable[[Any], Sql | None]:
    def f(v):
        ps = _palavras(v)
        if not ps:
            return None
        return " AND ".join(f"{col} LIKE ?" for _ in ps), [f"%{p}%" for p in ps]
    return f


def _in_int(col: str, nome: str) -> Callable[[Any], Sql | None]:
    def f(v):
        vals = _ints(v, nome)
        return (f"{col} IN ({_ph(len(vals))})", vals) if vals else None
    return f


def _cmp(col: str, op: str, conv: Callable[[Any, str], Any], nome: str) -> Callable[[Any], Sql | None]:
    def f(v):
        x = conv(v, nome)
        return (f"{col} {op} ?", [x]) if x is not None else None
    return f


def _sim_nao(sim: str, nao: str) -> Callable[[Any], Sql | None]:
    def f(v):
        b = _bool(v)
        if b is None:
            return None
        return (sim if b else nao), []
    return f


def _q_rapida(v) -> Sql | None:
    texto = " ".join(_lista(v)) if isinstance(v, list) else str(v or "")
    texto = texto.strip()
    if not texto:
        return None
    dig = re.sub(r"\D", "", texto)
    if dig and len(dig) == len(re.sub(r"[\s./-]", "", texto)):
        if len(dig) < 2:
            raise FiltroInvalido("Digite ao menos 2 dígitos do CNPJ.")
        return "e.cnpj_completo >= ? AND e.cnpj_completo < ?", [dig, dig + "~"]
    alfa = re.sub(r"[\s./-]", "", texto).upper()
    if re.fullmatch(r"[A-Z0-9]{12}\d{2}", alfa) and any(c.isdigit() for c in alfa[:12]):
        return "e.cnpj_completo = ?", [alfa]  # CNPJ alfanumerico completo
    ps = _palavras(texto)
    if not ps or max(len(p) for p in ps) < 2:
        raise FiltroInvalido("Digite ao menos 2 letras.")
    conds = " AND ".join("(e.razao_social_norm LIKE ? OR e.nome_fantasia_norm LIKE ?)" for _ in ps)
    params: list[Any] = []
    for p in ps:
        params += [f"%{p}%", f"%{p}%"]
    return conds, params


def _alnum(x: str) -> str:
    """CNPJ sem mascara, preservando letras (CNPJ alfanumerico)."""
    return re.sub(r"[^A-Za-z0-9]", "", x).upper()


def _cnpj_prefixo(v) -> Sql | None:
    vals = [_alnum(x) for x in _lista(v)]
    vals = [x for x in vals if x]
    if not vals:
        return None
    if any(len(x) < 2 or len(x) > 14 for x in vals):
        raise FiltroInvalido("CNPJ: informe de 2 a 14 caracteres.")
    exatos = [x for x in vals if len(x) == 14]
    prefixos = [x for x in vals if len(x) < 14]
    partes, params = [], []
    if exatos:
        partes.append(f"e.cnpj_completo IN ({_ph(len(exatos))})")
        params += exatos
    for p in prefixos:
        partes.append("(e.cnpj_completo >= ? AND e.cnpj_completo < ?)")
        params += [p, p + "~"]
    return "(" + " OR ".join(partes) + ")", params


def _cnpj_basico(v) -> Sql | None:
    vals = [_alnum(x) for x in _lista(v)]
    vals = [x for x in vals if x]
    if not vals:
        return None
    if any(len(x) != 8 for x in vals):
        raise FiltroInvalido("CNPJ básico (raiz) tem 8 caracteres.")
    return f"e.cnpj_basico IN ({_ph(len(vals))})", vals


def _uf(v) -> Sql | None:
    vals = [x.upper() for x in _lista(v)]
    if not vals:
        return None
    if any(not re.fullmatch(r"[A-Z]{2}", x) for x in vals):
        raise FiltroInvalido("UF inválida.")
    return f"e.uf IN ({_ph(len(vals))})", vals


def _municipio_nome(v) -> Sql | None:
    nomes = [normalize_name_for_search(x) for x in _lista(v)]
    nomes = [n for n in nomes if n]
    if not nomes:
        return None
    sub = " OR ".join("strip_accents(upper(m.descricao)) = ?" for _ in nomes)
    return f"e.municipio IN (SELECT m.codigo FROM municipios m WHERE {sub})", nomes


def _cep(v) -> Sql | None:
    vals = [re.sub(r"\D", "", x) for x in _lista(v)]
    vals = [x for x in vals if x]
    if not vals:
        return None
    if any(len(x) < 2 or len(x) > 8 for x in vals):
        raise FiltroInvalido("CEP: informe de 2 a 8 dígitos (prefixo).")
    return "(" + " OR ".join("e.cep LIKE ?" for _ in vals) + ")", [f"{x}%" for x in vals]


def _tem_telefone(v) -> Sql | None:
    b = _bool(v)
    if b is None:
        return None
    c = "(e.telefone_1 IS NOT NULL OR e.telefone_2 IS NOT NULL)"
    return (c if b else f"NOT {c}"), []


def _tipo_telefone(posicao: str) -> Callable[[Any], Sql | None]:
    def f(v):
        vals = _lista(v)
        if not vals:
            return None
        if any(x not in ("celular", "fixo", "indeterminado") for x in vals):
            raise FiltroInvalido("Tipo de telefone: celular, fixo ou indeterminado.")
        cols = {"principal": ["e.tipo_telefone_1"], "secundario": ["e.tipo_telefone_2"],
                "qualquer": ["e.tipo_telefone_1", "e.tipo_telefone_2"]}[posicao]
        partes = [f"{c} IN ({_ph(len(vals))})" for c in cols]
        return "(" + " OR ".join(partes) + ")", vals * len(cols)
    return f


def _ddd(v) -> Sql | None:
    vals = _lista(v)
    if not vals:
        return None
    if any(not re.fullmatch(r"[1-9]\d", x) for x in vals):
        raise FiltroInvalido("DDD: dois dígitos (ex.: 63).")
    ph = _ph(len(vals))
    return f"(substr(e.telefone_1, 1, 2) IN ({ph}) OR substr(e.telefone_2, 1, 2) IN ({ph}))", vals * 2


def _email_dominio(v) -> Sql | None:
    vals = [x.lower().lstrip("@") for x in _lista(v)]
    if not vals:
        return None
    if any(not re.fullmatch(r"[a-z0-9.\-]+\.[a-z]{2,}", x) for x in vals):
        raise FiltroInvalido("Domínio de e-mail inválido (ex.: empresa.com.br).")
    return f"e.email_dominio IN ({_ph(len(vals))})", vals


def _email_tipo(v) -> Sql | None:
    vals = _lista(v)
    if not vals:
        return None
    if vals[-1] == "dominio_proprio":
        return "e.email_webmail = false", []
    if vals[-1] == "webmail":
        return "e.email_webmail = true", []
    raise FiltroInvalido("Tipo de e-mail: dominio_proprio ou webmail.")


_TEL = "(e.telefone_1 IS NOT NULL OR e.telefone_2 IS NOT NULL)"
_MAIL = "e.email IS NOT NULL"
COMBINACOES_CONTATO = {
    "telefone_e_email": ("Telefone e e-mail", f"({_TEL} AND {_MAIL})"),
    "so_telefone": ("Somente telefone", f"({_TEL} AND NOT {_MAIL})"),
    "so_email": ("Somente e-mail", f"(NOT {_TEL} AND {_MAIL})"),
    "algum": ("Algum contato oficial", f"({_TEL} OR {_MAIL})"),
    "sem_contato": ("Sem nenhum contato oficial", f"(NOT {_TEL} AND NOT {_MAIL})"),
}


def _contato(v) -> Sql | None:
    vals = _lista(v)
    if not vals:
        return None
    if vals[-1] not in COMBINACOES_CONTATO:
        raise FiltroInvalido("Combinação de contato inválida.")
    return COMBINACOES_CONTATO[vals[-1]][1], []


def _cnae_principal(v) -> Sql | None:
    vals = _ints(v, "CNAE")
    return (f"e.cnae_fiscal_principal IN ({_ph(len(vals))})", vals) if vals else None


def _cnae_secundario(modo: str) -> Callable[[Any], Sql | None]:
    def f(v):
        vals = _ints(v, "CNAE")
        if not vals:
            return None
        fn = "list_has_all" if modo == "todos" else "list_has_any"
        return f"{fn}(e.cnaes_secundarios, ?::INTEGER[])", [vals]
    return f


def _cnae_qualquer(v) -> Sql | None:
    vals = _ints(v, "CNAE")
    if not vals:
        return None
    return (f"(e.cnae_fiscal_principal IN ({_ph(len(vals))}) OR list_has_any(e.cnaes_secundarios, ?::INTEGER[]))",
            [*vals, vals])


def _faixas_sql(prefixos: list[str], escopo: str) -> str:
    """Prefixos CNAE validados viram faixas numericas LITERAIS (so digitos)."""
    conds = []
    for p in prefixos:
        ini, fim = faixas(p)
        conds.append(f"(e.cnae_fiscal_principal >= {ini} AND e.cnae_fiscal_principal < {fim})")
        if escopo == "qualquer":
            conds.append(f"len(list_filter(e.cnaes_secundarios, lambda c: c >= {ini} AND c < {fim})) > 0")
    return "(" + " OR ".join(conds) + ")"


def _cnae_prefixo(escopo: str) -> Callable[[Any], Sql | None]:
    def f(v):
        vals = [re.sub(r"\D", "", x) for x in _lista(v)]
        vals = [x for x in vals if x]
        if not vals:
            return None
        if any(not 2 <= len(x) <= 7 for x in vals):
            raise FiltroInvalido("Grupo CNAE: de 2 (divisão) a 7 (subclasse) dígitos.")
        return _faixas_sql(vals, escopo), []
    return f


def _palavra_chave(escopo: str) -> Callable[[Any], Sql | None]:
    def f(v):
        chaves = _lista(v)
        if not chaves:
            return None
        prefixos: list[str] = []
        for k in chaves:
            if k not in PALAVRAS_CHAVE:
                raise FiltroInvalido(f"Atividade desconhecida: {k}.")
            prefixos += PALAVRAS_CHAVE[k]["prefixos"]
        return _faixas_sql(prefixos, escopo), []
    return f


def _cnae_descricao(escopo: str) -> Callable[[Any], Sql | None]:
    def f(v):
        ps = _palavras(v)
        if not ps:
            return None
        sub = " AND ".join("strip_accents(upper(c.descricao)) LIKE ?" for _ in ps)
        codigos = f"(SELECT c.codigo FROM cnaes c WHERE {sub})"
        params = [f"%{p}%" for p in ps]
        if escopo == "principal":
            return f"e.cnae_fiscal_principal IN {codigos}", params
        return (f"(e.cnae_fiscal_principal IN {codigos} OR "
                f"list_has_any(e.cnaes_secundarios, (SELECT list(c.codigo) FROM cnaes c WHERE {sub})))",
                params * 2)
    return f


# -- socios (condicoes sobre alias s; agrupadas num unico EXISTS no formulario)

def _socio_nome(v) -> Sql | None:
    ps = _palavras(v)
    if not ps:
        return None
    if max(len(p) for p in ps) < 3:
        raise FiltroInvalido("Nome do sócio: ao menos 3 letras.")
    return " AND ".join("s.nome_socio_norm LIKE ?" for _ in ps), [f"%{p}%" for p in ps]


def _socio_documento(v) -> Sql | None:
    vals = _lista(v)
    if not vals:
        return None
    docs = []
    for x in vals:
        d = re.sub(r"[.\-/\s]", "", x)
        if re.fullmatch(r"\d{14}", d) or re.fullmatch(r"\*{3}\d{6}\*{2}", d):
            docs.append(d)
        else:
            raise FiltroInvalido("Documento do sócio: CNPJ (14 dígitos) ou CPF como publicado (***123456**).")
    return f"s.cpf_cnpj_socio IN ({_ph(len(docs))})", docs


def _socio_chave(v) -> Sql | None:
    vals = _lista(v)
    if not vals:
        return None
    if any(not re.fullmatch(r"[0-9a-f]{16}", x) for x in vals):
        raise FiltroInvalido("Identificador de sócio inválido.")
    return f"s.socio_chave IN ({_ph(len(vals))})", vals


def _socio_multi(v) -> Sql | None:
    n = _num(v, "sócio em várias empresas")
    if n is None:
        return None
    if n > 1_000_000:
        raise FiltroInvalido("Sócio em várias empresas: número alto demais.")
    return "s.socio_chave IN (SELECT socio_chave FROM socio_resumo WHERE qtd_empresas >= ?)", [max(2, int(n))]


def _compartilha(v) -> Sql | None:
    vals = [_alnum(x) for x in _lista(v)]
    vals = [x for x in vals if x]
    if not vals:
        return None
    raizes = [x[:8] for x in vals]
    if any(len(x) not in (8, 14) for x in vals):
        raise FiltroInvalido("Compartilha sócios com: CNPJ ou raiz (8 dígitos).")
    ph = _ph(len(raizes))
    return (f"s.socio_chave IN (SELECT socio_chave FROM socios WHERE cnpj_basico IN ({ph})) "
            f"AND s.cnpj_basico NOT IN ({ph})", raizes * 2)


def _socio_admin(v) -> Sql | None:
    b = _bool(v)
    if b is None:
        return None
    lista = ", ".join(str(q) for q in QUALIFICACOES_ADMINISTRACAO)
    return (f"s.qualificacao_socio IN ({lista})" if b else f"s.qualificacao_socio NOT IN ({lista})"), []


# -- complementares (resolvidos no SQLite)

def _comp(chave: str) -> Callable[[Any], Sql | None]:
    def f(v):
        return ("__comp__", [(chave, v)])  # marcador; resolvido em _resolver_comp
    return f


SIM_NAO = (("sim", "Sim"), ("nao", "Não"))
OP_SITUACAO = tuple((str(k), v.title()) for k, v in SITUACOES.items())
OP_PORTE = tuple((str(k), v) for k, v in PORTES_CURTO.items())

_CAMPOS: list[Campo] = [
    # Identificacao
    Campo("q", "Busca rápida (CNPJ, razão social ou nome fantasia)", "identificacao", "texto", _q_rapida),
    Campo("cnpj", "CNPJ (completo ou início)", "identificacao", "texto", _cnpj_prefixo),
    Campo("cnpj_basico", "Raiz do CNPJ (8 dígitos)", "identificacao", "texto", _cnpj_basico),
    Campo("razao", "Razão social contém", "identificacao", "texto", _texto_palavras("e.razao_social_norm")),
    Campo("fantasia", "Nome fantasia contém", "identificacao", "texto", _texto_palavras("e.nome_fantasia_norm")),
    Campo("tem_fantasia", "Tem nome fantasia", "identificacao", "bool",
          _sim_nao("e.nome_fantasia IS NOT NULL", "e.nome_fantasia IS NULL"), SIM_NAO),
    Campo("natureza", "Natureza jurídica", "identificacao", "multi", _in_int("e.natureza_juridica", "natureza")),
    Campo("matriz_filial", "Matriz ou filial", "identificacao", "enum",
          _in_int("e.identificador_matriz_filial", "matriz/filial"), (("1", "Matriz"), ("2", "Filial"))),
    Campo("abertura_de", "Abertura a partir de", "identificacao", "data",
          _cmp("e.data_inicio_atividade", ">=", _data, "abertura")),
    Campo("abertura_ate", "Abertura até", "identificacao", "data",
          _cmp("e.data_inicio_atividade", "<=", _data, "abertura")),
    Campo("idade_min", "Idade mínima (anos)", "identificacao", "numero",
          lambda v: (None if (n := _idade(v)) is None else ("e.data_inicio_atividade <= ?", [_anos_atras(n)]))),
    Campo("idade_max", "Idade máxima (anos)", "identificacao", "numero",
          lambda v: (None if (n := _idade(v)) is None else ("e.data_inicio_atividade >= ?", [_anos_atras(n + 1)]))),
    Campo("capital_min", "Capital social mínimo (R$)", "perfil", "numero",
          _cmp("e.capital_social", ">=", _num, "capital")),
    Campo("capital_max", "Capital social máximo (R$)", "perfil", "numero",
          _cmp("e.capital_social", "<=", _num, "capital")),
    Campo("situacao", "Situação cadastral", "identificacao", "multi",
          _in_int("e.situacao_cadastral", "situação"), OP_SITUACAO),
    Campo("data_situacao_de", "Data da situação a partir de", "identificacao", "data",
          _cmp("e.data_situacao_cadastral", ">=", _data, "data da situação")),
    Campo("data_situacao_ate", "Data da situação até", "identificacao", "data",
          _cmp("e.data_situacao_cadastral", "<=", _data, "data da situação")),
    Campo("motivo", "Motivo da situação", "identificacao", "multi", _in_int("e.motivo_situacao_cadastral", "motivo")),
    # Geografia
    Campo("pais", "País (código)", "geografia", "multi", _in_int("e.pais", "país")),
    Campo("uf", "UF", "geografia", "multi", _uf),
    Campo("municipio", "Município (código da Receita)", "geografia", "multi", _in_int("e.municipio", "município")),
    Campo("municipio_nome", "Município (nome exato)", "geografia", "texto", _municipio_nome),
    Campo("bairro", "Bairro contém", "geografia", "texto", _texto_palavras("strip_accents(upper(e.bairro))")),
    Campo("cep", "CEP (início)", "geografia", "texto", _cep),
    Campo("logradouro", "Logradouro contém", "geografia", "texto", _texto_palavras("strip_accents(upper(e.logradouro))")),
    Campo("endereco_completo", "Endereço completo", "qualidade", "bool",
          _sim_nao("e.endereco_completo", "NOT e.endereco_completo"), SIM_NAO,
          ajuda="Logradouro, número, bairro, CEP de 8 dígitos, município e UF preenchidos."),
    # Contatos oficiais
    Campo("tem_telefone", "Tem telefone (oficial)", "contatos", "bool", _tem_telefone, SIM_NAO),
    Campo("tem_telefone_principal", "Tem telefone principal", "contatos", "bool",
          _sim_nao("e.telefone_1 IS NOT NULL", "e.telefone_1 IS NULL"), SIM_NAO),
    Campo("tem_telefone_secundario", "Tem telefone secundário", "contatos", "bool",
          _sim_nao("e.telefone_2 IS NOT NULL", "e.telefone_2 IS NULL"), SIM_NAO),
    Campo("tipo_telefone", "Tipo de telefone", "contatos", "multi", _tipo_telefone("qualquer"),
          (("celular", "Celular"), ("fixo", "Fixo"), ("indeterminado", "Indeterminado")),
          ajuda="Plano de numeração da Anatel: celular começa por 6 a 9 (a Receita publica sem o nono dígito); fixo, por 2 a 5."),
    Campo("tipo_telefone_principal", "Tipo do telefone principal", "contatos", "multi", _tipo_telefone("principal"),
          (("celular", "Celular"), ("fixo", "Fixo"), ("indeterminado", "Indeterminado"))),
    Campo("ddd", "DDD", "contatos", "multi", _ddd),
    Campo("tem_email", "Tem e-mail (oficial)", "contatos", "bool", _sim_nao(_MAIL, f"NOT {_MAIL}"), SIM_NAO),
    Campo("email_dominio", "Domínio do e-mail", "contatos", "texto", _email_dominio),
    Campo("email_tipo", "Tipo de e-mail", "contatos", "enum", _email_tipo,
          (("dominio_proprio", "Domínio próprio provável"), ("webmail", "Webmail gratuito")),
          ajuda="Domínio próprio = não pertence a provedores gratuitos conhecidos (Gmail, Hotmail...). Não confirma que seja corporativo."),
    Campo("contato", "Combinação de contatos", "contatos", "enum", _contato,
          tuple((k, v[0]) for k, v in COMBINACOES_CONTATO.items())),
    # Contatos complementares
    Campo("comp_tem", "Tem contato complementar", "complementares", "bool", _comp("tem"), SIM_NAO),
    Campo("comp_tipo", "Tipo do contato complementar", "complementares", "enum", _comp("tipo"),
          (("telefone", "Telefone"), ("email", "E-mail"), ("site", "Site"))),
    Campo("tem_site", "Tem site (complementar)", "complementares", "bool", _comp("site"), SIM_NAO),
    Campo("comp_verificado", "Contato complementar verificado", "complementares", "bool", _comp("verificado"), SIM_NAO),
    Campo("comp_desatualizado", "Verificação desatualizada", "complementares", "bool", _comp("desatualizado"), SIM_NAO),
    Campo("comp_divergente", "Diverge do contato oficial", "complementares", "bool", _comp("difere"), SIM_NAO),
    Campo("comp_verificado_desde", "Verificado a partir de", "complementares", "data", _comp("verificado_desde")),
    Campo("origem_contato", "Origem do contato", "qualidade", "enum", _comp("origem"),
          (("oficial", "Só oficial (Receita)"), ("complementar", "Tem complementar"), ("ambos", "Oficial e complementar"))),
    # CNAE
    Campo("cnae_principal", "CNAE principal", "cnae", "multi", _cnae_principal),
    Campo("cnae_secundario", "CNAE secundário (qualquer um)", "cnae", "multi", _cnae_secundario("qualquer")),
    Campo("cnae_secundario_todos", "CNAE secundário (todos)", "cnae", "multi", _cnae_secundario("todos")),
    Campo("cnae", "CNAE principal ou secundário", "cnae", "multi", _cnae_qualquer),
    Campo("cnae_grupo", "Grupo CNAE (principal)", "cnae", "texto", _cnae_prefixo("principal"),
          ajuda="Prefixo do código: 2 dígitos = divisão, 3 = grupo, 4-5 = classe."),
    Campo("cnae_grupo_qualquer", "Grupo CNAE (principal ou secundário)", "cnae", "texto", _cnae_prefixo("qualquer")),
    Campo("cnae_descricao", "Descrição da atividade contém", "cnae", "texto", _cnae_descricao("qualquer")),
    Campo("atividade", "Atividade (atalho para códigos CNAE)", "cnae", "multi", _palavra_chave("principal"),
          tuple((k, v["rotulo"]) for k, v in PALAVRAS_CHAVE.items())),
    Campo("atividade_qualquer", "Atividade (principal ou secundária)", "cnae", "multi", _palavra_chave("qualquer"),
          tuple((k, v["rotulo"]) for k, v in PALAVRAS_CHAVE.items())),
    # Socios
    Campo("socio_nome", "Nome do sócio contém", "socios", "texto", _socio_nome, socio=True),
    Campo("socio_qualificacao", "Qualificação do sócio", "socios", "multi",
          _in_int("s.qualificacao_socio", "qualificação"), socio=True),
    Campo("socio_administrador", "Sócio administrador", "socios", "bool", _socio_admin, SIM_NAO, socio=True,
          ajuda="Qualificações de administração ou titularidade da tabela oficial."),
    Campo("socio_entrada_de", "Entrada do sócio a partir de", "socios", "data",
          _cmp("s.data_entrada_sociedade", ">=", _data, "entrada"), socio=True),
    Campo("socio_entrada_ate", "Entrada do sócio até", "socios", "data",
          _cmp("s.data_entrada_sociedade", "<=", _data, "entrada"), socio=True),
    Campo("socio_tipo", "Tipo de sócio", "socios", "multi", _in_int("s.identificador_de_socio", "tipo de sócio"),
          tuple((str(k), v) for k, v in TIPOS_SOCIO.items()), socio=True),
    Campo("socio_documento", "Documento do sócio (como publicado)", "socios", "texto", _socio_documento, socio=True),
    Campo("socio_chave", "Sócio (identificador)", "socios", "texto", _socio_chave, socio=True),
    Campo("socio_multiempresas", "Sócio presente em pelo menos N empresas", "socios", "numero", _socio_multi, socio=True),
    Campo("compartilha_socios_com", "Compartilha sócios com (CNPJ)", "socios", "texto", _compartilha, socio=True),
    Campo("tem_socios", "Tem sócios (QSA)", "socios", "bool", _sim_nao("e.qtd_socios > 0", "e.qtd_socios = 0"), SIM_NAO),
    Campo("qtd_socios_min", "Quantidade mínima de sócios", "socios", "numero", _cmp("e.qtd_socios", ">=", _num, "sócios")),
    Campo("qtd_socios_max", "Quantidade máxima de sócios", "socios", "numero", _cmp("e.qtd_socios", "<=", _num, "sócios")),
    # Perfil
    Campo("porte", "Porte", "perfil", "multi", _in_int("e.porte_empresa", "porte"), OP_PORTE),
    Campo("simples", "Optante pelo Simples", "perfil", "bool",
          _sim_nao("e.opcao_simples = 'S'", "coalesce(e.opcao_simples, 'N') <> 'S'"), SIM_NAO,
          ajuda="Informação cadastral; não comprova regularidade fiscal."),
    Campo("mei", "Optante pelo MEI", "perfil", "bool",
          _sim_nao("e.opcao_mei = 'S'", "coalesce(e.opcao_mei, 'N') <> 'S'"), SIM_NAO,
          ajuda="Informação cadastral; não comprova regularidade fiscal."),
    # Qualidade
    Campo("dados_completos", "Cadastro completo (endereço + contato)", "qualidade", "bool",
          _sim_nao(f"(e.endereco_completo AND ({_TEL} OR {_MAIL}))",
                   f"NOT (e.endereco_completo AND ({_TEL} OR {_MAIL}))"), SIM_NAO),
    Campo("sem_empresa", "Estabelecimento sem empresa na base", "qualidade", "bool",
          _sim_nao("NOT e.tem_empresa", "e.tem_empresa"), SIM_NAO),
]
CAMPOS: dict[str, Campo] = {c.nome: c for c in _CAMPOS}

GRUPOS_CAMPOS = {
    "identificacao": "Identificação",
    "geografia": "Localização",
    "contatos": "Contatos oficiais",
    "complementares": "Contatos complementares",
    "cnae": "Atividade (CNAE)",
    "socios": "Sócios",
    "perfil": "Porte e perfil",
    "qualidade": "Qualidade dos dados",
}

ORDENACOES = {
    "razao": ("Razão social", "e.razao_social_norm", "ASC"),
    "abertura": ("Data de abertura", "e.data_inicio_atividade", "DESC"),
    "capital": ("Capital social", "e.capital_social", "DESC"),
    "local": ("UF e município", "e.uf, e.municipio", "ASC"),
    "situacao": ("Situação", "e.situacao_cadastral", "ASC"),
    "cnpj": ("CNPJ", "e.cnpj_completo", "ASC"),
    "socios": ("Quantidade de sócios", "e.qtd_socios", "DESC"),
}

# Colunas de resultado/exportacao: (rotulo, expressao sobre pg/aux, restrita)
# "restrita" = dado de contato ou pessoal: exportar exige informar a finalidade.
COLUNAS: dict[str, tuple[str, str, bool]] = {
    "cnpj": ("CNPJ", "pg.cnpj_completo", False),
    "razao_social": ("Razão social", "pg.razao_social", False),
    "nome_fantasia": ("Nome fantasia", "pg.nome_fantasia", False),
    "matriz_filial": ("Matriz/filial", "pg.identificador_matriz_filial", False),
    "situacao": ("Situação", "pg.situacao_cadastral", False),
    "data_situacao": ("Data da situação", "pg.data_situacao_cadastral", False),
    "abertura": ("Abertura", "pg.data_inicio_atividade", False),
    "natureza": ("Natureza jurídica", "nat.descricao", False),
    "porte": ("Porte", "pg.porte_empresa", False),
    "capital_social": ("Capital social", "pg.capital_social", False),
    "cnae_principal": ("CNAE principal", "pg.cnae_fiscal_principal", False),
    "cnae_descricao": ("Atividade principal", "cn.descricao", False),
    "cnaes_secundarios": ("CNAEs secundários", "pg.cnae_fiscal_secundaria", False),
    "logradouro": ("Endereço", "concat_ws(' ', pg.tipo_logradouro, pg.logradouro, pg.numero, pg.complemento)", False),
    "bairro": ("Bairro", "pg.bairro", False),
    "cep": ("CEP", "pg.cep", False),
    "municipio": ("Município", "mun.descricao", False),
    "municipio_codigo": ("Código do município (Receita)", "pg.municipio", False),
    "uf": ("UF", "pg.uf", False),
    "telefone_1": ("Telefone 1", "pg.telefone_1", True),
    "tipo_telefone_1": ("Tipo tel. 1", "pg.tipo_telefone_1", False),
    "telefone_2": ("Telefone 2", "pg.telefone_2", True),
    "tipo_telefone_2": ("Tipo tel. 2", "pg.tipo_telefone_2", False),
    "email": ("E-mail", "pg.email", True),
    "qtd_socios": ("Qtd. de sócios", "pg.qtd_socios", False),
    "simples": ("Simples", "pg.opcao_simples", False),
    "mei": ("MEI", "pg.opcao_mei", False),
    "socios": ("Sócios (nomes)", "(SELECT string_agg(s.nome_socio, '; ' ORDER BY s.nome_socio) FROM socios s "
                                 "WHERE s.cnpj_basico = pg.cnpj_basico)", True),
}
# Padrao sem colunas restritas: contatos so saem quando escolhidos (com finalidade).
COLUNAS_PADRAO = ["cnpj", "razao_social", "nome_fantasia", "situacao", "municipio", "uf",
                  "cnae_principal", "cnae_descricao", "abertura", "porte", "qtd_socios"]
COLUNAS_TELA = ["cnpj", "razao_social", "nome_fantasia", "matriz_filial", "situacao", "abertura",
                "porte", "capital_social", "cnae_principal", "cnae_descricao", "municipio", "municipio_codigo", "uf",
                "bairro", "telefone_1", "tipo_telefone_1", "telefone_2", "tipo_telefone_2", "email",
                "qtd_socios", "simples", "mei"]


# ---------------------------------------------------------------- filtros


@dataclass
class Filtros:
    campos: dict[str, Any] = field(default_factory=dict)  # nome -> valor (formulario)
    excluir: dict[str, Any] = field(default_factory=dict)  # nome -> valor (NAO)
    arvore: dict | None = None
    ordem: str = "razao"
    direcao: str | None = None
    pagina: int = 1
    por_pagina: int = 25

    @classmethod
    def de_params(cls, params: dict[str, Any]) -> Filtros:
        """De query string (valores ja agrupados em listas por nome)."""
        f = cls()
        for k, v in params.items():
            vals = v if isinstance(v, list) else [v]
            vals = [x for x in vals if x not in (None, "")]
            if not vals:
                continue
            if k in CAMPOS:
                f.campos[k] = vals if len(vals) > 1 or CAMPOS[k].tipo == "multi" else vals[0]
            elif k.startswith("excluir_") and k[8:] in CAMPOS:
                f.excluir[k[8:]] = vals if len(vals) > 1 or CAMPOS[k[8:]].tipo == "multi" else vals[0]
            elif k == "arvore":
                try:
                    f.arvore = json.loads(vals[-1]) if isinstance(vals[-1], str) else vals[-1]
                except ValueError as e:
                    raise FiltroInvalido("Construtor de filtros: JSON inválido.") from e
            elif k == "ordem":
                f.ordem = str(vals[-1])
                if f.ordem not in ORDENACOES:
                    raise FiltroInvalido("Ordenação inválida.")
            elif k == "direcao":
                f.direcao = str(vals[-1]).lower()
                if f.direcao not in ("asc", "desc"):
                    raise FiltroInvalido("Direção de ordenação inválida.")
            elif k == "pagina":
                f.pagina = _inteiro_param(vals[-1], 1, 1, 100_000)
            elif k == "por_pagina":
                f.por_pagina = _inteiro_param(vals[-1], 25, 1, get_settings().busca_max_por_pagina)
        return f

    def para_params(self) -> dict[str, Any]:
        d: dict[str, Any] = dict(self.campos)
        d.update({f"excluir_{k}": v for k, v in self.excluir.items()})
        if self.arvore:
            d["arvore"] = json.dumps(self.arvore, ensure_ascii=False)
        if self.ordem != "razao":
            d["ordem"] = self.ordem
        if self.direcao:
            d["direcao"] = self.direcao
        return d

    def vazio(self) -> bool:
        return not self.campos and not self.excluir and not self.arvore


def _inteiro_param(v: Any, padrao: int, minimo: int, maximo: int) -> int:
    try:
        return max(minimo, min(maximo, int(str(v))))
    except ValueError:
        return padrao


# ---------------------------------------------------------------- compilacao


@dataclass
class _Ctx:
    comp_cache: dict[str, list[str]] = field(default_factory=dict)


def _resolver_comp(itens: list[tuple[str, Any]], ctx: _Ctx) -> Sql:
    """Criterios de contato complementar -> lista de CNPJs (do SQLite)."""
    kwargs: dict[str, Any] = {}
    origem = None
    tem: bool | None = None
    for chave, v in itens:
        if chave == "tem":
            tem = _bool(v)
        elif chave == "tipo":
            t = _lista(v)[-1] if _lista(v) else None
            if t not in contatos_srv.TIPOS:
                raise FiltroInvalido("Tipo de contato complementar inválido.")
            kwargs["tipo"] = t
        elif chave == "site":
            b = _bool(v)
            if b is not None:
                kwargs["tipo"] = "site"
                tem = b
        elif chave == "verificado":
            kwargs["verificado"] = _bool(v)
        elif chave == "desatualizado":
            kwargs["desatualizado"] = _bool(v)
        elif chave == "difere":
            kwargs["difere"] = _bool(v)
        elif chave == "verificado_desde":
            kwargs["verificado_desde"] = _data(v, "verificação")
        elif chave == "origem":
            origem = _lista(v)[-1] if _lista(v) else None
            if origem not in ("oficial", "complementar", "ambos"):
                raise FiltroInvalido("Origem do contato inválida.")
    chave_cache = json.dumps(kwargs, default=str, sort_keys=True)
    if chave_cache not in ctx.comp_cache:
        ctx.comp_cache[chave_cache] = contatos_srv.cnpjs_com(**{k: v for k, v in kwargs.items() if v is not None})
    cnpjs = ctx.comp_cache[chave_cache]
    dentro = "e.cnpj_completo IN (SELECT unnest(?::VARCHAR[]))"
    oficial = f"({_TEL} OR {_MAIL})"
    if origem == "oficial":
        return f"({oficial} AND NOT {dentro})", [cnpjs]
    if origem == "complementar":
        return dentro, [cnpjs]
    if origem == "ambos":
        return f"({oficial} AND {dentro})", [cnpjs]
    if tem is False:
        return f"NOT {dentro}", [cnpjs]
    return dentro, [cnpjs]


def _compilar_folha(nome: str, valor: Any, ctx: _Ctx, agrupar_socios: list | None = None) -> Sql | None:
    campo = CAMPOS.get(nome)
    if campo is None:
        raise FiltroInvalido(f"Filtro desconhecido: {nome}.")
    r = campo.compilar(valor)
    if r is None:
        return None
    sql, params = r
    if sql == "__comp__":
        return _resolver_comp(params, ctx)
    if campo.socio:
        if agrupar_socios is not None:
            agrupar_socios.append((sql, params))
            return None
        return f"EXISTS (SELECT 1 FROM socios s WHERE s.cnpj_basico = e.cnpj_basico AND {sql})", params
    return sql, params


def _compilar_arvore(no: Any, ctx: _Ctx, prof: int = 0, contador: list[int] | None = None) -> Sql | None:
    contador = contador if contador is not None else [0]
    if prof > MAX_PROFUNDIDADE:
        raise FiltroInvalido("Construtor de filtros: grupos aninhados demais.")
    if not isinstance(no, dict):
        raise FiltroInvalido("Construtor de filtros: estrutura inválida.")
    contador[0] += 1
    if contador[0] > MAX_ITENS_ARVORE:
        raise FiltroInvalido(f"Construtor de filtros: no máximo {MAX_ITENS_ARVORE} condições.")
    negar = bool(_bool(no.get("negar"))) if not isinstance(no.get("negar"), bool) else no["negar"]
    if "campo" in no:
        r = _compilar_folha(str(no["campo"]), no.get("valor"), ctx)
    else:
        op = str(no.get("op", "E")).upper()
        if op not in ("E", "OU", "AND", "OR"):
            raise FiltroInvalido("Operador inválido (use E ou OU).")
        juntor = " AND " if op in ("E", "AND") else " OR "
        partes, params = [], []
        itens = no.get("itens") or []
        if not isinstance(itens, list):
            raise FiltroInvalido("Construtor de filtros: `itens` deve ser uma lista.")
        for item in itens:
            sub = _compilar_arvore(item, ctx, prof + 1, contador)
            if sub:
                partes.append(f"({sub[0]})")
                params += sub[1]
        r = (juntor.join(partes), params) if partes else None
    if r is None:
        return None
    # NAO sobre dado ausente (NULL) tem de incluir a linha: "nao e do DDD 63"
    # vale para quem nao tem telefone.
    return (f"NOT coalesce(({r[0]}), false)", r[1]) if negar else r


def compilar(f: Filtros) -> Sql:
    """WHERE completo (sempre ha ao menos 'TRUE')."""
    ctx = _Ctx()
    partes: list[str] = []
    params: list[Any] = []
    socios_agrupados: list[Sql] = []
    for nome, valor in f.campos.items():
        r = _compilar_folha(nome, valor, ctx, socios_agrupados)
        if r:
            partes.append(f"({r[0]})")
            params += r[1]
    if socios_agrupados:
        # Mesmo socio precisa atender a todos os criterios de socio do formulario.
        cond = " AND ".join(f"({s})" for s, _ in socios_agrupados)
        partes.append(f"EXISTS (SELECT 1 FROM socios s WHERE s.cnpj_basico = e.cnpj_basico AND {cond})")
        for _, p in socios_agrupados:
            params += p
    for nome, valor in f.excluir.items():
        r = _compilar_folha(nome, valor, ctx)
        if r:
            partes.append(f"NOT coalesce(({r[0]}), false)")
            params += r[1]
    if f.arvore:
        r = _compilar_arvore(f.arvore, ctx)
        if r:
            partes.append(f"({r[0]})")
            params += r[1]
    return (" AND ".join(partes) if partes else "TRUE"), params


def _ordem_sql(f: Filtros) -> str:
    if f.ordem not in ORDENACOES:
        raise FiltroInvalido("Ordenação inválida.")
    _, expr, padrao = ORDENACOES[f.ordem]
    direcao = (f.direcao or padrao).upper()
    if direcao not in ("ASC", "DESC"):
        raise FiltroInvalido("Direção de ordenação inválida.")
    cols = [f"{c.strip()} {direcao} NULLS LAST" for c in expr.split(",")]
    return ", ".join(cols + ["e.cnpj_completo ASC"])


# ---------------------------------------------------------------- execucao


@dataclass
class Resultado:
    total: int
    pagina: int
    por_pagina: int
    itens: list[dict]
    mes_referencia: str | None
    tempo_ms: int
    ordem: str
    direcao: str

    @property
    def paginas(self) -> int:
        return max(1, -(-self.total // self.por_pagina))


def _base():
    ativa = base_ativa()
    if ativa is None:
        raise BuscaIndisponivel("A base local da Receita não está instalada. Instale em Base de Dados CNPJ.")
    return ativa


def _checar_schema(conn) -> dict:
    meta = metadados(conn)
    if meta.get("schema_versao") != SCHEMA_VERSAO:
        raise BuscaIndisponivel("A base local está no formato antigo. Atualize em Base de Dados CNPJ.")
    return meta


class _Controle:
    def __init__(self, cancelar: threading.Event | None, timeout: float | None) -> None:
        self.cancelar = cancelar
        self.limite = time.monotonic() + (timeout or get_settings().busca_timeout_segundos)
        self.motivo: str | None = None

    def deve_parar(self) -> bool:
        if self.cancelar is not None and self.cancelar.is_set():
            self.motivo = "cancelada"
            return True
        if time.monotonic() > self.limite:
            self.motivo = "tempo"
            return True
        return False


def _executar(conn, ctrl: _Controle, sql: str, params: list) -> list[tuple]:
    try:
        with Vigia(conn, ctrl.deve_parar, intervalo=0.2):
            return conn.execute(sql, params).fetchall()
    except duckdb.InterruptException as e:
        if ctrl.motivo == "tempo":
            raise BuscaCancelada(
                "A busca passou do tempo limite. Acrescente filtros (UF, município, situação) para reduzir o volume."
            ) from e
        raise BuscaCancelada("Busca cancelada.") from e


_SELECT_PAGINA = """
WITH pg AS (
    SELECT e.*, row_number() OVER () AS _ord FROM (
        SELECT e.* FROM estabelecimentos e WHERE {where} ORDER BY {ordem} LIMIT ? OFFSET ?
    ) e
)
SELECT {colunas}, pg._ord
FROM pg
LEFT JOIN municipios mun ON mun.codigo = pg.municipio
LEFT JOIN naturezas nat ON nat.codigo = pg.natureza_juridica
LEFT JOIN cnaes cn ON cn.codigo = pg.cnae_fiscal_principal
ORDER BY pg._ord
"""


def _colunas_sql(colunas: list[str]) -> str:
    return ", ".join(f"{COLUNAS[c][1]} AS {c}" for c in colunas)


def buscar(f: Filtros, *, colunas: list[str] | None = None, cancelar: threading.Event | None = None,
           timeout: float | None = None, contar: bool = True) -> Resultado:
    colunas = [c for c in (colunas or COLUNAS_TELA) if c in COLUNAS_TELA]
    if "cnpj" not in colunas:
        colunas.insert(0, "cnpj")
    where, params = compilar(f)
    ordem = _ordem_sql(f)
    ativa = _base()
    ctrl = _Controle(cancelar, timeout)
    t0 = time.monotonic()
    with conectar_leitura(ativa.caminho) as conn:
        meta = _checar_schema(conn)
        offset = (f.pagina - 1) * f.por_pagina
        sql = _SELECT_PAGINA.format(where=where, ordem=ordem, colunas=_colunas_sql(colunas))
        rows = _executar(conn, ctrl, sql, [*params, f.por_pagina, offset])
        nomes = [*colunas, "_ord"]
        itens = [dict(zip(nomes, r, strict=True)) for r in rows]
        if contar:
            if f.pagina == 1 and len(itens) < f.por_pagina:
                total = len(itens)
            else:
                total = _executar(conn, ctrl, f"SELECT count(*) FROM estabelecimentos e WHERE {where}", params)[0][0]
        else:
            total = -1
    for it in itens:
        it.pop("_ord", None)
        _rotular(it)
    _, _, padrao = ORDENACOES[f.ordem]
    return Resultado(
        total=total, pagina=f.pagina, por_pagina=f.por_pagina, itens=itens,
        mes_referencia=meta.get("mes_referencia"), tempo_ms=int((time.monotonic() - t0) * 1000),
        ordem=f.ordem, direcao=(f.direcao or padrao).lower(),
    )


def _rotular(it: dict) -> None:
    if "situacao" in it:
        it["situacao_codigo"] = it["situacao"]
        it["situacao"] = SITUACOES.get(it["situacao"], it["situacao"])
    if "matriz_filial" in it:
        it["matriz_filial"] = MATRIZ_FILIAL.get(it["matriz_filial"], it["matriz_filial"])
    if "porte" in it:
        it["porte"] = PORTES_CURTO.get(it["porte"], it["porte"])
    if "capital_social" in it and it["capital_social"] is not None:
        it["capital_social"] = float(it["capital_social"])
    if "cnae_principal" in it and it["cnae_principal"] is not None:
        it["cnae_principal"] = f"{int(it['cnae_principal']):07d}"


def iterar_exportacao(f: Filtros, colunas: list[str], limite: int, lote: int = 5000,
                      cancelar: threading.Event | None = None):
    """Gera linhas em lotes (fetchmany): a exportacao nunca carrega tudo na RAM."""
    colunas = [c for c in colunas if c in COLUNAS] or COLUNAS_PADRAO
    where, params = compilar(f)
    ordem = _ordem_sql(f)
    ativa = _base()
    with conectar_leitura(ativa.caminho) as conn:
        _checar_schema(conn)
        sql = f"""
            WITH pg AS (SELECT e.* FROM estabelecimentos e WHERE {where} ORDER BY {ordem} LIMIT ?)
            SELECT {_colunas_sql(colunas)} FROM pg
            LEFT JOIN municipios mun ON mun.codigo = pg.municipio
            LEFT JOIN naturezas nat ON nat.codigo = pg.natureza_juridica
            LEFT JOIN cnaes cn ON cn.codigo = pg.cnae_fiscal_principal
        """
        cur = conn.execute(sql, [*params, limite])
        while True:
            if cancelar is not None and cancelar.is_set():
                raise BuscaCancelada("Exportação cancelada.")
            rows = cur.fetchmany(lote)
            if not rows:
                break
            for r in rows:
                it = dict(zip(colunas, r, strict=True))
                _rotular(it)
                yield it


def contar(f: Filtros, timeout: float | None = None) -> int:
    where, params = compilar(f)
    ativa = _base()
    ctrl = _Controle(None, timeout)
    with conectar_leitura(ativa.caminho) as conn:
        _checar_schema(conn)
        return _executar(conn, ctrl, f"SELECT count(*) FROM estabelecimentos e WHERE {where}", params)[0][0]


DIMENSOES = {
    "uf": ("e.uf", None),
    "situacao": ("e.situacao_cadastral", SITUACOES),
    "porte": ("e.porte_empresa", PORTES_CURTO),
    "matriz_filial": ("e.identificador_matriz_filial", MATRIZ_FILIAL),
    "tipo_telefone_1": ("e.tipo_telefone_1", None),
    "municipio": ("e.municipio", "municipios"),
    "cnae_principal": ("e.cnae_fiscal_principal", "cnaes"),
}


def contadores(f: Filtros, dimensoes: list[str], *, limite: int = 15,
               cancelar: threading.Event | None = None, timeout: float | None = None) -> dict[str, list[dict]]:
    """Quantos resultados por valor de cada dimensao (para os filtros)."""
    where, params = compilar(f)
    ativa = _base()
    ctrl = _Controle(cancelar, timeout)
    out: dict[str, list[dict]] = {}
    with conectar_leitura(ativa.caminho) as conn:
        _checar_schema(conn)
        for d in dimensoes:
            if d not in DIMENSOES:
                raise FiltroInvalido(f"Contador desconhecido: {d}.")
            expr, rotulos = DIMENSOES[d]
            rows = _executar(conn, ctrl,
                             f"SELECT {expr} AS v, count(*) AS n FROM estabelecimentos e WHERE {where} "
                             f"GROUP BY 1 ORDER BY 2 DESC LIMIT {int(limite)}", params)
            nomes: dict = {}
            if isinstance(rotulos, str) and rows:
                cods = [r[0] for r in rows if r[0] is not None]
                if cods:
                    nomes = dict(conn.execute(
                        f"SELECT codigo, descricao FROM {rotulos} WHERE codigo IN ({_ph(len(cods))})", cods).fetchall())
            elif isinstance(rotulos, dict):
                nomes = rotulos
            out[d] = [{"valor": v, "rotulo": nomes.get(v, v) if v is not None else "(vazio)", "total": n}
                      for v, n in rows]
    return out


def duplicidades(cnpjs: list[str]) -> dict[str, dict]:
    """Para os CNPJs de uma pagina: quantas OUTRAS empresas (raizes) usam o
    mesmo telefone principal ou e-mail -- sinal de possivel duplicidade ou de
    contato do escritorio de contabilidade, nao uma conclusao."""
    cnpjs = [c for c in cnpjs if re.fullmatch(r"[0-9A-Z]{14}", c)][:200]
    if not cnpjs:
        return {}
    ativa = _base()
    with conectar_leitura(ativa.caminho) as conn:
        _checar_schema(conn)
        base = conn.execute(
            f"SELECT cnpj_completo, cnpj_basico, telefone_1, email FROM estabelecimentos "
            f"WHERE cnpj_completo IN ({_ph(len(cnpjs))})", cnpjs).fetchall()
        tels = sorted({r[2] for r in base if r[2]})
        mails = sorted({r[3] for r in base if r[3]})
        cont_tel: dict = {}
        cont_mail: dict = {}
        if tels:
            cont_tel = {t: set(rs) for t, rs in conn.execute(
                f"SELECT telefone_1, list(DISTINCT cnpj_basico) FROM estabelecimentos "
                f"WHERE telefone_1 IN ({_ph(len(tels))}) GROUP BY 1", tels).fetchall()}
        if mails:
            cont_mail = {m: set(rs) for m, rs in conn.execute(
                f"SELECT email, list(DISTINCT cnpj_basico) FROM estabelecimentos "
                f"WHERE email IN ({_ph(len(mails))}) GROUP BY 1", mails).fetchall()}
    out = {}
    for cnpj, basico, tel, mail in base:
        t = len(cont_tel.get(tel, set()) - {basico}) if tel else 0
        m = len(cont_mail.get(mail, set()) - {basico}) if mail else 0
        if t or m:
            out[cnpj] = {"telefone_compartilhado": t, "email_compartilhado": m}
    return out


# ---------------------------------------------------------------- descricoes


def resumo_texto(f: Filtros, nomes_municipios: dict[int, str] | None = None) -> str:
    """Resumo legivel (historico de buscas). Nao inclui nomes de socios (LGPD)."""
    partes = []
    for nome, valor in f.campos.items():
        partes.append(_rotulo_valor(nome, valor, nomes_municipios))
    for nome, valor in f.excluir.items():
        partes.append("NÃO " + _rotulo_valor(nome, valor, nomes_municipios))
    if f.arvore:
        partes.append("+ condições do construtor")
    return "; ".join(partes)[:480] or "Todos os estabelecimentos"


def _rotulo_valor(nome: str, valor: Any, nomes_municipios: dict | None = None) -> str:
    c = CAMPOS[nome]
    if nome in ("socio_nome", "socio_documento", "socio_chave"):
        return f"{c.rotulo}: (informado)"
    vals = _lista(valor)
    opcoes = dict(c.opcoes)
    if nome == "municipio" and nomes_municipios:
        vals = [nomes_municipios.get(int(v), v) if re.fullmatch(r"[0-9]+", str(v)) else v for v in vals]
    rot = [opcoes.get(v, v) for v in vals]
    return f"{c.rotulo}: {', '.join(str(x) for x in rot)}"


def chips(f: Filtros, nomes: dict[str, dict] | None = None) -> list[dict]:
    """Filtros ativos como etiquetas removiveis (params sem aquele filtro)."""
    nomes = nomes or {}
    out = []
    for origem, prefixo in ((f.campos, ""), (f.excluir, "excluir_")):
        for nome, valor in origem.items():
            c = CAMPOS[nome]
            opcoes = dict(c.opcoes)
            vals = _lista(valor)
            for v in vals:
                rotulo = nomes.get(nome, {}).get(v) or opcoes.get(v) or v
                out.append({"param": prefixo + nome, "valor": v,
                            "texto": f"{'NÃO ' if prefixo else ''}{c.rotulo}: {rotulo}"})
            if not vals and valor:
                out.append({"param": prefixo + nome, "valor": str(valor), "texto": f"{c.rotulo}: {valor}"})
    if f.arvore:
        out.append({"param": "arvore", "valor": None, "texto": "Condições do construtor (E/OU/NÃO)"})
    return out


def lista_municipios(ufs: list[str]) -> list[dict]:
    ufs = [u.upper() for u in ufs if re.fullmatch(r"[A-Za-z]{2}", u)]
    if not ufs:
        return []
    ativa = _base()
    with conectar_leitura(ativa.caminho) as conn:
        _checar_schema(conn)
        rows = conn.execute(
            f"SELECT uf, codigo, nome, qtd FROM municipios_uf WHERE uf IN ({_ph(len(ufs))}) ORDER BY uf, nome", ufs
        ).fetchall()
    return [{"uf": u, "codigo": c, "nome": n, "qtd": q} for u, c, n, q in rows]


def lista_dominio(tabela: str, q: str = "", limite: int = 2000) -> list[dict]:
    if tabela not in ("cnaes", "naturezas", "motivos", "qualificacoes", "paises"):
        raise FiltroInvalido("Tabela inválida.")
    ativa = _base()
    with conectar_leitura(ativa.caminho) as conn:
        _checar_schema(conn)
        ps = _palavras(q) if q else []
        dig = re.sub(r"\D", "", q or "")
        where, params = "TRUE", []
        if dig and dig == (q or "").strip().replace(".", "").replace("-", "").replace("/", ""):
            where, params = "lpad(CAST(codigo AS VARCHAR), 7, '0') LIKE ? OR CAST(codigo AS VARCHAR) LIKE ?", [f"{dig}%", f"{dig}%"]
        elif ps:
            where = " AND ".join("strip_accents(upper(descricao)) LIKE ?" for _ in ps)
            params = [f"%{p}%" for p in ps]
        rows = conn.execute(
            f"SELECT codigo, descricao FROM {tabela} WHERE {where} ORDER BY codigo LIMIT {int(limite)}", params
        ).fetchall()
    return [{"codigo": c, "descricao": d} for c, d in rows]


def nomes_para_chips(f: Filtros) -> dict[str, dict]:
    """Descricoes de codigos usados nos filtros (municipio, CNAE, natureza...)."""
    pedidos = {
        "municipio": "municipios", "natureza": "naturezas", "motivo": "motivos",
        "cnae_principal": "cnaes", "cnae_secundario": "cnaes", "cnae_secundario_todos": "cnaes", "cnae": "cnaes",
        "socio_qualificacao": "qualificacoes", "pais": "paises",
    }
    usados = {**f.campos, **f.excluir}
    if not any(k in usados for k in pedidos):
        return {}
    ativa = base_ativa()
    if ativa is None:
        return {}
    out: dict[str, dict] = {}
    try:
        with conectar_leitura(ativa.caminho) as conn:
            for campo, tabela in pedidos.items():
                if campo not in usados:
                    continue
                cods = [int(v) for v in _lista(usados[campo]) if re.fullmatch(r"[0-9]{1,9}", str(v))]
                if cods:
                    rows = conn.execute(
                        f"SELECT codigo, descricao FROM {tabela} WHERE codigo IN ({_ph(len(cods))})", cods).fetchall()
                    out[campo] = {str(c): d for c, d in rows}
    except duckdb.Error:
        return {}
    return out


def descricao_campos() -> list[dict]:
    """Catalogo de filtros (para a interface, a API e a documentacao)."""
    return [
        {"nome": c.nome, "rotulo": c.rotulo, "grupo": c.grupo, "grupo_rotulo": GRUPOS_CAMPOS[c.grupo],
         "tipo": c.tipo, "opcoes": [{"valor": v, "rotulo": r} for v, r in c.opcoes], "ajuda": c.ajuda}
        for c in _CAMPOS
    ]


def info_base() -> dict | None:
    ativa = base_ativa()
    if ativa is None:
        return None
    try:
        with conectar_leitura(ativa.caminho) as conn:
            meta = metadados(conn)
    except duckdb.Error:
        return None
    return {"mes": meta.get("mes_referencia"), "schema_ok": meta.get("schema_versao") == SCHEMA_VERSAO}

