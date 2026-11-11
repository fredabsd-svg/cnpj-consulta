"""Base organizada por estado (UF): mesma informacao, leitura muito menor nas
buscas regionais. Cobre a reorganizacao, a tarefa do painel, a etapa do
pipeline e as consultas nos dois formatos (ordem de CNPJ e por UF)."""

from __future__ import annotations

import io

import duckdb
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.main import app
from app.receita import armazenamento as arm
from app.receita import jobs, servico
from app.receita.duck import (
    META_ORDEM,
    ORDEM_UF,
    conectar_leitura,
    dica_uf,
    em_lista,
    organizada_por_uf,
)
from app.receita.reorganizar import Interrompida, reorganizar
from app.services import busca_avancada as ba
from app.services import ficha as ficha_srv

CNPJ = "12345678000195"
FILIAL = "12345678000276"


def _linhas(path, sql):
    with duckdb.connect(str(path), read_only=True) as c:
        return c.execute(sql).fetchall()


# ---------------------------------------------------------------- reorganizar


def test_reorganizar_mantem_o_conteudo_e_agrupa_por_uf(base_ordem_cnpj, base_local, tmp_path):
    destino = tmp_path / "uf.duckdb"
    progresso = []
    r = reorganizar(base_local, destino, temp_dir=tmp_path / "tmp", progresso=lambda f, t: progresso.append(f))
    assert r["estabelecimentos"] == r["cnpj_uf"] == 4 and progresso and progresso[-1] == pytest.approx(1.0)
    # mesmo conteudo em todas as tabelas (a ordem fisica e o que muda)
    with duckdb.connect(str(destino), read_only=True) as c:
        c.execute(f"ATTACH '{base_local.as_posix()}' AS o (READ_ONLY)")
        tabelas = [t for (t,) in c.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog = 'o'").fetchall()]
        assert "estabelecimentos" in tabelas and "socios" in tabelas
        for t in tabelas:
            if t == "metadata":
                continue
            assert c.execute(f'SELECT count(*) FROM (SELECT * FROM o."{t}" EXCEPT SELECT * FROM "{t}")').fetchone()[0] == 0
            assert c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] == \
                c.execute(f'SELECT count(*) FROM o."{t}"').fetchone()[0]
        # estabelecimentos: UF e, dentro dela, CNPJ
        chaves = c.execute("SELECT uf, cnpj_completo FROM estabelecimentos").fetchall()
        assert chaves == sorted(chaves)
        # cnpj_uf em ordem de CNPJ, uma linha por estabelecimento
        mapa = c.execute("SELECT cnpj_completo, cnpj_basico, uf FROM cnpj_uf").fetchall()
        assert mapa == sorted(mapa) and {m[0]: m[2] for m in mapa} == dict((cn, uf) for uf, cn in chaves)
        # socios_por_chave em ordem de socio
        por_chave = c.execute("SELECT socio_chave, cnpj_basico FROM socios_por_chave").fetchall()
        assert por_chave == sorted(por_chave)
        assert len(por_chave) == c.execute("SELECT count(*) FROM socios").fetchone()[0]
        assert c.execute("SELECT valor FROM metadata WHERE chave = ?", [META_ORDEM]).fetchone()[0] == ORDEM_UF
    assert organizada_por_uf(destino) and not organizada_por_uf(base_local)


def test_reorganizar_em_varias_fatias(base_ordem_cnpj, base_local, tmp_path, monkeypatch):
    from app.receita import reorganizar as mod

    monkeypatch.setattr(mod, "LINHAS_POR_FATIA", 1)  # forca uma fatia por linha
    destino = tmp_path / "uf.duckdb"
    reorganizar(base_local, destino, temp_dir=tmp_path / "tmp")
    assert _linhas(destino, "SELECT count(*) FROM estabelecimentos")[0][0] == 4
    chaves = _linhas(destino, "SELECT uf, cnpj_completo FROM estabelecimentos")
    assert chaves == sorted(chaves)
    assert sorted(_linhas(destino, "SELECT * FROM socios")) == sorted(_linhas(base_local, "SELECT * FROM socios"))


