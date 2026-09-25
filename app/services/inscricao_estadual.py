"""Inscricao estadual (IE): portal CCC e web service de consulta cadastral da SEFAZ.

Duas formas, as duas oficiais:

1. **Portal CCC** (Cadastro Centralizado de Contribuintes, SVRS): consulta de
   todas as UFs, mas exige login gov.br e nao pode ser exibido dentro do app
   (X-Frame-Options). O app abre o portal em nova janela e ajuda com o CNPJ e a UF.
2. **Web service CadConsultaCadastro4** (opcional): a mesma consulta, direto no
   app, usando o certificado digital A1 (e-CNPJ) do escritorio em TLS mutuo.
   Configure CERTIFICADO_A1_PATH e CERTIFICADO_A1_SENHA no .env.

Seguranca do certificado: a senha fica so no .env local; a chave privada e
carregada na memoria e gravada apenas por instantes, CIFRADA, num arquivo
temporario privado exigido pelo modulo ssl -- removido logo em seguida.
Nada do certificado e logado ou exibido.
"""

from __future__ import annotations

import logging
import os
import ssl
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from app import __version__
from app.config import Settings, get_settings

log = logging.getLogger(__name__)

WSDL_NS = "http://www.portalfiscal.inf.br/nfe/wsdl/CadConsultaCadastro4"
NFE_NS = "http://www.portalfiscal.inf.br/nfe"
SOAP_ACTION = f"{WSDL_NS}/consultaCadastro"

UFS = (
    "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split()
)

# Endpoints de producao do CadConsultaCadastro4, conforme a "Relacao de Servicos
# Web" do Portal Nacional da NF-e (conferida em 2026-09). UF ausente = a SEFAZ
# nao oferece o servico (AL, AP, CE, DF, MA, PA, PI, RJ, RO, RR, SE, TO): so CCC.
_SVRS = "https://cad.svrs.rs.gov.br/ws/cadconsultacadastro/cadconsultacadastro4.asmx"
ENDPOINTS: dict[str, str] = {
    "AM": "https://nfe.sefaz.am.gov.br/services2/services/CadConsultaCadastro4",
    "BA": "https://nfe.sefaz.ba.gov.br/webservices/CadConsultaCadastro4/CadConsultaCadastro4.asmx",
    "GO": "https://nfe.sefaz.go.gov.br/nfe/services/CadConsultaCadastro4",
    "MG": "https://nfe.fazenda.mg.gov.br/nfe2/services/CadConsultaCadastro4",
    "MS": "https://nfe.sefaz.ms.gov.br/ws/CadConsultaCadastro4",
    "MT": "https://nfe.sefaz.mt.gov.br/nfews/v2/services/CadConsultaCadastro4",
    "PE": "https://nfe.sefaz.pe.gov.br/nfe-service/services/CadConsultaCadastro4",
    "PR": "https://nfe.sefa.pr.gov.br/nfe/CadConsultaCadastro4",
    "SP": "https://nfe.fazenda.sp.gov.br/ws/cadconsultacadastro4.asmx",
    "RS": _SVRS,
    # Atendidas pela SVRS
    "AC": _SVRS, "ES": _SVRS, "PB": _SVRS, "RN": _SVRS, "SC": _SVRS,
}
# Raizes ICP-Brasil (v5 e v10): varias SEFAZ usam certificado de servidor da
# ICP-Brasil, ausente do repositorio de CAs do Python fora do Windows.
ICP_BRASIL_CA = Path(__file__).resolve().parent.parent / "certs" / "icp-brasil-raiz.pem"

