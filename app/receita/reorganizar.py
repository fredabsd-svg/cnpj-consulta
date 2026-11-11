"""Organiza a base por estado (UF) para as buscas regionais ficarem rapidas.

O DuckDB guarda cada coluna em blocos de ~120 mil linhas com o minimo e o maximo
de cada bloco, e pula os blocos que nao podem conter o valor procurado. Com a
tabela `estabelecimentos` gravada em ordem de CNPJ, os estados ficam espalhados
por todos os blocos: uma busca "UF = TO" (0,6% da base) precisa ler a tabela
inteira. Gravando a tabela agrupada por UF (e por CNPJ dentro de cada UF), a
mesma busca le so os blocos do Tocantins -- ate 100 vezes menos disco.

A consulta por CNPJ continua direta gracas a tabela `cnpj_uf` (CNPJ -> UF, em
ordem de CNPJ): com a UF em maos, a leitura volta a cair num unico bloco.

`reorganizar()` copia uma base (publicada ou recem-consolidada) para um arquivo
novo nesse formato, em fatias pequenas (memoria limitada), sem alterar a origem.
O conteudo e identico; so muda a ordem fisica e entra a tabela `cnpj_uf`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb

from app.config import Settings, get_settings
from app.receita.duck import META_ORDEM, ORDEM_UF, Vigia, configurar, limitar_threads, sql_str

log = logging.getLogger(__name__)

LINHAS_POR_FATIA = 1_500_000  # cada fatia e ordenada na memoria: mantem o uso baixo
# Tabelas grandes ja gravadas em ordem pela chave: copiadas em fatias pela mesma chave
CHAVES_ORDENADAS = {"empresas": "cnpj_basico", "socios": "cnpj_basico", "simples": "cnpj_basico",
                    "socio_resumo": "socio_chave", "nomes_busca": "nome_norm"}
# Desempate dentro da chave (mantem a ordem original das linhas)
ORDEM_EXTRA = {"socios": "nome_socio_norm"}


class Interrompida(Exception):  # noqa: N818 -- sinal de pausa/cancelamento, nao erro
    pass


def _limites(conn, tabela: str, chave: str, linhas: int) -> list[Any]:
    """Valores da chave que dividem a tabela em fatias de ~LINHAS_POR_FATIA linhas."""
    n = -(-linhas // LINHAS_POR_FATIA)
    if n <= 1:
        return []
    amostra = sorted(r[0] for r in conn.execute(
        f"SELECT {chave} FROM o.{tabela} WHERE {chave} IS NOT NULL USING SAMPLE reservoir(20000 ROWS) REPEATABLE (7)"
    ).fetchall())
    if not amostra:
        return []
    cortes = sorted({amostra[i * len(amostra) // n] for i in range(1, n)})
    return cortes


def _faixas(chave: str, cortes: list[Any]) -> list[tuple[str, list[Any]]]:
    """Condicoes que cobrem todos os valores da chave, em ordem crescente (nulos por ultimo)."""
    if not cortes:
        return [("TRUE", [])]
    out: list[tuple[str, list[Any]]] = [(f"{chave} < ?", [cortes[0]])]
    for a, b in zip(cortes, cortes[1:], strict=False):
        out.append((f"{chave} >= ? AND {chave} < ?", [a, b]))
    out.append((f"{chave} >= ?", [cortes[-1]]))
    out.append((f"{chave} IS NULL", []))
    return out


def reorganizar(origem: Path, destino: Path, *, settings: Settings | None = None, temp_dir: Path | None = None,
                progresso: Callable[[float, str], None] | None = None,
                deve_parar: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Copia `origem` para `destino` com estabelecimentos agrupados por UF e a
    tabela `cnpj_uf`. Devolve {tabela: linhas}. `destino` nao pode existir."""
    settings = settings or get_settings()
    if destino.exists():
        raise FileExistsError(destino)
    t0 = time.monotonic()
    parar = deve_parar or (lambda: False)
    conn = duckdb.connect(str(destino))
    feitas = 0
    try:
        configurar(conn, temp_dir or destino.parent / "tmp", settings)
        limitar_threads(conn)
        conn.execute(f"ATTACH {sql_str(origem.as_posix())} AS o (READ_ONLY)")
        tabelas = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog = 'o' AND table_schema = 'main' "
            "AND table_type = 'BASE TABLE' ORDER BY table_name").fetchall()]
        if "estabelecimentos" not in tabelas:
            raise ValueError("A base de origem não tem a tabela estabelecimentos.")
        derivadas = ("cnpj_uf", "socios_por_chave")  # recriadas aqui (se a origem ja for organizada)
        contagens = {t: conn.execute(f'SELECT count(*) FROM o."{t}"').fetchone()[0]
                     for t in tabelas if t not in derivadas}
        total = sum(contagens.values()) + contagens["estabelecimentos"]  # + cnpj_uf

        def executar(sql: str, params: list[Any] | None = None) -> None:
            if parar():
                raise Interrompida
            try:
                with Vigia(conn, parar):
                    conn.execute(sql, params or [])
            except duckdb.InterruptException as e:
                raise Interrompida from e

        def avancar(linhas: int, tabela: str) -> None:
            nonlocal feitas
            feitas += linhas
            if progresso:
                progresso(min(1.0, feitas / max(1, total)), tabela)

        for t in sorted(contagens, key=lambda x: (x == "estabelecimentos", x)):  # a maior por ultimo
            executar(f'CREATE TABLE "{t}" AS SELECT * FROM o."{t}" LIMIT 0')
            if t == "estabelecimentos":
                continue
            chave = CHAVES_ORDENADAS.get(t)
            colunas = {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_catalog = 'o' AND table_name = ?", [t]
            ).fetchall()}
            if chave and chave in colunas:
                ordem = ", ".join([chave] + [c for c in [ORDEM_EXTRA.get(t)] if c and c in colunas])
                for cond, params in _faixas(chave, _limites(conn, t, chave, contagens[t])):
                    executar(f'INSERT INTO "{t}" SELECT * FROM o."{t}" WHERE {cond} ORDER BY {ordem}', params)
            else:
                primeira = conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_catalog = 'o' AND table_name = ? "
                    "ORDER BY ordinal_position LIMIT 1", [t]).fetchone()[0]
                executar(f'INSERT INTO "{t}" SELECT * FROM o."{t}" ORDER BY "{primeira}"')
            avancar(contagens[t], t)

        # estabelecimentos: um estado por vez (nulos por ultimo), CNPJ crescente dentro do estado
        n_est = contagens["estabelecimentos"]
        cortes = _limites(conn, "estabelecimentos", "cnpj_completo", n_est)
        ufs = conn.execute("SELECT uf, count(*) FROM o.estabelecimentos GROUP BY 1 ORDER BY 1 NULLS LAST").fetchall()
        for uf, qtd in ufs:
            cond_uf, p_uf = ("uf = ?", [uf]) if uf is not None else ("uf IS NULL", [])
            n = -(-qtd // LINHAS_POR_FATIA)
            meus = sorted({cortes[i * len(cortes) // n] for i in range(1, n)}) if cortes and n > 1 else []
            faixas = [f for f in _faixas("cnpj_completo", meus) if f[0] != "cnpj_completo IS NULL"]
            for cond, params in faixas:
                executar(f"INSERT INTO estabelecimentos SELECT * FROM o.estabelecimentos "
                         f"WHERE {cond_uf} AND {cond} ORDER BY cnpj_completo", [*p_uf, *params])
            avancar(qtd, f"estabelecimentos ({uf or 'sem UF'})")
        # CNPJ sem valor nao existe na base consolidada (a consolidacao exige 14 caracteres)

        # cnpj_uf: CNPJ -> UF, em ordem de CNPJ (a origem ja esta nessa ordem: as faixas leem so o seu trecho)
        executar("CREATE TABLE cnpj_uf (cnpj_completo VARCHAR, cnpj_basico VARCHAR, uf VARCHAR)")
        for cond, params in _faixas("cnpj_completo", cortes):
            executar("INSERT INTO cnpj_uf SELECT cnpj_completo, cnpj_basico, uf FROM o.estabelecimentos "
                     f"WHERE {cond} ORDER BY cnpj_completo", params)
        avancar(n_est, "cnpj_uf")

        # socios_por_chave: participacoes em ordem de socio (a pagina do socio e os filtros
        # por socio leem so o trecho dele; `socios` fica em ordem de empresa)
        colunas_socios = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_catalog = 'o' AND table_name = 'socios'"
        ).fetchall()} if "socios" in contagens else set()
        if {"socio_chave", "cnpj_basico", "qualificacao_socio", "data_entrada_sociedade"} <= colunas_socios:
            executar("CREATE TABLE socios_por_chave AS SELECT socio_chave, cnpj_basico, qualificacao_socio, "
                     "data_entrada_sociedade FROM o.socios LIMIT 0")
            for cond, params in _faixas("socio_chave", _limites(conn, "socios", "socio_chave", contagens["socios"])):
                executar("INSERT INTO socios_por_chave SELECT socio_chave, cnpj_basico, qualificacao_socio, "
                         f"data_entrada_sociedade FROM o.socios WHERE {cond} ORDER BY socio_chave, cnpj_basico", params)
            if conn.execute("SELECT count(*) FROM socios_por_chave").fetchone()[0] != contagens["socios"]:
                raise RuntimeError("Reorganização: socios_por_chave incompleta.")

        if "metadata" in contagens:
            executar("DELETE FROM metadata WHERE chave = ?", [META_ORDEM])
            executar("INSERT INTO metadata VALUES (?, ?)", [META_ORDEM, ORDEM_UF])
        else:
            executar("CREATE TABLE metadata (chave VARCHAR, valor VARCHAR)")
            executar("INSERT INTO metadata VALUES (?, ?)", [META_ORDEM, ORDEM_UF])

        # Conferencia: nenhuma linha a mais ou a menos em nenhuma tabela
        for t, n in contagens.items():
            extra = 1 if t == "metadata" and not conn.execute(
                "SELECT count(*) FROM o.metadata WHERE chave = ?", [META_ORDEM]).fetchone()[0] else 0
            novo = conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            if novo != n + extra:
                raise RuntimeError(f"Reorganização: a tabela {t} ficou com {novo} linhas (esperado {n + extra}).")
        if conn.execute("SELECT count(*) FROM cnpj_uf").fetchone()[0] != n_est:
            raise RuntimeError("Reorganização: cnpj_uf incompleta.")
        conn.execute("DETACH o")
        conn.execute("CHECKPOINT")
    except BaseException:
        conn.close()
        for p in (destino, destino.with_name(destino.name + ".wal")):
            p.unlink(missing_ok=True)
        raise
    conn.close()
    log.info("base reorganizada por UF em %.0fs", time.monotonic() - t0)
    return {**contagens, "cnpj_uf": n_est, "_segundos": round(time.monotonic() - t0, 1)}
