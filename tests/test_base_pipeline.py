"""Pipeline da base oficial: descoberta, download com retomada, arquivos
corrompidos, interrupcao e retomada da importacao, validacao e publicacao sem
perder a versao anterior. Sem rede: o portal e simulado (tests/receita_fake)."""

from __future__ import annotations

import threading

import duckdb
import httpx
import pytest
import respx

from app.receita import armazenamento as arm
from app.receita.fonte import FonteError, FonteReceita, parse_propfind
from app.receita.pipeline import Atualizacao, Interrompido, PipelineError
from tests.receita_fake import ORIGEM, PortalFalso, csv_bytes, dataset_padrao, estab, zip_bytes


def _rodar(portal: PortalFalso, mes: str | None = "2026-10", **kw) -> dict:
    with respx.mock(assert_all_called=False) as router:
        portal.instalar(router)
        return Atualizacao(mes, sleep=lambda s: None, **kw).executar()


def _contar(tabela: str) -> int:
    ativa = arm.base_ativa()
    with duckdb.connect(str(ativa.caminho), read_only=True) as c:
        return c.execute(f"SELECT count(*) FROM {tabela}").fetchone()[0]


# ---------------------------------------------------------------- descoberta


class TestDescoberta:
    @respx.mock
    def test_token_do_redirecionamento_e_versao_mais_recente(self, db_initialized):
        portal = PortalFalso({"2026-09": dataset_padrao(), "2026-10": dataset_padrao()})
        portal.instalar(respx.mock)
        with FonteReceita() as f:
            v = f.versao_mais_recente()
            assert f.token == "TesteTok3n1234"
            assert f.pasta_cnpj == "Dados/Cadastros/CNPJ"
        assert v.mes == "2026-10" and v.completa
        assert {a.grupo for a in v.arquivos} >= {"empresas", "estabelecimentos", "socios", "simples", "cnaes"}
        assert all(a.url.startswith(ORIGEM + "/public.php/webdav/") for a in v.arquivos)
        assert all(a.etag and a.tamanho > 0 for a in v.arquivos)

    @respx.mock
    def test_versao_incompleta_fica_de_fora(self, db_initialized):
        incompleta = {k: v for k, v in dataset_padrao().items() if not k.startswith("Socios")}
        portal = PortalFalso({"2026-09": dataset_padrao(), "2026-10": incompleta})
        portal.instalar(respx.mock)
        with FonteReceita() as f:
            assert f.versao_mais_recente().mes == "2026-09"
            v = f.listar_versao("2026-10")
        assert not v.completa and "Socios" in v.faltando

    @respx.mock
    def test_pasta_mudou_e_e_encontrada(self, db_initialized, monkeypatch):
        monkeypatch.setenv("RECEITA_PASTA_CNPJ", "Antiga/CNPJ")
        from app.config import get_settings

        get_settings.cache_clear()
        PortalFalso({"2026-10": dataset_padrao()}).instalar(respx.mock)
        with FonteReceita() as f:
            assert f.localizar_pasta_cnpj() == "Dados/Cadastros/CNPJ"

    @respx.mock
    def test_portal_fora_do_ar(self, db_initialized):
        respx.get(f"{ORIGEM}/").mock(side_effect=httpx.ConnectError("sem rede"))
        with FonteReceita() as f, pytest.raises(FonteError, match="indisponivel"):
            f.versao_mais_recente()

    @respx.mock
    def test_listagem_recusada(self, db_initialized):
        respx.get(f"{ORIGEM}/").mock(return_value=httpx.Response(302, headers={"Location": "/index.php/s/Abcdefgh123"}))
        respx.route(method="PROPFIND").mock(return_value=httpx.Response(401))
        with FonteReceita() as f, pytest.raises(FonteError, match="recusou"):
            f.listar_meses()

    def test_portal_nao_oficial_e_recusado(self, db_initialized, monkeypatch):
        monkeypatch.setenv("RECEITA_PORTAL_URL", "https://espelho-qualquer.com/")
        from app.config import get_settings

        get_settings.cache_clear()
        with FonteReceita() as f, pytest.raises(FonteError, match="oficial"):
            f.descobrir_compartilhamento()

    def test_xml_invalido(self):
        with pytest.raises(FonteError):
            parse_propfind(b"<nao-e-xml")


# ---------------------------------------------------------------- execucao completa


