"""Chat com IA: exportacao da conversa (ou de uma resposta) em PDF e Excel."""

from __future__ import annotations

import io
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

RESPOSTA = """## Empresas do sócio

Encontrei **2 matrizes**. Veja [Receita](https://www.gov.br/receita) e `exportar_busca`.

### Matrizes

| CNPJ | Razão social | Abertura | Capital social | Participação | CEP |
|---|---|---|---|---|---|
| 12.345.678/0001-95 | CONSTRUTORA EXEMPLO LTDA | 12/03/2015 | R$ 1.250.000,00 | 50% | 77001-000 |
| 23.456.789/0001-10 | =HYPERLINK("x") | 01/07/2019 | R$ 350.000,00 | 33,3% | 01001-000 |

- item um
- item dois ✅

```sql
SELECT 1
```
"""

CONVERSA = {
    "titulo": "Sócio João / matrizes & filiais", "provedor": "anthropic", "modelo": "claude-sonnet-5-5",
    "exibicao": [
        {"tipo": "usuario", "texto": "Em que empresas aparece o sócio João?"},
        {"tipo": "ferramenta", "nome": "buscar_socio", "ok": True},
        {"tipo": "ferramenta", "nome": "buscar_socio", "ok": True},
        {"tipo": "assistente", "texto": RESPOSTA},
        {"tipo": "usuario", "texto": "E as filiais?"},
        {"tipo": "assistente", "texto": "| Situação | Filiais |\n|---|---:|\n| ATIVA | 29 |\n| BAIXADA | 1 |"},
        {"tipo": "aviso", "texto": "Parei após 8 rodadas."},
    ],
}


class TestEstrutura:
    def test_turnos(self):
        from app.chat.exportar import turnos

        t = turnos(CONVERSA["exibicao"])
        assert [x.numero for x in t] == [1, 2]
        assert t[0].ferramentas == ["buscar_socio"]
        assert [tipo for tipo, _ in t[1].itens] == ["assistente", "aviso"]

    def test_markdown_em_blocos(self):
        from app.chat.exportar import blocos

        tipos = [b.tipo for b in blocos(RESPOSTA)]
        assert tipos == ["titulo", "paragrafo", "titulo", "tabela", "lista", "codigo"]
        tab = next(b for b in blocos(RESPOSTA) if b.tipo == "tabela")
        assert tab.cabecalho[0] == "CNPJ" and len(tab.linhas) == 2

    def test_texto_puro(self):
        from app.chat.exportar import texto_puro

        assert texto_puro("**a** e `b` e [c](https://x.y) e *d*") == "a e b e c e d"

    @pytest.mark.parametrize(("texto", "cab", "valor", "fmt"), [
        ("1.234", "Quantidade", 1234, "#,##0"),
        ("12,5", "Média", 12.5, "#,##0.0"),
        ("R$ 1.250.000,00", "Capital", 1250000.0, '"R$" #,##0.00;-"R$" #,##0.00'),
        ("33,3%", "Part.", pytest.approx(0.333), "0.0%"),
        ("12/03/2015", "Abertura", datetime(2015, 3, 12), "DD/MM/YYYY"),
        ("12.345.678", "CNPJ base", "12.345.678", None),
        ("01234", "Quantidade", "01234", None),
        ("63999998888", "Total", "63999998888", None),
        ("77001-000", "CEP", "77001-000", None),
        ("ATIVA", "Situação", "ATIVA", None),
    ])
    def test_converter_valor(self, texto, cab, valor, fmt):
        from app.chat.exportar import converter_valor

        assert converter_valor(texto, cab) == (valor, fmt)

    def test_nome_arquivo(self):
        from app.chat.exportar import nome_arquivo

        quando = datetime(2026, 11, 14, 9, 5)
        assert nome_arquivo(CONVERSA, "pdf", None, quando) == \
            "consulta-ia_socio-joao-matrizes-filiais_2026-11-14_0905.pdf"
        assert nome_arquivo(CONVERSA, "xlsx", 2, quando).endswith("_resposta-2_2026-11-14_0905.xlsx")

    def test_conversa_sem_resposta(self):
        from app.chat import exportar

        with pytest.raises(ValueError):
            exportar.gerar_pdf({"titulo": "x", "provedor": "openai", "modelo": "m", "exibicao": []})
        with pytest.raises(LookupError):
            exportar.gerar_xlsx(CONVERSA, 9)


