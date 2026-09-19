"""Busca reversa por socio na base local da Receita Federal.

Requer base local habilitada (RECEITA_LOCAL_ENABLED=true) e base ja
sincronizada (sync-receita). NUNCA consulta provedores externos: nao ha
API publica gratuita que busque empresas pelo nome do socio.

Cada empresa aparece UMA vez (usa-se o estabelecimento matriz); UF e
municipio filtram pela sede.

Performance: syncs novos materializam `socios.nome_socio_norm` (sem acento,
maiusculo) e criam indice. Bases antigas sem a coluna usam o fallback
`strip_accents(upper(s.nome_socio))` ate o proximo `sync-receita`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.normalizer import normalize_name_for_search
from app.providers.receita_local import (
    SITUACOES,
    code_to_int,
    connect_readonly,
    existing_tables,
    lookup_join,
    rows_as_dicts,
)

MAX_LIMIT = 200
MIN_QUERY_LEN = 3


def _db_path() -> str:
    settings = get_settings()
    if not settings.receita_local_enabled:
        raise RuntimeError(
            "Busca por socio requer base local. "
            "Habilite RECEITA_LOCAL_ENABLED=true e execute: python -m app.cli sync-receita --mes YYYY-MM"
        )
    if not Path(settings.receita_local_path).exists():
        raise RuntimeError(
            f"Base local nao encontrada em {settings.receita_local_path}. "
            "Execute: python -m app.cli sync-receita --mes YYYY-MM"
        )
    return settings.receita_local_path


def _post_process(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in rows:
        code = code_to_int(r.get("situacao"))
        if code is not None:
            r["situacao"] = SITUACOES.get(code, r["situacao"])
    return rows


def _has_nome_socio_norm(conn) -> bool:
    rows = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'socios' AND column_name = 'nome_socio_norm' LIMIT 1"
    ).fetchall()
    return bool(rows)


def search_partners(
    nome: str,
    *,
    uf: str | None = None,
    municipio: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Busca socios por nome (parcial, sem acento) com filtros opcionais."""
    db_path = _db_path()
    nome_norm = normalize_name_for_search(nome)
    if not nome_norm or len(nome_norm) < MIN_QUERY_LEN:
        return []
    limit = max(1, min(int(limit), MAX_LIMIT))
    uf_clean = uf.strip().upper() if uf and uf.strip() else None

    with connect_readonly(db_path) as conn:
        tables = existing_tables(conn)
        j_mun, d_mun = lookup_join(tables, "municipios", "mun", "e.municipio")
        j_qual, d_qual = lookup_join(tables, "qualificacoes", "q", "s.qualificacao_socio")
        municipio_expr = f"COALESCE({d_mun}, e.municipio)"
        use_norm = _has_nome_socio_norm(conn)
        # Preferir coluna materializada; fallback para bases importadas antes desta release.
        nome_expr = "s.nome_socio_norm" if use_norm else "strip_accents(upper(s.nome_socio))"

        sql = [
            "SELECT",
            "  s.nome_socio,",
            "  e.cnpj_completo AS cnpj,",
            "  em.razao_social,",
            "  e.situacao_cadastral AS situacao,",
            f"  COALESCE({d_qual}, s.qualificacao_socio) AS qualificacao,",
            "  s.data_entrada_sociedade AS data_entrada,",
            "  e.uf,",
            f"  {municipio_expr} AS municipio",
            "FROM socios s",
            "JOIN empresas em ON em.cnpj_basico = s.cnpj_basico",
            "JOIN estabelecimentos e ON e.cnpj_basico = s.cnpj_basico",
            "  AND TRY_CAST(e.identificador_matriz_filial AS INTEGER) = 1",
            j_mun,
            j_qual,
            "WHERE 1 = 1",
        ]
        # Cada palavra precisa aparecer no nome, em qualquer posicao
        # ("joao silva" encontra "JOAO DA SILVA").
        params: list[Any] = []
        for word in nome_norm.split():
            sql.append(f"AND {nome_expr} LIKE ?")
            params.append(f"%{word}%")
        if uf_clean:
            sql.append("AND e.uf = ?")
            params.append(uf_clean)
        if municipio and (mun_norm := normalize_name_for_search(municipio)):
            sql.append(f"AND strip_accents(upper({municipio_expr})) LIKE ?")
            params.append(f"%{mun_norm}%")
        sql.append("ORDER BY s.nome_socio, em.razao_social LIMIT ?")
        params.append(limit)

        rows = conn.execute("\n".join(sql), params).fetchall()
        return _post_process(rows_as_dicts(conn, rows))


def companies_of_partner_document(documento: str, limit: int = 200) -> list[dict[str, Any]]:
    """Empresas em que um CNPJ (pessoa juridica) figura como socio.

    O CPF de pessoa fisica vem mascarado na base publica, entao so faz
    sentido buscar por CNPJ completo de socio PJ.
    """
    if not documento or not str(documento).strip():
        return []
    db_path = _db_path()
    limit = max(1, min(int(limit), MAX_LIMIT))
    with connect_readonly(db_path) as conn:
        tables = existing_tables(conn)
        j_qual, d_qual = lookup_join(tables, "qualificacoes", "q", "s.qualificacao_socio")
        rows = conn.execute(
            f"""
            SELECT e.cnpj_completo AS cnpj, em.razao_social,
                   e.situacao_cadastral AS situacao,
                   COALESCE({d_qual}, s.qualificacao_socio) AS qualificacao,
                   s.data_entrada_sociedade AS data_entrada, e.uf
            FROM socios s
            JOIN empresas em ON em.cnpj_basico = s.cnpj_basico
            JOIN estabelecimentos e ON e.cnpj_basico = s.cnpj_basico
              AND TRY_CAST(e.identificador_matriz_filial AS INTEGER) = 1
            {j_qual}
            WHERE s.cpf_cnpj_socio = ?
            ORDER BY em.razao_social
            LIMIT ?
            """,
            [str(documento).strip(), limit],
        ).fetchall()
        return _post_process(rows_as_dicts(conn, rows))
