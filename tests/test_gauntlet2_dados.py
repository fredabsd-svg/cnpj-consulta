"""Regressao do Gauntlet Loop (rodada 2, dados: pipeline, busca e consulta): cada teste foi escrito por um
critico independente, reproduziu um defeito real em 811dfe5 e hoje guarda a correcao."""

from __future__ import annotations

from datetime import date

import duckdb
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.receita import armazenamento as arm
from app.receita.pipeline import Atualizacao, Interrompido
from app.services import busca_avancada as ba
from tests.receita_fake import PortalFalso, csv_bytes, dataset_padrao, zip_bytes

CNPJ = "19131243000197"
MR = f"https://minhareceita.org/{CNPJ}"
BA = f"https://brasilapi.com.br/api/cnpj/v1/{CNPJ}"
RW = f"https://www.receitaws.com.br/v1/cnpj/{CNPJ}"


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


# ---------------------------------------------------------------- pipeline


def test_d2_arquivo_que_saiu_do_portal_continua_na_base_publicada(db_initialized):
    """1a tentativa importa Socios0..2 e pausa na consolidacao. A Receita
    republica a pasta sem o Socios2.zip (partes renumeradas/removidas). A
    retomada usa TODAS as tabelas de bruto._importados, inclusive a do arquivo
    que nao existe mais na listagem: a base publicada traz socio fantasma."""
    dados = dataset_padrao()
    dados["Socios2.zip"] = zip_bytes("K3241.K03200Y2.D61010.SOCIOCSV", csv_bytes([
        ["12345678", "2", "FULANO FANTASMA", "***777777**", "22", "20200101", "", "***000000**", "", "00", "4"],
    ]))
    portal = PortalFalso({"2026-10": dados})
    with respx.mock(assert_all_called=False) as router:
        portal.instalar(router)
        with pytest.raises(Interrompido):
            Atualizacao("2026-10", reporter=_PausaNaConsolidacao("cnaes"), sleep=lambda s: None).executar()
        del dados["Socios2.zip"]  # Receita republicou a pasta sem essa parte
        Atualizacao("2026-10", sleep=lambda s: None).executar()
    nomes = [r[0] for r in _q("SELECT nome_socio FROM socios ORDER BY 1")]
    arquivos = [r[0] for r in _q("SELECT nome FROM importacao_arquivos ORDER BY 1")]
    assert "FULANO FANTASMA" not in nomes, f"socio de arquivo removido do portal publicado; arquivos={arquivos}"


def test_d2_forcar_espaco_publica_versao_incompleta_sem_aviso(db_initialized):
    """--forcar / 'Ignorar avisos de espaco e de queda nas contagens' tambem
    desliga, em silencio, a recusa de versao INCOMPLETA (pasta ainda em
    publicacao, Estabelecimentos1 faltando): a base sai publicada sem uma parte
    inteira e o relatorio nao traz nenhum aviso de que faltavam arquivos."""
    dados = dataset_padrao()
    dados["Estabelecimentos2.zip"] = dados.pop("Estabelecimentos1.zip")  # parte 1 ainda nao subiu
    try:
        with respx.mock(assert_all_called=False) as router:
            PortalFalso({"2026-10": dados}).instalar(router)
            rel = Atualizacao("2026-10", forcar=True, sleep=lambda s: None).executar()
    except Exception as e:  # noqa: BLE001 -- recusar e o comportamento correto
        print("recusada:", e)
        return
    avisos = " | ".join(rel["avisos"])
    assert "Estabelecimentos1" in avisos or "incompleta" in avisos.lower(), (
        f"publicada {rel['publicada']} sem a parte Estabelecimentos1 e sem aviso; avisos={avisos!r}")


