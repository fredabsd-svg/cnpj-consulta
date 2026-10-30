"""Regressao do Gauntlet Loop (rodada 3, dados: pipeline, busca e consulta): cada teste foi escrito por um
critico independente, reproduziu um defeito real em b5704d8 e hoje guarda a correcao."""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.receita import armazenamento as arm
from app.receita.pipeline import Atualizacao, Interrompido
from app.services import busca_avancada as ba
from tests import receita_fake as rf

CONTABIL = "12345678000195"
CARTORIO = "55666777000100"  # CNPJ ficticio (a base nao confere DV)
SOCIO_PJ_ALFA = "12ABC34501DE35"  # CNPJ alfanumerico valido (IN RFB 2.229/2024)


def _app():
    from app.main import app

    return app


def _cn(params: dict) -> list[str]:
    return sorted(i["cnpj"] for i in ba.buscar(ba.Filtros.de_params(params)).itens)


class _PausaNaConsolidacao:
    def __init__(self, apos: str) -> None:
        self.apos, self.logs = apos, []

    def atualizar(self, forcar=False, **campos):
        pass

    def solicitacao(self):
        return "pausar" if any(m.startswith(f"{self.apos}:") for m in self.logs) else None

    def log(self, mensagem, nivel="info", etapa=None):
        self.logs.append(mensagem)


@pytest.fixture
def base_extra(monkeypatch, db_initialized):
    """Base padrao + cartorio com ordinal na razao social, cujo socio e uma PJ
    com CNPJ alfanumerico (emitido desde jul/2026)."""
    from app.config import get_settings
    from app.providers.registry import get_registry

    monkeypatch.setenv("RECEITA_LOCAL_ENABLED", "true")
    get_settings.cache_clear()
    get_registry.cache_clear()
    files = rf.dataset_padrao()
    files["Empresas2.zip"] = rf.zip_bytes("K3241.K03200Y2.D61010.EMPRECSV", rf.csv_bytes([
        ["55666777", "CARTORIO DO 2º OFICIO DE NOTAS", "2062", "49", "1000,00", "01", ""]]))
    files["Estabelecimentos2.zip"] = rf.zip_bytes("K3241.K03200Y2.D61010.ESTABELE", rf.csv_bytes([
        rf.estab("55666777", "0001", "00", uf="TO", municipio="9733")]))
    files["Socios2.zip"] = rf.zip_bytes("K3241.K03200Y2.D61010.SOCIOCSV", rf.csv_bytes([
        ["55666777", "1", "ALFA NOVA LTDA", SOCIO_PJ_ALFA, "22", "20200101", "", "***000000**", "", "00", "0"]]))
    with respx.mock(assert_all_called=False) as router:
        rf.PortalFalso({"2026-10": files}).instalar(router)
        Atualizacao(sleep=lambda s: None).executar()
    arm.limpar_cache()
    return arm.base_ativa().caminho


# ---------------------------------------------------------------- busca avancada


def test_d3_1_criterios_complementares_combinados_valem_para_contatos_diferentes(base_local):
    """'Tipo do contato complementar = telefone' + 'Contato complementar verificado
    = sim' sao resolvidos cada um por uma consulta separada (_compilar_folha chama
    _resolver_comp com UM item por campo), embora cnpjs_com() aceite os criterios
    juntos. A empresa com telefone NAO verificado e site verificado entra como se
    tivesse telefone verificado."""
    from app.services import contatos

    contatos.salvar(CONTABIL, tipo="telefone", valor="63999998888", fonte="site oficial", verificado=False)
    contatos.salvar(CONTABIL, tipo="site", valor="https://contafacil.com.br", fonte="site oficial", verificado=True)
    assert contatos.cnpjs_com(tipo="telefone", verificado=True) == []  # verdade no SQLite
    r = _cn({"comp_tipo": "telefone", "comp_verificado": "sim"})
    assert r == [], f"telefone complementar verificado: nenhum existe, busca devolveu {r}"


def test_d3_2_idade_fracionaria_e_truncada_em_silencio():
    """_idade aceita 1.5 (ou '1,5'), mas _anos_atras usa int(anos): idade_min=1.5
    vira 'aberta ha pelo menos 1 ano'. Empresa aberta ha 1 ano e 1 dia passa."""
    try:
        sql, params = ba.compilar(ba.Filtros(campos={"idade_min": "1.5"}))
    except ba.FiltroInvalido:
        return  # correcao escolhida: idade fracionaria e recusada com mensagem, nao truncada
    hoje = date.today()
    try:
        um_ano = hoje.replace(year=hoje.year - 1)
    except ValueError:
        um_ano = hoje.replace(year=hoje.year - 1, day=28)
    aberta = um_ano - timedelta(days=1)  # 1,0 ano de idade
    expr = sql.replace("e.data_inicio_atividade", "d")
    entra = duckdb.execute(f"SELECT {expr} FROM (SELECT ?::DATE AS d)", [*params, aberta]).fetchone()[0]
    assert not entra, f"aberta em {aberta} (1 ano) passou em idade_min=1.5: {sql} {params}"


