"""Exportacao dos resultados da busca (CSV e Excel) sem carregar tudo na RAM.

- Ate EXPORTACAO_SINCRONA_MAX linhas: o arquivo e gerado em fluxo na propria
  resposta (CSV linha a linha; Excel em modo write_only num arquivo temporario).
- Acima disso: vira tarefa em segundo plano (thread do servidor), com arquivo
  em data/receita/exportacoes e link de download quando terminar.
- Qualquer coluna pode ser exportada (telefones, e-mail, socios, CPF do titular):
  o app e de uso do proprio dono. A exportacao fica na auditoria (numero de
  linhas e colunas -- nunca os dados).
- Celulas que comecam com = + - @ sao neutralizadas (injecao de formula).
"""

from __future__ import annotations

import csv
import io
import logging
import tempfile
import threading
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.core.formatting import csv_cell
from app.db import get_session_factory, init_database
from app.models.base_cnpj import Exportacao
from app.receita.armazenamento import exportacoes_dir
from app.services.busca_avancada import (
    COLUNAS,
    COLUNAS_PADRAO,
    Filtros,
    iterar_exportacao,
    sem_dados_de_socio,
)

log = logging.getLogger(__name__)


class ExportacaoInvalida(ValueError):
    pass


def validar_colunas(colunas: list[str] | None, finalidade: str | None = None) -> list[str]:
    """Colunas pedidas que existem (ou as padrao). `finalidade` e opcional e so vai
    para a auditoria."""
    return [c for c in dict.fromkeys(colunas or []) if c in COLUNAS] or list(COLUNAS_PADRAO)


LINHAS_POR_ABA = 1_000_000  # o Excel aceita 1.048.576 linhas por aba


def limite_linhas(total: int | None = None) -> int | None:
    """Teto de linhas da exportacao (None = sem teto, o padrao)."""
    teto = get_settings().exportacao_max_linhas
    if teto <= 0:
        return None
    return min(total, teto) if total is not None else teto


