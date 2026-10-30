"""Ferramentas do chat: as mesmas do servidor MCP (uma camada so para os dois)
mais consultas SQL somente-leitura nas duas bases.

Como o chat so abre para o administrador, as ferramentas administrativas do MCP
(atualizar a base, pausar/retomar, contatos complementares) ficam disponiveis.
Toda chamada que altera dados grava auditoria com origem=chat.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from app import mcp_server
from app.chat import sql

DICAS_SQL_BASE = (
    "Tabelas principais: estabelecimentos (uma linha por CNPJ, ja com razao_social, natureza_juridica, "
    "porte_empresa, capital_social, opcao_simples/opcao_mei, telefone_1/telefone_2/email normalizados), "
    "empresas (por cnpj_basico), socios (cpf_cnpj_socio vem MASCARADO pela Receita), socio_resumo, simples, "
    "e as tabelas de codigos cnaes, municipios, naturezas, motivos, qualificacoes, paises, municipios_uf. "
    "Codigos: situacao_cadastral 1=NULA 2=ATIVA 3=SUSPENSA 4=INAPTA 8=BAIXADA; identificador_matriz_filial "
    "1=matriz 2=filial; porte_empresa 0=nao informado 1=micro empresa 3=EPP 5=demais; opcao_simples/opcao_mei "
    "'S'/'N'; municipio = codigo da Receita (JOIN municipios ON codigo = municipio); cnae_fiscal_principal "
    "inteiro de 7 digitos (JOIN cnaes ON codigo = cnae_fiscal_principal); cnaes_secundarios INTEGER[]. "
    "Use cnpj_completo para o CNPJ de 14 caracteres. A tabela metadata tem mes_referencia."
)

EXTRAS: list[dict[str, Any]] = [
    {
        "nome": "descrever_bancos",
        "descricao": "Lista tabelas e colunas da base da Receita (DuckDB) e do banco do app (SQLite) para "
                     "montar consultas SQL. Chame antes da primeira consulta SQL da conversa.",
        "esquema": {"type": "object", "properties": {}},
    },
    {
        "nome": "consultar_sql_base",
        "descricao": "Executa UM SELECT (ou WITH ... SELECT) somente-leitura na base oficial da Receita (DuckDB, "
                     "dialeto DuckDB). Use para perguntas que as ferramentas prontas nao cobrem: rankings, "
                     "agregacoes, cruzamentos. Resultado limitado em linhas; agregue sempre que possivel. "
                     + DICAS_SQL_BASE,
        "esquema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "Uma instrucao SELECT/WITH."},
                "limite": {"type": "integer", "description": "Maximo de linhas devolvidas."},
            },
            "required": ["sql"],
        },
    },
    {
        "nome": "consultar_sql_app",
        "descricao": "Executa UM SELECT somente-leitura no banco do app (SQLite): listas (lista_empresas, "
                     "lista_item), buscas salvas (busca_salva), historico de consultas (history), favoritos "
                     "(favorite), contatos complementares (contato_complementar), tarefas da base (base_job, "
                     "base_job_log), auditoria (auditoria_admin) e conversas do chat (chat_conversa).",
        "esquema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "Uma instrucao SELECT/WITH (dialeto SQLite)."},
                "limite": {"type": "integer"},
            },
            "required": ["sql"],
        },
    },
]
_NOMES_EXTRAS = {e["nome"] for e in EXTRAS}
_ARQUIVO_EXPORTADO = re.compile(r"^(mcp|chat)-\d{8}-\d{6}(-[0-9a-f]{8})?\.(xlsx|csv)$")

_lock = threading.Lock()


def _preparar_sync() -> None:
    with _lock:
        if not mcp_server._admin_registrado:  # idempotente; religa se alguem desligou
            mcp_server.configurar(True)


async def _preparar() -> None:
    # Confere sempre (custo zero quando ja esta pronto): as ferramentas
    # administrativas podem ter sido desligadas no servidor MCP deste processo.
    if not mcp_server._admin_registrado:
        await asyncio.to_thread(_preparar_sync)


async def catalogo() -> list[dict[str, Any]]:
    """Ferramentas em formato neutro: {nome, descricao, esquema}."""
    await _preparar()
    itens = [{"nome": t.name, "descricao": (t.description or "").strip(), "esquema": t.input_schema}
             for t in await mcp_server.mcp.list_tools()]
    return [*itens, *EXTRAS]


def arquivo_exportado_valido(nome: str) -> bool:
    return bool(_ARQUIVO_EXPORTADO.fullmatch(nome or ""))


def _link_download(dados: Any) -> Any:
    if isinstance(dados, dict) and isinstance(dados.get("arquivo"), str):
        nome = Path(dados["arquivo"]).name
        if arquivo_exportado_valido(nome):
            dados["link_download"] = f"/chat/arquivos/{nome}"
    return dados


def _texto(dados: Any) -> str:
    return json.dumps(dados, ensure_ascii=False, default=str)


def _cortar(texto: str, maximo: int) -> str:
    if len(texto) <= maximo:
        return texto
    return (texto[:maximo] + f"\n[resultado cortado: {len(texto)} caracteres no total. Refine a consulta "
            "(menos linhas, por_pagina menor, filtros ou agregacao) para ver o restante.]")


async def _extra(nome: str, entrada: dict[str, Any]) -> Any:
    from app.services import busca_avancada as ba

    try:
        if nome == "descrever_bancos":
            base: Any
            try:
                base = await asyncio.to_thread(sql.esquema_base)
            except ba.BuscaIndisponivel as e:
                base = {"indisponivel": str(e)}
            return {"base_receita": base, "banco_app": await asyncio.to_thread(sql.esquema_app),
                    "dicas_base_receita": DICAS_SQL_BASE}
        consulta = str(entrada.get("sql") or "")
        limite = entrada.get("limite")
        if limite is not None and not isinstance(limite, int):
            raise ToolError("`limite` deve ser um número inteiro.")
        if nome == "consultar_sql_base":
            r = await asyncio.to_thread(sql.consultar_base, consulta, limite)
        else:
            r = await asyncio.to_thread(sql.consultar_app, consulta, limite)
        mcp_server._auditar("chat.sql", f"banco={'receita' if nome.endswith('base') else 'app'} "
                                        f"linhas={r['quantidade']} caracteres_sql={len(consulta)}")
        return r
    except sql.ConsultaInvalida as e:
        raise ToolError(str(e)) from e
    except ba.BuscaIndisponivel as e:
        raise mcp_server._base_ou_erro(e) from e


async def executar(nome: str, entrada: Any, *, max_chars: int) -> tuple[bool, str]:
    """Roda uma ferramenta. Devolve (ok, texto para o modelo). Nunca levanta."""
    await _preparar()
    if not isinstance(entrada, dict):
        return False, "Entrada inválida: esperado um objeto JSON com os parâmetros da ferramenta."
    token = mcp_server.ORIGEM.set("chat")
    try:
        if nome in _NOMES_EXTRAS:
            texto = _texto(await _extra(nome, entrada))
        else:
            r = await mcp_server.mcp.call_tool(nome, entrada)
            partes = [getattr(c, "text", "") for c in (getattr(r, "content", None) or [])]
            texto = "\n".join(p for p in partes if p)
            if getattr(r, "is_error", False):
                return False, _cortar(texto or "Erro na ferramenta.", max_chars)
            try:
                texto = _texto(_link_download(json.loads(texto)))
            except ValueError:
                pass
        return True, _cortar(texto, max_chars)
    except ToolError as e:
        msg = re.sub(r"^Error executing tool \w+: ", "", str(e))
        if msg.startswith("Unknown tool"):
            msg = f"Ferramenta desconhecida: {nome}"
        return False, msg
    except Exception as e:  # noqa: BLE001 -- erro inesperado vira resultado de erro para o modelo
        return False, f"Erro inesperado na ferramenta {nome}: {type(e).__name__}"
    finally:
        mcp_server.ORIGEM.reset(token)