def test_d3_3_contar_false_grava_total_menos_um_no_historico(base_local):
    """/api/busca?contar=false devolve total=-1 (nao contou) e registrar_recente
    grava esse -1 -- inclusive SOBRESCREVENDO o total real da mesma busca."""
    c = TestClient(_app())
    assert c.get("/api/busca", params={"uf": "TO"}).json()["total"] == 3
    c.get("/api/busca", params={"uf": "TO", "contar": "false"})
    recente = c.get("/api/buscas-recentes").json()[0]
    assert recente["total"] in (3, None), f"historico de buscas: 'UF: TO' com total {recente['total']}"


def test_d3_5_coluna_socios_com_finalidade_some_e_auditoria_registra_entrega(base_local):
    """colunas=socios (restrita, anunciada em /api/busca/campos) com finalidade:
    a API audita 'busca.api_contatos colunas=socios' como se tivesse entregado,
    mas buscar() descarta a coluna (restrita fora de COLUNAS_TELA) em silencio:
    nem a coluna nem um aviso."""
    from app.receita import jobs

    c = TestClient(_app())
    assert c.get("/api/busca/campos").json()["colunas"]["socios"]["restrita"] is True
    r = c.get("/api/busca", params={"cnpj": CONTABIL, "colunas": ["socios"],
                                    "finalidade": "diligencia cadastral de clientes"}).json()
    auditado = [a["detalhe"] for a in jobs.auditoria() if a["acao"] == "busca.api_contatos"]
    item = r["itens"][0]
    assert "socios" in item or "aviso" in r, (
        f"coluna pedida sumiu sem aviso: item={item}; auditoria diz entregue: {auditado}")


def test_d3_7_sugestoes_com_ordinal_nao_acham_a_razao_publicada(base_extra):
    """A correcao do ordinal (º/ª) foi feita so em _palavras: ficha.sugestoes (caixa
    de busca do inicio e do topo) normaliza com unidecode ('2º' -> '2O') e procura
    o prefixo 'CARTORIO DO 2O' em nomes_busca, que guarda 'CARTORIO DO 2 OFICIO'."""
    from app.services import ficha

    assert [s["cnpj"] for s in ficha.sugestoes("CARTORIO DO 2 OF")] == [CARTORIO]
    sug = [s["cnpj"] for s in ficha.sugestoes("CARTORIO DO 2º OF")]
    assert sug == [CARTORIO], f"razao digitada como publicada sem sugestao: {sug}"


# ---------------------------------------------------------------- consulta / ficha


def test_d3_6_socio_pj_com_cnpj_alfanumerico_vira_asteriscos_na_ficha(base_extra):
    """mascarar_documento so reconhece CNPJ de socio PJ com 14 DIGITOS; um CNPJ
    alfanumerico (publico) cai no ramo final e vira '***' na ficha e na pagina
    do socio. A busca (socio_documento) ja aceita esse formato."""
    from app.services import ficha

    f = ficha.ficha_local(CARTORIO)
    doc = [s["documento"] for s in f["socios"]]
    assert doc == ["12.ABC.345/01DE-35"], f"CNPJ publico do socio PJ apagado: {doc}"


@respx.mock
def test_d3_8_resposta_200_com_objeto_vazio_conta_como_fonte_ok(db_initialized):
    """A rodada 2 recusou 200 com corpo vazio, mas 200 com '{}' (proxy/erro
    disfarcado) ainda passa: a fonte aparece como OK (200, sem erro) sem dado."""
    cnpj = "19131243000197"
    respx.get(f"https://minhareceita.org/{cnpj}").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}").mock(return_value=httpx.Response(503))
    respx.get(f"https://www.receitaws.com.br/v1/cnpj/{cnpj}").mock(return_value=httpx.Response(503))
    body = TestClient(_app()).get(f"/api/companies/{cnpj}").json()
    mr = next(f for f in body["fontes"] if f["fonte"] == "minha_receita")
    assert body["razao_social"] is None
    assert not (mr["status"] == 200 and not mr["erro"]), f"'{{}}' contado como fonte OK: {mr}"


# ---------------------------------------------------------------- pipeline


def test_d3_4_retomada_publica_com_relatorio_sem_arquivos(db_initialized):
    """Pausa na consolidacao e Retomar: _baixar_e_importar sai cedo ('if not pend:
    return') ANTES de preencher relatorio.arquivos/bytes_baixados. A tarefa
    termina 'concluido' com relatorio sem nenhum arquivo importado, sem linhas e
    sem a soma de rejeitadas da validacao (o aviso de linhas rejeitadas some)."""
    dados = rf.dataset_padrao()
    with respx.mock(assert_all_called=False) as router:
        rf.PortalFalso({"2026-10": dados}).instalar(router)
        with pytest.raises(Interrompido):
            Atualizacao("2026-10", reporter=_PausaNaConsolidacao("cnaes"), sleep=lambda s: None).executar()
        rel = Atualizacao("2026-10", sleep=lambda s: None).executar()
    publicados = [r[0] for r in duckdb.connect(str(arm.base_ativa().caminho), read_only=True)
                  .execute("SELECT nome FROM importacao_arquivos ORDER BY 1").fetchall()]
    assert sorted(rel["arquivos"]) == publicados, (
        f"relatorio da versao publicada lista {sorted(rel['arquivos'])}; base tem {len(publicados)} arquivos")