def test_d2_cancelar_durante_a_validacao_publica_mesmo_assim(db_initialized):
    """Depois da consolidacao nao ha mais nenhum _checar(): um Cancelar pedido
    durante a validacao (minutos numa base real: contagens e orfaos de 60 mi de
    linhas) nunca e atendido -- a versao e PUBLICADA e a tarefa termina
    'concluido'. O painel promete atender no proximo ponto seguro."""

    class _CancelaNaValidacao:
        def __init__(self):
            self.etapas: set[str] = set()

        def atualizar(self, forcar=False, **campos):
            if campos.get("etapa"):
                self.etapas.add(campos["etapa"])

        def solicitacao(self):
            return "cancelar" if "validacao" in self.etapas else None

        def log(self, mensagem, nivel="info", etapa=None):
            if etapa:
                self.etapas.add(etapa)

    with respx.mock(assert_all_called=False) as router:
        PortalFalso({"2026-10": dataset_padrao()}).instalar(router)
        try:
            rel = Atualizacao("2026-10", reporter=_CancelaNaValidacao(), sleep=lambda s: None).executar()
        except Interrompido:
            rel = None
    ativa = arm.base_ativa()
    assert rel is None and ativa is None, f"cancelada na validacao e publicada: {ativa and ativa.caminho.name}"


def test_d2_queda_entre_trocar_ponteiro_e_gravar_estado_perde_versao_anterior(db_initialized, monkeypatch):
    """A correcao de P3 grava 'publicada' no estado.json DEPOIS de arm.publicar(),
    que ja trocou o ATIVA.json e rodou a poda. Queda nessa janela (aqui, dentro
    da poda): a retomada nao ve 'publicada', consolida e publica o mesmo mes de
    novo; o 'anterior' vira a copia duplicada e a versao anterior real (2026-09)
    e podada -- o mesmo defeito de P3, por outra porta."""
    import time as _t

    with respx.mock(assert_all_called=False) as router:
        PortalFalso({"2026-09": dataset_padrao()}).instalar(router)
        Atualizacao("2026-09", sleep=lambda s: None).executar()
    v09 = arm.base_ativa().caminho.name
    orig = arm.podar_versoes
    quedas = {"n": 0}

    def poda_com_queda(settings=None):
        if quedas["n"] == 0:
            quedas["n"] += 1
            raise RuntimeError("processo morto (simulado) logo apos trocar o ponteiro")
        return orig(settings)

    monkeypatch.setattr(arm, "podar_versoes", poda_com_queda)
    with respx.mock(assert_all_called=False) as router:
        PortalFalso({"2026-10": dataset_padrao()}).instalar(router)
        with pytest.raises(RuntimeError):
            Atualizacao("2026-10", sleep=lambda s: None).executar()
        assert arm.base_ativa().mes == "2026-10" and arm.base_ativa().anterior == v09
        _t.sleep(1.1)  # nome da versao tem resolucao de segundos
        Atualizacao("2026-10", sleep=lambda s: None).executar()  # "Retomar"
    arquivos = [v["arquivo"] for v in arm.listar_versoes()]
    assert v09 in arquivos, f"versao anterior real apagada; restam {arquivos}"


def test_d2_mes_com_quebra_de_linha_passa_na_validacao(db_initialized, admin_headers, monkeypatch):
    """MES_RE usa '$', que casa antes de um '\\n' final: a API aceita
    {"mes": "2026-10\\n"} (202) e grava a tarefa com esse mes, que vira nome de
    pasta de trabalho e caminho do PROPFIND. Devia ser 400 como '2026-13'."""
    from app.receita import jobs

    monkeypatch.setattr(jobs, "iniciar_processo", lambda job_id, log_dir: 0)
    c = TestClient(_app())
    r = c.post("/api/admin/base/atualizar", json={"mes": "2026-10\n"}, headers=admin_headers)
    ultimo = jobs.ultimo_job()
    assert r.status_code == 400, (r.status_code, ultimo and repr(ultimo["mes"]))