def test_reorganizar_cancelada_nao_deixa_arquivo(base_ordem_cnpj, base_local, tmp_path):
    destino = tmp_path / "uf.duckdb"
    with pytest.raises(Interrompida):
        reorganizar(base_local, destino, temp_dir=tmp_path / "tmp", deve_parar=lambda: True)
    assert not destino.exists()
    with pytest.raises(FileExistsError):
        reorganizar(base_local, base_local)


# ---------------------------------------------------------------- pipeline e tarefa


def test_atualizacao_ja_publica_organizada(base_local):
    assert organizada_por_uf(base_local)
    assert servico.info_base_ativa()["por_uf"] is True
    with pytest.raises(ValueError, match="já está organizada"):
        servico.iniciar_organizacao(em_processo=False)


def test_tarefa_organizar_troca_o_arquivo_da_versao_ativa(base_ordem_cnpj, base_local):
    antes = ficha_srv.ficha_local(CNPJ)
    total_antes = ba.buscar(ba.Filtros.de_params({"uf": ["TO"]})).total
    assert servico.info_base_ativa()["por_uf"] is False
    job = servico.iniciar_organizacao(em_processo=False)
    assert job["tipo"] == "organizar"
    assert servico.executar_job(job["id"]) == "concluido"
    fim = jobs.obter(job["id"])
    assert fim["relatorio"]["contagens"]["estabelecimentos"] == 4 and fim["progresso"] == 100
    ativa = arm.base_ativa()
    assert ativa.caminho != base_local and not base_local.exists()  # o arquivo antigo sai no fim
    assert organizada_por_uf(ativa.caminho) and ativa.mes == "2026-10"
    assert not (arm.data_dir() / "trabalho" / "organizar").exists()
    # mesmas respostas depois da troca
    depois = ficha_srv.ficha_local(CNPJ)
    for k in ("identificacao", "endereco", "contatos_oficiais", "cnaes", "socios", "estabelecimentos"):
        assert depois[k] == antes[k]
    assert ba.buscar(ba.Filtros.de_params({"uf": ["TO"]})).total == total_antes
    # a validacao da base organizada passa
    v = servico.iniciar_validacao(em_processo=False)
    assert servico.executar_job(v["id"]) == "concluido"


def test_tarefa_organizar_sem_espaco_nao_toca_na_base(base_ordem_cnpj, base_local, monkeypatch):
    monkeypatch.setattr(arm, "espaco_livre", lambda p: 10)
    job = servico.iniciar_organizacao(em_processo=False)
    assert servico.executar_job(job["id"]) == "falhou"
    assert "Espaço em disco insuficiente" in jobs.obter(job["id"])["erro"]
    assert arm.base_ativa().caminho == base_local and base_local.exists() and not organizada_por_uf(base_local)


def test_painel_api_e_cli_oferecem_organizar(base_ordem_cnpj, base_local, admin_headers, monkeypatch):
    monkeypatch.setattr(jobs, "iniciar_processo", lambda *a, **k: None)
    c = TestClient(app)
    assert c.post("/api/admin/base/organizar").status_code in (401, 403)
    pagina = c.get("/base", headers=admin_headers).text
    assert "Organizar por estado agora" in pagina and "ainda não organizada por estado" in pagina
    r = c.post("/api/admin/base/organizar", headers=admin_headers)
    assert r.status_code == 202 and r.json()["tipo"] == "organizar"
    assert c.post("/api/admin/base/organizar", headers=admin_headers).status_code == 409  # ja ha tarefa
    assert "Organização por estado #" in c.get("/base", headers=admin_headers).text


def test_painel_mostra_base_organizada(base_local, admin_headers):
    pagina = TestClient(app).get("/base", headers=admin_headers).text
    assert "organizada por estado" in pagina and "Organizar por estado agora" not in pagina