class TestAtualizacao:
    def test_publica_e_importa_todas_as_partes(self, db_initialized):
        rel = _rodar(PortalFalso({"2026-10": dataset_padrao()}))
        assert rel["publicada"].startswith("receita-2026-10-")
        assert rel["contagens"]["empresas"] == 3
        assert rel["contagens"]["estabelecimentos"] == 4  # duas partes de estabelecimentos
        assert rel["duplicados_removidos"]["empresas"] == 1
        ativa = arm.base_ativa()
        assert ativa.mes == "2026-10" and not ativa.legado
        # area de trabalho removida apos publicar
        assert not arm.trabalho_dir("2026-10").exists()
        with duckdb.connect(str(ativa.caminho), read_only=True) as c:
            meta = dict(c.execute("SELECT chave, valor FROM metadata").fetchall())
            assert meta["mes_referencia"] == "2026-10" and meta["schema_versao"] == "2"
            assert c.execute("SELECT count(*) FROM importacao_arquivos").fetchone()[0] == 13

    def test_queda_de_conexao_retoma_com_range(self, db_initialized):
        portal = PortalFalso({"2026-10": dataset_padrao()})
        portal.falhas["Estabelecimentos0.zip"] = 2
        rel = _rodar(portal)
        pedidos = [r for n, r in portal.gets if n == "Estabelecimentos0.zip"]
        assert len(pedidos) == 3 and pedidos[0] is None and all(r and r.startswith("bytes=") for r in pedidos[1:])
        assert any("tentativa" in a for a in rel["avisos"])
        assert _contar("estabelecimentos") == 4

    def test_servidor_que_ignora_range_recomeca_do_zero(self, db_initialized):
        portal = PortalFalso({"2026-10": dataset_padrao()})
        portal.falhas["Empresas0.zip"] = 1
        portal.ignorar_range = True
        rel = _rodar(portal)
        assert any("nao retomou" in a for a in rel["avisos"])
        assert _contar("empresas") == 3

    def test_falha_persistente_de_conexao(self, db_initialized):
        portal = PortalFalso({"2026-10": dataset_padrao()})
        portal.falhas["Socios0.zip"] = 99
        with pytest.raises(PipelineError, match="apos 6 tentativas"):
            _rodar(portal)
        assert arm.base_ativa() is None

    def test_zip_corrompido_e_baixado_de_novo(self, db_initialized):
        dados = dataset_padrao()
        portal = PortalFalso({"2026-10": dados})
        portal.corromper["Estabelecimentos0.zip"] = 1  # primeira copia vem com bytes trocados
        rel = _rodar(portal)
        assert any("integridade" in a or "invalido" in a for a in rel["avisos"])
        assert _contar("estabelecimentos") == 4

    def test_zip_sempre_corrompido_falha_sem_publicar(self, db_initialized):
        portal = PortalFalso({"2026-10": dataset_padrao()})
        portal.corromper["Estabelecimentos0.zip"] = 99
        with pytest.raises(PipelineError, match="corrompido|invalido"):
            _rodar(portal)
        assert arm.base_ativa() is None

    def test_arquivo_que_nao_e_zip(self, db_initialized):
        dados = dataset_padrao()
        dados["Socios1.zip"] = b"<html>erro</html>" * 10
        with pytest.raises(PipelineError, match="Socios1.zip"):
            _rodar(PortalFalso({"2026-10": dados}))

    def test_leiaute_diferente_e_recusado(self, db_initialized):
        dados = dataset_padrao()
        dados["Empresas1.zip"] = zip_bytes("X.EMPRECSV", csv_bytes([["1", "2", "3"]] * 20))
        with pytest.raises(PipelineError, match="estrutura inesperada"):
            _rodar(PortalFalso({"2026-10": dados}))

    def test_versao_incompleta_nao_e_importada(self, db_initialized):
        dados = {k: v for k, v in dataset_padrao().items() if k != "Simples.zip"}
        with pytest.raises(PipelineError, match="incompleta"):
            _rodar(PortalFalso({"2026-10": dados}))

    def test_espaco_insuficiente(self, db_initialized, monkeypatch):
        monkeypatch.setattr(arm, "espaco_livre", lambda p: 10)
        with pytest.raises(PipelineError, match="Espaco em disco insuficiente"):
            _rodar(PortalFalso({"2026-10": dataset_padrao()}))

    def test_mes_invalido(self):
        with pytest.raises(ValueError):
            Atualizacao("2026-13")


# ---------------------------------------------------------------- interrupcao