def test_d2_retomada_apos_publicar_apaga_o_relatorio_da_tarefa(db_initialized, monkeypatch):
    """Queda entre publicar e limpar; o usuario clica Retomar. _concluir_publicada
    devolve um relatorio NOVO (sem contagens/comparacao/arquivos) que sobrescreve o
    da tarefa: o painel mostra 'concluido' com 0 registros importados e sem o
    resumo da validacao da base que de fato foi publicada."""
    from app.receita import jobs, servico

    orig = Atualizacao._limpar
    quedas = {"n": 0}

    def limpar_com_queda(self, tudo):
        if quedas["n"] == 0:
            quedas["n"] += 1
            raise RuntimeError("processo morto (simulado) logo apos publicar")
        return orig(self, tudo)

    monkeypatch.setattr(Atualizacao, "_limpar", limpar_com_queda)
    with respx.mock(assert_all_called=False) as router:
        PortalFalso({"2026-10": dataset_padrao()}).instalar(router)
        job = jobs.criar_job("atualizar", "2026-10")
        assert servico.executar_job(job["id"], sleep=lambda s: None) == "falhou"
        assert jobs.obter(job["id"])["relatorio"]["contagens"]["estabelecimentos"] == 4
        servico.retomar(job["id"], em_processo=False)
        assert servico.executar_job(job["id"], sleep=lambda s: None) == "concluido"
    fim = jobs.obter(job["id"])
    assert arm.base_ativa().mes == "2026-10"
    assert fim["relatorio"]["contagens"], (fim["registros"], fim["relatorio"])


def test_d2_retomar_concorrente_com_nova_tarefa_cria_duas_ativas(db_initialized, monkeypatch):
    """criar_job agora e atomico (trava 'criar-tarefa'), mas preparar_retomada
    confere 'ha tarefa ativa?' e grava 'pendente' FORA dessa trava. Retomar no
    painel enquanto o agendador/CLI cria uma tarefa: as duas ficam ativas; o
    processo que perder a corrida pela trava da base marca a SUA tarefa como
    'falhou' (a retomada pedida pelo usuario pode ser a que falha)."""
    import threading

    from app.receita import jobs

    j1 = jobs.criar_job("atualizar", "2026-09")
    jobs.JobReporter(j1["id"]).finalizar("falhou", erro="queda de rede")
    orig = jobs.job_ativo
    outra: dict = {}

    def job_ativo_durante_corrida():
        r = orig()
        if not outra:  # o outro pedido chega entre a conferencia e a gravacao
            t = threading.Thread(target=lambda: outra.setdefault("job", jobs.criar_job("atualizar", "2026-10")))
            t.start()
            t.join(10)
        return r

    monkeypatch.setattr(jobs, "job_ativo", job_ativo_durante_corrida)
    jobs.preparar_retomada(j1["id"])
    monkeypatch.undo()
    ativos = [(j["id"], j["mes"]) for j in jobs.historico() if j["status_efetivo"] in jobs.ATIVOS]
    assert len(ativos) <= 1, f"duas tarefas ativas ao mesmo tempo: {ativos}"


# ---------------------------------------------------------------- busca avancada

CONTABIL = "12345678000195"
FILIAL = "12345678000276"
MERCADO = "87654321000198"
MEI = "11222333000181"


def _app():
    from app.main import app

    return app


def _cn(params: dict) -> list[str]:
    return sorted(i["cnpj"] for i in ba.buscar(ba.Filtros.de_params(params)).itens)


def test_d2_api_busca_descarta_colunas_anunciadas_em_campos(base_local):
    """/api/busca/campos (e descrever_filtros no MCP) anuncia natureza, cep,
    logradouro, data_situacao e cnaes_secundarios como colunas; desde que
    buscar() passou a filtrar por COLUNAS_TELA, /api/busca?colunas=... as
    descarta em silencio e devolve so o CNPJ."""
    c = TestClient(_app())
    anunciadas = c.get("/api/busca/campos").json()["colunas"]
    pedidas = ["natureza", "cep", "logradouro", "data_situacao", "cnaes_secundarios"]
    assert all(p in anunciadas for p in pedidas)
    r = c.get("/api/busca", params={"cnpj": CONTABIL, "colunas": pedidas})
    assert r.status_code == 200, r.text[:200]
    item = r.json()["itens"][0]
    faltando = [p for p in pedidas if p not in item]
    assert not faltando, f"colunas pedidas e anunciadas sumiram: {faltando}; item={item}"


