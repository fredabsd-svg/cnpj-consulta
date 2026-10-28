"""Portal falso da Receita (WebDAV do Nextcloud) e gerador de dados no leiaute
oficial, para testar o pipeline sem rede."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from collections.abc import Iterator

import httpx
import respx

from app.receita.layout import ESTABELECIMENTOS_COLS

ORIGEM = "https://arquivos.receitafederal.gov.br"
TOKEN = "TesteTok3n1234"
PASTA = "/public.php/webdav/Dados/Cadastros/CNPJ"


def csv_bytes(rows: list[list[str]]) -> bytes:
    lines = [";".join(f'"{v}"' for v in row) for row in rows]
    return ("\n".join(lines) + "\n").encode("latin-1")


def zip_bytes(member: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, data)
    return buf.getvalue()


def estab(basico: str, ordem: str, dv: str, *, matriz: bool = True, fantasia: str = "", situacao: str = "02",
          cnae: str = "6920601", secundarios: str = "", uf: str = "TO", municipio: str = "9733",
          ddd1: str = "", tel1: str = "", ddd2: str = "", tel2: str = "", email: str = "",
          inicio: str = "20200115", bairro: str = "CENTRO", cep: str = "77001000", numero: str = "100",
          logradouro: str = "DAS FLORES") -> list[str]:
    row = [
        basico, ordem, dv, "1" if matriz else "2", fantasia, situacao, "20200115", "00", "", "",
        inicio, cnae, secundarios, "RUA", logradouro, numero, "", bairro, cep, uf, municipio,
        ddd1, tel1, ddd2, tel2, "", "", email, "", "",
    ]
    assert len(row) == len(ESTABELECIMENTOS_COLS)
    return row


def dominios() -> dict[str, bytes]:
    return {
        "Cnaes.zip": zip_bytes("F.K03200$Z.D61010.CNAECSV", csv_bytes([
            ["6920601", "Atividades de contabilidade"],
            ["6920602", "Atividades de consultoria e auditoria contábil e tributária"],
            ["6911701", "Serviços advocatícios"],
            ["6201501", "Desenvolvimento de programas de computador sob encomenda"],
            ["4711302", "Comércio varejista de mercadorias em geral - supermercados"],
            ["8211300", "Serviços combinados de escritório e apoio administrativo"],
        ])),
        "Motivos.zip": zip_bytes("F.K03200$Z.D61010.MOTICSV", csv_bytes([["00", "SEM MOTIVO"], ["01", "EXTINCAO"]])),
        "Municipios.zip": zip_bytes("F.K03200$Z.D61010.MUNICCSV", csv_bytes([
            ["9733", "PALMAS"], ["9241", "ARAGUAINA"], ["9701", "GURUPI"], ["7107", "SAO PAULO"],
        ])),
        "Naturezas.zip": zip_bytes("F.K03200$Z.D61010.NATJUCSV", csv_bytes([
            ["2062", "Sociedade Empresária Limitada"], ["2135", "Empresário (Individual)"],
        ])),
        "Paises.zip": zip_bytes("F.K03200$Z.D61010.PAISCSV", csv_bytes([["105", "BRASIL"]])),
        "Qualificacoes.zip": zip_bytes("F.K03200$Z.D61010.QUALSCSV", csv_bytes([
            ["05", "Administrador"], ["22", "Sócio"], ["49", "Sócio-Administrador"], ["50", "Empresário"],
        ])),
    }


def dataset_padrao() -> dict[str, bytes]:
    """Duas empresas (uma com matriz+filial em partes diferentes), socios, Simples."""
    files = dominios()
    files["Empresas0.zip"] = zip_bytes("K3241.K03200Y0.D61010.EMPRECSV", csv_bytes([
        ["12345678", "CONTABIL TESTE LTDA", "2062", "49", "15000,00", "01", ""],
    ]))
    files["Empresas1.zip"] = zip_bytes("K3241.K03200Y1.D61010.EMPRECSV", csv_bytes([
        ["87654321", "MERCADO EXEMPLO LTDA", "2062", "49", "250000,50", "03", ""],
        # duplicado de proposito (a consolidacao remove)
        ["87654321", "MERCADO EXEMPLO LTDA", "2062", "49", "250000,50", "03", ""],
        # empresario individual / MEI: sem QSA, sem telefone e sem e-mail
        ["11222333", "JOSE PEREIRA 12345678900", "2135", "50", "5000,00", "01", ""],
    ]))
    files["Estabelecimentos0.zip"] = zip_bytes("K3241.K03200Y0.D61010.ESTABELE", csv_bytes([
        estab("12345678", "0001", "95", fantasia="CONTA FACIL", ddd1="63", tel1="32151234",
              ddd2="63", tel2="92345678", email="CONTATO@CONTAFACIL.COM.BR", secundarios="6920602,8211300"),
        estab("87654321", "0001", "98", fantasia="", cnae="4711302", uf="TO", municipio="9241",
              email="mercado@gmail.com"),
    ]))
    files["Estabelecimentos1.zip"] = zip_bytes("K3241.K03200Y1.D61010.ESTABELE", csv_bytes([
        estab("12345678", "0002", "76", matriz=False, uf="TO", municipio="9701", bairro="", cep="7700"),
        estab("11222333", "0001", "81", cnae="9602501", uf="GO", municipio="9373", inicio="20250301"),
    ]))
    files["Socios0.zip"] = zip_bytes("K3241.K03200Y0.D61010.SOCIOCSV", csv_bytes([
        ["12345678", "2", "MARIA DA SILVA", "***123456**", "49", "20200115", "", "***000000**", "", "00", "5"],
        ["12345678", "2", "JOAO SOUZA", "***654321**", "22", "20210310", "", "***000000**", "", "00", "4"],
    ]))
    files["Socios1.zip"] = zip_bytes("K3241.K03200Y1.D61010.SOCIOCSV", csv_bytes([
        ["87654321", "2", "MARIA DA SILVA", "***123456**", "05", "20190101", "", "***000000**", "", "00", "5"],
        # homonimo com CPF diferente: outra pessoa
        ["87654321", "2", "MARIA DA SILVA", "***999888**", "22", "20190101", "", "***000000**", "", "00", "3"],
    ]))
    files["Simples.zip"] = zip_bytes("F.K03200$W.SIMPLES.CSV.D61010", csv_bytes([
        ["12345678", "S", "20200115", "00000000", "N", "00000000", "00000000"],
        ["11222333", "S", "20250301", "00000000", "S", "20250301", "00000000"],
    ]))
    return files


def _propfind_xml(base_href: str, itens: list[tuple[str, bool, int, str]]) -> str:
    def resp(href: str, pasta: bool, tam: int, etag: str) -> str:
        rt = "<d:resourcetype><d:collection/></d:resourcetype>" if pasta else "<d:resourcetype/>"
        extra = "" if pasta else f"<d:getcontentlength>{tam}</d:getcontentlength>"
        return (
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
            f"<d:getlastmodified>Tue, 13 Oct 2026 22:00:16 GMT</d:getlastmodified>{rt}{extra}"
            f"<d:getetag>&quot;{etag}&quot;</d:getetag></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )

    body = resp(base_href, True, 0, "pasta")
    for nome, pasta, tam, etag in itens:
        body += resp(base_href + nome + ("/" if pasta else ""), pasta, tam, etag)
    return f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">{body}</d:multistatus>'


class PortalFalso:
    """Simula o compartilhamento: redirect do portal, PROPFIND e GET com Range."""

    def __init__(self, versoes: dict[str, dict[str, bytes]]) -> None:
        self.versoes = versoes
        self.falhas: dict[str, int] = {}  # nome -> quantas vezes cortar a conexao no meio
        self.corromper: dict[str, int] = {}  # nome -> quantas vezes servir bytes errados
        self.ignorar_range = False
        self.gets: list[tuple[str, str | None]] = []

    @staticmethod
    def etag(data: bytes) -> str:
        return hashlib.md5(data, usedforsecurity=False).hexdigest()

    def instalar(self, router: respx.Router) -> None:
        router.get(f"{ORIGEM}/").mock(
            return_value=httpx.Response(302, headers={"Location": f"{ORIGEM}/index.php/s/{TOKEN}"})
        )
        router.route(method="PROPFIND", url__startswith=f"{ORIGEM}/public.php/webdav").mock(side_effect=self._propfind)
        router.get(url__regex=rf"{re.escape(ORIGEM)}{re.escape(PASTA)}/\d{{4}}-\d{{2}}/.+").mock(side_effect=self._get)

    def _propfind(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization"), "PROPFIND sem o token publico"
        path = request.url.path
        if not path.endswith("/"):
            path += "/"
        arvore = {
            "/public.php/webdav/": [("Dados", True, 0, "d")],
            "/public.php/webdav/Dados/": [("Cadastros", True, 0, "c")],
            "/public.php/webdav/Dados/Cadastros/": [("CNPJ", True, 0, "n")],
            f"{PASTA}/": [(m, True, 0, m) for m in sorted(self.versoes)],
        }
        for mes, files in self.versoes.items():
            arvore[f"{PASTA}/{mes}/"] = [(n, False, len(d), self.etag(d)) for n, d in sorted(files.items())]
        if path not in arvore:
            return httpx.Response(404)
        return httpx.Response(207, text=_propfind_xml(path, arvore[path]))

    def _get(self, request: httpx.Request) -> httpx.Response:
        mes, nome = request.url.path.split("/")[-2:]
        data = self.versoes.get(mes, {}).get(nome)
        rng = request.headers.get("range")
        self.gets.append((nome, rng))
        if data is None:
            return httpx.Response(404)
        if self.corromper.get(nome):
            self.corromper[nome] -= 1
            ruim = bytearray(data)
            meio = len(ruim) // 2
            ruim[meio:meio + 8] = b"\x00" * 8
            data = bytes(ruim)
        inicio, status, headers = 0, 200, {"ETag": f'"{self.etag(data)}"'}
        if rng and not self.ignorar_range:
            if_range = request.headers.get("if-range", "").strip('"')
            if not if_range or if_range == self.etag(self.versoes[mes][nome]):
                inicio = int(rng.removeprefix("bytes=").split("-")[0])
                if inicio >= len(data):
                    return httpx.Response(416)
                status = 206
                headers["Content-Range"] = f"bytes {inicio}-{len(data) - 1}/{len(data)}"
        corpo = data[inicio:]
        if self.falhas.get(nome):
            self.falhas[nome] -= 1
            return httpx.Response(status, headers=headers, content=_cortado(corpo))
        return httpx.Response(status, headers=headers, content=corpo)


def _cortado(corpo: bytes) -> Iterator[bytes]:
    metade = max(1, len(corpo) // 2)
    yield corpo[:metade]
    raise httpx.ReadError("conexao interrompida (simulada)")
