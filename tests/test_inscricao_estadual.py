"""Inscricao estadual: portal CCC e web service CadConsultaCadastro4 (certificado A1)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services import inscricao_estadual as ie

CNPJ = "19131243000197"
URL_SP = "https://sefaz.example.test/ws/cadconsultacadastro4.asmx"

RESPOSTA_OK = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
 <soap:Body>
  <nfeResultMsg xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/CadConsultaCadastro4">
   <retConsCad versao="2.00" xmlns="http://www.portalfiscal.inf.br/nfe">
    <infCons>
     <verAplic>SP_NFE_PL009_V4</verAplic><cStat>111</cStat>
     <xMotivo>Consulta cadastro com uma ocorrência</xMotivo>
     <UF>SP</UF><CNPJ>{CNPJ}</CNPJ><dhCons>2026-09-25T10:00:00-03:00</dhCons><cUF>35</cUF>
     <infCad>
      <IE>123456789110</IE><CNPJ>{CNPJ}</CNPJ><UF>SP</UF><cSit>1</cSit>
      <indCredNFe>1</indCredNFe><indCredCTe>4</indCredCTe>
      <xNome>OPEN KNOWLEDGE BRASIL</xNome><xFant>REDE PELO CONHECIMENTO LIVRE</xFant>
      <xRegApur>NORMAL - REGIME PERIÓDICO DE APURAÇÃO</xRegApur><CNAE>9430800</CNAE>
      <dIniAtiv>2013-10-03</dIniAtiv><dUltSit>2013-10-03</dUltSit>
      <ender><xLgr>AVENIDA PAULISTA</xLgr><nro>37</nro><xCpl>ANDAR 4</xCpl><xBairro>BELA VISTA</xBairro>
       <cMun>3550308</cMun><xMun>SAO PAULO</xMun><CEP>01311902</CEP></ender>
     </infCad>
    </infCons>
   </retConsCad>
  </nfeResultMsg>
 </soap:Body>
</soap:Envelope>"""

RESPOSTA_SEM_IE = """<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body>
<nfeResultMsg xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/CadConsultaCadastro4">
<retConsCad versao="2.00" xmlns="http://www.portalfiscal.inf.br/nfe"><infCons>
<cStat>259</cStat><xMotivo>Rejeição: CNPJ da consulta não cadastrado como contribuinte na UF</xMotivo>
<UF>SP</UF></infCons></retConsCad></nfeResultMsg></soap:Body></soap:Envelope>"""