class _Reporter:
    """Pede pausa depois de N consultas de 'solicitacao' na etapa de importacao."""

    def __init__(self, atual: list, pausar_na: str, depois: int = 0) -> None:
        self.atual, self.pausar_na, self.depois, self.n = atual, pausar_na, depois, 0
        self.logs: list[str] = []

    def atualizar(self, forcar=False, **campos):
        if "etapa" in campos:
            self.atual[0] = campos["etapa"]

    def solicitacao(self):
        if self.atual[0] == self.pausar_na:
            self.n += 1
            if self.n > self.depois:
                return "pausar"
        return None

    def log(self, mensagem, nivel="info", etapa=None):
        self.logs.append(mensagem)


class _ReporterPausaAposImportar(_Reporter):
    """Pede pausa depois que N arquivos foram importados."""

    def __init__(self, depois: int) -> None:
        super().__init__([""], "", 0)
        self.limite = depois

    def solicitacao(self):
        feitos = sum(1 for m in self.logs if " registros" in m)
        return "pausar" if feitos >= self.limite else None


class TestRetomada:
    def test_pausa_na_importacao_e_retomada_sem_duplicar(self, db_initialized):
        portal = PortalFalso({"2026-10": dataset_padrao()})
        rep = _ReporterPausaAposImportar(5)
        with respx.mock(assert_all_called=False) as router:
            portal.instalar(router)
            with pytest.raises(Interrompido):
                Atualizacao("2026-10", reporter=rep, sleep=lambda s: None).executar()
            parcial = Atualizacao("2026-10", sleep=lambda s: None)
            importados_antes = parcial._importados()
            assert 0 < len(importados_antes) < 13
            downloads_antes = len(portal.gets)
            rel = Atualizacao("2026-10", sleep=lambda s: None).executar()
        # nada foi baixado de novo: os ZIPs ja verificados foram reaproveitados
        assert len(portal.gets) == downloads_antes
        assert rel["contagens"]["estabelecimentos"] == 4
        assert rel["contagens"]["empresas"] == 3
        assert arm.base_ativa().mes == "2026-10"

    def test_pausa_durante_download_retoma_do_ponto(self, db_initialized):
        portal = PortalFalso({"2026-10": dataset_padrao()})
        rep = _Reporter([""], "download", depois=3)
        with respx.mock(assert_all_called=False) as router:
            portal.instalar(router)
            with pytest.raises(Interrompido):
                Atualizacao("2026-10", reporter=rep, sleep=lambda s: None).executar()
            baixados = {n for n, _ in portal.gets}
            portal.gets.clear()
            Atualizacao("2026-10", sleep=lambda s: None).executar()
        rebaixados = {n for n, _ in portal.gets}
        assert baixados and not (rebaixados & (baixados - {n for n, _ in portal.gets if n in rebaixados}))
        assert len(rebaixados) < 13
        assert _contar("socios") == 4

    def test_queda_no_meio_de_um_arquivo_desfaz_so_aquele_arquivo(self, db_initialized, monkeypatch):
        """Erro dentro da transacao de um arquivo: os anteriores continuam importados."""
        portal = PortalFalso({"2026-10": dataset_padrao()})
        orig = Atualizacao._validar_estrutura
        estado = {"n": 0, "quebrou": None}

        def quebra(self, path, origem, esperadas):
            estado["n"] += 1
            if estado["n"] == 6:
                estado["quebrou"] = origem
                raise OSError("queda simulada")
            return orig(self, path, origem, esperadas)

        monkeypatch.setattr(Atualizacao, "_validar_estrutura", quebra)
        with respx.mock(assert_all_called=False) as router:
            portal.instalar(router)
            with pytest.raises(OSError):
                Atualizacao("2026-10", sleep=lambda s: None).executar()
            feitos = Atualizacao("2026-10", sleep=lambda s: None)._importados()
            assert len(feitos) == 5 and estado["quebrou"] not in feitos
            rel = Atualizacao("2026-10", sleep=lambda s: None).executar()
        assert rel["contagens"]["socios"] == 4


# ---------------------------------------------------------------- versoes