# Retornos comuns (cStat) com orientacao ao usuario.
CSTAT_AJUDA = {
    "257": "A SEFAZ só atende certificados de empresas emissoras de NF-e, e o certificado configurado não é de "
    "emissor. Use um certificado de empresa emissora ou consulte pelo portal CCC.",
    "258": "A SEFAZ considerou o CNPJ inválido.",
    "265": "A UF da consulta difere da UF atendida por este web service.",
    "280": "Certificado do escritório inválido.",
    "281": "Certificado do escritório vencido: renove o A1.",
    "282": "O certificado não traz CNPJ.",
    "283": "Cadeia do certificado com problema.",
    "284": "Certificado revogado.",
    "285": "O certificado não é ICP-Brasil.",
    "286": "A SEFAZ não conseguiu verificar a revogação do certificado; tente mais tarde.",
    "108": "Serviço da SEFAZ paralisado momentaneamente; tente mais tarde.",
    "109": "Serviço da SEFAZ paralisado sem previsão; use o portal CCC.",
    "656": "A SEFAZ bloqueou por consumo indevido (consultas repetidas); aguarde antes de tentar de novo.",
}
_CSTAT_SEM_CADASTRO = {"259", "261", "264"}

SITUACAO = {"0": "Não habilitado", "1": "Habilitado"}
CREDENCIAMENTO = {
    "0": "Não credenciado",
    "1": "Credenciado",
    "2": "Credenciado com obrigatoriedade para todas as operações",
    "3": "Credenciado com obrigatoriedade parcial",
    "4": "Credenciamento a critério da UF",
}

_TTL = 6 * 3600
_TTL_FALHA = 300


@dataclass(slots=True)
class Inscricao:
    ie: str
    uf: str
    situacao: str
    habilitada: bool
    nome: str | None = None
    fantasia: str | None = None
    regime_apuracao: str | None = None
    cnae: str | None = None
    inicio_atividade: date | None = None
    ultima_situacao: date | None = None
    baixa: date | None = None
    credenciamento_nfe: str | None = None
    credenciamento_cte: str | None = None
    ie_unica: str | None = None
    ie_atual: str | None = None
    endereco: str | None = None
    municipio: str | None = None


@dataclass(slots=True)
class ConsultaIE:
    uf: str
    consultada: bool
    inscricoes: list[Inscricao] = field(default_factory=list)
    cstat: str | None = None
    motivo: str | None = None
    erro: str | None = None
    do_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_enabled(settings: Settings | None = None) -> bool:
    cfg = settings or get_settings()
    return bool(cfg.certificado_a1_path and cfg.certificado_a1_senha)


def endpoint_for(uf: str) -> str | None:
    return ENDPOINTS.get((uf or "").upper())


# ---------------------------------------------------------------------------
# Certificado A1 -> contexto TLS (TLS mutuo)
# ---------------------------------------------------------------------------

_ssl_ctx: tuple[tuple[str, float], ssl.SSLContext] | None = None


class CertificadoError(RuntimeError):
    """Certificado ausente, senha errada ou arquivo invalido."""


def _build_ssl_context(cfg: Settings) -> ssl.SSLContext:
    from cryptography.hazmat.primitives.serialization import (
        BestAvailableEncryption,
        Encoding,
        PrivateFormat,
        pkcs12,
    )

    path = Path(cfg.certificado_a1_path).expanduser()
    try:
        data = path.read_bytes()
    except OSError as e:
        raise CertificadoError(f"não foi possível ler o certificado em {path.name}") from e
    senha = cfg.certificado_a1_senha.encode()
    try:
        key, cert, extras = pkcs12.load_key_and_certificates(data, senha)
    except ValueError as e:
        raise CertificadoError("senha do certificado incorreta ou arquivo .pfx inválido") from e
    if key is None or cert is None:
        raise CertificadoError("o arquivo .pfx não contém chave privada e certificado")

    ctx = ssl.create_default_context()
    if ICP_BRASIL_CA.exists():
        ctx.load_verify_locations(cafile=str(ICP_BRASIL_CA))
    if cfg.sefaz_ca_bundle:
        ctx.load_verify_locations(cafile=str(Path(cfg.sefaz_ca_bundle).expanduser()))
    chain = cert.public_bytes(Encoding.PEM) + b"".join(c.public_bytes(Encoding.PEM) for c in extras or [])
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, BestAvailableEncryption(senha))
    # O modulo ssl so carrega de arquivo: grava por instantes (chave CIFRADA) num
    # diretorio privado e apaga em seguida.
    with tempfile.TemporaryDirectory(prefix="cnpj-cert-") as tmp:
        os.chmod(tmp, 0o700)
        cert_file, key_file = Path(tmp) / "c.pem", Path(tmp) / "k.pem"
        cert_file.write_bytes(chain)
        key_file.write_bytes(key_pem)
        os.chmod(key_file, 0o600)
        ctx.load_cert_chain(str(cert_file), str(key_file), password=senha)
    return ctx


