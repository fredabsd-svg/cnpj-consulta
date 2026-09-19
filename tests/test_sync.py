"""Teste de integracao do sincronizador da base da Receita (sem rede).

Gera CSVs no leiaute oficial (ISO-8859-1, ';', aspas), importa com o
sincronizador e confere provedor local + busca por socio sobre o resultado.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from app.config import get_settings
from app.providers.receita_local import _fetch_company_sync
from app.services.reconciliation import reconcile
from app.sync.receita_federal import (
    ESTABELECIMENTOS_COLS,
    ReceitaFederalSync,
    SyncError,
)

ESTAB_MATRIZ = [
    "12345678", "0001", "95", "1", "FANTASIA TESTE", "02", "20200115", "00", "", "",
    "20200115", "6920601", "6911701,8211300", "RUA", "DAS FLORES", "100", "SALA 1",
    "CENTRO", "01001000", "SP", "7107", "11", "23851939", "", "", "", "",
    "CONTATO@EXEMPLO.COM.BR", "", "",
]
ESTAB_FILIAL = ESTAB_MATRIZ[:1] + ["0002", "76", "2"] + ESTAB_MATRIZ[4:]


def _csv(rows: list[list[str]]) -> bytes:
    lines = [";".join(f'"{v}"' for v in row) for row in rows]
    return ("\n".join(lines) + "\n").encode("latin-1")


def _write_dataset(extract_dir: Path) -> None:
    assert len(ESTAB_MATRIZ) == len(ESTABELECIMENTOS_COLS)
    extract_dir.mkdir(parents=True)
    files = {
        "K3241.K03200Y0.D60809.EMPRECSV": [["12345678", "EMPRESA TESTE LTDA", "2062", "49", "1000,00", "01", ""]],
        # Duas partes de estabelecimentos: ambas precisam ser importadas
        "K3241.K03200Y0.D60809.ESTABELE": [ESTAB_MATRIZ],
        "K3241.K03200Y1.D60809.ESTABELE": [ESTAB_FILIAL],
        "K3241.K03200Y0.D60809.SOCIOCSV": [
            ["12345678", "2", "JOÃO DA SILVA", "***123456**", "49", "20200115", "", "***000000**", "", "00", "5"]
        ],
        "F.K03200$W.SIMPLES.CSV.D60809": [["12345678", "S", "20200115", "00000000", "N", "00000000", "00000000"]],
        "F.K03200$Z.D60809.CNAECSV": [
            ["6920601", "Atividades de contabilidade"],
            ["6911701", "Serviços advocatícios"],
        ],
        "F.K03200$Z.D60809.MOTICSV": [["00", "SEM MOTIVO"]],
        "F.K03200$Z.D60809.MUNICCSV": [["7107", "SAO PAULO"]],
        "F.K03200$Z.D60809.NATJUCSV": [["2062", "Sociedade Empresária Limitada"]],
        "F.K03200$Z.D60809.PAISCSV": [["105", "BRASIL"]],
        "F.K03200$Z.D60809.QUALSCSV": [["49", "Sócio-Administrador"]],
    }
    for name, rows in files.items():
        (extract_dir / name).write_bytes(_csv(rows))


@pytest.fixture
def imported_db(tmp_path: Path) -> Path:
    data_dir = tmp_path / "receita"
    _write_dataset(data_dir / "extracted")
    messages: list[str] = []
    ReceitaFederalSync(mes="2026-08", data_dir=data_dir, only_import=True, progress=messages.append).run()
    db = data_dir / "receita.db"
    assert db.exists()
    assert not (data_dir / "extracted").exists()  # limpeza apos sucesso
    return db


def test_import_and_local_lookup(imported_db: Path):
    data = _fetch_company_sync(str(imported_db), "12345678000195")
    assert data is not None
    assert data["natureza_juridica_descricao"] == "Sociedade Empresária Limitada"
    assert data["municipio_descricao"] == "SAO PAULO"
    assert data["motivo_situacao_descricao"] == "SEM MOTIVO"
    assert data["cnae_fiscal_principal_descricao"] == "Atividades de contabilidade"
    assert data["correio_eletronico"] == "CONTATO@EXEMPLO.COM.BR"  # colunas alinhadas ao leiaute
    assert data["numero_telefone_1"] == "23851939"
    assert data["filiais"] == ["12345678000276"]  # 2a parte do arquivo foi importada
    assert data["opcao_simples"] == "S"
    assert data["socios"][0]["nome_socio"] == "JOÃO DA SILVA"
    assert data["socios"][0]["qualificacao_socio_descricao"] == "Sócio-Administrador"
    assert data["_mes_referencia"] == "2026-08"
    descs = {c["codigo"]: c["descricao"] for c in data["cnaes_secundarios"]}
    assert descs["6911701"] == "Serviços advocatícios"
    assert descs["8211300"] is None  # sem descricao no dominio: nao inventa


def test_reconciled_view(imported_db: Path):
    from app.providers.base import ProviderResult

    raw = _fetch_company_sync(str(imported_db), "12345678000195")
    c = reconcile("12345678000195", [ProviderResult("receita_local", raw, 200)])
    assert c.situacao_cadastral == "ATIVA"
    assert c.porte == "MICRO EMPRESA"
    assert c.capital_social == 1000.0
    assert c.endereco.municipio == "SAO PAULO"
    assert c.email == "contato@exemplo.com.br"
    assert c.opcao_simples is True
    assert c.data_abertura.isoformat() == "2020-01-15"


def test_partner_search_on_imported_db(imported_db: Path, monkeypatch):
    from app.services.partner_search import search_partners

    monkeypatch.setenv("RECEITA_LOCAL_ENABLED", "true")
    monkeypatch.setenv("RECEITA_LOCAL_PATH", str(imported_db))
    get_settings.cache_clear()
    assert len(search_partners("joao silva")) == 1  # palavras em qualquer posicao
    assert search_partners("maria silva") == []
    rows = search_partners("joão da", municipio="sao paulo")
    assert len(rows) == 1  # matriz apenas, mesmo com filial
    assert rows[0]["qualificacao"] == "Sócio-Administrador"
    assert rows[0]["municipio"] == "SAO PAULO"


def test_invalid_month():
    with pytest.raises(ValueError):
        ReceitaFederalSync(mes="2026-13", data_dir=Path("."))


def test_url_uses_month(tmp_path: Path):
    sync = ReceitaFederalSync(mes="2026-08", data_dir=tmp_path, base_url="https://exemplo.gov.br/cnpj/")
    assert sync.url_for("Empresas0.zip") == "https://exemplo.gov.br/cnpj/2026-08/Empresas0.zip"


@respx.mock
def test_download_404_gives_clear_error(tmp_path: Path):
    sync = ReceitaFederalSync(mes="2026-08", data_dir=tmp_path, base_url="https://exemplo.gov.br/cnpj")
    respx.get("https://exemplo.gov.br/cnpj/2026-08/Cnaes.zip").mock(return_value=httpx.Response(404))
    from app.sync.receita_federal import DatasetFile

    with httpx.Client() as client, pytest.raises(SyncError, match="nao encontrado"):
        sync._download_one(client, DatasetFile("Cnaes.zip", 0), tmp_path / "Cnaes.zip", 1)


@respx.mock
def test_download_ok(tmp_path: Path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("F.K03200$Z.D60809.CNAECSV", '"6920601";"Contabilidade"\n')
    sync = ReceitaFederalSync(mes="2026-08", data_dir=tmp_path, base_url="https://exemplo.gov.br/cnpj")
    respx.get("https://exemplo.gov.br/cnpj/2026-08/Cnaes.zip").mock(
        return_value=httpx.Response(200, content=buf.getvalue())
    )
    from app.sync.receita_federal import DatasetFile

    target = tmp_path / "Cnaes.zip"
    with httpx.Client() as client:
        sync._download_one(client, DatasetFile("Cnaes.zip", 0), target, 1)
    assert zipfile.is_zipfile(target)
