"""Regressao do Gauntlet Loop (rodada 1, pipeline da base local): cada teste foi
escrito por um critico independente para reproduzir um defeito real e hoje
guarda a correcao."""

from __future__ import annotations

import duckdb
import pytest
import respx

from app.receita import armazenamento as arm
from app.receita.pipeline import Atualizacao, Interrompido, PipelineError
from tests.receita_fake import PortalFalso, csv_bytes, dataset_padrao, estab, zip_bytes


def _rodar(portal: PortalFalso, mes: str | None = "2026-10", **kw) -> dict:
    with respx.mock(assert_all_called=False) as router:
        portal.instalar(router)
        return Atualizacao(mes, sleep=lambda s: None, **kw).executar()


def _q(sql: str):
    ativa = arm.base_ativa()
    with duckdb.connect(str(ativa.caminho), read_only=True) as c:
        return c.execute(sql).fetchall()


class _PausaNaConsolidacao:
    """Pede pausa assim que a etapa `apos` da consolidacao foi registrada."""

    def __init__(self, apos: str) -> None:
        self.apos = apos
        self.logs: list[str] = []

    def atualizar(self, forcar=False, **campos):
        pass

    def solicitacao(self):
        return "pausar" if any(m.startswith(f"{self.apos}:") for m in self.logs) else None

    def log(self, mensagem, nivel="info", etapa=None):
        self.logs.append(mensagem)


# ---------------------------------------------------------------- P1


def test_p1_arquivo_republicado_apos_pausa_na_consolidacao_publica_dado_velho(db_initialized):
    """A Receita substitui Empresas0.zip (novo ETag) entre a pausa e a retomada.
    O pipeline reimporta o bruto, mas pula as etapas de consolidacao ja
    registradas em _etapas: a base publicada fica com a razao social antiga."""
    dados = dataset_padrao()
    portal = PortalFalso({"2026-10": dados})
    with respx.mock(assert_all_called=False) as router:
        portal.instalar(router)
        with pytest.raises(Interrompido):
            Atualizacao("2026-10", reporter=_PausaNaConsolidacao("empresas"), sleep=lambda s: None).executar()
        # Receita republica o arquivo (conteudo novo, ETag novo)
        dados["Empresas0.zip"] = zip_bytes("K3241.K03200Y0.D61010.EMPRECSV", csv_bytes([
            ["12345678", "CONTABIL NOVA RAZAO LTDA", "2062", "49", "15000,00", "01", ""],
        ]))
        rel = Atualizacao("2026-10", sleep=lambda s: None).executar()
    assert any("mudou no portal" in a for a in rel["avisos"])  # o pipeline percebeu a troca
    assert rel["arquivos"]["Empresas0.zip"]["etag"] == PortalFalso.etag(dados["Empresas0.zip"])
    razao = _q("SELECT razao_social FROM empresas WHERE cnpj_basico = '12345678'")[0][0]
    assert razao == "CONTABIL NOVA RAZAO LTDA", f"publicado dado velho: {razao!r}"


# ---------------------------------------------------------------- P2


def test_p2_falha_na_validacao_nunca_se_resolve_mesmo_com_fonte_corrigida(db_initialized):
    """Versao com estabelecimentos truncados falha na validacao (correto). A
    Receita corrige os arquivos (novo ETag); a nova tentativa reimporta o bruto
    mas reaproveita o final.duckdb consolidado da tentativa ruim e falha de novo
    -- para sempre, ate alguem rodar `base limpar`."""
    _rodar(PortalFalso({"2026-09": dataset_padrao()}), "2026-09")
    ruim = dataset_padrao()
    ruim["Estabelecimentos0.zip"] = zip_bytes("K.ESTABELE", csv_bytes([estab("12345678", "0001", "95")]))
    ruim["Estabelecimentos1.zip"] = zip_bytes("K.ESTABELE", csv_bytes([estab("12345678", "0002", "76", matriz=False)]))
    portal = PortalFalso({"2026-10": ruim})
    with respx.mock(assert_all_called=False) as router:
        portal.instalar(router)
        with pytest.raises(PipelineError, match="Validacao falhou"):
            Atualizacao("2026-10", sleep=lambda s: None).executar()
        # fonte corrigida pela Receita
        portal.versoes["2026-10"] = dataset_padrao()
        rel = Atualizacao("2026-10", sleep=lambda s: None).executar()
    assert rel["contagens"]["estabelecimentos"] == 4


# ---------------------------------------------------------------- P3