def ssl_context(cfg: Settings) -> ssl.SSLContext:
    """Contexto TLS com o certificado do escritorio (recarrega se o arquivo mudar)."""
    global _ssl_ctx
    path = str(Path(cfg.certificado_a1_path).expanduser())
    try:
        stamp = (path, Path(path).stat().st_mtime)
    except OSError:
        stamp = (path, 0.0)
    if _ssl_ctx is None or _ssl_ctx[0] != stamp:
        _ssl_ctx = (stamp, _build_ssl_context(cfg))
    return _ssl_ctx[1]


def reset_ssl_context() -> None:
    global _ssl_ctx
    _ssl_ctx = None


# ---------------------------------------------------------------------------
# SOAP
# ---------------------------------------------------------------------------


def build_envelope(cnpj: str, uf: str) -> str:
    cons = (
        f'<ConsCad xmlns="{NFE_NS}" versao="2.00">'
        f"<infCons><xServ>CONS-CAD</xServ><UF>{uf}</UF><CNPJ>{cnpj}</CNPJ></infCons>"
        "</ConsCad>"
    )
    if uf == "MT":  # MT exige o wrapper consultaCadastro em volta do nfeDadosMsg
        body = f'<consultaCadastro xmlns="{WSDL_NS}"><nfeDadosMsg>{cons}</nfeDadosMsg></consultaCadastro>'
    else:
        body = f'<nfeDadosMsg xmlns="{WSDL_NS}">{cons}</nfeDadosMsg>'
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">'
        f"<soap12:Body>{body}</soap12:Body>"
        "</soap12:Envelope>"
    )


def _text(el: ET.Element | None, tag: str) -> str | None:
    if el is None:
        return None
    found = el.find(f"{{*}}{tag}")
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def parse_response(xml_text: str, uf: str) -> ConsultaIE:
    """Le o retConsCad (tolerante a prefixos/namespaces)."""
    root = ET.fromstring(xml_text)
    ret = root if root.tag.endswith("retConsCad") else root.find(".//{*}retConsCad")
    if ret is None:
        raise ValueError("resposta sem retConsCad")
    inf = ret.find("{*}infCons")
    cstat, motivo = _text(inf, "cStat"), _text(inf, "xMotivo")
    if cstat not in ("111", "112") and cstat not in _CSTAT_SEM_CADASTRO:
        # Rejeicao (certificado, emissor, servico parado...): nao e "sem IE".
        ajuda = CSTAT_AJUDA.get(cstat or "", "")
        return ConsultaIE(
            uf=uf,
            consultada=False,
            cstat=cstat,
            motivo=motivo,
            erro=f"SEFAZ-{uf} recusou a consulta ({cstat}: {motivo or 'sem motivo'}). {ajuda}".strip(),
        )
    result = ConsultaIE(uf=uf, consultada=True, cstat=cstat, motivo=motivo)
    for cad in inf.findall("{*}infCad") if inf is not None else []:
        ender = cad.find("{*}ender")
        rua = ", ".join(filter(None, [_text(ender, "xLgr"), _text(ender, "nro")]))
        partes = [rua, _text(ender, "xCpl"), _text(ender, "xBairro")]
        cep = _text(ender, "CEP")
        sit = _text(cad, "cSit") or ""
        result.inscricoes.append(
            Inscricao(
                ie=_text(cad, "IE") or "—",
                uf=_text(cad, "UF") or uf,
                situacao=SITUACAO.get(sit, sit or "Não informada"),
                habilitada=sit == "1",
                nome=_text(cad, "xNome"),
                fantasia=_text(cad, "xFant"),
                regime_apuracao=_text(cad, "xRegApur"),
                cnae=_text(cad, "CNAE"),
                inicio_atividade=_date(_text(cad, "dIniAtiv")),
                ultima_situacao=_date(_text(cad, "dUltSit")),
                baixa=_date(_text(cad, "dBaixa")),
                credenciamento_nfe=CREDENCIAMENTO.get(_text(cad, "indCredNFe") or "", None),
                credenciamento_cte=CREDENCIAMENTO.get(_text(cad, "indCredCTe") or "", None),
                ie_unica=_text(cad, "IEUnica"),
                ie_atual=_text(cad, "IEAtual"),
                endereco=" · ".join(p for p in partes if p) or None,
                municipio="/".join(filter(None, [_text(ender, "xMun"), _text(cad, "UF")]))
                + (f" · CEP {cep[:5]}-{cep[5:]}" if cep and len(cep) == 8 else ""),
            )
        )
    return result


