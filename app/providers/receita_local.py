"""Adaptador para a base local da Receita Federal (DuckDB).

Permite consulta por CNPJ SEM depender de APIs externas e habilita a
busca reversa por socio.

A base e montada pelo sincronizador app.sync.receita_federal. Decisoes:
- Uma conexao somente-leitura POR CONSULTA, fechada em seguida. Manter a
  conexao aberta travaria o arquivo no Windows e impediria o sync de
  substituir a base com o servidor rodando.
- DuckDB e sincrono: as consultas rodam em thread (asyncio.to_thread) para
  nao bloquear o event loop do servidor.
- As tabelas de dominio (naturezas, municipios, ...) sao unidas por codigo
  numerico quando existem; bases antigas sem elas continuam funcionando.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from app.providers.base import Provider, ProviderError, ProviderResult

# Codigos oficiais do leiaute de dados abertos do CNPJ.
SITUACOES = {1: "NULA", 2: "ATIVA", 3: "SUSPENSA", 4: "INAPTA", 8: "BAIXADA"}
PORTES = {0: "NAO INFORMADO", 1: "MICRO EMPRESA", 3: "EMPRESA DE PEQUENO PORTE", 5: "DEMAIS"}
TIPOS_SOCIO = {1: "PESSOA_JURIDICA", 2: "PESSOA_FISICA", 3: "ESTRANGEIRO"}


def code_to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@contextmanager
def connect_readonly(path: str | Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        yield conn
    finally:
        conn.close()


def existing_tables(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute("SELECT table_name FROM information_schema.tables").fetchall()
    return {r[0] for r in rows}


def lookup_join(tables: set[str], table: str, alias: str, code_expr: str) -> tuple[str, str]:
    """LEFT JOIN numa tabela de dominio (codigo, descricao), se ela existir."""
    if table not in tables:
        return "", "NULL"
    join = (
        f"LEFT JOIN {table} {alias} "
        f"ON TRY_CAST({alias}.codigo AS INTEGER) = TRY_CAST({code_expr} AS INTEGER)"
    )
    return join, f"{alias}.descricao"


def rows_as_dicts(conn: duckdb.DuckDBPyConnection, rows: list[tuple]) -> list[dict[str, Any]]:
    cols = [d[0] for d in conn.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]


def _fetch_company_sync(db_path: str, cnpj: str) -> dict[str, Any] | None:
    with connect_readonly(db_path) as conn:
        tables = existing_tables(conn)
        j_nat, d_nat = lookup_join(tables, "naturezas", "nat", "e.natureza_juridica")
        j_mun, d_mun = lookup_join(tables, "municipios", "mun", "est.municipio")
        j_mot, d_mot = lookup_join(tables, "motivos", "mot", "est.motivo_situacao_cadastral")
        j_cnae, d_cnae = lookup_join(tables, "cnaes", "cn", "est.cnae_fiscal_principal")
        if "simples" in tables:
            j_simples = "LEFT JOIN simples sim ON sim.cnpj_basico = est.cnpj_basico"
            simples_cols = "sim.opcao_simples, sim.opcao_mei"
        else:
            j_simples, simples_cols = "", "NULL AS opcao_simples, NULL AS opcao_mei"

        row = conn.execute(
            f"""
            SELECT
                e.cnpj_basico, e.razao_social, e.natureza_juridica,
                {d_nat} AS natureza_juridica_descricao,
                e.capital_social, e.porte_empresa,
                est.cnpj_completo, est.identificador_matriz_filial, est.nome_fantasia,
                est.situacao_cadastral, est.data_situacao_cadastral,
                est.motivo_situacao_cadastral, {d_mot} AS motivo_situacao_descricao,
                est.data_inicio_atividade,
                est.cnae_fiscal_principal, {d_cnae} AS cnae_fiscal_principal_descricao,
                est.cnae_fiscal_secundaria,
                est.tipo_logradouro, est.logradouro, est.numero, est.complemento,
                est.bairro, est.cep, est.uf,
                est.municipio, {d_mun} AS municipio_descricao,
                est.ddd_telefone_1, est.numero_telefone_1,
                est.ddd_telefone_2, est.numero_telefone_2,
                est.correio_eletronico, est.situacao_especial, est.data_situacao_especial,
                {simples_cols}
            FROM estabelecimentos est
            JOIN empresas e ON e.cnpj_basico = est.cnpj_basico
            {j_nat} {j_mun} {j_mot} {j_cnae} {j_simples}
            WHERE est.cnpj_completo = ?
            LIMIT 1
            """,
            [cnpj],
        ).fetchall()
        if not row:
            return None
        data = rows_as_dicts(conn, row)[0]

        # CNAEs secundarios com descricao
        codes = [c.strip() for c in (data.get("cnae_fiscal_secundaria") or "").split(",") if c.strip()]
        descs: dict[int, str] = {}
        if codes and "cnaes" in tables:
            ints = [i for i in (code_to_int(c) for c in codes) if i is not None]
            if ints:
                descs = dict(
                    conn.execute(
                        "SELECT TRY_CAST(codigo AS INTEGER), descricao FROM cnaes "
                        "WHERE list_contains(?, TRY_CAST(codigo AS INTEGER))",
                        [ints],
                    ).fetchall()
                )
        data["cnaes_secundarios"] = [
            {"codigo": c, "descricao": descs.get(code_to_int(c) or -1)} for c in codes
        ]

        # Socios
        j_qual, d_qual = lookup_join(tables, "qualificacoes", "q", "s.qualificacao_socio")
        socios = conn.execute(
            f"""
            SELECT s.identificador_de_socio, s.nome_socio, s.cpf_cnpj_socio,
                   s.qualificacao_socio, {d_qual} AS qualificacao_socio_descricao,
                   s.data_entrada_sociedade, s.faixa_etaria
            FROM socios s {j_qual}
            WHERE s.cnpj_basico = ?
            ORDER BY s.nome_socio
            """,
            [data["cnpj_basico"]],
        ).fetchall()
        data["socios"] = rows_as_dicts(conn, socios)

        # Demais estabelecimentos da mesma empresa (matriz/filiais)
        filiais = conn.execute(
            "SELECT cnpj_completo FROM estabelecimentos "
            "WHERE cnpj_basico = ? AND cnpj_completo <> ? ORDER BY cnpj_ordem LIMIT 1000",
            [data["cnpj_basico"], cnpj],
        ).fetchall()
        data["filiais"] = [r[0] for r in filiais]

        data["_importado_em"] = None
        if "metadata" in tables:
            meta = dict(conn.execute("SELECT chave, valor FROM metadata").fetchall())
            data["_importado_em"] = meta.get("importado_em")
            data["_mes_referencia"] = meta.get("mes_referencia")
        return data


class ReceitaLocalProvider(Provider):
    name = "receita_local"
    is_mirror_of_rfb = False  # FONTE PRIMARIA (a propria RFB)

    @property
    def enabled(self) -> bool:
        if not self.settings.receita_local_enabled:
            return False
        return Path(self.settings.receita_local_path).exists()

    @property
    def base_url(self) -> str:
        # Nao usa HTTP; exposto para interface
        return f"duckdb://{self.settings.receita_local_path}"

    def build_url(self, cnpj: str) -> str:
        return f"local://receita/{cnpj}"

    @property
    def rate_limit_per_minute(self) -> int:
        return 100_000  # sem limite pratico: e um arquivo local

    async def fetch_company(self, cnpj: str) -> ProviderResult:
        async def _do() -> ProviderResult:
            path = self.settings.receita_local_path
            data = await asyncio.to_thread(_fetch_company_sync, path, cnpj)
            if data is None:
                raise ProviderError("CNPJ nao encontrado na base local", status=404, source=self.name)
            updated = None
            if data.get("_importado_em"):
                try:
                    updated = datetime.fromisoformat(data["_importado_em"])
                except ValueError:
                    updated = None
            if updated is None:  # base antiga sem metadados: usa a data do arquivo
                mtime = await asyncio.to_thread(lambda: Path(path).stat().st_mtime)
                updated = datetime.fromtimestamp(mtime, tz=UTC)
            return ProviderResult(
                provider=self.name,
                raw=data,
                http_status=200,
                url=self.build_url(cnpj),
                updated_at=updated,
                is_mirror_of_rfb=self.is_mirror_of_rfb,
            )

        return await self._timed(_do())
