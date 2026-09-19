"""Testes do pacote contador: checklist de diligencia e relatorio personalizado."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.company import CompanyUnified, PartnerPublic, SourceEntry
from app.services.diligence import build_diligence_checklist, diligence_summary

CNPJ = "19131243000197"


@pytest.fixture
def client(db_initialized):
    return TestClient(app)


def _mock_sources(brasilapi_payload, receitaws_payload):
    respx.get(f"https://minhareceita.org/{CNPJ}").mock(
        return_value=httpx.Response(200, json=brasilapi_payload)
    )
    respx.get(f"https://brasilapi.com.br/api/cnpj/v1/{CNPJ}").mock(
        return_value=httpx.Response(200, json=brasilapi_payload)
    )
    respx.get(f"https://www.receitaws.com.br/v1/cnpj/{CNPJ}").mock(
        return_value=httpx.Response(200, json=receitaws_payload)
    )


def _base_company(**overrides) -> CompanyUnified:
    now = datetime.now(timezone.utc)
    data = dict(
        cnpj=CNPJ,
        cnpj_formatado="19.131.243/0001-97",
        razao_social="OPEN KNOWLEDGE BRASIL",
        situacao_cadastral="ATIVA",
        capital_social=0.0,
        opcao_simples=False,
        opcao_mei=False,
        socios=[
            PartnerPublic(
                nome="MARIA EXEMPLO",
                qualificacao="Presidente",
                documento_mascarado="***123456**",
                fonte="brasilapi",
            )
        ],
        fontes=[
            SourceEntry(fonte="brasilapi", status=200, data_consulta=now),
            SourceEntry(fonte="receitaws", status=200, data_consulta=now),
        ],
        conflitos=[],
    )
    data.update(overrides)
    return CompanyUnified(**data)


class TestDiligenceHelpers:
    def test_all_ok_when_ativa_with_sources(self):
        items = build_diligence_checklist(_base_company())
        by_id = {i.id: i for i in items}
        assert by_id["situacao_ativa"].status == "ok"
        assert by_id["simples_mei"].status == "ok"
        assert by_id["capital"].status == "ok"
        assert by_id["qsa"].status == "ok"
        assert by_id["divergencias"].status == "ok"
        assert by_id["fontes_ok"].status == "ok"
        summary = diligence_summary(items)
        assert summary["falha"] == 0

    def test_situacao_nao_ativa_falha(self):
        items = build_diligence_checklist(_base_company(situacao_cadastral="BAIXADA"))
        assert next(i for i in items if i.id == "situacao_ativa").status == "falha"

    def test_divergencias_alerta(self):
        items = build_diligence_checklist(
            _base_company(conflitos=["razao_social: fontes divergem"])
        )
        assert next(i for i in items if i.id == "divergencias").status == "alerta"

    def test_qsa_vazio_info(self):
        items = build_diligence_checklist(_base_company(socios=[]))
        assert next(i for i in items if i.id == "qsa").status == "info"

    def test_capital_ausente_alerta(self):
        items = build_diligence_checklist(_base_company(capital_social=None))
        assert next(i for i in items if i.id == "capital").status == "alerta"

    def test_fontes_parciais_alerta(self):
        now = datetime.now(timezone.utc)
        company = _base_company(
            fontes=[
                SourceEntry(fonte="brasilapi", status=200, data_consulta=now),
                SourceEntry(fonte="receitaws", status=503, data_consulta=now, erro="down"),
            ]
        )
        item = next(i for i in build_diligence_checklist(company) if i.id == "fontes_ok")
        assert item.status == "alerta"
        assert "1 de 2" in item.detalhe


class TestRelatorioRoutes:
    @respx.mock
    def test_relatorio_html_200_contains_key_fields(self, client, brasilapi_payload, receitaws_payload):
        _mock_sources(brasilapi_payload, receitaws_payload)
        r = client.get(f"/empresa/{CNPJ}/relatorio")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        body = r.text
        assert "Relatorio personalizado" in body
        assert "OPEN KNOWLEDGE BRASIL" in body
        assert "19.131.243/0001-97" in body
        assert "Checklist de diligencia" in body
        assert "Destaque de divergencias" in body
        assert "Quadro societario" in body
        assert "Fontes consultadas" in body
        assert "Disclaimer" in body
        assert "***123456**" in body  # CPF mascarado
        assert "CNPJ Consulta" in body  # marca / logo alt
        # Sem layout interativo (sidebar)
        assert 'class="sidebar"' not in body

    @respx.mock
    def test_export_relatorio_api(self, client, brasilapi_payload, receitaws_payload):
        _mock_sources(brasilapi_payload, receitaws_payload)
        r = client.get(f"/api/companies/{CNPJ}/export.relatorio")
        assert r.status_code == 200
        assert "OPEN KNOWLEDGE BRASIL" in r.text
        assert "Checklist de diligencia" in r.text

    @respx.mock
    def test_company_page_has_report_button_and_checklist(
        self, client, brasilapi_payload, receitaws_payload
    ):
        _mock_sources(brasilapi_payload, receitaws_payload)
        r = client.get(f"/empresa/{CNPJ}")
        assert r.status_code == 200
        assert "Relatorio personalizado" in r.text
        assert f"/empresa/{CNPJ}/relatorio" in r.text
        assert "Checklist de diligencia" in r.text

    def test_relatorio_invalid_cnpj(self, client):
        r = client.get("/empresa/123/relatorio")
        assert r.status_code == 400