def test_p3_retomar_depois_de_queda_apos_publicar_perde_a_versao_anterior(db_initialized, monkeypatch):
    """Queda do processo entre a publicacao e o fim da limpeza (aqui simulada
    por excecao em _limpar). A tarefa fica 'falhou/interrompida' e o usuario
    retoma: o pipeline consolida e PUBLICA o mesmo mes outra vez; o ponteiro
    'anterior' passa a ser a copia duplicada e a versao anterior real (2026-09)
    e podada do disco -- nao ha mais como restaura-la."""
    import time as _t

    _rodar(PortalFalso({"2026-09": dataset_padrao()}), "2026-09")
    v09 = arm.base_ativa().caminho.name
    portal = PortalFalso({"2026-10": dataset_padrao()})
    orig = Atualizacao._limpar
    estado = {"quedas": 0}

    def limpar_com_queda(self, tudo):
        if estado["quedas"] == 0:
            estado["quedas"] += 1
            raise RuntimeError("processo morto (simulado) logo apos publicar")
        return orig(self, tudo)

    monkeypatch.setattr(Atualizacao, "_limpar", limpar_com_queda)
    with respx.mock(assert_all_called=False) as router:
        portal.instalar(router)
        with pytest.raises(RuntimeError):
            Atualizacao("2026-10", sleep=lambda s: None).executar()
        assert arm.base_ativa().mes == "2026-10" and arm.base_ativa().anterior == v09
        _t.sleep(1.1)  # nome da versao tem resolucao de segundos
        Atualizacao("2026-10", sleep=lambda s: None).executar()  # "Retomar"
    arquivos = [v["arquivo"] for v in arm.listar_versoes()]
    assert v09 in arquivos, f"versao anterior real apagada; restam {arquivos}"
    assert arm.restaurar_anterior() == v09


# ---------------------------------------------------------------- P4


