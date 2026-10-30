"""Consultas SQL somente-leitura para o chat com IA.

Duas bases:

- base da Receita (DuckDB, versao ativa): conexao read_only, sem acesso a
  arquivos, rede ou extensoes (`enable_external_access=false` travado com
  `lock_configuration`), uma unica instrucao SELECT por vez e tempo limite;
- banco do app (SQLite: listas, buscas salvas, historico, auditoria, contatos
  complementares): aberto em modo somente-leitura e com um autorizador que so
  permite leitura e bloqueia a tabela com as chaves dos provedores.

Escritas nunca passam por aqui: alteracoes usam as ferramentas do app, que
validam os dados e gravam auditoria.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import duckdb

from app.config import get_settings
from app.receita.duck import Vigia, _safe_size

TABELAS_BLOQUEADAS = {"chat_provedor"}


class ConsultaInvalida(ValueError):
    pass


def _limite(limite: int | None) -> int:
    teto = get_settings().chat_sql_max_linhas
    return max(1, min(int(limite or teto), teto))


def _resultado(colunas: list[str], linhas: list[tuple], limite: int, t0: float) -> dict[str, Any]:
    from app.mcp_server import _json

    return {
        "colunas": colunas,
        "linhas": _json([list(r) for r in linhas[:limite]]),
        "quantidade": min(len(linhas), limite),
        "truncado": len(linhas) > limite,
        "limite": limite,
        "ms": round((time.monotonic() - t0) * 1000),
    }


# ---------------------------------------------------------------- base da Receita (DuckDB)


def _caminho_base() -> Path:
    from app.services import busca_avancada as ba

    return Path(ba._base().caminho)


def _conectar_base(path: Path) -> duckdb.DuckDBPyConnection:
    cfg = get_settings()
    conn = duckdb.connect(str(path), read_only=True)
    try:
        if cfg.receita_duckdb_memoria:
            conn.execute(f"SET memory_limit = '{_safe_size(cfg.receita_duckdb_memoria)}'")
        if cfg.receita_duckdb_threads > 0:
            conn.execute(f"SET threads = {int(cfg.receita_duckdb_threads)}")
        conn.execute("SET enable_external_access = false")
        conn.execute("SET lock_configuration = true")
    except Exception:
        conn.close()
        raise
    return conn


def consultar_base(sql: str, limite: int | None = None) -> dict[str, Any]:
    """Executa um SELECT na base da Receita (versao ativa)."""
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise ConsultaInvalida("Informe a consulta SQL.")
    limite = _limite(limite)
    conn = _conectar_base(_caminho_base())
    try:
        try:
            instrucoes = conn.extract_statements(sql)
        except duckdb.Error as e:
            raise ConsultaInvalida(f"SQL inválido: {e}") from e
        if len(instrucoes) != 1 or instrucoes[0].type != duckdb.StatementType.SELECT:
            raise ConsultaInvalida("Só é permitida uma instrução SELECT (ou WITH ... SELECT) por vez.")
        t0 = time.monotonic()
        prazo = t0 + get_settings().chat_sql_timeout_segundos
        try:
            with Vigia(conn, lambda: time.monotonic() > prazo, intervalo=0.2):
                cur = conn.execute(f"SELECT * FROM ({sql}) AS consulta LIMIT {limite + 1}")
                linhas = cur.fetchall()
        except duckdb.InterruptException as e:
            raise ConsultaInvalida(
                "A consulta passou do tempo limite. Filtre mais (UF, município, CNAE) ou agregue.") from e
        except duckdb.Error as e:
            raise ConsultaInvalida(f"Erro na consulta: {e}") from e
        return _resultado([d[0] for d in cur.description], linhas, limite, t0)
    finally:
        conn.close()


def esquema_base() -> dict[str, Any]:
    conn = _conectar_base(_caminho_base())
    try:
        cols = conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position").fetchall()
        meta = dict(conn.execute("SELECT chave, valor FROM metadata").fetchall()) \
            if any(c[0] == "metadata" for c in cols) else {}
    finally:
        conn.close()
    tabelas: dict[str, list[str]] = {}
    for tabela, coluna, tipo in cols:
        tabelas.setdefault(tabela, []).append(f"{coluna} {tipo}")
    return {"metadados": meta, "tabelas": tabelas}


# ---------------------------------------------------------------- banco do app (SQLite)


def _caminho_app() -> str:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        raise ConsultaInvalida("Consulta SQL ao banco do app só é suportada com SQLite.")
    path = url.removeprefix("sqlite:///")
    if ":memory:" in path or not path:
        raise ConsultaInvalida("Banco do app em memória: nada a consultar.")
    return str(Path(path).expanduser().resolve())


_PERMITIDOS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
               getattr(sqlite3, "SQLITE_RECURSIVE", 33)}


def _autorizador(acao: int, arg1: str | None, arg2: str | None, _db: str | None, _gatilho: str | None) -> int:
    if acao not in _PERMITIDOS:
        return sqlite3.SQLITE_DENY
    if acao == sqlite3.SQLITE_READ and (arg1 or "").lower() in TABELAS_BLOQUEADAS:
        return sqlite3.SQLITE_DENY
    if acao == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {"load_extension", "readfile", "writefile"}:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _conectar_app() -> sqlite3.Connection:
    path = _caminho_app()
    if not Path(path).exists():
        raise ConsultaInvalida("O banco do app ainda não foi criado.")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10, check_same_thread=False)
    conn.set_authorizer(_autorizador)
    return conn


def consultar_app(sql: str, limite: int | None = None) -> dict[str, Any]:
    """Executa um SELECT no banco do app (somente leitura)."""
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise ConsultaInvalida("Informe a consulta SQL.")
    if not re.match(r"(?is)^\s*(select|with)\b", sql):
        raise ConsultaInvalida("Só é permitida uma instrução SELECT (ou WITH ... SELECT).")
    limite = _limite(limite)
    conn = _conectar_app()
    t0 = time.monotonic()
    prazo = t0 + get_settings().chat_sql_timeout_segundos
    conn.set_progress_handler(lambda: 1 if time.monotonic() > prazo else 0, 10_000)
    try:
        try:
            cur = conn.execute(sql)
            linhas = cur.fetchmany(limite + 1)
        except (sqlite3.Error, sqlite3.Warning) as e:
            if "interrupted" in str(e):
                raise ConsultaInvalida("A consulta passou do tempo limite.") from e
            if "not authorized" in str(e) or "prohibited" in str(e):
                raise ConsultaInvalida(
                    "Consulta não permitida: só leitura, e a tabela de chaves dos provedores é bloqueada.") from e
            raise ConsultaInvalida(f"Erro na consulta: {e}") from e
        return _resultado([d[0] for d in (cur.description or [])], linhas, limite, t0)
    finally:
        conn.close()


def esquema_app() -> dict[str, list[str]]:
    conn = _conectar_app()
    conn.set_authorizer(None)  # PRAGMA table_info so le o catalogo
    try:
        nomes = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: [f"{c[1]} {c[2]}".strip() for c in conn.execute(f'PRAGMA table_info("{n}")')]
                for n in nomes if n.lower() not in TABELAS_BLOQUEADAS}
    finally:
        conn.close()