def _valor(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    return v


def _celula_csv(v: Any) -> Any:
    v = _valor(v)
    if isinstance(v, float):
        return f"{v:.2f}".replace(".", ",")
    return csv_cell(v)


def gerar_csv(f: Filtros, colunas: list[str], limite: int | None) -> Iterator[bytes]:
    yield "﻿".encode()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([COLUNAS[c][0] for c in colunas])
    for i, it in enumerate(iterar_exportacao(f, colunas, limite), 1):
        w.writerow([_celula_csv(it.get(c)) for c in colunas])
        if i % 2000 == 0:
            yield buf.getvalue().encode("utf-8")
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue().encode("utf-8")


def _celula_xlsx(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, date):  # date e datetime: o Excel recebe a data de verdade
        return v
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


# Largura (em caracteres) e formato de cada coluna da planilha
_LARGURAS = {"cnpj": 17, "razao_social": 46, "cpf_titular": 16, "nome_fantasia": 30, "matriz_filial": 12,
             "situacao": 12, "data_situacao": 14, "abertura": 12, "natureza": 34, "porte": 18, "capital_social": 18,
             "cnae_principal": 13, "cnae_descricao": 46, "cnaes_secundarios": 32, "logradouro": 44, "bairro": 22,
             "cep": 11, "municipio": 24, "municipio_codigo": 13, "uf": 6, "telefone_1": 15, "tipo_telefone_1": 11,
             "telefone_2": 15, "tipo_telefone_2": 11, "email": 34, "qtd_socios": 10, "simples": 9, "mei": 7,
             "socios": 60}
_FORMATOS = {"capital_social": "moeda", "qtd_socios": "inteiro"}


def _aba_resumo(f: Filtros, colunas: list[str], linhas: int) -> list[list[Any]]:
    """Aba "Resumo": de onde veio a planilha (filtros, data, total, fonte)."""
    from app.receita.armazenamento import base_ativa
    from app.services import busca_avancada as ba

    try:
        filtros = [c["texto"] for c in ba.chips(f, ba.nomes_para_chips(f))]
    except Exception:  # noqa: BLE001 -- o resumo nunca derruba a exportacao
        log.debug("resumo da exportacao sem os filtros", exc_info=True)
        filtros = []
    ativa = base_ativa()
    ordem = ba.ORDENACOES.get(f.ordem, ("",))[0]
    if (f.direcao or "").lower() == "desc":
        ordem += " (decrescente)"
    out: list[list[Any]] = [
        [("Relatório de empresas", "titulo")],
        [("CNPJ Consulta · base local da Receita Federal", "nota")],
        [],
        [("Gerado em", "rotulo"), datetime.now()],
        [("Empresas na planilha", "rotulo"), f"{linhas:,}".replace(",", ".")],
        [("Referência da base", "rotulo"), (ativa.mes if ativa else None) or "—"],
        [("Ordenação", "rotulo"), ordem or "—"],
    ]
    for i, texto in enumerate(filtros or ["Nenhum filtro (base inteira)"]):
        out.append([("Filtros aplicados" if i == 0 else "", "rotulo"), (texto, "quebra")])
    out += [
        [("Colunas", "rotulo"), (", ".join(COLUNAS[c][0] for c in colunas), "quebra")],
        [],
        [("Fonte", "rotulo"), "Receita Federal — dados abertos do CNPJ (base local)"],
        [("Observação", "rotulo"), ("Telefones e e-mails são os declarados pela empresa à Receita e podem estar "
                                    "desatualizados. Simples/MEI é informação cadastral e não comprova "
                                    "regularidade fiscal.", "quebra")],
    ]
    return out


def gravar_xlsx(f: Filtros, colunas: list[str], limite: int | None, destino: Path,
                progresso=None) -> int:
    """Excel em fluxo (xlsx_rapido). Acima de 1 milhao de linhas continua em novas
    abas (Empresas, Empresas 2, ...): o Excel nao abre mais que 1.048.576 linhas
    numa aba. A ultima aba ("Resumo") diz de onde veio a planilha."""
    from app.services.xlsx_rapido import PlanilhaRapida

    pl = PlanilhaRapida(destino, LINHAS_POR_ABA)
    n = 0
    try:
        pl.nova_aba("Empresas", [COLUNAS[c][0] for c in colunas],
                    larguras=[_LARGURAS.get(c, 18) for c in colunas],
                    formatos=[_FORMATOS.get(c) for c in colunas])
        for it in iterar_exportacao(f, colunas, limite):
            pl.linha([_celula_xlsx(it.get(c)) for c in colunas])
            n += 1
            if progresso and n % 5000 == 0:
                progresso(n)
        pl.aba_simples("Resumo", _aba_resumo(f, colunas, n), larguras=[24, 100])
    finally:
        pl.fechar()
    return n


def xlsx_temporario(f: Filtros, colunas: list[str], limite: int | None) -> Path:
    tmp = tempfile.NamedTemporaryFile(prefix="busca-", suffix=".xlsx", delete=False)  # noqa: SIM115
    tmp.close()
    path = Path(tmp.name)
    gravar_xlsx(f, colunas, limite, path)
    return path


def gravar_csv(f: Filtros, colunas: list[str], limite: int | None, destino: Path, progresso=None) -> int:
    n = 0
    with destino.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow([COLUNAS[c][0] for c in colunas])
        for it in iterar_exportacao(f, colunas, limite):
            w.writerow([_celula_csv(it.get(c)) for c in colunas])
            n += 1
            if progresso and n % 5000 == 0:
                progresso(n)
    return n


# ---------- segundo plano ----------


def criar_exportacao(f: Filtros, colunas: list[str], formato: str, finalidade: str | None, total: int) -> dict:
    if formato not in ("csv", "xlsx"):
        raise ExportacaoInvalida("Formato inválido.")
    init_database()
    with get_session_factory()() as s:
        e = Exportacao(formato=formato, filtros=f.para_params(), colunas=colunas,
                       finalidade=(finalidade or "")[:300] or None, total=total, status="pendente")
        s.add(e)
        s.commit()
        exp_id = e.id
    threading.Thread(target=_executar, args=(exp_id,), daemon=True, name=f"exportacao-{exp_id}").start()
    return obter(exp_id)


# Uma exportacao grande por vez: cada uma le a base inteira em fluxo; duas ou
# mais juntas, mais as buscas da tela, esgotavam memoria e disco.
_uma_por_vez = threading.Lock()


def _executar(exp_id: int) -> None:
    with _uma_por_vez:
        _executar_agora(exp_id)


def _executar_agora(exp_id: int) -> None:
    factory = get_session_factory()
    with factory() as s:
        e = s.get(Exportacao, exp_id)
        if e is None:
            return
        e.status = "executando"
        s.commit()
        formato, filtros, colunas = e.formato, dict(e.filtros), list(e.colunas)
    pasta = exportacoes_dir()
    pasta.mkdir(parents=True, exist_ok=True)
    nome = f"exportacao-{exp_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}.{formato}"
    destino = pasta / nome
    limite = limite_linhas()

    def progresso(n: int) -> None:
        with factory() as s2:
            x = s2.get(Exportacao, exp_id)
            if x:
                x.feitas = n
                s2.commit()

    try:
        f = Filtros.de_params(filtros)
        gravar = gravar_xlsx if formato == "xlsx" else gravar_csv
        n = gravar(f, colunas, limite, destino, progresso)
        status, erro = "concluido", None
    except Exception as ex:  # noqa: BLE001
        log.exception("exportacao %s falhou", exp_id)
        destino.unlink(missing_ok=True)
        n, status, erro = 0, "falhou", f"{type(ex).__name__}: {ex}"[:500]
    with factory() as s:
        e = s.get(Exportacao, exp_id)
        if e:
            e.status, e.erro, e.feitas = status, erro, n
            e.arquivo = nome if status == "concluido" else None
            e.finalizado_em = datetime.now()
            s.commit()


def obter(exp_id: int) -> dict | None:
    with get_session_factory()() as s:
        e = s.get(Exportacao, exp_id)
        if e is None:
            return None
        d = {c.name: getattr(e, c.name) for c in Exportacao.__table__.columns}
        d["filtros"] = sem_dados_de_socio(dict(d.get("filtros") or {}))
        return d


def listar(limite: int = 20) -> list[dict]:
    init_database()
    with get_session_factory()() as s:
        rows = s.scalars(select(Exportacao).order_by(Exportacao.id.desc()).limit(limite))
        out = []
        for e in rows:
            d = {c.name: getattr(e, c.name) for c in Exportacao.__table__.columns}
            d["filtros"] = sem_dados_de_socio(dict(d.get("filtros") or {}))  # listagem sem dado pessoal
            out.append(d)
        return out


def caminho_arquivo(exp: dict) -> Path | None:
    if not exp.get("arquivo"):
        return None
    p = exportacoes_dir() / exp["arquivo"]
    # nome vem do banco, mas confere que nao escapa da pasta
    return p if p.is_file() and p.parent.resolve() == exportacoes_dir().resolve() else None
