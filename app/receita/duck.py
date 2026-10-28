"""Conexoes DuckDB da base local (somente leitura para consultas)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb

from app.config import Settings, get_settings

SCHEMA_VERSAO = "2"


def configurar(conn: duckdb.DuckDBPyConnection, temp_dir: Path | None, settings: Settings | None = None) -> None:
    """Limita memoria/threads: o excedente vai para disco (nunca a base toda na RAM)."""
    settings = settings or get_settings()
    if settings.receita_duckdb_memoria:
        conn.execute(f"SET memory_limit = '{_safe_size(settings.receita_duckdb_memoria)}'")
    if settings.receita_duckdb_threads > 0:
        conn.execute(f"SET threads = {int(settings.receita_duckdb_threads)}")
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
        conn.execute(f"SET temp_directory = '{_sql_path(temp_dir)}'")
    conn.execute("SET preserve_insertion_order = false")


def _safe_size(valor: str) -> str:
    v = valor.strip().upper().replace(" ", "")
    num = v.rstrip("KMGTIB%")
    if not num.replace(".", "", 1).isdigit():
        raise ValueError(f"RECEITA_DUCKDB_MEMORIA invalida: {valor!r} (ex.: 4GB)")
    return v


def _sql_path(p: Path) -> str:
    return p.as_posix().replace("'", "''")


def sql_str(valor: str) -> str:
    return "'" + valor.replace("'", "''") + "'"


@contextmanager
def conectar_leitura(path: str | Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Uma conexao somente-leitura por consulta, fechada em seguida.

    Manter conexoes abertas travaria o arquivo no Windows e impediria a
    remocao de versoes antigas.
    """
    conn = duckdb.connect(str(path), read_only=True)
    try:
        yield conn
    finally:
        conn.close()


def rows_as_dicts(conn: duckdb.DuckDBPyConnection, rows: list[tuple]) -> list[dict[str, Any]]:
    cols = [d[0] for d in conn.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]


def tabelas(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
    return {r[0] for r in rows}


def metadados(conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    if "metadata" not in tabelas(conn):
        return {}
    return dict(conn.execute("SELECT chave, valor FROM metadata").fetchall())


class Vigia:
    """Interrompe a instrucao DuckDB em execucao quando `deve_parar()` vira True.

    Usado para pausar/cancelar importacoes longas e para cancelar buscas: a
    transacao em curso e desfeita pelo DuckDB, sem corromper o arquivo.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, deve_parar: Callable[[], bool], intervalo: float = 1.0):
        self.conn = conn
        self.deve_parar = deve_parar
        self.intervalo = intervalo
        self.disparou = False
        self._fim = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._fim.wait(self.intervalo):
            try:
                if self.deve_parar():
                    self.disparou = True
                    self.conn.interrupt()
                    return
            except Exception:  # noqa: BLE001 -- o vigia nunca derruba a tarefa
                continue

    def __enter__(self) -> Vigia:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._fim.set()
        self._thread.join(timeout=5)