def _pfx(tmp_path: Path, senha: bytes = b"segredo") -> Path:
    """Certificado A1 de TESTE (autoassinado), gerado na hora."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ESCRITORIO TESTE:00000000000000")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "teste.pfx"
    path.write_bytes(pkcs12.serialize_key_and_certificates(b"teste", key, cert, None, BestAvailableEncryption(senha)))
    return path


@pytest.fixture(autouse=True)
def _isola(monkeypatch):
    ie.clear_cache()
    ie.reset_ssl_context()
    monkeypatch.setattr(ie, "ENDPOINTS", {"SP": URL_SP})
    yield
    ie.clear_cache()
    ie.reset_ssl_context()


@pytest.fixture
def certificado(tmp_path, monkeypatch):
    monkeypatch.setenv("CERTIFICADO_A1_PATH", str(_pfx(tmp_path)))
    monkeypatch.setenv("CERTIFICADO_A1_SENHA", "segredo")
    get_settings.cache_clear()


@pytest.fixture
def client(db_initialized):
    return TestClient(app)


def _mock_fontes(payload):
    respx.get(f"https://minhareceita.org/{CNPJ}").mock(return_value=httpx.Response(200, json=payload))
    respx.get(f"https://brasilapi.com.br/api/cnpj/v1/{CNPJ}").mock(return_value=httpx.Response(200, json=payload))
    respx.get(f"https://www.receitaws.com.br/v1/cnpj/{CNPJ}").mock(return_value=httpx.Response(404))


class TestEnvelopeEParse:
    def test_envelope_cons_cad(self):
        xml = ie.build_envelope(CNPJ, "SP")
        assert '<ConsCad xmlns="http://www.portalfiscal.inf.br/nfe" versao="2.00">' in xml
        assert f"<xServ>CONS-CAD</xServ><UF>SP</UF><CNPJ>{CNPJ}</CNPJ>" in xml
        assert 'xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/CadConsultaCadastro4"' in xml

    def test_parse_uma_inscricao(self):
        r = ie.parse_response(RESPOSTA_OK, "SP")
        assert r.consultada and r.cstat == "111"
        (i,) = r.inscricoes
        assert i.ie == "123456789110" and i.habilitada and i.situacao == "Habilitado"
        assert i.regime_apuracao.startswith("NORMAL")
        assert i.inicio_atividade == dt.date(2013, 10, 3)
        assert i.credenciamento_nfe == "Credenciado"
        assert "AVENIDA PAULISTA, 37" in i.endereco
        assert i.municipio == "SAO PAULO/SP · CEP 01311-902"

    def test_parse_sem_inscricao(self):
        r = ie.parse_response(RESPOSTA_SEM_IE, "SP")
        assert r.consultada and r.cstat == "259" and r.inscricoes == []


class TestConsulta:
    async def test_sem_certificado(self):
        r = await ie.consultar_ie(CNPJ, "SP")
        assert not r.consultada and "Certificado" in r.erro

    async def test_uf_sem_web_service(self, certificado):
        r = await ie.consultar_ie(CNPJ, "AC")
        assert not r.consultada and "CCC" in r.erro

    async def test_uf_invalida(self, certificado):
        assert (await ie.consultar_ie(CNPJ, "XX")).erro == "UF inválida."

    @respx.mock
    async def test_consulta_com_certificado(self, certificado):
        route = respx.post(URL_SP).mock(return_value=httpx.Response(200, text=RESPOSTA_OK))
        r = await ie.consultar_ie(CNPJ, "sp")
        assert r.consultada and r.inscricoes[0].ie == "123456789110"
        req = route.calls[0].request
        assert "application/soap+xml" in req.headers["content-type"]
        assert 'action="http://www.portalfiscal.inf.br/nfe/wsdl/CadConsultaCadastro4/consultaCadastro"' in req.headers["content-type"]
        assert f"<CNPJ>{CNPJ}</CNPJ>" in req.content.decode()
        # cache: nao consulta a SEFAZ de novo
        again = await ie.consultar_ie(CNPJ, "SP")
        assert again.do_cache and route.call_count == 1
        await ie.consultar_ie(CNPJ, "SP", force_refresh=True)
        assert route.call_count == 2

    async def test_senha_errada(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CERTIFICADO_A1_PATH", str(_pfx(tmp_path)))
        monkeypatch.setenv("CERTIFICADO_A1_SENHA", "errada")
        get_settings.cache_clear()
        r = await ie.consultar_ie(CNPJ, "SP")
        assert not r.consultada and "senha" in r.erro

    def test_chave_nao_fica_em_disco(self, tmp_path, certificado):
        """O arquivo temporario da chave e apagado assim que o ssl carrega."""
        import tempfile

        antes = set(Path(tempfile.gettempdir()).glob("cnpj-cert-*"))
        ie.ssl_context(get_settings())
        assert set(Path(tempfile.gettempdir()).glob("cnpj-cert-*")) == antes

    @respx.mock
    async def test_http_500_sem_xml(self, certificado):
        respx.post(URL_SP).mock(return_value=httpx.Response(500, text="erro"))
        r = await ie.consultar_ie(CNPJ, "SP")
        assert not r.consultada and "indisponível" in r.erro


class TestPaginaEApi:
    @respx.mock
    def test_sem_certificado_mostra_ccc(self, client, brasilapi_payload):
        _mock_fontes(brasilapi_payload)
        r = client.get(f"/empresa/{CNPJ}/inscricao-estadual")
        assert r.status_code == 200
        assert "https://dfe-portal.svrs.rs.gov.br/NFE/CCC" in r.text
        assert "CERTIFICADO_A1_PATH" in r.text
        assert "sede em SP" in r.text

    def test_api_desligada_503(self, client):
        assert client.get(f"/api/companies/{CNPJ}/inscricao-estadual", params={"uf": "SP"}).status_code == 503

    @respx.mock
    def test_com_certificado_consulta_uf_da_sede(self, client, certificado, brasilapi_payload):
        _mock_fontes(brasilapi_payload)
        respx.post(URL_SP).mock(return_value=httpx.Response(200, text=RESPOSTA_OK))
        r = client.get(f"/empresa/{CNPJ}/inscricao-estadual")
        assert r.status_code == 200
        assert "123456789110" in r.text and "Habilitado" in r.text
        assert "segredo" not in r.text  # senha nunca aparece
        api = client.get(f"/api/companies/{CNPJ}/inscricao-estadual", params={"uf": "SP"}).json()
        assert api["inscricoes"][0]["ie"] == "123456789110"

    @respx.mock
    def test_com_certificado_sem_inscricao(self, client, certificado, brasilapi_payload):
        _mock_fontes(brasilapi_payload)
        respx.post(URL_SP).mock(return_value=httpx.Response(200, text=RESPOSTA_SEM_IE))
        r = client.get(f"/empresa/{CNPJ}/inscricao-estadual", params={"uf": "SP"})
        assert "Nenhuma inscrição estadual em SP" in r.text


RESPOSTA_257 = """<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body>
<nfeResultMsg xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/CadConsultaCadastro4">
<retConsCad versao="2.00" xmlns="http://www.portalfiscal.inf.br/nfe"><infCons>
<cStat>257</cStat><xMotivo>Rejeicao: Solicitante nao habilitado para emissao da NF-e</xMotivo>
<UF>SP</UF></infCons></retConsCad></nfeResultMsg></soap:Body></soap:Envelope>"""


class TestTabelaOficial:
    def test_ufs_com_e_sem_web_service(self, monkeypatch):
        monkeypatch.undo()  # tabela real, sem o ENDPOINTS de teste
        tabela = ie.ENDPOINTS
        assert len(tabela) == 15
        assert tabela["SC"] == tabela["RS"]  # SC atendida pela SVRS
        for uf in ("RJ", "CE", "DF", "MA"):
            assert uf not in tabela
        assert all(u.startswith("https://") for u in tabela.values())

    def test_envelope_mt_tem_wrapper(self):
        xml = ie.build_envelope(CNPJ, "MT")
        assert "<consultaCadastro xmlns=" in xml and "<nfeDadosMsg><ConsCad" in xml
        assert "<consultaCadastro" not in ie.build_envelope(CNPJ, "SP")

    def test_raiz_icp_brasil_empacotada(self):
        texto = ie.ICP_BRASIL_CA.read_text()
        assert texto.count("BEGIN CERTIFICATE") == 2
        import ssl

        ssl.create_default_context().load_verify_locations(cafile=str(ie.ICP_BRASIL_CA))

    def test_cnpj_alfanumerico_no_envelope(self):
        assert "<CNPJ>12ABC34501DE35</CNPJ>" in ie.build_envelope("12ABC34501DE35", "SP")


class TestRejeicoes:
    @respx.mock
    async def test_257_emissor_nao_habilitado(self, certificado):
        respx.post(URL_SP).mock(return_value=httpx.Response(200, text=RESPOSTA_257))
        r = await ie.consultar_ie(CNPJ, "SP")
        assert not r.consultada and r.cstat == "257"
        assert "emissoras de NF-e" in r.erro and "CCC" in r.erro

    def test_259_nao_e_rejeicao(self):
        r = ie.parse_response(RESPOSTA_SEM_IE, "SP")
        assert r.consultada and r.erro is None