class TestArquivos:
    def test_pdf(self):
        from app.chat import exportar

        pdf = exportar.gerar_pdf(CONVERSA)
        assert pdf.startswith(b"%PDF") and len(pdf) > 3000
        so_um = exportar.gerar_pdf(CONVERSA, 2)
        assert so_um.startswith(b"%PDF") and len(so_um) < len(pdf)

    def test_pdf_tabela_larga_e_texto_estranho(self):
        from app.chat import exportar

        cab = "| " + " | ".join(f"Coluna {i}" for i in range(12)) + " |"
        sep = "|" + "---|" * 12
        lin = "| " + " | ".join("x" * 30 for _ in range(12)) + " |"
        conv = {**CONVERSA, "exibicao": [
            {"tipo": "usuario", "texto": "<script>&"},
            {"tipo": "assistente", "texto": f"{cab}\n{sep}\n{lin}\n\nTexto com <tags> & emoji 🚀 e ```"},
        ]}
        assert exportar.gerar_pdf(conv).startswith(b"%PDF")

    def test_excel(self):
        from app.chat import exportar

        wb = load_workbook(io.BytesIO(exportar.gerar_xlsx(CONVERSA)))
        assert wb.sheetnames == ["Conversa", "Matrizes", "Tabela 2"]
        conv = wb["Conversa"]
        textos = [c.value for c in conv["B"] if c.value]
        assert "Em que empresas aparece o sócio João?" in textos
        link = next(c for c in conv["B"] if c.hyperlink)
        assert link.hyperlink.location == "'Matrizes'!A1"

        m = wb["Matrizes"]
        assert m.freeze_panes == "A5" and m.auto_filter.ref == "A4:F6"
        assert [c.value for c in m[4]] == ["CNPJ", "Razão social", "Abertura", "Capital social", "Participação", "CEP"]
        assert m["A5"].value == "12.345.678/0001-95"
        assert m["C5"].value == datetime(2015, 3, 12)
        assert m["D5"].value == 1250000.0 and "R$" in m["D5"].number_format
        assert m["E5"].value == 0.5
        assert m["F6"].value == "01001-000"
        assert m["B6"].data_type == "s"  # texto, nao formula
        assert wb["Tabela 2"]["B5"].value == 29

    def test_excel_so_uma_resposta(self):
        from app.chat import exportar

        wb = load_workbook(io.BytesIO(exportar.gerar_xlsx(CONVERSA, 2)))
        assert wb.sheetnames == ["Conversa", "Tabela 1"]
        assert "RESPOSTA 2" in wb["Conversa"]["A1"].value


@pytest.fixture
def cliente(db_initialized) -> TestClient:
    from app.main import app

    return TestClient(app)


class TestRota:
    def _conversa(self) -> int:
        from app.chat import conversas

        c = conversas.criar("anthropic", "claude-sonnet-5-5")
        conversas.salvar(c["id"], historico=[], exibicao=CONVERSA["exibicao"], titulo="Sócio João")
        return c["id"]

    def test_exige_admin(self, cliente):
        cid = self._conversa()
        assert cliente.get(f"/chat/api/conversas/{cid}/exportar?formato=pdf").status_code == 401

    def test_pdf_e_excel(self, cliente, admin_headers):
        from app.receita import jobs

        cid = self._conversa()
        r = cliente.get(f"/chat/api/conversas/{cid}/exportar?formato=pdf", headers=admin_headers)
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert r.content.startswith(b"%PDF")
        assert 'filename="consulta-ia_socio-joao_' in r.headers["content-disposition"]
        assert r.headers["cache-control"] == "no-store"

        r = cliente.get(f"/chat/api/conversas/{cid}/exportar?formato=xlsx&turno=1", headers=admin_headers)
        assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
        assert "_resposta-1_" in r.headers["content-disposition"]

        reg = [a for a in jobs.auditoria() if a["acao"] == "chat.exportacao"]
        assert len(reg) == 2 and "origem=chat" in reg[0]["detalhe"]

    def test_erros(self, cliente, admin_headers):
        from app.chat import conversas

        cid = self._conversa()
        assert cliente.get(f"/chat/api/conversas/{cid}/exportar?formato=doc", headers=admin_headers).status_code == 422
        assert cliente.get(f"/chat/api/conversas/{cid}/exportar?turno=7", headers=admin_headers).status_code == 404
        assert cliente.get("/chat/api/conversas/99999/exportar", headers=admin_headers).status_code == 404
        vazia = conversas.criar("anthropic", "claude-sonnet-5-5")["id"]
        r = cliente.get(f"/chat/api/conversas/{vazia}/exportar", headers=admin_headers)
        assert r.status_code == 422 and "ainda não tem respostas" in r.json()["detail"]
