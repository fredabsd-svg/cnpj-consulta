"""Sincronizador da base oficial de CNPJ da Receita Federal.

Baixa os ZIPs publicos do mes pedido, extrai, importa TODAS as partes para
DuckDB (tabelas principais + dominios + Simples), valida e so entao troca
a base em uso.

Uso:
    python -m app.cli sync-receita --mes 2026-08

Layout oficial (dados abertos do CNPJ): {RECEITA_BASE_URL}/{YYYY-MM}/<Arquivo>.zip
Arquivos extraidos terminam em EMPRECSV, ESTABELE, SOCIOCSV, SIMPLES.CSV...,
CNAECSV, MOTICSV, MUNICCSV, NATJUCSV, PAISCSV, QUALSCSV (ISO-8859-1, ';').
"""

from __future__ import annotations

import logging
import re
import shutil
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

# Espaco livre recomendado: ~7 GB de ZIPs + ~25 GB de CSVs + base final.
MIN_FREE_GB = 40


@dataclass(frozen=True)
class DatasetFile:
    name: str  # nome do ZIP no servidor
    min_size_mb: float  # heuristica para detectar download truncado


MONTHLY_FILES: list[DatasetFile] = (
    [DatasetFile(f"Empresas{i}.zip", 5) for i in range(10)]
    + [DatasetFile(f"Estabelecimentos{i}.zip", 20) for i in range(10)]
    + [DatasetFile(f"Socios{i}.zip", 3) for i in range(10)]
    + [
        DatasetFile("Simples.zip", 20),
        DatasetFile("Cnaes.zip", 0.005),
        DatasetFile("Motivos.zip", 0.0005),
        DatasetFile("Municipios.zip", 0.01),
        DatasetFile("Naturezas.zip", 0.001),
        DatasetFile("Paises.zip", 0.001),
        DatasetFile("Qualificacoes.zip", 0.0005),
    ]
)

# Schemas oficiais (leiaute de dados abertos do CNPJ)
EMPRESAS_COLS = [
    "cnpj_basico", "razao_social", "natureza_juridica", "qualificacao_responsavel",
    "capital_social", "porte_empresa", "ente_federativo_responsavel",
]
ESTABELECIMENTOS_COLS = [
    "cnpj_basico", "cnpj_ordem", "cnpj_dv", "identificador_matriz_filial",
    "nome_fantasia", "situacao_cadastral", "data_situacao_cadastral",
    "motivo_situacao_cadastral", "nome_cidade_exterior", "pais",
    "data_inicio_atividade", "cnae_fiscal_principal", "cnae_fiscal_secundaria",
    "tipo_logradouro", "logradouro", "numero", "complemento", "bairro", "cep",
    "uf", "municipio", "ddd_telefone_1", "numero_telefone_1", "ddd_telefone_2",
    "numero_telefone_2", "ddd_fax", "numero_fax", "correio_eletronico",
    "situacao_especial", "data_situacao_especial",
]
SOCIOS_COLS = [
    "cnpj_basico", "identificador_de_socio", "nome_socio", "cpf_cnpj_socio",
    "qualificacao_socio", "data_entrada_sociedade", "pais",
    "representante_legal", "nome_representante", "qualificacao_representante_legal",
    "faixa_etaria",
]
SIMPLES_COLS = [
    "cnpj_basico", "opcao_simples", "data_opcao_simples", "data_exclusao_simples",
    "opcao_mei", "data_opcao_mei", "data_exclusao_mei",
]
DOMAIN_COLS = ["codigo", "descricao"]

# (tabela, sufixo do arquivo extraido, colunas)
MAIN_TABLES = [
    ("empresas", "EMPRECSV", EMPRESAS_COLS),
    ("estabelecimentos", "ESTABELE", ESTABELECIMENTOS_COLS),
    ("socios", "SOCIOCSV", SOCIOS_COLS),
    ("simples", "SIMPLES", SIMPLES_COLS),
]
DOMAIN_TABLES = [
    ("cnaes", "CNAECSV"),
    ("motivos", "MOTICSV"),
    ("municipios", "MUNICCSV"),
    ("naturezas", "NATJUCSV"),
    ("paises", "PAISCSV"),
    ("qualificacoes", "QUALSCSV"),
]


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _read_csv_sql(files: list[Path], cols: list[str]) -> str:
    """read_csv sobre TODAS as partes, tudo VARCHAR, tolerante a linhas ruins."""
    file_list = "[" + ", ".join(_sql_str(f.as_posix()) for f in files) + "]"
    columns = "{" + ", ".join(f"{_sql_str(c)}: 'VARCHAR'" for c in cols) + "}"
    return (
        f"read_csv({file_list}, delim=';', quote='\"', escape='\"', header=false, "
        f"encoding='latin-1', columns={columns}, ignore_errors=true, null_padding=true)"
    )


