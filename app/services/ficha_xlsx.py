"""Ficha da empresa em Excel (.xlsx), pronta para entregar ao cliente.

Abas: Resumo (identificacao, endereco, fonte), Contatos, Atividades, Sócios e
Estabelecimentos. Cabecalho colorido e congelado, setas de filtro, larguras
ajustadas, datas e valores como numeros de verdade.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.core.formatting import cep as format_cep
from app.core.formatting import cnae as format_cnae
from app.core.formatting import cnpj as format_cnpj
from app.services.xlsx_rapido import COR_CABECALHO

_FONTE_CAB = Font(bold=True, color="FFFFFF")
_FUNDO_CAB = PatternFill("solid", fgColor=COR_CABECALHO)
_FUNDO_SECAO = PatternFill("solid", fgColor="E6F4F7")
_BORDA = Border(bottom=Side(style="thin", color="D5DEE6"))
_TITULO = Font(bold=True, size=16, color=COR_CABECALHO)
_NOTA = Font(italic=True, size=10, color="64748B")
_ROTULO = Font(bold=True, color="334155")
_SECAO = Font(bold=True, color=COR_CABECALHO)
_SITUACAO = {"ATIVA": "15803D", "BAIXADA": "B91C1C", "NULA": "B91C1C", "INAPTA": "B45309", "SUSPENSA": "B45309"}


def _cel(v: Any) -> Any:
    """Valor seguro para a planilha (sem formula injetada; datas de verdade)."""
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, date):
        return v
    if isinstance(v, dict):  # opcao pelo Simples/MEI
        if not v.get("optante"):
            return "Não optante"
        return "Optante" + (f" desde {v['data_opcao']:%d/%m/%Y}" if v.get("data_opcao") else "")
    if isinstance(v, str) and v[:1] in "=+-@":
        return "'" + v
    return v


def _cnae(codigo: Any) -> str:
    return format_cnae(f"{int(codigo):07d}") if str(codigo).isdigit() else str(codigo)


def _tabela(ws, cabecalho: list[str], linhas: list[list[Any]], larguras: list[float]) -> None:
    ws.append(cabecalho)
    for c in ws[1]:
        c.font, c.fill = _FONTE_CAB, _FUNDO_CAB
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 28
    for linha in linhas:
        ws.append([_cel(v) for v in linha])
    for i, w in enumerate(larguras, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
            c.border = _BORDA
            if isinstance(c.value, date):
                c.number_format = "DD/MM/YYYY"
                c.alignment = Alignment(vertical="top", horizontal="left")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cabecalho))}{max(ws.max_row, 1)}"
    if not linhas:
        ws.append(["Nada consta na base."])
        ws.cell(row=2, column=1).font = _NOTA


def montar(f: dict[str, Any], complementares: list[dict]) -> bytes:
    """`f` e a ficha de app.services.ficha.ficha_local; devolve o .xlsx."""
    ident, e = f["identificacao"], f["endereco"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Resumo"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 70

    ws.append([ident.get("razao_social") or "Empresa"])
    ws["A1"].font = _TITULO
    ws.append([f"CNPJ {format_cnpj(ident['cnpj'])}" + (f" · {ident['nome_fantasia']}" if ident.get("nome_fantasia") else "")])
    ws["A2"].font = Font(bold=True, size=12)
    ws.append([f"Ficha cadastral · {f['base']['fonte']} · referência {f['base']['mes']} · "
               f"gerada em {datetime.now():%d/%m/%Y %H:%M}"])
    ws["A3"].font = _NOTA
    for n in (1, 2, 3):  # titulo ocupa as duas colunas
        ws.merge_cells(start_row=n, start_column=1, end_row=n, end_column=2)
    ws.row_dimensions[1].height = 24
    ws.append([])

    def secao(titulo: str) -> None:
        ws.append([titulo])
        n = ws.max_row
        for col in ("A", "B"):
            ws[f"{col}{n}"].fill = _FUNDO_SECAO
        ws[f"A{n}"].font = _SECAO

    def campo(rotulo: str, valor: Any, formato: str | None = None) -> None:
        if valor in (None, ""):
            return
        ws.append([rotulo, _cel(valor)])
        n = ws.max_row
        ws[f"A{n}"].font = _ROTULO
        ws[f"A{n}"].alignment = Alignment(vertical="top")
        ws[f"B{n}"].alignment = Alignment(vertical="top", horizontal="left", wrap_text=True)
        for col in ("A", "B"):
            ws[f"{col}{n}"].border = _BORDA
        if isinstance(valor, date):
            ws[f"B{n}"].number_format = "DD/MM/YYYY"
        elif formato:
            ws[f"B{n}"].number_format = formato

    secao("Identificação")
    campo("CNPJ", format_cnpj(ident["cnpj"]))
    campo("Tipo", ident.get("matriz_filial"))
    campo("Razão social", ident.get("razao_social"))
    campo("Nome fantasia", ident.get("nome_fantasia"))
    campo("Situação cadastral", ident.get("situacao"))
    if ident.get("situacao"):
        ws[f"B{ws.max_row}"].font = Font(bold=True, color=_SITUACAO.get(str(ident["situacao"]).upper(), "334155"))
    campo("Data da situação", ident.get("data_situacao"))
    campo("Motivo da situação", ident.get("motivo"))
    campo("Situação especial", ident.get("situacao_especial"))
    campo("Início da atividade", ident.get("data_abertura"))
    if ident.get("idade_anos") is not None:
        campo("Tempo de atividade", f"{ident['idade_anos']} ano(s)")
    campo("Natureza jurídica", ident.get("natureza"))
    campo("Porte", ident.get("porte"))
    campo("Capital social", ident.get("capital_social"), '"R$" #,##0.00')
    campo("Ente federativo responsável", ident.get("ente_federativo"))
    campo("Qualificação do responsável", ident.get("qualificacao_responsavel"))
    campo("Simples Nacional", ident.get("simples"))
    campo("MEI", ident.get("mei"))
    campo("CPF do titular (como publicado)", ident.get("cpf_titular"))
    ws.append([])

    secao("Endereço")
    campo("Logradouro", " ".join(filter(None, [e.get("tipo_logradouro"), e.get("logradouro")])))
    campo("Número", e.get("numero"))
    campo("Complemento", e.get("complemento"))
    campo("Bairro", e.get("bairro"))
    campo("CEP", format_cep(e["cep"]) if e.get("cep") else None)
    campo("Município / UF", " / ".join(filter(None, [e.get("municipio_nome"), e.get("uf")])))
    campo("País", e.get("pais_nome"))
    ws.append([])

    secao("Atividade principal")
    principal = f["cnaes"]["principal"]
    if principal:
        campo("CNAE", _cnae(principal["codigo"]))
        campo("Descrição", principal.get("descricao"))
    campo("Atividades secundárias", f"{len(f['cnaes']['secundarios'])} (aba Atividades)")
    ws.append([])

    secao("Números")
    campo("Sócios", f"{len(f['socios'])} (aba Sócios)")
    campo("Estabelecimentos da empresa", f"{f['estabelecimentos']['total']} (aba Estabelecimentos)")
    ws.append([])
    ws.append(["Dados públicos da Receita Federal (dados abertos do CNPJ). Não substitui o comprovante oficial. "
               "Contatos são os declarados pela empresa e podem estar desatualizados."])
    n = ws.max_row
    ws.merge_cells(start_row=n, start_column=1, end_row=n, end_column=2)
    ws[f"A{n}"].font = _NOTA
    ws[f"A{n}"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[n].height = 30

    def rotulo(texto: str | None) -> str | None:
        return {"email": "E-mail"}.get((texto or "").lower(), (texto or "").replace("_", " ").capitalize()) or None

    contatos = [[rotulo(c["tipo"]), c["formatado"], rotulo(c["subtipo"]), "Oficial (Receita)",
                 f"ref. {c['data_referencia']}"] for c in f["contatos_oficiais"]]
    contatos += [[rotulo(c["tipo"]), c["valor"], rotulo(c["subtipo"]), f"Complementar: {c['fonte']}",
                  c["data_verificacao"] if c["verificado"] else "não verificado"] for c in complementares]
    _tabela(wb.create_sheet("Contatos"), ["Tipo", "Contato", "Classificação", "Origem", "Verificado em"],
            contatos, [14, 38, 18, 32, 18])

    atividades = [["Principal", _cnae(principal["codigo"]), principal.get("descricao")]] if principal else []
    atividades += [["Secundária", _cnae(c["codigo"]), c.get("descricao")] for c in f["cnaes"]["secundarios"]]
    _tabela(wb.create_sheet("Atividades"), ["Tipo", "CNAE", "Descrição"], atividades, [14, 12, 90])

    socios = []
    for s in f["socios"]:
        rep = s.get("representante") or {}
        socios.append([s["nome"], s["tipo"], s["documento"], s["qualificacao"], s["data_entrada"],
                       s["faixa_etaria"], rep.get("nome"), rep.get("qualificacao")])
    _tabela(wb.create_sheet("Sócios"),
            ["Nome", "Tipo", "Documento (como publicado)", "Qualificação", "Entrada na sociedade", "Faixa etária",
             "Representante legal", "Qualificação do representante"], socios, [40, 18, 22, 30, 14, 18, 34, 28])

    estabs = [[format_cnpj(x["cnpj_completo"]), x.get("matriz_filial"), x.get("nome_fantasia"), x.get("situacao"),
               x.get("municipio_nome"), x.get("uf"), x.get("data_inicio_atividade")]
              for x in f["estabelecimentos"]["itens"]]
    _tabela(wb.create_sheet("Estabelecimentos"),
            ["CNPJ", "Tipo", "Nome fantasia", "Situação", "Município", "UF", "Abertura"], estabs,
            [22, 10, 34, 12, 26, 6, 13])

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
