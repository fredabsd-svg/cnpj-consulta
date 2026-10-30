"""Onde a base local fica no disco e qual versao esta em uso.

    {RECEITA_DATA_DIR}/
      ATIVA.json                      ponteiro da versao publicada (troca atomica)
      versoes/receita-2026-10-....duckdb
      versoes/receita-2026-09-....duckdb   anterior (restauravel)
      trabalho/2026-10/               area de trabalho de uma atualizacao
        estado.json                   estado de cada arquivo (retomada)
        zips/  extraido/  bruto.duckdb  final.duckdb  tmp/
      exportacoes/                    exportacoes em segundo plano

As consultas abrem a versao apontada por ATIVA.json (somente leitura, uma
conexao por consulta). Uma atualizacao so escreve na area de trabalho e, depois
de validada, grava o arquivo novo em versoes/ e troca o ponteiro com
os.replace (atomico). Quem estava consultando continua lendo o arquivo antigo
ate fechar a conexao; a versao anterior so e apagada quando sobra mais de
RECEITA_VERSOES_MANTIDAS.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

PONTEIRO = "ATIVA.json"


def data_dir(settings: Settings | None = None) -> Path:
    return Path((settings or get_settings()).receita_data_dir).expanduser()


def versoes_dir(settings: Settings | None = None) -> Path:
    return data_dir(settings) / "versoes"


def trabalho_dir(mes: str, settings: Settings | None = None) -> Path:
    return data_dir(settings) / "trabalho" / mes


def exportacoes_dir(settings: Settings | None = None) -> Path:
    return data_dir(settings) / "exportacoes"


def escrever_json_atomico(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def ler_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class BaseAtiva:
    caminho: Path
    mes: str | None
    publicada_em: str | None
    anterior: str | None  # nome do arquivo da versao anterior
    legado: bool = False  # base avulsa em RECEITA_LOCAL_PATH


_cache: tuple[float, str, BaseAtiva | None] | None = None


def base_ativa(settings: Settings | None = None) -> BaseAtiva | None:
    """Versao em uso (None = nenhuma base instalada). Barato: le um JSON pequeno."""
    global _cache
    settings = settings or get_settings()
    ponteiro = data_dir(settings) / PONTEIRO
    try:
        mtime = ponteiro.stat().st_mtime
    except OSError:
        mtime = -1.0
    if _cache and _cache[0] == mtime and _cache[1] == str(ponteiro) and mtime >= 0:
        return _cache[2]
    ativa: BaseAtiva | None = None
    if mtime >= 0 and (dados := ler_json(ponteiro)):
        caminho = versoes_dir(settings) / str(dados.get("arquivo", ""))
        if dados.get("arquivo") and caminho.is_file():
            ativa = BaseAtiva(
                caminho=caminho,
                mes=dados.get("mes"),
                publicada_em=dados.get("publicada_em"),
                anterior=dados.get("anterior"),
            )
        else:
            log.error("ATIVA.json aponta para %s, que nao existe", caminho)
    if ativa is None:
        legado = Path(settings.receita_local_path).expanduser()
        if legado.is_file():
            ativa = BaseAtiva(caminho=legado, mes=None, publicada_em=None, anterior=None, legado=True)
    _cache = (mtime, str(ponteiro), ativa)
    return ativa


def caminho_base_ativa(settings: Settings | None = None) -> str | None:
    ativa = base_ativa(settings)
    return str(ativa.caminho) if ativa else None


def limpar_cache() -> None:
    global _cache
    _cache = None


def nome_versao(mes: str) -> str:
    return f"receita-{mes}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.duckdb"


def publicar(arquivo_final: Path, mes: str, settings: Settings | None = None, *, podar: bool = True) -> Path:
    """Move a base validada para versoes/ e troca o ponteiro (atomico)."""
    settings = settings or get_settings()
    destino_dir = versoes_dir(settings)
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / nome_versao(mes)
    shutil.move(str(arquivo_final), destino)
    atual = ler_json(data_dir(settings) / PONTEIRO) or {}
    escrever_json_atomico(
        data_dir(settings) / PONTEIRO,
        {
            "arquivo": destino.name,
            "mes": mes,
            "publicada_em": datetime.now(UTC).isoformat(),
            "anterior": atual.get("arquivo"),
            "anterior_mes": atual.get("mes"),
        },
    )
    limpar_cache()
    if podar:
        podar_versoes(settings)
    return destino


def restaurar_anterior(settings: Settings | None = None) -> str:
    """Volta o ponteiro para a versao anterior (que precisa existir)."""
    settings = settings or get_settings()
    ponteiro = data_dir(settings) / PONTEIRO
    atual = ler_json(ponteiro)
    if not atual or not atual.get("anterior"):
        raise FileNotFoundError("Nao ha versao anterior registrada para restaurar.")
    anterior = versoes_dir(settings) / atual["anterior"]
    if not anterior.is_file():
        raise FileNotFoundError(f"O arquivo da versao anterior ({anterior.name}) nao existe mais.")
    escrever_json_atomico(
        ponteiro,
        {
            "arquivo": anterior.name,
            "mes": atual.get("anterior_mes") or _mes_do_nome(anterior.name),
            "publicada_em": datetime.now(UTC).isoformat(),
            "anterior": atual["arquivo"],
            "anterior_mes": atual.get("mes"),
            "restaurada": True,
        },
    )
    limpar_cache()
    return anterior.name


def _mes_do_nome(nome: str) -> str | None:
    partes = nome.split("-")
    return f"{partes[1]}-{partes[2]}" if len(partes) >= 3 and nome.startswith("receita-") else None


def listar_versoes(settings: Settings | None = None) -> list[dict]:
    settings = settings or get_settings()
    atual = ler_json(data_dir(settings) / PONTEIRO) or {}
    out = []
    for p in sorted(versoes_dir(settings).glob("receita-*.duckdb"), reverse=True):
        papel = "ativa" if p.name == atual.get("arquivo") else (
            "anterior" if p.name == atual.get("anterior") else "antiga"
        )
        st = p.stat()
        out.append(
            {
                "arquivo": p.name,
                "mes": _mes_do_nome(p.name),
                "tamanho": st.st_size,
                "modificado": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
                "papel": papel,
            }
        )
    return out


def podar_versoes(settings: Settings | None = None) -> list[str]:
    """Apaga versoes alem do limite; nunca a ativa nem a anterior."""
    settings = settings or get_settings()
    manter = max(2, settings.receita_versoes_mantidas)
    removidas = []
    versoes = listar_versoes(settings)
    for v in [v for v in versoes if v["papel"] == "antiga"][max(0, manter - 2):]:
        try:
            (versoes_dir(settings) / v["arquivo"]).unlink()
            (versoes_dir(settings) / (v["arquivo"] + ".wal")).unlink(missing_ok=True)
            removidas.append(v["arquivo"])
        except OSError as e:  # em uso (Windows): tenta de novo na proxima publicacao
            log.warning("versao %s nao removida agora: %s", v["arquivo"], e)
    return removidas


def remover_versao(arquivo: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    for v in listar_versoes(settings):
        if v["arquivo"] == arquivo:
            if v["papel"] == "ativa":
                raise PermissionError("A versao ativa nao pode ser removida.")
            (versoes_dir(settings) / arquivo).unlink()
            atual = ler_json(data_dir(settings) / PONTEIRO) or {}
            if atual.get("anterior") == arquivo:
                atual["anterior"] = None
                atual["anterior_mes"] = None
                escrever_json_atomico(data_dir(settings) / PONTEIRO, atual)
                limpar_cache()
            return
    raise FileNotFoundError(arquivo)


def tamanho_pasta(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def espaco_livre(path: Path) -> int:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free