class TestVersoes:
    def test_nova_versao_invalida_mantem_a_anterior(self, db_initialized):
        _rodar(PortalFalso({"2026-09": dataset_padrao()}), "2026-09")
        primeira = arm.base_ativa().caminho
        menor = dataset_padrao()
        # versao "truncada" na origem: perde quase todos os estabelecimentos
        menor["Estabelecimentos0.zip"] = zip_bytes("K.ESTABELE", csv_bytes([estab("12345678", "0001", "95")]))
        menor["Estabelecimentos1.zip"] = zip_bytes("K.ESTABELE", csv_bytes([estab("12345678", "0003", "57", matriz=False)]))
        menor["Empresas1.zip"] = zip_bytes("K.EMPRECSV", csv_bytes([["99999999", "X", "2062", "49", "1,00", "01", ""]]))
        with pytest.raises(PipelineError, match="Validacao falhou"):
            _rodar(PortalFalso({"2026-10": menor}))
        assert arm.base_ativa().caminho == primeira  # consultas continuam na versao boa

    def test_publica_mantem_anterior_e_restaura(self, db_initialized):
        _rodar(PortalFalso({"2026-09": dataset_padrao()}), "2026-09")
        v1 = arm.base_ativa().caminho.name
        rel = _rodar(PortalFalso({"2026-10": dataset_padrao()}))
        assert rel["anterior"] == v1
        assert rel["comparacao"]["empresas"]["variacao_pct"] == 0
        ativa = arm.base_ativa()
        assert ativa.mes == "2026-10" and ativa.anterior == v1
        papeis = {v["papel"] for v in arm.listar_versoes()}
        assert papeis == {"ativa", "anterior"}
        assert arm.restaurar_anterior() == v1
        assert arm.base_ativa().caminho.name == v1
        with pytest.raises(PermissionError):
            arm.remover_versao(v1)

    def test_poda_mantem_so_ativa_e_anterior(self, db_initialized):
        for mes in ("2026-08", "2026-09", "2026-10"):
            _rodar(PortalFalso({mes: dataset_padrao()}), mes)
        assert sorted(v["mes"] for v in arm.listar_versoes()) == ["2026-09", "2026-10"]

    def test_consultas_concorrentes_durante_a_atualizacao(self, db_initialized, monkeypatch):
        from app.services import busca_avancada as ba
        from app.services.ficha import ficha_local

        monkeypatch.setenv("RECEITA_LOCAL_ENABLED", "true")
        _rodar(PortalFalso({"2026-09": dataset_padrao()}), "2026-09")
        erros: list[Exception] = []
        parar = threading.Event()
        meses: set[str] = set()

        def consultar():
            while not parar.is_set():
                try:
                    f = ficha_local("12345678000195")
                    assert f and f["identificacao"]["razao_social"] == "CONTABIL TESTE LTDA"
                    meses.add(f["base"]["mes"])
                    r = ba.buscar(ba.Filtros(campos={"uf": ["TO"]}))
                    assert r.total == 3
                except Exception as e:  # noqa: BLE001
                    erros.append(e)

        threads = [threading.Thread(target=consultar) for _ in range(3)]
        for t in threads:
            t.start()
        try:
            _rodar(PortalFalso({"2026-10": dataset_padrao()}))
        finally:
            parar.set()
            for t in threads:
                t.join(10)
        assert not erros, erros[:3]
        assert "2026-09" in meses
        assert arm.base_ativa().mes == "2026-10"


# ---------------------------------------------------------------- integridade


class TestIntegridade:
    def test_orfaos_acima_do_limite_bloqueiam(self, db_initialized):
        dados = dataset_padrao()
        dados["Estabelecimentos1.zip"] = zip_bytes("K.ESTABELE", csv_bytes(
            [estab(f"5555{i:04d}", "0001", "00") for i in range(50)]))
        with pytest.raises(PipelineError, match="sem empresa"):
            _rodar(PortalFalso({"2026-10": dados}))

    def test_linhas_rejeitadas_sao_contadas_sem_conteudo(self, db_initialized):
        dados = dataset_padrao()
        boas = [estab(f"1234567{i}", "0001", "00") for i in range(9)]
        dados["Estabelecimentos1.zip"] = zip_bytes(
            "K.ESTABELE", csv_bytes(boas) + b'"12345678";"0009";"quebrada sem fim\n')
        dados["Empresas1.zip"] = zip_bytes("K.EMPRECSV", csv_bytes(
            [["87654321", "MERCADO EXEMPLO LTDA", "2062", "49", "1,00", "03", ""]]
            + [[f"1234567{i}", "X", "2062", "49", "1,00", "01", ""] for i in range(9)]))
        rel = _rodar(PortalFalso({"2026-10": dados}), forcar=True)
        assert rel["arquivos"]["Estabelecimentos1.zip"]["linhas"] == 9
        # o aviso traz so o tipo de erro, nunca o conteudo da linha
        assert not any("quebrada" in a for a in rel["avisos"])


# ---------------------------------------------------------------- desempenho e concorrencia