# ---------------------------------------------------------------- consultas nos dois formatos


def test_dica_uf_e_em_lista(base_local):
    with conectar_leitura(base_local) as conn:
        assert dica_uf(conn, base_local, "e", cnpj=CNPJ) == (" AND (e.uf IN (?))", ["TO"])
        assert dica_uf(conn, base_local, "e", cnpj="00000000000000") == (" AND 1 = 0", [])
        assert dica_uf(conn, base_local, "e", cnpjs=[]) == (" AND 1 = 0", [])
        sql, params = dica_uf(conn, base_local, "x", basicos=["12345678", "87654321"])
        assert sql.startswith(" AND (x.uf IN (") and sorted(params) == ["TO"]
    assert em_lista("c", ["a", "b"]) == ("c IN (?, ?)", ["a", "b"])
    cond, params = em_lista("c", [str(i) for i in range(300)])
    assert "unnest" in cond and len(params) == 1 and len(params[0]) == 300


def test_dica_uf_nao_muda_nada_na_base_em_ordem_de_cnpj(base_ordem_cnpj, base_local):
    with conectar_leitura(base_local) as conn:
        assert dica_uf(conn, base_local, "e", cnpj=CNPJ) == ("", [])


@pytest.mark.parametrize("formato", ["uf", "cnpj"])
def test_mesmas_respostas_nos_dois_formatos(formato, request, monkeypatch):
    if formato == "cnpj":
        monkeypatch.setenv("RECEITA_ORGANIZAR_POR_UF", "false")
    base = request.getfixturevalue("base_local")
    assert organizada_por_uf(base) is (formato == "uf")
    f = ficha_srv.ficha_local(CNPJ)
    assert f["identificacao"]["razao_social"] == "CONTABIL TESTE LTDA"
    assert f["estabelecimentos"]["total"] == 2 and len(f["socios"]) == 2
    assert ficha_srv.ficha_local("00000000000191") is None
    # pagina do socio: participacoes e matrizes
    chave = next(s["chave"] for s in f["socios"] if s["nome"] == "MARIA DA SILVA")
    socio = ficha_srv.ficha_socio(chave)
    assert {e["cnpj"] for e in socio["empresas"]} >= {CNPJ}
    # autocompletar por CNPJ e por nome
    assert [s["cnpj"] for s in ficha_srv.sugestoes("12345678")] == [CNPJ, FILIAL]
    assert CNPJ in [s["cnpj"] for s in ficha_srv.sugestoes("contabil tes")]
    assert ficha_srv.sugestoes("zzzz nao existe") == []
    # busca: municipio sem UF ganha a UF sozinho; resultado igual
    r = ba.buscar(ba.Filtros.de_params({"municipio_nome": ["PALMAS"]}))
    assert [i["cnpj"] for i in r.itens] == [CNPJ]
    r = ba.buscar(ba.Filtros.de_params({"uf": ["TO"], "ordem": "cnpj"}))
    cnpjs = [i["cnpj"] for i in r.itens]
    assert cnpjs == sorted(cnpjs) and r.total == len(cnpjs)
    # provedor da consulta por CNPJ
    resp = TestClient(app).get(f"/empresa/{CNPJ}")
    assert resp.status_code == 200 and "CONTABIL TESTE LTDA" in resp.text


def test_uf_implicita_pelo_municipio(base_local):
    where, params = ba.compilar(ba.Filtros.de_params({"municipio_nome": ["PALMAS"]}))
    assert "e.uf IN" in where and "TO" in params
    where, _ = ba.compilar(ba.Filtros.de_params({"uf": ["TO"], "municipio_nome": ["PALMAS"]}))
    assert where.count("e.uf IN") == 1  # ja tinha UF: nada a acrescentar


# ---------------------------------------------------------------- contadores e cache