def test_p4_duas_tarefas_simultaneas_sao_criadas(db_initialized, monkeypatch):
    """criar_job le 'ha tarefa ativa?' e insere sem exclusao mutua: dois
    pedidos simultaneos (dois cliques, painel + agendador) criam DUAS tarefas
    pendentes. O segundo processo sai como 'ocupado' e deixa a sua tarefa
    'pendente'; job_ativo() passa a apontar para ela (id maior), e
    Pausar/Cancelar no painel vao para a tarefa errada."""
    import threading

    from app.receita import jobs, servico, trava

    jobs.init_database()
    fabrica = jobs.get_session_factory()
    barreira = threading.Barrier(2, timeout=2)

    class _Sessao:
        def __init__(self):
            self._s = fabrica()

        def __enter__(self):
            self._s.__enter__()
            return self

        def __exit__(self, *exc):
            return self._s.__exit__(*exc)

        def scalars(self, *a, **k):
            r = list(self._s.scalars(*a, **k))
            try:
                barreira.wait()  # sem exclusao mutua, as duas leituras acontecem antes de qualquer insercao
            except threading.BrokenBarrierError:
                pass  # com a trava, a outra thread nunca chega aqui junto: a barreira expira
            return r

        def __getattr__(self, nome):
            return getattr(self._s, nome)

    monkeypatch.setattr(jobs, "get_session_factory", lambda: _Sessao)
    resultados: list = []

    def pedir():
        try:
            resultados.append(jobs.criar_job("atualizar", "2026-10"))
        except jobs.JobEmAndamento as e:
            resultados.append(e)

    ts = [threading.Thread(target=pedir) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    monkeypatch.undo()
    criados = sorted((r for r in resultados if isinstance(r, dict)), key=lambda r: r["id"])
    if len(criados) == 2:
        # o processo da 1a tarefa segura a trava; o da 2a sai "ocupado"
        t = trava.TravaProcesso()
        assert t.adquirir()
        try:
            assert servico.executar_job(criados[1]["id"]) == "ocupado"
            ativo = jobs.job_ativo()["id"]
            print("job_ativo ->", ativo, "tarefas:", [c["id"] for c in criados])
        finally:
            t.liberar()
    assert len(criados) == 1, f"{len(criados)} tarefas 'pendente' criadas ao mesmo tempo"


# ---------------------------------------------------------------- P5


def test_p5_cancelar_nao_mantem_os_downloads_concluidos(db_initialized, monkeypatch):
    """Doc e log: 'Cancelar mantem os downloads concluidos'. Com o padrao
    RECEITA_MANTER_ZIPS=false, o ZIP de cada arquivo ja importado foi apagado
    e o cancelamento apaga o bruto.duckdb: tudo que ja tinha sido baixado e
    importado e baixado DE NOVO na proxima atualizacao."""
    from app.receita import jobs, servico

    portal = PortalFalso({"2026-10": dataset_padrao()})
    importados = {"n": 0}
    log_orig = jobs.JobReporter.log

    def log(self, mensagem, nivel="info", etapa=None):
        if " registros" in mensagem and ".zip" in mensagem:
            importados["n"] += 1
        return log_orig(self, mensagem, nivel, etapa)

    monkeypatch.setattr(jobs.JobReporter, "log", log)
    monkeypatch.setattr(jobs.JobReporter, "solicitacao", lambda self: "cancelar" if importados["n"] >= 8 else None)
    with respx.mock(assert_all_called=False) as router:
        portal.instalar(router)
        job = jobs.criar_job("atualizar", "2026-10")
        assert servico.executar_job(job["id"], sleep=lambda s: None) == "cancelado"
        msgs = [m["mensagem"] for m in jobs.logs(job["id"])]
        assert any("Downloads concluidos foram mantidos" in m for m in msgs)
        baixados = {n for n, _ in portal.gets}
        assert len(baixados) == 13  # todos os downloads tinham terminado
        portal.gets.clear()
        monkeypatch.setattr(jobs.JobReporter, "solicitacao", lambda self: None)
        job2 = jobs.criar_job("atualizar", "2026-10")
        assert servico.executar_job(job2["id"], sleep=lambda s: None) == "concluido"
    rebaixados = sorted({n for n, _ in portal.gets})
    assert not rebaixados, f"{len(rebaixados)} arquivo(s) baixados de novo apos cancelar: {rebaixados}"


# ---------------------------------------------------------------- P6


def test_p6_versao_com_parte_faltando_e_publicada(db_initialized):
    """Pasta em publicacao: Estabelecimentos0 e Estabelecimentos2 ja estao no
    portal, Estabelecimentos1 ainda nao. 'completa' so confere GRUPOS, nao a
    sequencia de partes; na primeira carga nao ha versao ativa para comparar e a
    base sai publicada sem uma parte inteira (aqui, 3 estabelecimentos em vez de 5)."""
    dados = dataset_padrao()
    dados["Estabelecimentos2.zip"] = dados.pop("Estabelecimentos1.zip")
    # Estabelecimentos1.zip (ainda subindo) teria mais 2 estabelecimentos
    dados["Estabelecimentos2.zip"] = zip_bytes("K.ESTABELE", csv_bytes([
        estab("12345678", "0002", "76", matriz=False, municipio="9701"),
    ]))
    with pytest.raises(PipelineError, match="incompleta|faltam|parte"):
        rel = _rodar(PortalFalso({"2026-10": dados}))
        print("publicada:", rel["publicada"], rel["contagens"])


# ---------------------------------------------------------------- P7


def test_p7_simples_truncado_e_publicado(db_initialized):
    """A comparacao com a versao ativa so olha empresas, estabelecimentos e
    socios. Um Simples.zip que perdeu metade das linhas (ou um Cnaes.zip
    truncado) passa na validacao e e publicado."""
    _rodar(PortalFalso({"2026-09": dataset_padrao()}), "2026-09")
    antes = _q("SELECT count(*) FROM simples")[0][0]
    dados = dataset_padrao()
    dados["Simples.zip"] = zip_bytes("F.K03200$W.SIMPLES.CSV.D61010", csv_bytes([
        ["12345678", "S", "20200115", "00000000", "N", "00000000", "00000000"],
    ]))
    dados["Cnaes.zip"] = zip_bytes("F.K03200$Z.D61010.CNAECSV", csv_bytes([["6920601", "Atividades de contabilidade"]]))
    with pytest.raises(PipelineError, match="Validacao falhou"):
        _rodar(PortalFalso({"2026-10": dados}))
        depois = _q("SELECT count(*) FROM simples")[0][0]
        print(f"simples {antes} -> {depois}; cnaes -> {_q('SELECT count(*) FROM cnaes')[0][0]}")


# ---------------------------------------------------------------- P8


def test_p8_cli_mes_invalido_estoura_e_deixa_tarefa_executando(db_initialized):
    """`base atualizar --mes 2026-13`: o CLI nao valida o mes, cria a tarefa e o
    processo de trabalho estoura ValueError FORA do try (construtor de
    Atualizacao). Traceback no terminal e a tarefa fica 'executando' --
    bloqueando novas atualizacoes ate o batimento expirar."""
    from typer.testing import CliRunner

    from app.cli import app as cli
    from app.receita import jobs

    r = CliRunner().invoke(cli, ["base", "atualizar", "--mes", "2026-13"])
    ultimo = jobs.ultimo_job()
    print("exit:", r.exit_code, "exc:", repr(r.exception), "status:", ultimo and ultimo["status"])
    assert not isinstance(r.exception, ValueError), "excecao nao tratada no CLI"
    assert ultimo is None or ultimo["status"] not in ("executando", "pendente")