def test_d2_idade_maxima_inclui_quem_ja_completou_mais_anos():
    """idade_max=5 vira data_inicio >= hoje - 6 anos: a empresa aberta
    exatamente ha 6 anos (6 anos completos hoje) entra no resultado."""
    sql, params = ba.compilar(ba.Filtros(campos={"idade_max": "5"}))
    hoje = date.today()
    try:
        seis = hoje.replace(year=hoje.year - 6)
    except ValueError:
        seis = hoje.replace(year=hoje.year - 6, day=28)
    expr = sql.replace("e.data_inicio_atividade", "d")
    entra = duckdb.execute(f"SELECT {expr} FROM (SELECT ?::DATE AS d)", [*params, seis]).fetchone()[0]
    assert not entra, f"aberta em {seis} (6 anos completos) passou em idade_max=5: {sql} {params}"


def test_d2_capital_com_separador_americano_vira_valor_mil_vezes_menor(base_local):
    """'20,000.00' (milhar com virgula e decimal com ponto, como clientes MCP e
    planilhas em ingles mandam) vira 20.0 sem erro: capital_min devolve as tres
    empresas em vez de so a de capital 250.000,50."""
    r = _cn({"capital_min": "20,000.00"})
    assert r == [MERCADO], f"capital_min='20,000.00' interpretado como {ba._num('20,000.00', 'c')}: {r}"


def test_d2_socio_documento_cnpj_alfanumerico_recusado():
    """cnpj, cnpj_basico e compartilha_socios_com aceitam CNPJ alfanumerico; o
    filtro socio_documento (socio pessoa juridica) so aceita 14 DIGITOS e recusa
    um CNPJ alfanumerico valido."""
    try:
        ba.compilar(ba.Filtros(campos={"socio_documento": "12ABC34501DE35"}))
    except ba.FiltroInvalido as e:
        pytest.fail(f"CNPJ alfanumerico de socio PJ recusado: {e}")


def test_d2_busca_recente_reabre_outra_busca_quando_tinha_socio(base_local):
    """sem_dados_de_socio poda a folha de socio da arvore, mas a busca recente
    guarda o total e o resumo da busca ORIGINAL. Com OU, podar a folha muda o
    resultado: (socio JOAO SOUZA) OU (UF=GO) da 3; a busca guardada, so UF=GO,
    da 1 -- e o historico diz '3 resultados' para a busca que reabre com 1."""
    import json as _json

    c = TestClient(_app())
    arv = _json.dumps({"op": "OU", "itens": [{"campo": "socio_nome", "valor": "JOAO SOUZA"},
                                              {"campo": "uf", "valor": ["GO"]}]})
    original = c.get("/api/busca", params={"arvore": arv}).json()["total"]
    recentes = c.get("/api/buscas-recentes").json()
    assert original == 3
    if not recentes:
        return  # correcao escolhida: busca com criterio de socio nao vai para o historico
    recente = recentes[0]
    reaberta = c.get("/api/busca", params=recente["filtros"]).json()["total"]
    assert original == 3
    assert reaberta == recente["total"], (
        f"historico: {recente['resumo']!r} = {recente['total']}; reabrir {recente['filtros']} da {reaberta}")


PAU_DARCO = "55666777000100"  # CNPJ ficticio (a busca nao confere DV)


@pytest.fixture
def base_nomes(monkeypatch, db_initialized):
    """Base padrao + municipio oficial com apostrofo (Pau D'Arco/TO) e razao
    social com ordinal (2º), como aparecem nas tabelas da Receita."""
    from app.config import get_settings
    from app.providers.registry import get_registry
    from tests import receita_fake as rf

    monkeypatch.setenv("RECEITA_LOCAL_ENABLED", "true")
    get_settings.cache_clear()
    get_registry.cache_clear()
    files = rf.dataset_padrao()
    files["Municipios.zip"] = zip_bytes("F.K03200$Z.D61010.MUNICCSV", csv_bytes([
        ["9733", "PALMAS"], ["9241", "ARAGUAINA"], ["9701", "GURUPI"], ["7107", "SAO PAULO"],
        ["9665", "PAU D'ARCO"],
    ]))
    files["Empresas2.zip"] = zip_bytes("K3241.K03200Y2.D61010.EMPRECSV", csv_bytes([
        ["55666777", "CARTORIO DO 2º OFICIO DE NOTAS", "2062", "49", "1000,00", "01", ""]]))
    files["Estabelecimentos2.zip"] = zip_bytes("K3241.K03200Y2.D61010.ESTABELE", csv_bytes([
        rf.estab("55666777", "0001", "00", uf="TO", municipio="9665")]))
    with respx.mock(assert_all_called=False) as router:
        PortalFalso({"2026-10": files}).instalar(router)
        Atualizacao(sleep=lambda s: None).executar()
    arm.limpar_cache()
    return arm.base_ativa().caminho


