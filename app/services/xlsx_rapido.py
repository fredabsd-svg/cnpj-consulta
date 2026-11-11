"""Escritor de planilha .xlsx em fluxo, rapido e sem carregar nada na RAM.

O .xlsx e um ZIP com XML. Aqui a aba e escrita direto no ZIP, linha a linha,
com textos "inline" (sem tabela de textos compartilhados): o arquivo abre no
Excel, LibreOffice e Google Planilhas. O cabecalho sai colorido e congelado, com
setas de filtro; as colunas tem largura propria; datas e valores sao numeros de
verdade (o Excel ordena, filtra e soma). E de 5 a 10 vezes mais rapido que montar
celula por celula com o openpyxl, o que importa em exportacoes de milhoes de
linhas. Acima de `linhas_por_aba` a exportacao continua numa nova aba.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

_PROIBIDOS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")  # invalidos em XML 1.0
_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_NS_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


# Estilos (indices de cellXfs em styles.xml)
E_PADRAO, E_CABECALHO, E_DATA, E_MOEDA, E_INTEIRO, E_TITULO, E_ROTULO, E_QUEBRA, E_DATA_HORA, E_NOTA = range(10)
FORMATOS = {"data": E_DATA, "moeda": E_MOEDA, "inteiro": E_INTEIRO, "titulo": E_TITULO, "rotulo": E_ROTULO,
            "quebra": E_QUEBRA, "nota": E_NOTA}
_EPOCA = date(1899, 12, 30)  # dia zero do Excel
COR_CABECALHO = "0E7490"


def _letra(n: int) -> str:
    """1 -> A, 27 -> AA."""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _celula(v: Any, estilo: int = 0) -> str:
    if v is None or v == "":
        return "<c/>"
    if isinstance(v, bool):
        return f'<c t="b"><v>{int(v)}</v></c>'
    if isinstance(v, int | float):
        return f'<c s="{estilo}"><v>{v}</v></c>' if estilo else f"<c><v>{v}</v></c>"
    if isinstance(v, datetime):  # data de verdade: o Excel ordena e filtra por periodo
        d = v.replace(tzinfo=None) - datetime(1899, 12, 30)
        return f'<c s="{E_DATA_HORA}"><v>{d.days + d.seconds / 86400:.6f}</v></c>'
    if isinstance(v, date):
        return f'<c s="{E_DATA}"><v>{(v - _EPOCA).days}</v></c>'
    texto = escape(_PROIBIDOS.sub("", str(v)))
    if estilo in (E_TITULO, E_ROTULO, E_QUEBRA, E_NOTA):
        return f'<c t="inlineStr" s="{estilo}"><is><t xml:space="preserve">{texto}</t></is></c>'
    return f'<c t="inlineStr"><is><t xml:space="preserve">{texto}</t></is></c>'


class PlanilhaRapida:
    def __init__(self, destino: Path, linhas_por_aba: int = 1_000_000) -> None:
        self.zip = zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED, compresslevel=1)
        self._pasta = destino.parent
        self._tmp: str | None = None
        self.linhas_por_aba = linhas_por_aba
        self.abas: list[str] = []
        self._aba = None
        self._linha = 0
        self._cabecalho: list[str] | None = None
        self._nome = "Empresas"
        self._larguras: list[float] | None = None
        self._estilos: list[int] | None = None
        self._colunas = 0

    # -- abas
    def _abrir_aba(self, nome: str) -> None:
        self._fechar_aba()
        self.abas.append(nome)
        # A aba vai primeiro para um temporario (no mesmo disco do destino): assim o
        # ZIP so usa ZIP64 se a aba passar de 4 GB (o LibreOffice nao le ZIP64).
        fd, self._tmp = tempfile.mkstemp(prefix=".aba-", suffix=".xml", dir=self._pasta)
        self._aba = os.fdopen(fd, "wb", buffering=1 << 20)
        congelar = ('<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                    if self._cabecalho is not None else "")
        cols = ""
        if self._larguras:
            cols = "<cols>" + "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
                                      for i, w in enumerate(self._larguras, 1)) + "</cols>"
        self._aba.write(f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet {_NS} {_NS_R}>'
                        f'<sheetViews><sheetView workbookViewId="0" showGridLines="{int(self._cabecalho is not None)}">'
                        f'{congelar}</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/>{cols}'
                        "<sheetData>".encode())
        self._linha = 0
        if self._cabecalho is not None:
            self._linha = 1
            cel = "".join(f'<c t="inlineStr" s="{E_CABECALHO}"><is><t>{escape(_PROIBIDOS.sub("", str(v)))}</t></is></c>'
                          for v in self._cabecalho)
            self._aba.write(f'<row r="1" ht="30" customHeight="1">{cel}</row>'.encode())

    def _fechar_aba(self) -> None:
        if self._aba is not None:
            filtro = ""
            if self._cabecalho is not None and self._colunas:
                # setas de filtro no cabecalho (classificar e filtrar sem configurar nada)
                filtro = f'<autoFilter ref="A1:{_letra(self._colunas)}{max(self._linha, 1)}"/>'
            self._aba.write(f"</sheetData>{filtro}".encode() + b"</worksheet>")
            self._aba.close()
            self._aba = None
            try:
                self.zip.write(self._tmp, f"xl/worksheets/sheet{len(self.abas)}.xml")
            finally:
                os.unlink(self._tmp)
                self._tmp = None

    def _escrever(self, valores: Iterable[Any]) -> None:
        self._linha += 1
        if self._estilos:
            cel = "".join(map(_celula, valores, self._estilos))
        else:
            cel = "".join(_celula(v) for v in valores)
        self._aba.write(f'<row r="{self._linha}">{cel}</row>'.encode())

    # -- uso
    def nova_aba(self, nome: str, cabecalho: list[str] | None = None, *, larguras: list[float] | None = None,
                 formatos: list[str | None] | None = None) -> None:
        """Abas de dados com cabecalho repetido (ex.: Empresas, Empresas 2...).
        `larguras`: largura de cada coluna (em caracteres). `formatos`: por coluna,
        None, "moeda" ou "inteiro" (datas ja saem formatadas pelo tipo do valor)."""
        self._fechar_aba()  # antes de trocar o cabecalho: a aba anterior fecha com o filtro dela
        self._nome, self._cabecalho = nome, cabecalho
        self._colunas = len(cabecalho or [])
        self._larguras = larguras
        self._estilos = [FORMATOS.get(f or "", 0) for f in formatos] if formatos else None
        if self._estilos and len(self._estilos) != self._colunas:
            raise ValueError("formatos: um por coluna")
        self._abrir_aba(nome)

    def linha(self, valores: Iterable[Any]) -> None:
        if self._linha - (1 if self._cabecalho is not None else 0) >= self.linhas_por_aba:
            self._abrir_aba(f"{self._nome} {sum(1 for a in self.abas if a.startswith(self._nome)) + 1}")
        self._escrever(valores)

    def aba_simples(self, nome: str, linhas: list[list[Any]], larguras: list[float] | None = None) -> None:
        """Aba de texto (resumo, fonte). Cada celula e um valor ou (valor, formato),
        com formato "titulo", "rotulo", "quebra", "nota", "inteiro" ou "moeda"."""
        self._fechar_aba()
        self._cabecalho, self._estilos, self._colunas = None, None, 0
        self._larguras = larguras
        self._abrir_aba(nome)
        for linha in linhas:
            self._linha += 1
            cel = "".join(_celula(v[0], FORMATOS.get(v[1], 0)) if isinstance(v, tuple) else _celula(v) for v in linha)
            self._aba.write(f'<row r="{self._linha}">{cel}</row>'.encode())

    def fechar(self) -> None:
        self._fechar_aba()
        n = len(self.abas)
        sheets = "".join(f'<sheet name="{escape(a)}" sheetId="{i}" r:id="rId{i}"/>' for i, a in enumerate(self.abas, 1))
        rels = "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                       f'relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, n + 1))
        rels += (f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                 'relationships/styles" Target="styles.xml"/>')
        tipos = "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
                        'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, n + 1))
        x = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        self.zip.writestr("[Content_Types].xml", x + (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/'
            f'vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{tipos}</Types>'))
        self.zip.writestr("_rels/.rels", x + (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/></Relationships>'))
        self.zip.writestr("xl/workbook.xml", x + f"<workbook {_NS} {_NS_R}><sheets>{sheets}</sheets></workbook>")
        self.zip.writestr("xl/_rels/workbook.xml.rels", x + (
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}'
            "</Relationships>"))
        fonte = '<sz val="11"/><name val="Calibri"/>'
        self.zip.writestr("xl/styles.xml", x + (
            f"<styleSheet {_NS}>"
            '<numFmts count="4"><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/>'
            '<numFmt numFmtId="165" formatCode="&quot;R$&quot;\\ #,##0.00"/>'
            '<numFmt numFmtId="166" formatCode="#,##0"/>'
            '<numFmt numFmtId="167" formatCode="dd/mm/yyyy\\ hh:mm"/></numFmts>'
            f'<fonts count="5"><font>{fonte}</font>'
            f'<font><b/><color rgb="FFFFFFFF"/>{fonte}</font>'
            f'<font><b/><color rgb="FF{COR_CABECALHO}"/><sz val="16"/><name val="Calibri"/></font>'
            f"<font><b/>{fonte}</font>"
            '<font><i/><color rgb="FF64748B"/><sz val="10"/><name val="Calibri"/></font></fonts>'
            '<fills count="3"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            f'<fill><patternFill patternType="solid"><fgColor rgb="FF{COR_CABECALHO}"/><bgColor indexed="64"/>'
            "</patternFill></fill></fills>"
            '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
            '<border><left style="thin"><color rgb="FF0B5A70"/></left><right style="thin"><color rgb="FF0B5A70"/>'
            '</right><top/><bottom style="thin"><color rgb="FF0B5A70"/></bottom><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="10">'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" '
            'applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
            '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1">'
            '<alignment horizontal="left"/></xf>'
            '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
            '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
            '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
            '<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1">'
            '<alignment vertical="top"/></xf>'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
            '<alignment vertical="top" wrapText="1"/></xf>'
            '<xf numFmtId="167" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1">'
            '<alignment horizontal="left"/></xf>'
            '<xf numFmtId="0" fontId="4" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
            "</cellXfs>"
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>"))
        self.zip.close()