_cache: dict[tuple[str, str], tuple[float, ConsultaIE]] = {}


async def consultar_ie(
    cnpj: str,
    uf: str,
    *,
    force_refresh: bool = False,
    settings: Settings | None = None,
) -> ConsultaIE:
    """Consulta a IE do CNPJ na SEFAZ da UF. Nunca levanta excecao."""
    cfg = settings or get_settings()
    uf = (uf or "").upper()
    if uf not in UFS:
        return ConsultaIE(uf=uf, consultada=False, erro="UF inválida.")
    url = endpoint_for(uf)
    if url is None:
        return ConsultaIE(
            uf=uf, consultada=False, erro=f"A SEFAZ de {uf} não oferece a consulta cadastral por web service. Use o portal CCC."
        )
    if not is_enabled(cfg):
        return ConsultaIE(uf=uf, consultada=False, erro="Certificado A1 não configurado.")

    key = (cnpj, uf)
    hit = None if force_refresh else _cache.get(key)
    if hit and time.monotonic() - hit[0] < (_TTL if hit[1].consultada else _TTL_FALHA):
        hit[1].do_cache = True
        return hit[1]

    result = await _call(url, cnpj, uf, cfg)
    if len(_cache) > 512:
        _cache.clear()
    _cache[key] = (time.monotonic(), result)
    return result


async def _call(url: str, cnpj: str, uf: str, cfg: Settings) -> ConsultaIE:
    try:
        ctx = ssl_context(cfg)
    except CertificadoError as e:
        return ConsultaIE(uf=uf, consultada=False, erro=f"Certificado: {e}.")
    headers = {
        "Content-Type": f'application/soap+xml; charset=utf-8; action="{SOAP_ACTION}"',
        "User-Agent": f"cnpj-consulta/{__version__}",
    }
    try:
        # A consulta cadastral "nao tem a mesma disponibilidade dos demais" servicos (MOC): 30 s.
        async with httpx.AsyncClient(verify=ctx, timeout=max(30.0, float(cfg.request_timeout_seconds))) as client:
            resp = await client.post(url, content=build_envelope(cnpj, uf).encode(), headers=headers)
        if resp.status_code >= 400 and "retConsCad" not in resp.text:
            return ConsultaIE(uf=uf, consultada=False, erro=f"SEFAZ-{uf} indisponível (HTTP {resp.status_code}).")
        return parse_response(resp.text, uf)
    except ssl.SSLError as e:
        log.warning("TLS com SEFAZ-%s falhou: %s", uf, type(e).__name__)
        return ConsultaIE(
            uf=uf,
            consultada=False,
            erro=f"Falha de TLS com a SEFAZ-{uf}. Confira o certificado A1 e, se preciso, SEFAZ_CA_BUNDLE.",
        )
    except httpx.TimeoutException:
        return ConsultaIE(uf=uf, consultada=False, erro=f"SEFAZ-{uf} não respondeu a tempo.")
    except httpx.HTTPError as e:
        log.warning("consulta cadastral SEFAZ-%s falhou: %s", uf, type(e).__name__)
        return ConsultaIE(uf=uf, consultada=False, erro=f"Falha de conexão com a SEFAZ-{uf}.")
    except (ET.ParseError, ValueError):
        return ConsultaIE(uf=uf, consultada=False, erro=f"SEFAZ-{uf} respondeu em formato inesperado.")


def clear_cache() -> None:
    _cache.clear()