def test_d2_municipio_nome_com_apostrofo_nunca_encontra(base_nomes):
    """municipio_nome compara strip_accents(upper(descricao)) = nome normalizado
    SEM pontuacao: "PAU D'ARCO" (nome exato da tabela oficial) vira "PAU D ARCO"
    e nao casa com nada. Vale para Santa Barbara D'Oeste, Dias D'Avila, Olho
    D'Agua, Pingo-D'Agua, Xique-Xique...: zero resultados, sem aviso."""
    assert _q("SELECT descricao FROM municipios WHERE codigo = 9665") == [("PAU D'ARCO",)]
    assert _cn({"municipio": "9665"}) == [PAU_DARCO]  # pelo codigo, acha
    assert _cn({"municipio_nome": "PAU D'ARCO"}) == [PAU_DARCO]


def test_d2_razao_com_ordinal_digitado_como_publicado_nao_encontra(base_nomes):
    """A base normaliza com strip_accents (o 'º' some: '2 OFICIO'); a consulta
    normaliza com unidecode ('º' -> 'O': '2O OFICIO'). Digitar a razao social
    exatamente como publicada nao encontra a empresa."""
    assert _q("SELECT razao_social_norm FROM empresas WHERE cnpj_basico = '55666777'") == [
        ("CARTORIO DO 2 OFICIO DE NOTAS",)]
    assert _cn({"razao": "2º OFICIO"}) == [PAU_DARCO]


def test_d2_nome_com_ano_vira_cnpj_na_pagina_inicial_e_na_busca(base_local):
    """'MERCADINHO2024' (nome sem espaco, 14 caracteres, termina em 2 digitos):
    /consulta (_parece_cnpj) responde 400 'CNPJ invalido' em vez de abrir a busca
    por nome, e na busca q= vira 'cnpj_completo = ?' -- o nome nunca e procurado.
    Um CNPJ alfanumerico tem a raiz com digitos nas posicoes da Receita; a
    heuristica aceita qualquer palavra com digito."""
    problemas = []
    r = TestClient(_app()).get("/consulta", params={"cnpj": "MERCADINHO2024"}, follow_redirects=False)
    if not (r.status_code == 303 and r.headers.get("location", "").startswith("/busca")):
        problemas.append(f"/consulta -> {r.status_code}")
    sql, params = ba._q_rapida("MERCADINHO2024")
    if "razao_social_norm" not in sql:
        problemas.append(f"q -> {sql} {params}")
    assert not problemas, problemas


@respx.mock
def test_d2_resposta_200_vazia_conta_como_fonte_ok(db_initialized):
    """A correcao da rodada 1 recusa 200 com HTML, mas 200 com corpo VAZIO (proxy,
    portal cativo) vira {} e passa como fonte OK: status 200, sem erro, sem dado."""
    respx.get(MR).mock(return_value=httpx.Response(200, content=b""))
    respx.get(BA).mock(return_value=httpx.Response(503, text="fora"))
    respx.get(RW).mock(return_value=httpx.Response(503, text="fora"))
    body = TestClient(_app()).get(f"/api/companies/{CNPJ}").json()
    mr = next(f for f in body["fontes"] if f["fonte"] == "minha_receita")
    assert body["razao_social"] is None
    assert not (mr["status"] == 200 and not mr["erro"]), f"resposta vazia contada como fonte OK: {mr}"
