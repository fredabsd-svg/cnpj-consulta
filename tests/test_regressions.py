"""Regressoes da revisao de 2026-09: endereco, CSV, datas."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

import httpx
import respx
from fastapi.testclient import TestClient

from app.core.formatting import data_br
from app.main import app
from app.providers.base import ProviderResult
from app.services.reconciliation import reconcile

CNPJ = "19131243000197"


def _mk(provider: str, raw: dict) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        raw=raw,
        http_status=200,
        fetched_at=datetime(2026, 9, 1, tzinfo=UTC),
        is_mirror_of_rfb=provider != "minha_receita",
    )


def _address_conflicts(a: dict, b: dict) -> list[str]:
    base = {"razao_social": "X", "municipio": "SAO PAULO", "uf": "SP", "cep": "01311902"}
    rws = {"nome": "X", "municipio": "SAO PAULO", "uf": "SP", "cep": "01.311-902"}
    c = reconcile(CNPJ, [_mk("minha_receita", base | a), _mk("receitaws", rws | b)])
    return [x for x in c.conflitos if x.startswith("Endereco:")]


class TestAddressNormalization:
    def test_tipo_de_logradouro_ausente_nao_e_conflito(self):
        """Minha Receita manda o tipo em campo separado; ReceitaWS junta ao nome."""
        assert not _address_conflicts(
            {"logradouro": "PAULISTA", "numero": "37"},
            {"logradouro": "AVENIDA PAULISTA", "numero": "37"},
        )

    def test_abreviacao_do_tipo_nao_e_conflito(self):
        assert not _address_conflicts(
            {"descricao_tipo_de_logradouro": "AVENIDA", "logradouro": "PAULISTA", "numero": "37"},
            {"logradouro": "AV. PAULISTA", "numero": "37"},
        )

    def test_numero_repetido_no_logradouro_nao_e_conflito(self):
        assert not _address_conflicts(
            {"logradouro": "PAULISTA 37", "numero": "37"},
            {"logradouro": "AV PAULISTA", "numero": "37"},
        )

    def test_sem_numero_em_formatos_diferentes(self):
        assert not _address_conflicts(
            {"logradouro": "RUA DAS FLORES", "numero": "S/N"},
            {"logradouro": "R DAS FLORES", "numero": "SN"},
        )

    def test_complemento_ausente_em_uma_fonte_nao_e_conflito(self):
        assert not _address_conflicts(
            {"logradouro": "PAULISTA", "numero": "37", "complemento": "ANDAR 4"},
            {"logradouro": "AV PAULISTA", "numero": "37"},
        )

    def test_abreviacao_no_bairro_e_ordem_do_complemento(self):
        assert not _address_conflicts(
            {"logradouro": "UM", "numero": "1", "bairro": "JARDIM AMERICA", "complemento": "SALA 2"},
            {"logradouro": "RUA UM", "numero": "1", "bairro": "JD AMERICA", "complemento": "2 SL"},
        )

    def test_numero_diferente_continua_conflito(self):
        assert _address_conflicts(
            {"logradouro": "PAULISTA", "numero": "37"},
            {"logradouro": "AVENIDA PAULISTA", "numero": "1000"},
        )

    def test_rua_diferente_continua_conflito(self):
        assert _address_conflicts(
            {"logradouro": "RUA UM", "numero": "10"},
            {"logradouro": "RUA DOIS", "numero": "10"},
        )

    def test_fixtures_reais_sem_nenhum_conflito(self, brasilapi_payload, receitaws_payload):
        c = reconcile(
            CNPJ,
            [
                _mk("minha_receita", brasilapi_payload),
                _mk("brasilapi", brasilapi_payload),
                _mk("receitaws", receitaws_payload),
            ],
        )
        assert c.conflitos == []
        conf = {p.confianca for p in c.campos_procedencia if p.campo == "endereco"}
        assert conf == {"alta"}


class TestCsvFormulaInjection:
    @respx.mock
    def test_celula_com_formula_e_neutralizada(self, db_initialized, brasilapi_payload):
        payload = brasilapi_payload | {"nome_fantasia": "=HYPERLINK(\"http://x\")"}
        respx.get(f"https://minhareceita.org/{CNPJ}").mock(return_value=httpx.Response(200, json=payload))
        respx.get(f"https://brasilapi.com.br/api/cnpj/v1/{CNPJ}").mock(
            return_value=httpx.Response(200, json=payload)
        )
        respx.get(f"https://www.receitaws.com.br/v1/cnpj/{CNPJ}").mock(return_value=httpx.Response(404))
        r = TestClient(app).get(f"/api/companies/{CNPJ}/export.csv")
        assert r.status_code == 200
        rows = dict(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
        assert rows["nome_fantasia"].startswith("'=")


class TestDataBr:
    def test_string_iso_so_data_nao_ganha_hora(self):
        assert data_br("2024-01-31") == "31/01/2024"

    def test_string_iso_com_hora_mantem_hora(self):
        assert data_br("2024-01-31T10:20:00") == "31/01/2024 10:20"

    def test_formato_receita(self):
        assert data_br("20240131") == "31/01/2024"