class SyncError(RuntimeError):
    """Falha na sincronizacao (mensagem pronta para o usuario)."""


class ReceitaFederalSync:
    """Gerencia download, extracao e importacao da base RFB."""

    def __init__(
        self,
        mes: str,
        data_dir: Path,
        keep_zip: bool = False,
        only_download: bool = False,
        only_import: bool = False,
        base_url: str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mes):
            raise ValueError("Mes deve estar no formato YYYY-MM (ex: 2026-08)")
        self.mes = mes
        self.data_dir = data_dir
        self.keep_zip = keep_zip
        self.only_download = only_download
        self.only_import = only_import
        self.base_url = (base_url or get_settings().receita_base_url).rstrip("/")
        self.zip_dir = data_dir / "zips" / mes
        self.extract_dir = data_dir / "extracted"
        self.db_path = data_dir / "receita.db"
        self.staging_db = data_dir / "receita.staging.db"
        self._progress = progress or (lambda msg: log.info("%s", msg))

    def url_for(self, name: str) -> str:
        return f"{self.base_url}/{self.mes}/{name}"

    def run(self) -> None:
        t0 = time.monotonic()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.zip_dir.mkdir(parents=True, exist_ok=True)
        self.extract_dir.mkdir(parents=True, exist_ok=True)
        self._check_free_space()

        if not self.only_import:
            self._download_all()
        if self.only_download:
            self._progress(f"Download concluido em {time.monotonic() - t0:.0f}s")
            return

        self._extract_all()
        self._import_all()
        self._cleanup()
        self._progress(f"Sincronizacao concluida em {(time.monotonic() - t0) / 60:.1f} min")

    # ---------- Pre-checagens ----------

    def _check_free_space(self) -> None:
        free_gb = shutil.disk_usage(self.data_dir).free / 1024**3
        if free_gb < MIN_FREE_GB:
            self._progress(
                f"AVISO: apenas {free_gb:.0f} GB livres em {self.data_dir}; "
                f"recomendado >= {MIN_FREE_GB} GB para o processo completo."
            )

    # ---------- Download ----------

    def _download_all(self) -> None:
        self._progress(f"Baixando {len(MONTHLY_FILES)} arquivos de {self.base_url}/{self.mes}/")
        with httpx.Client(timeout=httpx.Timeout(60, read=300), follow_redirects=True) as client:
            for i, f in enumerate(MONTHLY_FILES, 1):
                target = self.zip_dir / f.name
                if target.exists() and self._zip_ok(target, f):
                    self._progress(f"[{i:2}/{len(MONTHLY_FILES)}] {f.name} ja baixado")
                    continue
                self._download_one(client, f, target, i)

    @staticmethod
    def _zip_ok(path: Path, info: DatasetFile) -> bool:
        if path.stat().st_size < info.min_size_mb * 1024 * 1024:
            return False
        return zipfile.is_zipfile(path)

    def _download_one(self, client: httpx.Client, info: DatasetFile, target: Path, idx: int) -> None:
        """Download com retomada (HTTP Range) e ate 5 tentativas."""
        url = self.url_for(info.name)
        partial = target.with_suffix(target.suffix + ".part")
        last_error: Exception | None = None
        for attempt in range(1, 6):
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code == 404:
                        raise SyncError(
                            f"{url} nao encontrado (404). Verifique se o mes {self.mes} ja foi "
                            "publicado pela Receita ou ajuste RECEITA_BASE_URL no .env."
                        )
                    resp.raise_for_status()
                    if offset and resp.status_code != 206:
                        offset = 0  # servidor ignorou o Range: recomeca
                    total = int(resp.headers.get("Content-Length") or 0) + offset
                    mode = "ab" if offset else "wb"
                    done, last_report = offset, time.monotonic()
                    with partial.open(mode) as fh:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                            fh.write(chunk)
                            done += len(chunk)
                            if time.monotonic() - last_report > 5:
                                pct = f" {done * 100 / total:.0f}%" if total else ""
                                self._progress(
                                    f"[{idx:2}/{len(MONTHLY_FILES)}] {info.name}{pct} "
                                    f"({done / 1024**2:.0f} MB)"
                                )
                                last_report = time.monotonic()
                partial.replace(target)
                if not zipfile.is_zipfile(target):
                    target.unlink(missing_ok=True)
                    raise SyncError(f"{info.name} baixado mas nao e um ZIP valido")
                self._progress(
                    f"[{idx:2}/{len(MONTHLY_FILES)}] {info.name} ok "
                    f"({target.stat().st_size / 1024**2:.0f} MB)"
                )
                return
            except SyncError:
                raise
            except (httpx.HTTPError, OSError) as e:
                last_error = e
                wait = min(60, 5 * attempt)
                self._progress(f"{info.name}: falha ({e}); tentativa {attempt}/5, aguardando {wait}s")
                time.sleep(wait)
        raise SyncError(f"Nao foi possivel baixar {info.name}: {last_error}")

    # ---------- Extract ----------

    def _extract_all(self) -> None:
        zips = sorted(self.zip_dir.glob("*.zip"))
        if not zips and not any(self.extract_dir.iterdir()):
            raise SyncError(f"Nenhum ZIP em {self.zip_dir} e nenhum CSV em {self.extract_dir}.")
        for zip_path in zips:
            self._progress(f"Extraindo {zip_path.name}")
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(self.extract_dir)

    def _files(self, suffix: str) -> list[Path]:
        return sorted(p for p in self.extract_dir.iterdir() if p.is_file() and suffix in p.name.upper())

    # ---------- Import ----------

    def _import_all(self) -> None:
        import duckdb

        self._progress(f"Importando para {self.staging_db} (pode levar de 20 a 60 min)")
        self.staging_db.unlink(missing_ok=True)
        conn = duckdb.connect(str(self.staging_db))
        try:
            for table, suffix, cols in MAIN_TABLES:
                files = self._files(suffix)
                if not files:
                    if table == "simples":
                        conn.execute(f"CREATE TABLE simples ({', '.join(c + ' VARCHAR' for c in cols)})")
                        self._progress("AVISO: arquivo do Simples nao encontrado; tabela criada vazia")
                        continue
                    raise SyncError(f"Nenhum arquivo *{suffix}* em {self.extract_dir}")
                self._progress(f"Tabela {table}: {len(files)} arquivo(s)")
                select = f"SELECT * FROM {_read_csv_sql(files, cols)}"
                if table == "estabelecimentos":
                    select = (
                        "SELECT *, cnpj_basico || cnpj_ordem || cnpj_dv AS cnpj_completo "
                        f"FROM {_read_csv_sql(files, cols)}"
                    )
                conn.execute(f"CREATE TABLE {table} AS {select}")

            for table, suffix in DOMAIN_TABLES:
                files = self._files(suffix)
                if files:
                    conn.execute(f"CREATE TABLE {table} AS SELECT * FROM {_read_csv_sql(files, DOMAIN_COLS)}")
                else:
                    conn.execute(f"CREATE TABLE {table} (codigo VARCHAR, descricao VARCHAR)")
                    self._progress(f"AVISO: dominio {table} nao encontrado; descricoes ficarao vazias")

            conn.execute("CREATE TABLE metadata (chave VARCHAR, valor VARCHAR)")
            conn.execute(
                "INSERT INTO metadata VALUES ('mes_referencia', ?), ('importado_em', ?)",
                [self.mes, datetime.now(UTC).isoformat()],
            )

            self._progress("Criando indices")
            conn.execute("CREATE INDEX idx_estab_cnpj_completo ON estabelecimentos(cnpj_completo)")
            conn.execute("CREATE INDEX idx_estab_cnpj_basico ON estabelecimentos(cnpj_basico)")
            conn.execute("CREATE INDEX idx_socios_cnpj_basico ON socios(cnpj_basico)")
            conn.execute("CREATE INDEX idx_socios_documento ON socios(cpf_cnpj_socio)")
            conn.execute("CREATE INDEX idx_empresas_cnpj ON empresas(cnpj_basico)")
            conn.execute("CREATE INDEX idx_simples_cnpj ON simples(cnpj_basico)")

            counts = {
                t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("empresas", "estabelecimentos", "socios")
            }
            self._progress("Registros: " + ", ".join(f"{t}={n:,}" for t, n in counts.items()))
            if not all(counts.values()):
                raise SyncError(f"Importacao incompleta, base NAO substituida: {counts}")
            conn.execute("CHECKPOINT")
        finally:
            conn.close()

        self._swap_database()

    def _swap_database(self) -> None:
        """Troca a base em uso so apos validacao, mantendo 1 backup."""
        backup = self.db_path.with_suffix(".db.bak")
        try:
            if self.db_path.exists():
                backup.unlink(missing_ok=True)
                self.db_path.replace(backup)
            self.staging_db.replace(self.db_path)
        except PermissionError as e:
            raise SyncError(
                f"Nao foi possivel substituir {self.db_path}: arquivo em uso. "
                "Pare o servidor e rode novamente com --only-import."
            ) from e
        self._progress(f"Base atualizada em {self.db_path} (backup anterior: {backup.name})")

    # ---------- Cleanup ----------

    def _cleanup(self) -> None:
        if not self.keep_zip:
            self._progress("Removendo ZIPs")
            shutil.rmtree(self.zip_dir, ignore_errors=True)
        self._progress("Removendo CSVs extraidos")
        shutil.rmtree(self.extract_dir, ignore_errors=True)
