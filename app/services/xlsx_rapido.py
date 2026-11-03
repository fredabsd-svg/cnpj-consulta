"""Escritor de planilha .xlsx em fluxo, rapido e sem carregar nada na RAM.

O .xlsx e um ZIP com XML. Aqui a aba e escrita direto no ZIP, linha a linha,
com textos "inline" (sem tabela de textos compartilhados): o arquivo abre no
Excel, LibreOffice e Google Planilhas. E de 5 a 10 vezes mais rapido que montar
celula por celula com o openpyxl, o que importa em exportacoes de milhoes de
linhas. Acima de `linhas_por_aba` a exportacao continua numa nova aba.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

_PROIBIDOS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")  # invalidos em XML 1.0
_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_NS_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _celula(v: Any) -> str:
    if v is None or v == "":
        return "<c/>"
    if isinstance(v, bool):
        return f'<c t="b"><v>{int(v)}</v></c>'
    if isinstance(v, int | float):
        return f"<c><v>{v}</v></c>"
    texto = escape(_PROIBIDOS.sub("", str(v)))
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

    # -- abas
    def _abrir_aba(self, nome: str) -> None:
        self._fechar_aba()
        self.abas.append(nome)
        # A aba vai primeiro para um temporario (no mesmo disco do destino): assim o
        # ZIP so usa ZIP64 se a aba passar de 4 GB (o LibreOffice nao le ZIP64).
        fd, self._tmp = tempfile.mkstemp(prefix=".aba-", suffix=".xml", dir=self._pasta)
        self._aba = os.fdopen(fd, "wb", buffering=1 << 20)
        self._aba.write(f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet {_NS} {_NS_R}>'
                        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
                        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetData>'.encode())
        self._linha = 0
        if self._cabecalho is not None:
            self._escrever(self._cabecalho, estilo=1)

    def _fechar_aba(self) -> None:
        if self._aba is not None:
            self._aba.write(b"</sheetData></worksheet>")
            self._aba.close()
            self._aba = None
            try:
                self.zip.write(self._tmp, f"xl/worksheets/sheet{len(self.abas)}.xml")
            finally:
                os.unlink(self._tmp)
                self._tmp = None

    def _escrever(self, valores: Iterable[Any], estilo: int = 0) -> None:
        self._linha += 1
        if estilo:
            cel = "".join(f'<c t="inlineStr" s="{estilo}"><is><t>{escape(_PROIBIDOS.sub("", str(v)))}</t></is></c>'
                          for v in valores)
        else:
            cel = "".join(_celula(v) for v in valores)
        self._aba.write(f'<row r="{self._linha}">{cel}</row>'.encode())

    # -- uso
    def nova_aba(self, nome: str, cabecalho: list[str] | None = None) -> None:
        """Abas de dados com cabecalho repetido (ex.: Empresas, Empresas 2...)."""
        self._nome, self._cabecalho = nome, cabecalho
        self._abrir_aba(nome)

    def linha(self, valores: Iterable[Any]) -> None:
        if self._linha - (1 if self._cabecalho is not None else 0) >= self.linhas_por_aba:
            self._abrir_aba(f"{self._nome} {sum(1 for a in self.abas if a.startswith(self._nome)) + 1}")
        self._escrever(valores)

    def aba_simples(self, nome: str, linhas: list[list[Any]]) -> None:
        self._cabecalho = None
        self._abrir_aba(nome)
        for v in linhas:
            self._escrever(v)

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
        self.zip.writestr("xl/styles.xml", x + (
            f'<styleSheet {_NS}><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>"))
        self.zip.close()
