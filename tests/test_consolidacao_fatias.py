"""Consolidacao em fatias: as tabelas grandes sao montadas por prefixo do CNPJ
(menos memoria por consulta) e a retomada continua da fatia em que parou."""

from __future__ import annotations

import duckdb
import pytest
import respx

from app.receita import armazenamento as arm
from app.receita.pipeline import FATIAS_CHAVE, FATIAS_CNPJ, FATIAS_CNPJ_FINAS, Atualizacao
from tests.receita_fake import PortalFalso, dataset_padrao


def _q(sql: str):
    with duckdb.connect(str(arm.base_ativa().caminho), read_only=True) as c:
        return c.execute(sql).fetchall()


def test_fatias_cobrem_tudo_sem_repetir():
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('00000001'), ('09999999'), ('12345678'), ('99999999'), "
                "('X1234567'), ('1X234567')) v(cnpj_basico)")
    for fatias in (FATIAS_CNPJ, FATIAS_CNPJ_FINAS):
        vistos = []
        for _, filtro in fatias.partes():
            vistos += [r[0] for r in con.execute(f"SELECT cnpj_basico FROM t WHERE {filtro} ORDER BY 1").fetchall()]
        assert sorted(vistos) == sorted(r[0] for r in con.execute("SELECT * FROM t").fetchall())
        assert len(vistos) == 6
        digitos = [v for v in vistos if v.isdigit()]
        assert digitos == sorted(digitos)  # fatias em ordem: a tabela final fica ordenada
    assert len(FATIAS_CNPJ.partes()) == 11 and len(FATIAS_CNPJ_FINAS.partes()) == 101
    assert len(FATIAS_CHAVE.partes()) == 17


def test_falta_de_memoria_no_meio_retoma_da_fatia_seguinte(db_initialized, monkeypatch):
    dados = dataset_padrao()
    portal = PortalFalso({"2026-10": dados})
    original = Atualizacao._transacao
    executadas: list[str] = []
    estado = {"falhou": False}

    def transacao(self, conn, comandos):
        marca = next((c for c in comandos if c.startswith("INSERT INTO _etapas VALUES ('estabelecimentos#")), None)
        if marca and "#12'" in marca and not estado["falhou"]:
            estado["falhou"] = True
            raise duckdb.OutOfMemoryException("Out of Memory Error (simulado)")
        if marca:
            executadas.append(marca)
        return original(self, conn, comandos)

    monkeypatch.setattr(Atualizacao, "_transacao", transacao)
    monkeypatch.setattr("app.receita.pipeline.LINHAS_SEM_FATIAR", 0)  # base de teste e pequena: forca as fatias
    with respx.mock(assert_all_called=False) as router:
        portal.instalar(router)
        with pytest.raises(duckdb.OutOfMemoryException):
            Atualizacao("2026-10", sleep=lambda s: None).executar()
        antes = len(executadas)
        assert antes == 12  # fatias 0..11 gravadas antes da falha
        rel = Atualizacao("2026-10", sleep=lambda s: None).executar()
    assert len(executadas) - antes == 101 - 12  # so as que faltavam
    assert rel["contagens"]["estabelecimentos"] == 4
    linhas = _q("SELECT cnpj_completo FROM estabelecimentos")
    assert len(linhas) == len(set(linhas)) == 4  # nada repetido nem perdido
    assert _q("SELECT count(*) FROM empresas")[0][0] == rel["contagens"]["empresas"]


def test_threads_limitadas_pela_memoria():
    from app.receita.duck import limitar_threads

    con = duckdb.connect()
    con.execute("SET memory_limit = '2GB'")
    con.execute("SET threads = 16")
    assert limitar_threads(con) == 3
    con.execute("SET memory_limit = '64GB'")
    con.execute("SET threads = 4")
    assert limitar_threads(con) == 4  # nunca aumenta


def test_base_pequena_nao_e_fatiada(db_initialized, monkeypatch):
    original = Atualizacao._transacao
    marcas: list[str] = []

    def transacao(self, conn, comandos):
        marcas.extend(c for c in comandos if c.startswith("INSERT INTO _etapas VALUES ('estabelecimentos#"))
        return original(self, conn, comandos)

    monkeypatch.setattr(Atualizacao, "_transacao", transacao)
    with respx.mock(assert_all_called=False) as router:
        PortalFalso({"2026-10": dataset_padrao()}).instalar(router)
        rel = Atualizacao("2026-10", sleep=lambda s: None).executar()
    assert len(marcas) == 1 and rel["contagens"]["estabelecimentos"] == 4
