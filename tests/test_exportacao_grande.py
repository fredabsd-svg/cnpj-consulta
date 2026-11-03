"""Exportacao sem teto de linhas, Excel em varias abas, ordem preservada e busca
na ordem gravada (CNPJ) por padrao."""

from __future__ import annotations

from openpyxl import load_workbook

from app.services import busca_avancada as ba
from app.services import exportacao


def test_padrao_e_a_ordem_gravada_sem_order_by(base_local):
    f = ba.Filtros()
    assert f.ordem == "cnpj" and ba._ordem_sql(f) == ""
    assert ba._ordem_sql(ba.Filtros(ordem="cnpj", direcao="desc")).startswith("ORDER BY e.cnpj_completo DESC")
    assert ba._ordem_sql(ba.Filtros(ordem="razao")).startswith("ORDER BY e.razao_social_norm ASC")
    r = ba.buscar(ba.Filtros.de_params({"uf": ["TO", "GO"]}))
    cnpjs = [i["cnpj"] for i in r.itens]
    assert cnpjs == sorted(cnpjs) and r.total == len(cnpjs) == 4


def test_exportacao_sem_teto_e_excel_em_abas(base_local, tmp_path, monkeypatch):
    monkeypatch.setattr(exportacao, "LINHAS_POR_ABA", 2)
    assert exportacao.limite_linhas(10_000_000) is None  # padrao: sem teto
    destino = tmp_path / "x.xlsx"
    f = ba.Filtros.de_params({"uf": ["TO", "GO"]})
    n = exportacao.gravar_xlsx(f, ["cnpj", "municipio", "natureza", "cnae_descricao", "socios"], None, destino)
    assert n == 4
    wb = load_workbook(destino, read_only=True)
    assert wb.sheetnames[:2] == ["Empresas", "Empresas 2"]
    linhas = [r for aba in ("Empresas", "Empresas 2") for r in list(wb[aba].iter_rows(values_only=True))[1:]]
    assert [r[0] for r in linhas] == sorted(r[0] for r in linhas)  # ordem de CNPJ preservada
    contabil = next(r for r in linhas if r[0] == "12345678000195")
    assert contabil[1] == "PALMAS" and contabil[2] == "Sociedade Empresária Limitada"
    assert contabil[3] == "Atividades de contabilidade" and contabil[4] == "JOAO SOUZA; MARIA DA SILVA"


def test_teto_configuravel(base_local, monkeypatch):
    monkeypatch.setenv("EXPORTACAO_MAX_LINHAS", "2")
    from app.config import get_settings

    get_settings.cache_clear()
    assert exportacao.limite_linhas(10) == 2 and exportacao.limite_linhas() == 2


def test_tela_marca_contatos_por_padrao_e_sem_restricao(base_local):
    from fastapi.testclient import TestClient

    from app.main import app

    html = TestClient(app).get("/busca", params={"uf": "TO"}).text
    assert "restrita" not in html and "Finalidade" not in html
    assert 'value="telefone_1" checked' in html and 'value="email" checked' in html


def test_env_antigo_do_instalador_nao_limita_mais(monkeypatch):
    """O .env criado pelo instalador trazia 30 s e 500 mil linhas de fabrica."""
    from app.config import Settings

    monkeypatch.setenv("BUSCA_TIMEOUT_SEGUNDOS", "30")
    monkeypatch.setenv("EXPORTACAO_MAX_LINHAS", "500000")
    s = Settings()
    assert s.busca_timeout_segundos == 300 and s.exportacao_max_linhas == 0
    monkeypatch.setenv("BUSCA_TIMEOUT_SEGUNDOS", "45")
    monkeypatch.setenv("EXPORTACAO_MAX_LINHAS", "1000")
    s = Settings()
    assert s.busca_timeout_segundos == 45 and s.exportacao_max_linhas == 1000  # escolha do usuario vale


def test_conexao_de_leitura_reaproveitada_e_fechada_na_troca(base_local):
    from app.receita import armazenamento as arm
    from app.receita import duck

    ba.contar(ba.Filtros())
    assert len(duck._leituras) == 1
    ba.contar(ba.Filtros.de_params({"uf": ["TO"]}))
    assert len(duck._leituras) == 1  # mesma conexao (cache quente)
    arm.limpar_cache()  # versao ativa mudou / vai ser apagada
    assert duck._leituras == {}
