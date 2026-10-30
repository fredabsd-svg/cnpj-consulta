"""Ficha cadastral completa a partir da base local da Receita.

Mostra SOMENTE campos que existem na base publica. Dados oficiais e dados
complementares (contatos cadastrados por um administrador, com fonte) ficam
separados e identificados. CPF de socio vem mascarado pela Receita
(***123456**); o app nunca tenta reconstitui-lo e mascara de novo qualquer
documento de pessoa fisica que chegue sem mascara.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import quote_plus

import duckdb

from app.core.normalizer import normalize_name_for_search
from app.receita.armazenamento import base_ativa
from app.receita.duck import SCHEMA_VERSAO, conectar_leitura, metadados, rows_as_dicts
from app.receita.layout import (
    FAIXAS_ETARIAS,
    MATRIZ_FILIAL,
    PORTES,
    QUALIFICACOES_ADMINISTRACAO,
    SITUACOES,
    TIPOS_SOCIO,
)

ORIGEM_RFB = "Receita Federal — dados abertos do CNPJ"


class BaseIndisponivel(RuntimeError):
    pass


def _abrir():
    ativa = base_ativa()
    if ativa is None:
        raise BaseIndisponivel("Base local da Receita não instalada.")
    return ativa


def _schema_ok(conn) -> bool:
    return metadados(conn).get("schema_versao") == SCHEMA_VERSAO


def mascarar_documento(doc: str | None, tipo: int | None) -> str | None:
    """Formata o documento do socio como publicado; PF sempre mascarado."""
    if not doc:
        return None
    d = doc.strip()
    if d.strip("*0") == "":  # ***000000**: a Receita nao informou documento
        return None
    if tipo == 1 and re.fullmatch(r"[0-9A-Z]{12}\d{2}", d.upper()):  # PJ: CNPJ (inclusive alfanumerico) e publico
        d = d.upper()
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if re.fullmatch(r"\*{3}\d{6}\*{2}", d):  # padrao da Receita
        return f"***.{d[3:6]}.{d[6:9]}-**"
    if re.fullmatch(r"\d{11}", d):  # nunca deveria vir assim: mascara igual a Receita
        return f"***.{d[3:6]}.{d[6:9]}-**"
    if tipo == 3:
        return None  # estrangeiro: a Receita nao publica documento util
    return "***" if any(c.isdigit() for c in d) else d


def formatar_telefone(t: str | None) -> str | None:
    if not t:
        return None
    if len(t) == 11:
        return f"({t[:2]}) {t[2:7]}-{t[7:]}"
    if len(t) == 10:
        return f"({t[:2]}) {t[2:6]}-{t[6:]}"
    return t


def _desc(conn, tabela: str, codigo: Any) -> str | None:
    if codigo is None:
        return None
    row = conn.execute(f"SELECT descricao FROM {tabela} WHERE codigo = ?", [int(codigo)]).fetchone()
    return row[0] if row else None


def _idade(d: date | None) -> int | None:
    if not d:
        return None
    hoje = date.today()
    return hoje.year - d.year - ((hoje.month, hoje.day) < (d.month, d.day))


def _mapa_url(e: dict) -> str | None:
    partes = [" ".join(filter(None, [e.get("tipo_logradouro"), e.get("logradouro")])), e.get("numero"),
              e.get("bairro"), e.get("municipio_nome"), e.get("uf"), e.get("cep")]
    texto = ", ".join(p for p in partes if p)
    if not texto or not e.get("uf"):
        return None
    return "https://www.openstreetmap.org/search?query=" + quote_plus(texto + ", Brasil")


def ficha_local(cnpj: str) -> dict[str, Any] | None:
    """Ficha da base local (None se o CNPJ nao estiver la)."""
    ativa = _abrir()
    with conectar_leitura(ativa.caminho) as conn:
        if not _schema_ok(conn):
            raise BaseIndisponivel("A base local está no formato antigo. Atualize pelo painel Base de Dados CNPJ.")
        meta = metadados(conn)
        rows = conn.execute(
            """
            SELECT e.*, m.descricao AS municipio_nome, p.descricao AS pais_nome,
                   mot.descricao AS motivo_descricao, c.descricao AS cnae_descricao,
                   em.qualificacao_responsavel, em.ente_federativo_responsavel,
                   n.descricao AS natureza_descricao, q.descricao AS qualificacao_responsavel_descricao,
                   s.data_opcao_simples, s.data_exclusao_simples, s.data_opcao_mei, s.data_exclusao_mei
            FROM estabelecimentos e
            LEFT JOIN municipios m ON m.codigo = e.municipio
            LEFT JOIN paises p ON p.codigo = e.pais
            LEFT JOIN motivos mot ON mot.codigo = e.motivo_situacao_cadastral
            LEFT JOIN cnaes c ON c.codigo = e.cnae_fiscal_principal
            LEFT JOIN empresas em ON em.cnpj_basico = e.cnpj_basico
            LEFT JOIN naturezas n ON n.codigo = e.natureza_juridica
            LEFT JOIN qualificacoes q ON q.codigo = em.qualificacao_responsavel
            LEFT JOIN simples s ON s.cnpj_basico = e.cnpj_basico
            WHERE e.cnpj_completo = ?
            """,
            [cnpj],
        ).fetchall()
        if not rows:
            return None
        e = rows_as_dicts(conn, rows)[0]
        mes = meta.get("mes_referencia")

        sec = e.get("cnaes_secundarios") or []
        desc_sec = dict(conn.execute(
            "SELECT codigo, descricao FROM cnaes WHERE list_contains(?, codigo)", [sec]).fetchall()) if sec else {}

        socios = _socios(conn, e["cnpj_basico"])
        estabs_total = conn.execute(
            "SELECT count(*) FROM estabelecimentos WHERE cnpj_basico = ?", [e["cnpj_basico"]]).fetchone()[0]
        estabs = rows_as_dicts(conn, conn.execute(
            """
            SELECT e.cnpj_completo, e.identificador_matriz_filial, e.nome_fantasia, e.situacao_cadastral,
                   e.uf, m.descricao AS municipio_nome, e.data_inicio_atividade
            FROM estabelecimentos e LEFT JOIN municipios m ON m.codigo = e.municipio
            WHERE e.cnpj_basico = ? ORDER BY e.cnpj_ordem LIMIT 300
            """,
            [e["cnpj_basico"]],
        ).fetchall())

    endereco = {
        "tipo_logradouro": e["tipo_logradouro"], "logradouro": e["logradouro"], "numero": e["numero"],
        "complemento": e["complemento"], "bairro": e["bairro"], "cep": e["cep"], "uf": e["uf"],
        "municipio_codigo": e["municipio"], "municipio_nome": e["municipio_nome"],
        "pais_codigo": e["pais"], "pais_nome": e["pais_nome"], "cidade_exterior": e["nome_cidade_exterior"],
        "completo": e["endereco_completo"],
    }
    endereco["mapa_url"] = _mapa_url(endereco)

    ficha = {
        "base": {"mes": mes, "arquivo": ativa.caminho.name, "importado_em": meta.get("importado_em"),
                 "fonte": ORIGEM_RFB},
        "identificacao": {
            "cnpj": e["cnpj_completo"],
            "cnpj_basico": e["cnpj_basico"],
            "matriz_filial": MATRIZ_FILIAL.get(e["identificador_matriz_filial"]),
            "razao_social": e["razao_social"],
            "nome_fantasia": e["nome_fantasia"],
            "situacao": SITUACOES.get(e["situacao_cadastral"]),
            "situacao_codigo": e["situacao_cadastral"],
            "data_situacao": e["data_situacao_cadastral"],
            "motivo": e["motivo_descricao"] if e["motivo_situacao_cadastral"] else None,
            "situacao_especial": e["situacao_especial"],
            "data_situacao_especial": e["data_situacao_especial"],
            "data_abertura": e["data_inicio_atividade"],
            "idade_anos": _idade(e["data_inicio_atividade"]),
            "natureza_codigo": e["natureza_juridica"],
            "natureza": e["natureza_descricao"],
            "porte_codigo": e["porte_empresa"],
            "porte": PORTES.get(e["porte_empresa"]) if e["porte_empresa"] is not None else None,
            "capital_social": float(e["capital_social"]) if e["capital_social"] is not None else None,
            "ente_federativo": e["ente_federativo_responsavel"],
            "qualificacao_responsavel": e["qualificacao_responsavel_descricao"],
            "simples": _opcao(e["opcao_simples"], e["data_opcao_simples"], e["data_exclusao_simples"]),
            "mei": _opcao(e["opcao_mei"], e["data_opcao_mei"], e["data_exclusao_mei"]),
        },
        "endereco": endereco,
        "contatos_oficiais": _contatos_oficiais(e, mes),
        "cnaes": {
            "principal": {"codigo": e["cnae_fiscal_principal"], "descricao": e["cnae_descricao"]}
            if e["cnae_fiscal_principal"] else None,
            "secundarios": [{"codigo": c, "descricao": desc_sec.get(c)} for c in sec],
        },
        "socios": socios,
        "estabelecimentos": {"total": estabs_total, "itens": [
            {**x, "matriz_filial": MATRIZ_FILIAL.get(x["identificador_matriz_filial"]),
             "situacao": SITUACOES.get(x["situacao_cadastral"])} for x in estabs
        ]},
    }
    ficha["historico"] = _historico(ficha, e, socios)
    ficha["alteracoes"] = alteracoes_desde_anterior(cnpj, ficha)
    return ficha


def _opcao(flag: str | None, inicio: date | None, fim: date | None) -> dict | None:
    if flag is None:
        return None
    return {"optante": flag == "S", "data_opcao": inicio, "data_exclusao": fim}


def _contatos_oficiais(e: dict, mes: str | None) -> list[dict]:
    out = []
    for i, posicao in ((1, "principal"), (2, "secundario")):
        t = e.get(f"telefone_{i}")
        bruto = " ".join(filter(None, [e.get(f"ddd_telefone_{i}"), e.get(f"numero_telefone_{i}")]))
        if t or bruto:
            out.append({
                "tipo": "telefone", "posicao": posicao, "valor": t or bruto,
                "formatado": formatar_telefone(t) or bruto, "subtipo": e.get(f"tipo_telefone_{i}") or "indeterminado",
                "valido": bool(t),
                # Publicado sem o nono digito: o app nao completa o numero.
                "sem_nono_digito": bool(t) and len(t) == 10 and e.get(f"tipo_telefone_{i}") == "celular",
            })
    fax = " ".join(filter(None, [e.get("ddd_fax"), e.get("numero_fax")]))
    if fax:
        out.append({"tipo": "fax", "posicao": "fax", "valor": fax, "formatado": fax, "subtipo": None, "valido": True})
    if e.get("correio_eletronico"):
        out.append({
            "tipo": "email", "posicao": "principal", "valor": e.get("email") or e["correio_eletronico"],
            "formatado": e.get("email") or e["correio_eletronico"], "valido": bool(e.get("email")),
            "subtipo": None if not e.get("email") else ("webmail" if e.get("email_webmail") else "dominio_proprio"),
        })
    for c in out:
        c.update(origem=ORIGEM_RFB, origem_tipo="oficial", data_referencia=mes, verificado=None,
                 observacao="Informado pela própria empresa à Receita; pode estar desatualizado.")
    return out


def _socios(conn, cnpj_basico: str) -> list[dict]:
    rows = rows_as_dicts(conn, conn.execute(
        """
        SELECT s.*, q.descricao AS qualificacao_descricao, p.descricao AS pais_nome,
               qr.descricao AS qualificacao_representante_descricao, r.qtd_empresas
        FROM socios s
        LEFT JOIN qualificacoes q ON q.codigo = s.qualificacao_socio
        LEFT JOIN qualificacoes qr ON qr.codigo = s.qualificacao_representante_legal
        LEFT JOIN paises p ON p.codigo = s.pais
        LEFT JOIN socio_resumo r ON r.socio_chave = s.socio_chave
        WHERE s.cnpj_basico = ?
        ORDER BY s.nome_socio_norm
        """,
        [cnpj_basico],
    ).fetchall())
    out = []
    for s in rows:
        tipo = s["identificador_de_socio"]
        rep = None
        if s.get("nome_representante") or s.get("representante_legal"):
            rep = {
                "nome": s.get("nome_representante"),
                "documento": mascarar_documento(s.get("representante_legal"), 2),
                "qualificacao": s.get("qualificacao_representante_descricao"),
            }
        out.append({
            "chave": s["socio_chave"],
            "nome": s["nome_socio"],
            "tipo_codigo": tipo,
            "tipo": TIPOS_SOCIO.get(tipo),
            "documento": mascarar_documento(s["cpf_cnpj_socio"], tipo),
            "qualificacao_codigo": s["qualificacao_socio"],
            "qualificacao": s["qualificacao_descricao"],
            "administrador": s["qualificacao_socio"] in QUALIFICACOES_ADMINISTRACAO,
            "data_entrada": s["data_entrada_sociedade"],
            "pais": s["pais_nome"] if s.get("pais") else None,
            "faixa_etaria": FAIXAS_ETARIAS.get(s["faixa_etaria"]) if s.get("faixa_etaria") else None,
            "representante": rep,
            "qtd_empresas": s.get("qtd_empresas"),
        })
    return out


def _historico(ficha: dict, e: dict, socios: list[dict]) -> list[dict]:
    """Linha do tempo com as datas que a base publica traz (a Receita nao
    publica o historico completo de alteracoes)."""
    ev: list[dict] = []
    ident = ficha["identificacao"]
    if ident["data_abertura"]:
        ev.append({"data": ident["data_abertura"], "evento": "Início da atividade (abertura)"})
    if ident["data_situacao"]:
        txt = f"Situação cadastral: {ident['situacao'] or '?'}"
        if ident["motivo"]:
            txt += f" — {ident['motivo']}"
        ev.append({"data": ident["data_situacao"], "evento": txt})
    if ident["situacao_especial"] and ident["data_situacao_especial"]:
        ev.append({"data": ident["data_situacao_especial"], "evento": f"Situação especial: {ident['situacao_especial']}"})
    for nome, op in (("Simples Nacional", ident["simples"]), ("MEI", ident["mei"])):
        if op and op["data_opcao"]:
            ev.append({"data": op["data_opcao"], "evento": f"Opção pelo {nome}"})
        if op and op["data_exclusao"]:
            ev.append({"data": op["data_exclusao"], "evento": f"Exclusão do {nome}"})
    for s in socios:
        if s["data_entrada"]:
            ev.append({"data": s["data_entrada"],
                       "evento": f"Entrada de {s['nome']} no quadro societário ({s['qualificacao'] or 'qualificação não informada'})"})
    return sorted(ev, key=lambda x: x["data"], reverse=True)


_CAMPOS_COMPARADOS = (
    ("situacao_cadastral", "Situação cadastral"),
    ("razao_social", "Razão social"),
    ("nome_fantasia", "Nome fantasia"),
    ("cnae_fiscal_principal", "CNAE principal"),
    ("logradouro", "Logradouro"),
    ("numero", "Número"),
    ("cep", "CEP"),
    ("municipio", "Município (código)"),
    ("telefone_1", "Telefone principal"),
    ("email", "E-mail"),
    ("capital_social", "Capital social"),
    ("porte_empresa", "Porte"),
    ("opcao_simples", "Opção pelo Simples"),
    ("opcao_mei", "Opção pelo MEI"),
    ("qtd_socios", "Quantidade de sócios"),
)


def alteracoes_desde_anterior(cnpj: str, ficha: dict | None = None) -> dict | None:
    """Compara com a versao anterior da base, se ela ainda estiver no disco."""
    ativa = base_ativa()
    if ativa is None or not ativa.anterior:
        return None
    anterior = ativa.caminho.parent / ativa.anterior
    if not anterior.is_file():
        return None
    cols = ", ".join(c for c, _ in _CAMPOS_COMPARADOS)
    try:
        with conectar_leitura(anterior) as old, conectar_leitura(ativa.caminho) as new:
            if not _schema_ok(old):
                return None
            mes_old = metadados(old).get("mes_referencia")
            a = old.execute(f"SELECT {cols} FROM estabelecimentos WHERE cnpj_completo = ?", [cnpj]).fetchone()
            b = new.execute(f"SELECT {cols} FROM estabelecimentos WHERE cnpj_completo = ?", [cnpj]).fetchone()
    except duckdb.Error:
        return None
    if b is None:
        return None
    if a is None:
        return {"mes_anterior": mes_old, "novo": True, "mudancas": []}
    mudancas = []
    for (campo, rotulo), va, vb in zip(_CAMPOS_COMPARADOS, a, b, strict=True):
        if va != vb:
            if campo == "situacao_cadastral":
                va, vb = SITUACOES.get(va, va), SITUACOES.get(vb, vb)
            # Contato e dado pessoal em potencial: so indica que mudou.
            if campo in ("telefone_1", "email"):
                va, vb = ("(vazio)" if not va else "(anterior)"), ("(vazio)" if not vb else "(atual)")
            mudancas.append({"campo": rotulo, "antes": va, "depois": vb})
    return {"mes_anterior": mes_old, "novo": False, "mudancas": mudancas}


# ---------- socio ----------


def ficha_socio(chave: str, limite: int = 500) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{16}", chave or ""):
        return None
    ativa = _abrir()
    with conectar_leitura(ativa.caminho) as conn:
        if not _schema_ok(conn):
            raise BaseIndisponivel("A base local está no formato antigo.")
        r = conn.execute("SELECT * FROM socio_resumo WHERE socio_chave = ?", [chave]).fetchone()
        if r is None:
            return None
        resumo = rows_as_dicts(conn, [r])[0]
        empresas = rows_as_dicts(conn, conn.execute(
            """
            SELECT e.cnpj_completo AS cnpj, e.razao_social, e.nome_fantasia, e.situacao_cadastral,
                   e.uf, m.descricao AS municipio_nome, q.descricao AS qualificacao,
                   s.qualificacao_socio, s.data_entrada_sociedade AS data_entrada
            FROM socios s
            JOIN estabelecimentos e ON e.cnpj_basico = s.cnpj_basico AND e.identificador_matriz_filial = 1
            LEFT JOIN municipios m ON m.codigo = e.municipio
            LEFT JOIN qualificacoes q ON q.codigo = s.qualificacao_socio
            WHERE s.socio_chave = ?
            ORDER BY e.razao_social LIMIT ?
            """,
            [chave, limite],
        ).fetchall())
    tipo = resumo["identificador_de_socio"]
    for x in empresas:
        x["situacao"] = SITUACOES.get(x["situacao_cadastral"])
        x["administrador"] = x["qualificacao_socio"] in QUALIFICACOES_ADMINISTRACAO
    return {
        "chave": chave,
        "nome": resumo["nome_socio"],
        "tipo": TIPOS_SOCIO.get(tipo),
        "tipo_codigo": tipo,
        "documento": mascarar_documento(resumo["cpf_cnpj_socio"], tipo),
        "qtd_empresas": resumo["qtd_empresas"],
        "empresas": empresas,
        "identificacao_provavel": tipo == 2,
    }


# ---------- sugestoes ----------


def sugestoes(q: str, limite: int = 8) -> list[dict]:
    """Autocompletar por CNPJ (prefixo) ou razao social/nome fantasia (prefixo)."""
    ativa = base_ativa()
    if ativa is None:
        return []
    q = (q or "").strip()
    digitos = re.sub(r"\D", "", q)
    limite = max(1, min(limite, 20))
    with conectar_leitura(ativa.caminho) as conn:
        if not _schema_ok(conn):
            return []
        if len(digitos) >= 4 and len(digitos) == len(re.sub(r"[\s./-]", "", q)):
            rows = conn.execute(
                """
                SELECT e.cnpj_completo, e.razao_social AS nome, 'C' AS tipo
                FROM estabelecimentos e WHERE e.cnpj_completo >= ? AND e.cnpj_completo < ?
                ORDER BY e.cnpj_completo LIMIT ?
                """,
                [digitos, digitos + "~", limite],
            ).fetchall()
        else:
            norm = normalize_name_for_search(re.sub(r"[ºª°]", "", q)) or ""  # base tira o ordinal
            if len(norm) < 3:
                return []
            rows = conn.execute(
                """
                SELECT cnpj_completo, nome_norm AS nome, tipo FROM nomes_busca
                WHERE nome_norm >= ? AND nome_norm < ?
                ORDER BY nome_norm, tipo DESC LIMIT ?
                """,
                [norm, norm + "~", limite * 3],
            ).fetchall()
        vistos, cnpjs = set(), []
        for cnpj, _nome, _tipo in rows:
            if cnpj not in vistos:
                vistos.add(cnpj)
                cnpjs.append(cnpj)
        cnpjs = cnpjs[:limite]
        if not cnpjs:
            return []
        det = {r[0]: r for r in conn.execute(
            f"""
            SELECT e.cnpj_completo, e.razao_social, e.nome_fantasia, e.situacao_cadastral, e.uf,
                   m.descricao, e.identificador_matriz_filial
            FROM estabelecimentos e LEFT JOIN municipios m ON m.codigo = e.municipio
            WHERE e.cnpj_completo IN ({", ".join("?" * len(cnpjs))})
            """,
            cnpjs,
        ).fetchall()}
    out = []
    for c in cnpjs:
        if c in det:
            _, razao, fantasia, sit, uf, mun, mf = det[c]
            out.append({"cnpj": c, "razao_social": razao, "nome_fantasia": fantasia,
                        "situacao": SITUACOES.get(sit), "uf": uf, "municipio": mun,
                        "matriz_filial": MATRIZ_FILIAL.get(mf)})
    return out
