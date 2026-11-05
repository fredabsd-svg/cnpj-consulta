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


def limitar_threads(conn: duckdb.DuckDBPyConnection, mb_por_thread: int = 512) -> int:
    """Junçoes grandes reservam memoria por thread: com muitos nucleos e pouca
    memoria o DuckDB fica sem espaco. Garante ~mb_por_thread MB por thread."""
    limite = conn.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    atual = int(conn.execute("SELECT current_setting('threads')").fetchone()[0])
    mb = _em_mb(str(limite))
    if mb is None:
        return atual
    maximo = max(1, int(mb // mb_por_thread))
    if atual > maximo:
        conn.execute(f"SET threads = {maximo}")
        return maximo
    return atual


def _em_mb(valor: str) -> float | None:
    """'3.7 GiB', '4GB', '512.0 MiB' -> MB."""
    import re

    m = re.fullmatch(r"\s*([\d.]+)\s*([KMGT]?)I?B\s*", valor.upper())
    if not m:
        return None
    fator = {"": 1 / 1_000_000, "K": 1 / 1000, "M": 1, "G": 1000, "T": 1_000_000}[m[2]]
    return float(m[1]) * fator


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


# Conexoes somente-leitura mantidas abertas por arquivo de base: o cache do DuckDB
# (blocos ja lidos e descompactados) fica quente entre as consultas. Abrir o
# arquivo a cada consulta jogava esse cache fora e, numa base de 60 milhoes de
# linhas num HD externo, cada busca relia gigabytes do disco.
_leituras: dict[str, duckdb.DuckDBPyConnection] = {}
_em_uso: dict[int, int] = {}  # id(conexao) -> cursores abertos
_aposentadas: dict[int, duckdb.DuckDBPyConnection] = {}  # fechar quando o ultimo cursor sair
_leituras_lock = threading.Lock()


def _chave(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


@contextmanager
def conectar_leitura(path: str | Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Cursor somente-leitura sobre a conexao compartilhada do arquivo.

    Cada uso recebe o proprio cursor (seguro entre threads). As conexoes sao
    fechadas por fechar_leituras() quando a versao ativa muda ou um arquivo vai
    ser apagado (no Windows, arquivo aberto nao pode ser removido)."""
    chave = _chave(path)
    with _leituras_lock:
        base = _leituras.get(chave)
        if base is None:
            base = duckdb.connect(chave, read_only=True)
            # O DuckDB reaproveita o banco ja aberto neste processo (ex.: uma consulta
            # ainda rodando na conexao antiga quando a versao foi trocada). Esse banco
            # ja foi configurado e travado aqui: so confere a protecao.
            travada = base.execute("SELECT current_setting('lock_configuration')").fetchone()[0]
            if travada:
                if base.execute("SELECT current_setting('enable_external_access')").fetchone()[0]:
                    base.close()
                    raise RuntimeError("Conexao de leitura travada sem a protecao de acesso externo.")
            else:
                _configurar_leitura(base)
            _leituras[chave] = base
        cur = base.cursor()
        _em_uso[id(base)] = _em_uso.get(id(base), 0) + 1
    try:
        yield cur
    finally:
        cur.close()
        with _leituras_lock:
            restantes = _em_uso.get(id(base), 1) - 1
            if restantes > 0:
                _em_uso[id(base)] = restantes
            else:
                _em_uso.pop(id(base), None)
                velha = _aposentadas.pop(id(base), None)
                if velha is not None:  # versao trocada durante esta consulta: fecha agora
                    _fechar(velha)


def _fechar(conn: duckdb.DuckDBPyConnection) -> None:
    try:
        conn.close()
    except duckdb.Error:
        pass


def _configurar_leitura(base: duckdb.DuckDBPyConnection) -> None:
    cfg = get_settings()
    if cfg.receita_duckdb_memoria:
        base.execute(f"SET memory_limit = '{_safe_size(cfg.receita_duckdb_memoria)}'")
    if cfg.receita_duckdb_threads > 0:
        base.execute(f"SET threads = {int(cfg.receita_duckdb_threads)}")
    limitar_threads(base)  # muitos nucleos com pouca memoria estouram as juncoes
    # O que nao couber na memoria vai para disco, com teto (nunca enche o disco)
    temp = Path(cfg.receita_temp_dir.strip()).expanduser() / "cnpj-consulta" if cfg.receita_temp_dir.strip() \
        else Path(cfg.receita_data_dir).expanduser() / "trabalho"
    (temp / "leitura").mkdir(parents=True, exist_ok=True)
    base.execute(f"SET temp_directory = '{_sql_path(temp / 'leitura')}'")
    base.execute(f"SET max_temp_directory_size = '{_safe_size(cfg.busca_temp_max)}'")
    # Somente leitura de verdade: sem ler/gravar outros arquivos, rede ou extensoes,
    # e configuracao travada (vale tambem para o SQL do chat, que usa esta mesma
    # conexao). Ajustes so aqui, na criacao.
    base.execute("SET enable_external_access = false")
    base.execute("SET lock_configuration = true")


def fechar_leituras(path: str | Path | None = None) -> None:
    """Fecha as conexoes de leitura (de um arquivo ou todas). Novas consultas ja
    abrem conexao nova; uma conexao com consulta em andamento (ex.: a versao
    ativa mudou no meio de uma busca) so fecha quando essa consulta termina."""
    with _leituras_lock:
        chaves = [_chave(path)] if path is not None else list(_leituras)
        for k in chaves:
            conn = _leituras.pop(k, None)
            if conn is None:
                continue
            if _em_uso.get(id(conn)):
                _aposentadas[id(conn)] = conn
            else:
                _fechar(conn)


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