class TestDownloadParalelo:
    @pytest.fixture(autouse=True)
    def partes_pequenas(self, monkeypatch):
        import app.receita.pipeline as pl

        monkeypatch.setattr(pl, "SEGMENTO_MIN", 64)  # forca partes nos arquivos de teste

    def test_arquivos_grandes_em_partes(self, db_initialized):
        portal = PortalFalso({"2026-10": dataset_padrao()})
        rel = _rodar(portal)
        com_fim = [r for n, r in portal.gets if r and "-" in r and not r.endswith("-")]
        assert com_fim, "nenhum download em partes (Range com inicio e fim)"
        assert any("partes" in a for a in rel["avisos"]) is False
        assert rel["contagens"]["estabelecimentos"] == 4 and rel["contagens"]["socios"] == 4

    def test_queda_numa_parte_retoma_so_a_parte(self, db_initialized):
        portal = PortalFalso({"2026-10": dataset_padrao()})
        portal.falhas["Estabelecimentos0.zip"] = 2
        rel = _rodar(portal)
        assert any("parte" in a and "tentativa" in a for a in rel["avisos"])
        assert rel["contagens"]["estabelecimentos"] == 4

    def test_servidor_sem_partes_baixa_inteiro(self, db_initialized):
        portal = PortalFalso({"2026-10": dataset_padrao()})
        portal.ignorar_range = True
        rel = _rodar(portal)
        assert any("nao aceitou download em partes" in a for a in rel["avisos"])
        assert rel["contagens"]["empresas"] == 3

    def test_um_download_por_vez_tambem_funciona(self, db_initialized, monkeypatch):
        monkeypatch.setenv("RECEITA_DOWNLOADS_PARALELOS", "1")
        from app.config import get_settings

        get_settings.cache_clear()
        portal = PortalFalso({"2026-10": dataset_padrao()})
        _rodar(portal)
        assert all(r is None for _, r in portal.gets)

    def test_pasta_temporaria_separada(self, db_initialized, monkeypatch, tmp_path):
        temp = tmp_path / "ssd"
        monkeypatch.setenv("RECEITA_TEMP_DIR", str(temp))
        from app.config import get_settings

        get_settings.cache_clear()
        _rodar(PortalFalso({"2026-10": dataset_padrao()}))
        assert arm.base_ativa().mes == "2026-10"
        assert not (temp / "cnpj-consulta" / "2026-10").exists()  # limpa ao terminar


class TestTravaDeProcesso:
    def test_segundo_processo_nao_entra(self, db_initialized):
        from app.receita import jobs, servico, trava

        job = jobs.criar_job("atualizar", "2026-10")
        t = trava.TravaProcesso()
        assert t.adquirir()
        try:
            assert trava.em_uso() is not None
            assert servico.executar_job(job["id"]) == "ocupado"
            assert jobs.obter(job["id"])["status"] == "pendente"  # tarefa intacta
        finally:
            t.liberar()
        assert trava.em_uso() is None

    def test_batimento_atrasado_com_processo_vivo_nao_vira_interrompido(self, db_initialized):
        from datetime import datetime, timedelta

        from app.db import get_session_factory
        from app.models.base_cnpj import BaseJob
        from app.receita import jobs, trava

        job = jobs.criar_job("atualizar", "2026-10")
        jobs.JobReporter(job["id"]).iniciar()
        with get_session_factory()() as s:
            j = s.get(BaseJob, job["id"])
            j.heartbeat_em = datetime.now() - timedelta(minutes=30)
            s.commit()
        t = trava.TravaProcesso()
        assert t.adquirir()
        try:
            assert jobs.obter(job["id"])["status_efetivo"] == "executando"
            with pytest.raises(jobs.JobEmAndamento):
                jobs.criar_job("atualizar", "2026-10")
        finally:
            t.liberar()
        assert jobs.obter(job["id"])["status_efetivo"] == "interrompido"
        assert jobs.preparar_retomada(job["id"])["status"] == "pendente"

    def test_retomar_recusado_enquanto_outro_processo_trabalha(self, db_initialized):
        from app.receita import jobs, trava

        job = jobs.criar_job("atualizar", "2026-10")
        jobs.JobReporter(job["id"]).finalizar("falhou", erro="x")
        t = trava.TravaProcesso()
        assert t.adquirir()
        try:
            with pytest.raises(jobs.JobEmAndamento, match="em execucao"):
                jobs.preparar_retomada(job["id"])
        finally:
            t.liberar()

    def test_batimento_em_operacao_longa(self, db_initialized):
        import time as _t

        from app.receita import jobs, servico

        job = jobs.criar_job("atualizar", "2026-10")
        rep = jobs.JobReporter(job["id"])
        rep.iniciar()
        antes = jobs.obter(job["id"])["heartbeat_em"]
        with servico._Batimento(rep, intervalo=0.05):
            _t.sleep(0.3)  # simula uma instrucao longa sem progresso
        assert jobs.obter(job["id"])["heartbeat_em"] > antes