def test_contadores_em_uma_leitura_batem_com_a_contagem_direta(base_local):
    ba.limpar_cache_resultados()
    f = ba.Filtros.de_params({"uf": ["TO"]})
    dims = ["uf", "situacao", "porte", "municipio", "cnae_principal", "tipo_telefone_1"]
    cont = ba.contadores(f, dims)
    assert set(cont) == set(dims)
    total = ba.contar(f)
    for d in dims:
        assert sum(i["total"] for i in cont[d]) == total, d
    assert {i["rotulo"]: i["total"] for i in cont["municipio"]} == {"ARAGUAINA": 1, "GURUPI": 1, "PALMAS": 1}
    assert ba.contadores(f, dims) == cont  # segunda vez vem do cache


def test_cache_de_totais_e_limpo_quando_a_base_muda(base_local, monkeypatch):
    ba.limpar_cache_resultados()
    f = ba.Filtros.de_params({"uf": ["TO"]})
    assert ba.contar(f) == 3
    chamadas = []
    original = ba._executar
    monkeypatch.setattr(ba, "_executar", lambda *a, **k: chamadas.append(1) or original(*a, **k))
    assert ba.contar(f) == 3 and not chamadas  # do cache: nenhuma consulta
    ba.limpar_cache_resultados()
    assert ba.contar(f) == 3 and chamadas


# ---------------------------------------------------------------- planilhas


def test_excel_da_busca_tem_filtro_larguras_datas_e_resumo(base_local, tmp_path):
    from datetime import datetime

    from app.services import exportacao

    destino = tmp_path / "x.xlsx"
    f = ba.Filtros.de_params({"uf": ["TO"], "situacao": ["2"], "ordem": "razao"})
    n = exportacao.gravar_xlsx(f, ["cnpj", "razao_social", "abertura", "capital_social", "simples", "qtd_socios"],
                               None, destino)
    wb = load_workbook(destino)
    assert n == 3 and wb.sheetnames == ["Empresas", "Resumo"]
    ws = wb["Empresas"]
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref == "A1:F4"
    assert ws.column_dimensions["B"].width > ws.column_dimensions["A"].width > 10
    cab = ws["A1"]
    assert cab.font.b and cab.fill.fgColor.rgb.endswith(exportacao_cor())
    linha = [c.value for c in ws[2]]
    assert linha[0] == CNPJ and isinstance(linha[2], datetime) and linha[3] == 15000.0
    assert ws["C2"].number_format == "dd/mm/yyyy" and "R$" in ws["D2"].number_format
    assert linha[4] == "Sim" and linha[5] == 2
    resumo = {r[0]: r[1] for r in wb["Resumo"].iter_rows(values_only=True) if r[0]}
    assert resumo["Empresas na planilha"] == "3" and resumo["Ordenação"] == "Razão social"
    assert resumo["Filtros aplicados"] == "UF: TO" and resumo["Referência da base"] == "2026-10"
    textos = [r[1] for r in wb["Resumo"].iter_rows(values_only=True)]
    assert "Situação cadastral: Ativa" in textos


def exportacao_cor() -> str:
    from app.services.xlsx_rapido import COR_CABECALHO

    return COR_CABECALHO


def test_ficha_em_excel_tem_as_cinco_abas(base_local):
    r = TestClient(app).get(f"/empresa/{CNPJ}/ficha.xlsx")
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Resumo", "Contatos", "Atividades", "Sócios", "Estabelecimentos"]
    resumo = {row[0]: row[1] for row in wb["Resumo"].iter_rows(values_only=True) if row[0]}
    assert resumo["CNPJ"] == "12.345.678/0001-95" and resumo["Capital social"] == 15000.0
    assert resumo["Situação cadastral"] == "ATIVA" and resumo["CEP"] == "77001-000"
    assert [row[1] for row in wb["Atividades"].iter_rows(min_row=2, values_only=True)][0] == "6920-6/01"
    assert wb["Sócios"].auto_filter.ref and wb["Sócios"].freeze_panes == "A2"
    assert len(list(wb["Estabelecimentos"].iter_rows(min_row=2, values_only=True))) == 2
