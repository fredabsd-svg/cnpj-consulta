"""Pipeline de atualizacao da base local (roda fora da interface).

Etapas (cada uma retomavel; uma falha nunca obriga a recomecar do zero):

  descoberta   versao mais recente (ou a pedida) e lista de arquivos no portal
  espaco       estimativa de disco necessaria x espaco livre
  download     ZIPs com retomada (Range + If-Range/ETag), novas tentativas
  verificacao  tamanho igual ao anunciado e ZIP legivel
  importacao   por arquivo: extrai (CRC conferido), valida colunas/codificacao,
               importa numa transacao e apaga o CSV
  consolidacao tabelas tipadas, deduplicadas e ordenadas + tabelas auxiliares
  validacao    contagens, comparacao com a versao ativa, orfaos, rejeitadas
  publicacao   move para versoes/ e troca o ponteiro (a versao ativa segue
               servindo consultas ate aqui)
  limpeza      remove a area de trabalho

Estado de retomada: `estado.json` (downloads/verificacao) e as tabelas
`_importados` (bruto.duckdb) e `_etapas` (final.duckdb), gravadas na MESMA
transacao do dado -- se o processo cair no meio de um arquivo, aquele arquivo
inteiro e desfeito e refeito; os demais nao.
"""

from __future__ import annotations

import csv
import io
import logging
import shutil
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx

from app.config import Settings, get_settings
from app.receita import armazenamento as arm
from app.receita.duck import SCHEMA_VERSAO, Vigia, configurar, sql_str
from app.receita.fonte import ArquivoRemoto, FonteError, FonteReceita, VersaoRemota
from app.receita.layout import GRUPO_POR_NOME, GRUPOS, MES_RE, ORDEM_GRUPOS
from app.receita.schema import (
    COLUNAS_OBRIGATORIAS,
    MACROS,
    SQL_EMPRESAS,
    SQL_ESTABELECIMENTOS,
    SQL_MUNICIPIOS_UF,
    SQL_NOMES_BUSCA,
    SQL_SIMPLES,
    SQL_SOCIO_RESUMO,
    SQL_SOCIOS,
    WEBMAIL,
    sql_dominio,
)

log = logging.getLogger(__name__)

ETAPAS: list[tuple[str, str, float]] = [
    # (codigo, rotulo, peso no progresso geral)
    ("descoberta", "Descoberta da versão", 1),
    ("espaco", "Espaço em disco", 1),
    ("download", "Download", 38),
    ("verificacao", "Verificação dos arquivos", 2),
    ("importacao", "Extração e importação", 30),
    ("consolidacao", "Consolidação", 22),
    ("validacao", "Validação", 4),
    ("publicacao", "Publicação", 1),
    ("limpeza", "Limpeza", 1),
]
ROTULOS = {c: r for c, r, _ in ETAPAS}
_PESO_TOTAL = sum(p for _, _, p in ETAPAS)

# Fatores de estimativa de disco (medidos na base 2026: ~7,3 GB de ZIPs).
FATOR_CSV = 4.5  # maior CSV extraido x seu ZIP
FATOR_BRUTO = 1.6  # tabelas brutas DuckDB x total de ZIPs
FATOR_FINAL = 2.0  # base publicada x total de ZIPs
MAX_REJEITADAS_PCT = 1.0  # acima disto o arquivo e recusado (leiaute mudou?)
TENTATIVAS_DOWNLOAD = 6
CHUNK = 1024 * 1024


class PipelineError(RuntimeError):
    """Falha com mensagem pronta para o usuario."""


class Interrompido(Exception):  # noqa: N818 -- e um sinal, nao um erro
    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo  # pausar | cancelar


@dataclass
class Relatorio:
    mes: str | None = None
    inicio: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    fim: str | None = None
    arquivos: dict[str, dict] = field(default_factory=dict)
    contagens: dict[str, int] = field(default_factory=dict)
    duplicados_removidos: dict[str, int] = field(default_factory=dict)
    orfaos: dict[str, int] = field(default_factory=dict)
    comparacao: dict[str, dict] = field(default_factory=dict)
    avisos: list[str] = field(default_factory=list)
    duracao_etapas: dict[str, float] = field(default_factory=dict)
    publicada: str | None = None
    anterior: str | None = None
    bytes_baixados: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class _NullReporter:
    """Reporter usado sem tarefa no banco (CLI simples e testes)."""

    def __init__(self, progress: Callable[[str], None] | None = None) -> None:
        self._progress = progress or (lambda m: log.info("%s", m))

    def atualizar(self, forcar: bool = False, **campos: Any) -> None:
        pass

    def solicitacao(self) -> str | None:
        return None

    def log(self, mensagem: str, nivel: str = "info", etapa: str | None = None) -> None:
        self._progress(mensagem)


class Atualizacao:
    def __init__(
        self,
        mes: str | None = None,
        *,
        settings: Settings | None = None,
        reporter: Any = None,
        fonte: FonteReceita | None = None,
        client: httpx.Client | None = None,
        forcar: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        if mes is not None and not MES_RE.match(mes):
            raise ValueError("Mes deve estar no formato AAAA-MM (ex.: 2026-10)")
        self.settings = settings or get_settings()
        self.mes = mes
        self.rep = reporter or _NullReporter(progress)
        self._client = client
        self.fonte = fonte or FonteReceita(self.settings, client=client)
        self.forcar = forcar
        self.sleep = sleep
        self.relatorio = Relatorio(mes=mes)
        self.versao: VersaoRemota | None = None
        self._etapa = "descoberta"
        self._ultima_checagem = 0.0
        self._pedido: str | None = None

    # ---------- caminhos ----------

    @property
    def dir(self) -> Path:
        assert self.mes
        return arm.trabalho_dir(self.mes, self.settings)

    @property
    def zips(self) -> Path:
        return self.dir / "zips"

    @property
    def extraido(self) -> Path:
        return self.dir / "extraido"

    @property
    def bruto_db(self) -> Path:
        return self.dir / "bruto.duckdb"

    @property
    def final_db(self) -> Path:
        return self.dir / "final.duckdb"

    @property
    def estado_path(self) -> Path:
        return self.dir / "estado.json"

    # ---------- controle ----------

    def _checar(self, forcar: bool = False) -> None:
        """Atende pedido de pausa/cancelamento (consulta o banco a cada 2 s)."""
        if self._pedido is None and (forcar or time.monotonic() - self._ultima_checagem > 2):
            self._ultima_checagem = time.monotonic()
            self._pedido = self.rep.solicitacao()
        if self._pedido in ("pausar", "cancelar"):
            raise Interrompido(self._pedido)

    def _deve_parar(self) -> bool:
        if self._pedido is None:
            self._pedido = self.rep.solicitacao()
        return self._pedido in ("pausar", "cancelar")

    def _progresso(self, frac_etapa: float, **campos: Any) -> None:
        frac_etapa = max(0.0, min(1.0, frac_etapa))
        feito = 0.0
        for codigo, _, peso in ETAPAS:
            if codigo == self._etapa:
                feito += peso * frac_etapa
                break
            feito += peso
        self.rep.atualizar(
            etapa=self._etapa,
            progresso=round(100 * feito / _PESO_TOTAL, 2),
            progresso_etapa=round(100 * frac_etapa, 1),
            **campos,
        )

    def _log(self, msg: str, nivel: str = "info") -> None:
        self.rep.log(msg, nivel=nivel, etapa=self._etapa)
        if nivel == "aviso":
            self.relatorio.avisos.append(msg)

    def _entrar(self, etapa: str, mensagem: str | None = None) -> float:
        self._etapa = etapa
        self._progresso(0.0, mensagem=mensagem or ROTULOS[etapa], forcar=True)
        self._log(mensagem or ROTULOS[etapa])
        return time.monotonic()

    def _sair(self, etapa: str, t0: float) -> None:
        self.relatorio.duracao_etapas[etapa] = round(time.monotonic() - t0, 1)
        self._progresso(1.0, forcar=True)

    # ---------- execucao ----------

    def executar(self) -> dict:
        try:
            t0 = self._entrar("descoberta", "Consultando o portal oficial da Receita Federal")
            self._descobrir()
            self._sair("descoberta", t0)

            t0 = self._entrar("espaco")
            self._checar_espaco()
            self._sair("espaco", t0)

            t0 = self._entrar("download")
            self._baixar_todos()
            self._sair("download", t0)

            t0 = self._entrar("verificacao")
            self._verificar_todos()
            self._sair("verificacao", t0)

            t0 = self._entrar("importacao")
            self._importar_todos()
            self._sair("importacao", t0)

            t0 = self._entrar("consolidacao", "Consolidando tabelas (pode levar de 20 a 60 minutos)")
            self._consolidar()
            self._sair("consolidacao", t0)

            t0 = self._entrar("validacao")
            self._validar()
            self._sair("validacao", t0)

            t0 = self._entrar("publicacao")
            self._publicar()
            self._sair("publicacao", t0)

            t0 = self._entrar("limpeza")
            self._limpar(tudo=True)
            self._sair("limpeza", t0)
        finally:
            self.relatorio.fim = datetime.now(UTC).isoformat()
            self.fonte.close() if self._client is None else None
        self._log(f"Base {self.mes} publicada: {self.relatorio.publicada}")
        return self.relatorio.as_dict()

    # ---------- descoberta ----------

    def _descobrir(self) -> None:
        try:
            if self.mes is None:
                self.versao = self.fonte.versao_mais_recente()
                self.mes = self.versao.mes
            else:
                self.versao = self.fonte.listar_versao(self.mes)
        except FonteError as e:
            raise PipelineError(str(e)) from e
        v = self.versao
        self.relatorio.mes = v.mes
        if not v.completa and not self.forcar:
            raise PipelineError(
                f"A versao {v.mes} esta incompleta no portal (faltam: {', '.join(v.faltando)}). "
                "Aguarde a Receita concluir a publicacao."
            )
        if v.ignorados:
            self._log(f"Arquivos desconhecidos ignorados: {', '.join(v.ignorados)}", "aviso")
        self.dir.mkdir(parents=True, exist_ok=True)
        estado = self._estado()
        arquivos = estado.setdefault("arquivos", {})
        for a in v.arquivos:
            atual = arquivos.get(a.nome)
            if atual and (atual.get("etag") != a.etag or atual.get("tamanho") != a.tamanho):
                self._log(f"{a.nome} mudou no portal desde a ultima tentativa; sera baixado de novo", "aviso")
                (self.zips / a.nome).unlink(missing_ok=True)
                (self.zips / (a.nome + ".part")).unlink(missing_ok=True)
                atual = None
            if not atual:
                arquivos[a.nome] = {"etag": a.etag, "tamanho": a.tamanho, "grupo": a.grupo,
                                    "baixado": False, "verificado": False}
        for nome in list(arquivos):
            if nome not in {a.nome for a in v.arquivos}:
                arquivos.pop(nome)
        estado["mes"] = v.mes
        self._salvar_estado(estado)
        self.rep.atualizar(mes=v.mes, bytes_total=v.tamanho_total, forcar=True)
        self._log(
            f"Versao {v.mes}: {len(v.arquivos)} arquivos, {v.tamanho_total / 1024**3:.2f} GB "
            f"(publicada em {v.modificado[:10] or '?'})"
        )

    def _estado(self) -> dict:
        return arm.ler_json(self.estado_path) or {}

    def _salvar_estado(self, estado: dict) -> None:
        arm.escrever_json_atomico(self.estado_path, estado)

    def _importados(self) -> dict[str, dict]:
        if not self.bruto_db.exists():
            return {}
        conn = duckdb.connect(str(self.bruto_db))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _importados (nome VARCHAR PRIMARY KEY, etag VARCHAR, "
                "tabela VARCHAR, linhas BIGINT, rejeitadas BIGINT, em TIMESTAMP)"
            )
            rows = conn.execute("SELECT nome, etag, tabela, linhas, rejeitadas FROM _importados").fetchall()
        finally:
            conn.close()
        return {r[0]: {"etag": r[1], "tabela": r[2], "linhas": r[3], "rejeitadas": r[4]} for r in rows}

    def _pendentes_importacao(self) -> list[ArquivoRemoto]:
        assert self.versao
        feitos = self._importados()
        return [a for a in self.versao.arquivos if feitos.get(a.nome, {}).get("etag") != a.etag]

    # ---------- espaco ----------

    def estimar_espaco(self) -> dict[str, int]:
        assert self.versao
        pend = self._pendentes_importacao()
        total_zip = self.versao.tamanho_total
        faltam_baixar = sum(
            max(0, a.tamanho - self._tamanho_local(a)) for a in pend
        )
        maior = max((a.tamanho for a in pend), default=0)
        bruto_atual = self.bruto_db.stat().st_size if self.bruto_db.exists() else 0
        necessario = (
            faltam_baixar
            + int(maior * FATOR_CSV)
            + max(0, int(total_zip * FATOR_BRUTO) - bruto_atual)
            + int(total_zip * FATOR_FINAL)
        )
        return {
            "total_zips": total_zip,
            "faltam_baixar": faltam_baixar,
            "necessario": necessario,
            "livre": arm.espaco_livre(self.dir),
            "base_final_estimada": int(total_zip * FATOR_FINAL),
        }

    def _tamanho_local(self, a: ArquivoRemoto) -> int:
        for p in (self.zips / a.nome, self.zips / (a.nome + ".part")):
            if p.exists():
                return p.stat().st_size
        return 0

    def _checar_espaco(self) -> None:
        e = self.estimar_espaco()
        self.relatorio.arquivos  # noqa: B018
        gb = 1024**3
        msg = (
            f"Necessario ~{e['necessario'] / gb:.1f} GB livres (download restante "
            f"{e['faltam_baixar'] / gb:.1f} GB); disponivel {e['livre'] / gb:.1f} GB em {self.dir}"
        )
        if e["livre"] < e["necessario"]:
            if not self.forcar:
                raise PipelineError("Espaco em disco insuficiente. " + msg)
            self._log("Espaco possivelmente insuficiente (continuando por --forcar). " + msg, "aviso")
        else:
            self._log(msg)

    # ---------- download ----------

    @property
    def client(self) -> httpx.Client:
        return self.fonte.client

    def _baixar_todos(self) -> None:
        assert self.versao
        self.zips.mkdir(parents=True, exist_ok=True)
        estado = self._estado()
        pend = self._pendentes_importacao()
        a_baixar = [a for a in pend if not estado["arquivos"][a.nome].get("baixado") or not (self.zips / a.nome).exists()]
        total = sum(a.tamanho for a in a_baixar)
        feito_base = sum(self._tamanho_local(a) for a in a_baixar)
        self._dl = {"total": total, "feito": feito_base, "janela": [(time.monotonic(), feito_base)]}
        if not a_baixar:
            self._log("Todos os arquivos ja estao baixados")
            return
        self._log(f"{len(a_baixar)} arquivo(s) para baixar ({total / 1024**3:.2f} GB)")
        for i, a in enumerate(a_baixar, 1):
            self._checar(forcar=True)
            self.rep.atualizar(arquivo_atual=a.nome, mensagem=f"Baixando {a.nome} ({i}/{len(a_baixar)})")
            self._baixar(a)
            estado = self._estado()
            estado["arquivos"][a.nome].update(baixado=True, verificado=False)
            self._salvar_estado(estado)
        self.relatorio.bytes_baixados = total

    def _contabilizar(self, n: int) -> None:
        d = self._dl
        d["feito"] += n
        agora = time.monotonic()
        janela = d["janela"]
        janela.append((agora, d["feito"]))
        while len(janela) > 2 and agora - janela[0][0] > 15:
            janela.pop(0)
        dt = agora - janela[0][0]
        vel = (d["feito"] - janela[0][1]) / dt if dt > 0.5 else 0.0
        restante = max(0, d["total"] - d["feito"])
        self._progresso(
            d["feito"] / d["total"] if d["total"] else 1.0,
            bytes_total=d["total"],
            bytes_feitos=d["feito"],
            velocidade_bps=round(vel, 1),
            eta_segundos=round(restante / vel, 0) if vel > 0 else None,
        )

    def _baixar(self, a: ArquivoRemoto) -> None:
        """Download com retomada. If-Range garante que, se o arquivo mudou no
        portal, o servidor devolve o arquivo inteiro (200) em vez de um pedaco
        da versao nova colado no pedaco da antiga."""
        destino = self.zips / a.nome
        parcial = self.zips / (a.nome + ".part")
        ultimo_erro = ""
        for tentativa in range(1, TENTATIVAS_DOWNLOAD + 1):
            offset = parcial.stat().st_size if parcial.exists() else 0
            if a.tamanho and offset > a.tamanho:
                parcial.unlink()
                self._contabilizar(-offset)
                offset = 0
            if a.tamanho and offset == a.tamanho:
                parcial.replace(destino)
                return
            headers = {}
            if offset:
                headers["Range"] = f"bytes={offset}-"
                if a.etag:
                    headers["If-Range"] = f'"{a.etag}"'
            try:
                with self.client.stream("GET", a.url, headers=headers, auth=self.fonte.auth) as resp:
                    if resp.status_code == 404:
                        raise PipelineError(
                            f"{a.nome} nao esta mais no portal (404). A Receita pode ter "
                            "substituido a versao; rode a atualizacao de novo."
                        )
                    if resp.status_code == 416:
                        # Pedimos alem do fim: o parcial nao bate com o remoto.
                        parcial.unlink(missing_ok=True)
                        self._contabilizar(-offset)
                        raise httpx.HTTPStatusError("416", request=resp.request, response=resp)
                    resp.raise_for_status()
                    if offset and resp.status_code != 206:
                        self._log(f"{a.nome}: servidor nao retomou (HTTP {resp.status_code}); recomecando", "aviso")
                        self._contabilizar(-offset)
                        offset = 0
                    elif offset:
                        inicio = resp.headers.get("content-range", "").removeprefix("bytes ").split("-")[0]
                        if inicio and inicio.isdigit() and int(inicio) != offset:
                            raise PipelineError(f"{a.nome}: resposta parcial inconsistente ({inicio} != {offset})")
                    with parcial.open("ab" if offset else "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=CHUNK):
                            fh.write(chunk)
                            self._contabilizar(len(chunk))
                            self._checar()
                tamanho = parcial.stat().st_size
                if a.tamanho and tamanho != a.tamanho:
                    raise httpx.ReadError(f"recebidos {tamanho} de {a.tamanho} bytes")
                parcial.replace(destino)
                self._log(f"{a.nome} baixado ({tamanho / 1024**2:.1f} MB)")
                return
            except (PipelineError, Interrompido):
                raise
            except (httpx.HTTPError, OSError) as e:
                ultimo_erro = f"{type(e).__name__}: {e}"
                espera = min(60, 2**tentativa)
                self._log(
                    f"{a.nome}: falha ({ultimo_erro}); tentativa {tentativa}/{TENTATIVAS_DOWNLOAD}, "
                    f"nova tentativa em {espera}s (o download continua de onde parou)",
                    "aviso",
                )
                for _ in range(int(espera)):
                    self._checar()
                    self.sleep(1)
        raise PipelineError(f"Nao foi possivel baixar {a.nome} apos {TENTATIVAS_DOWNLOAD} tentativas: {ultimo_erro}")

    # ---------- verificacao ----------

    def _verificar_todos(self) -> None:
        assert self.versao
        pend = self._pendentes_importacao()
        estado = self._estado()
        for i, a in enumerate(pend, 1):
            self._checar()
            info = estado["arquivos"][a.nome]
            path = self.zips / a.nome
            if info.get("verificado") and path.exists():
                continue
            self.rep.atualizar(arquivo_atual=a.nome, mensagem=f"Verificando {a.nome}")
            problema = self._problema_zip(path, a)
            if problema:
                path.unlink(missing_ok=True)
                info.update(baixado=False, verificado=False)
                self._salvar_estado(estado)
                self._log(f"{a.nome} invalido ({problema}); baixando de novo", "aviso")
                self._dl = {"total": a.tamanho, "feito": 0, "janela": [(time.monotonic(), 0)]}
                self._baixar(a)
                problema = self._problema_zip(path, a)
                if problema:
                    raise PipelineError(f"{a.nome} continua invalido apos novo download: {problema}")
            info.update(baixado=True, verificado=True)
            self._salvar_estado(estado)
            self._progresso(i / len(pend))
        self._log(f"{len(pend)} arquivo(s) verificados (tamanho e formato ZIP)")

    @staticmethod
    def _problema_zip(path: Path, a: ArquivoRemoto) -> str | None:
        if not path.exists():
            return "arquivo ausente"
        tamanho = path.stat().st_size
        if a.tamanho and tamanho != a.tamanho:
            return f"tamanho {tamanho} diferente do anunciado ({a.tamanho})"
        if not zipfile.is_zipfile(path):
            return "nao e um ZIP valido"
        try:
            with zipfile.ZipFile(path) as zf:
                membros = [m for m in zf.infolist() if not m.is_dir()]
        except (zipfile.BadZipFile, OSError) as e:
            return f"ZIP corrompido ({e})"
        if not membros:
            return "ZIP vazio"
        return None

    # ---------- importacao ----------

    def _conectar(self, path: Path) -> duckdb.DuckDBPyConnection:
        conn = duckdb.connect(str(path))
        configurar(conn, self.dir / "tmp", self.settings)
        return conn

    def _importar_todos(self) -> None:
        assert self.versao
        pend = sorted(self._pendentes_importacao(), key=lambda a: (ORDEM_GRUPOS[a.grupo], a.nome))
        total = sum(a.tamanho for a in self.versao.arquivos) or 1
        feito = total - sum(a.tamanho for a in pend)
        conn = self._conectar(self.bruto_db)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _importados (nome VARCHAR PRIMARY KEY, etag VARCHAR, "
                "tabela VARCHAR, linhas BIGINT, rejeitadas BIGINT, em TIMESTAMP)"
            )
            for a in pend:
                self._checar(forcar=True)
                self.rep.atualizar(arquivo_atual=a.nome, mensagem=f"Importando {a.nome}")
                self._progresso(feito / total)
                info = self._importar_arquivo(conn, a)
                feito += a.tamanho
                self.rep.atualizar(registros=self._total_bruto(conn))
                self._progresso(feito / total, forcar=True)
                self._log(
                    f"{a.nome}: {info['linhas']:,} registros"
                    + (f", {info['rejeitadas']:,} linha(s) rejeitada(s)" if info["rejeitadas"] else "")
                )
                if not self.settings.receita_manter_zips:
                    (self.zips / a.nome).unlink(missing_ok=True)
        finally:
            conn.close()
        for nome, info in self._importados().items():
            self.relatorio.arquivos[nome] = info

    @staticmethod
    def _total_bruto(conn) -> int:
        return conn.execute("SELECT coalesce(sum(linhas), 0) FROM _importados").fetchone()[0]

    def _extrair(self, a: ArquivoRemoto) -> list[Path]:
        """Extrai com protecao contra caminhos maliciosos e ZIP-bomba; o CRC de
        cada membro e conferido pelo zipfile ao fim da leitura."""
        origem = self.zips / a.nome
        destino = self.extraido / Path(a.nome).stem
        shutil.rmtree(destino, ignore_errors=True)
        destino.mkdir(parents=True)
        saida: list[Path] = []
        try:
            with zipfile.ZipFile(origem) as zf:
                membros = [m for m in zf.infolist() if not m.is_dir()]
                declarado = sum(m.file_size for m in membros)
                if declarado > max(200 * origem.stat().st_size, 1024**2):
                    raise PipelineError(f"{a.nome}: tamanho descompactado suspeito ({declarado} bytes)")
                if declarado > arm.espaco_livre(destino):
                    raise PipelineError(f"Sem espaco para extrair {a.nome} ({declarado / 1024**3:.1f} GB)")
                for i, m in enumerate(membros):
                    nome = Path(m.filename.replace("\\", "/")).name
                    if not nome or nome.startswith("."):
                        continue
                    alvo = destino / f"{i:02d}_{nome}"
                    escritos = 0
                    with zf.open(m) as src, alvo.open("wb") as dst:
                        while bloco := src.read(CHUNK * 4):
                            escritos += len(bloco)
                            if escritos > m.file_size:
                                raise PipelineError(f"{a.nome}: membro maior que o declarado")
                            dst.write(bloco)
                            self._checar()
                    saida.append(alvo)
        except zipfile.BadZipFile as e:
            shutil.rmtree(destino, ignore_errors=True)
            raise _ZipCorrompido(str(e)) from e
        except Interrompido:
            shutil.rmtree(destino, ignore_errors=True)
            raise
        return saida

    def _importar_arquivo(self, conn, a: ArquivoRemoto) -> dict:
        grupo = GRUPO_POR_NOME[a.grupo]
        try:
            csvs = self._extrair(a)
        except _ZipCorrompido as e:
            # CRC errado: o arquivo local esta corrompido. Baixa de novo uma vez.
            self._log(f"{a.nome}: falha de integridade ao extrair ({e}); baixando de novo", "aviso")
            (self.zips / a.nome).unlink(missing_ok=True)
            self._dl = {"total": a.tamanho, "feito": 0, "janela": [(time.monotonic(), 0)]}
            self._baixar(a)
            try:
                csvs = self._extrair(a)
            except _ZipCorrompido as e2:
                raise PipelineError(f"{a.nome} corrompido mesmo apos novo download: {e2}") from e2
        for p in csvs:
            self._validar_estrutura(p, a.nome, len(grupo.colunas))
        tabela = "r_" + Path(a.nome).stem
        files = "[" + ", ".join(sql_str(p.as_posix()) for p in csvs) + "]"
        cols = "{" + ", ".join(f"{sql_str(c)}: 'VARCHAR'" for c in grupo.colunas) + "}"
        rej = f"rej_{Path(a.nome).stem}"
        leitura = (
            f"read_csv({files}, delim=';', quote='\"', escape='\"', header=false, "
            f"encoding='latin-1', columns={cols}, ignore_errors=true, store_rejects=true, "
            f"rejects_table='{rej}', rejects_scan='{rej}_scan', parallel=true)"
        )
        with Vigia(conn, self._deve_parar):
            try:
                conn.execute("BEGIN TRANSACTION")
                conn.execute(f'DROP TABLE IF EXISTS "{tabela}"')
                conn.execute(f'CREATE TABLE "{tabela}" AS SELECT * FROM {leitura}')
                linhas = conn.execute(f'SELECT count(*) FROM "{tabela}"').fetchone()[0]
                rejeitadas = conn.execute(f"SELECT count(*) FROM {rej}").fetchone()[0]
                tipos = conn.execute(
                    f"SELECT error_type, count(*) FROM {rej} GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
                ).fetchall()
                total = linhas + rejeitadas
                if total == 0:
                    raise PipelineError(f"{a.nome} nao tem nenhum registro")
                pct = 100 * rejeitadas / total
                if pct > MAX_REJEITADAS_PCT and not self.forcar:
                    raise PipelineError(
                        f"{a.nome}: {rejeitadas:,} de {total:,} linhas rejeitadas ({pct:.2f}%). "
                        "O leiaute pode ter mudado; arquivo recusado."
                    )
                conn.execute("DELETE FROM _importados WHERE nome = ?", [a.nome])
                conn.execute(
                    "INSERT INTO _importados VALUES (?, ?, ?, ?, ?, ?)",
                    [a.nome, a.etag, tabela, linhas, rejeitadas, datetime.now(UTC).replace(tzinfo=None)],
                )
                conn.execute("COMMIT")
            except duckdb.InterruptException as e:
                conn.execute("ROLLBACK")
                raise Interrompido(self._pedido or "pausar") from e
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except duckdb.Error:
                    pass
                raise
            finally:
                for t in (rej, f"{rej}_scan"):
                    try:
                        conn.execute(f"DROP TABLE IF EXISTS {t}")
                    except duckdb.Error:
                        pass
        if rejeitadas:
            # So o tipo do erro: o conteudo da linha pode ter dados pessoais.
            detalhe = ", ".join(f"{t}: {n}" for t, n in tipos)
            self._log(f"{a.nome}: {rejeitadas:,} linha(s) rejeitada(s) ({detalhe})", "aviso")
        shutil.rmtree(self.extraido / Path(a.nome).stem, ignore_errors=True)
        return {"linhas": linhas, "rejeitadas": rejeitadas}

    def _validar_estrutura(self, path: Path, origem: str, esperadas: int) -> None:
        """Confere colunas e codificacao numa amostra antes de importar."""
        with path.open("rb") as fh:
            amostra = fh.read(256 * 1024)
        if not amostra.strip():
            raise PipelineError(f"{origem}: arquivo extraido vazio")
        if b"\x00" in amostra:
            raise PipelineError(f"{origem}: conteudo binario inesperado (arquivo corrompido?)")
        if amostra.startswith(b"\xef\xbb\xbf"):
            self._log(f"{origem}: arquivo com BOM UTF-8 (esperado ISO-8859-1)", "aviso")
        texto = amostra.decode("latin-1")
        linhas = texto.splitlines()[:-1] or texto.splitlines()  # ultima pode estar cortada
        linhas = [ln for ln in linhas[:500] if ln.strip()]
        ruins = 0
        contagens: dict[int, int] = {}
        for row in csv.reader(io.StringIO("\n".join(linhas)), delimiter=";", quotechar='"'):
            contagens[len(row)] = contagens.get(len(row), 0) + 1
            if len(row) != esperadas:
                ruins += 1
        if not linhas or ruins > 0.05 * len(linhas):
            mais_comum = max(contagens, key=contagens.get) if contagens else 0
            raise PipelineError(
                f"{origem}: estrutura inesperada ({mais_comum} colunas; o leiaute oficial tem "
                f"{esperadas}). A Receita pode ter mudado o formato."
            )
        try:
            texto.encode("ascii")
        except UnicodeEncodeError:
            # Tem acentos: confirma que NAO e UTF-8 (que viraria texto quebrado).
            try:
                amostra.decode("utf-8")
                self._log(f"{origem}: conteudo parece UTF-8 (esperado ISO-8859-1)", "aviso")
            except UnicodeDecodeError:
                pass

    # ---------- consolidacao ----------

    def _consolidar(self) -> None:
        importados = self._importados()
        assert self.versao
        faltando = [a.nome for a in self.versao.arquivos if a.nome not in importados]
        if faltando:
            raise PipelineError(f"Importacao incompleta: faltam {', '.join(faltando)}")
        por_grupo: dict[str, list[str]] = {}
        for info in importados.values():
            nome = info["tabela"].removeprefix("r_")
            grupo = next(g.nome for g in GRUPOS if nome.lower().startswith(g.prefixo.lower()))
            por_grupo.setdefault(grupo, []).append(info["tabela"])

        def bruto(grupo: str) -> str:
            tabs = sorted(por_grupo.get(grupo, []))
            if not tabs:
                cols = ", ".join(f"NULL::VARCHAR AS {c}" for c in GRUPO_POR_NOME[grupo].colunas)
                return f"(SELECT {cols} WHERE false)"
            return "(" + " UNION ALL ".join(f'SELECT * FROM bruto."{t}"' for t in tabs) + ")"

        fmt = {f"bruto_{g.nome}": bruto(g.nome) for g in GRUPOS}
        webmail = "[" + ", ".join(sql_str(d) for d in WEBMAIL) + "]"

        conn = self._conectar(self.final_db)
        try:
            conn.execute(f"ATTACH {sql_str(self.bruto_db.as_posix())} AS bruto (READ_ONLY)")
            conn.execute(MACROS)
            conn.execute("CREATE TABLE IF NOT EXISTS _etapas (nome VARCHAR PRIMARY KEY, info VARCHAR, em TIMESTAMP)")
            feitas = {r[0] for r in conn.execute("SELECT nome FROM _etapas").fetchall()}

            dup_emp = self._duplicados(conn, fmt["bruto_empresas"], "trim(cnpj_basico)")
            dup_est = self._duplicados(
                conn, fmt["bruto_estabelecimentos"], "trim(cnpj_basico) || trim(cnpj_ordem) || trim(cnpj_dv)"
            )
            passos: list[tuple[str, str]] = [
                (t, sql_dominio(t).format(**fmt))
                for t in ("cnaes", "motivos", "municipios", "naturezas", "paises", "qualificacoes")
            ]
            passos += [
                ("simples", SQL_SIMPLES.format(**fmt)),
                ("socios", SQL_SOCIOS.format(**fmt)),
                ("empresas", SQL_EMPRESAS.format(
                    distinct_empresas="DISTINCT ON (trim(cnpj_basico))" if dup_emp else "", **fmt)),
                ("estabelecimentos", SQL_ESTABELECIMENTOS.format(
                    distinct_estab="DISTINCT ON (trim(cnpj_basico) || trim(cnpj_ordem) || trim(cnpj_dv))" if dup_est else "",
                    webmail=webmail, **fmt)),
                ("socio_resumo", SQL_SOCIO_RESUMO),
                ("nomes_busca", SQL_NOMES_BUSCA),
                ("municipios_uf", SQL_MUNICIPIOS_UF),
            ]
            for i, (nome, sql) in enumerate(passos, 1):
                self._checar(forcar=True)
                self._progresso((i - 1) / len(passos), mensagem=f"Consolidando: {nome}", arquivo_atual=nome)
                if nome in feitas:
                    continue
                t0 = time.monotonic()
                self._transacao(conn, [f"DROP TABLE IF EXISTS {nome}", sql,
                                       f"INSERT INTO _etapas VALUES ('{nome}', NULL, now())"])
                n = conn.execute(f"SELECT count(*) FROM {nome}").fetchone()[0]
                self._log(f"{nome}: {n:,} registros ({time.monotonic() - t0:.0f}s)")

            self.relatorio.duplicados_removidos = self._dups_finais(conn, importados, dup_emp, dup_est)
            self._metadados(conn, importados)
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
        self._progresso(1.0)

    def _transacao(self, conn, comandos: list[str]) -> None:
        with Vigia(conn, self._deve_parar):
            try:
                conn.execute("BEGIN TRANSACTION")
                for c in comandos:
                    conn.execute(c)
                conn.execute("COMMIT")
            except duckdb.InterruptException as e:
                conn.execute("ROLLBACK")
                raise Interrompido(self._pedido or "pausar") from e
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except duckdb.Error:
                    pass
                raise

    @staticmethod
    def _duplicados(conn, origem: str, chave: str) -> int:
        n, d = conn.execute(f"SELECT count(*), count(DISTINCT {chave}) FROM {origem}").fetchone()
        return int(n - d)

    def _dups_finais(self, conn, importados: dict, dup_emp: int, dup_est: int) -> dict[str, int]:
        brutos: dict[str, int] = {}
        for info in importados.values():
            nome = info["tabela"].removeprefix("r_")
            grupo = next(g.nome for g in GRUPOS if nome.lower().startswith(g.prefixo.lower()))
            brutos[grupo] = brutos.get(grupo, 0) + int(info["linhas"])
        out = {"empresas": dup_emp, "estabelecimentos": dup_est}
        for t in ("socios", "simples"):
            final = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            out[t] = max(0, brutos.get(t, 0) - final)
        for t, n in out.items():
            if n:
                self._log(f"{t}: {n:,} registro(s) duplicado(s) removido(s)", "aviso")
        return out

    def _metadados(self, conn, importados: dict) -> None:
        assert self.versao
        conn.execute("DROP TABLE IF EXISTS metadata")
        conn.execute("CREATE TABLE metadata (chave VARCHAR, valor VARCHAR)")
        conn.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [
                ("mes_referencia", self.versao.mes),
                ("importado_em", datetime.now(UTC).isoformat()),
                ("schema_versao", SCHEMA_VERSAO),
                ("schema_socios", "nome_socio_norm_v1"),
                ("fonte", f"{self.fonte.origem} (compartilhamento publico oficial)"),
                ("pasta", self.fonte.pasta_cnpj or self.settings.receita_pasta_cnpj),
                ("publicado_na_fonte_em", self.versao.modificado),
            ],
        )
        conn.execute("DROP TABLE IF EXISTS importacao_arquivos")
        conn.execute(
            "CREATE TABLE importacao_arquivos (nome VARCHAR, tamanho BIGINT, etag VARCHAR, "
            "modificado VARCHAR, linhas BIGINT, rejeitadas BIGINT)"
        )
        remotos = {a.nome: a for a in self.versao.arquivos}
        conn.executemany(
            "INSERT INTO importacao_arquivos VALUES (?, ?, ?, ?, ?, ?)",
            [
                (n, remotos[n].tamanho if n in remotos else None, i["etag"],
                 remotos[n].modificado if n in remotos else None, i["linhas"], i["rejeitadas"])
                for n, i in sorted(importados.items())
            ],
        )

    # ---------- validacao ----------

    def _validar(self) -> None:
        conn = duckdb.connect(str(self.final_db))
        try:
            resultado = validar_base(conn, self.settings)
        finally:
            conn.close()
        self.relatorio.contagens = resultado["contagens"]
        self.relatorio.orfaos = resultado["orfaos"]
        for aviso in resultado["avisos"]:
            self._log(aviso, "aviso")
        erros = list(resultado["erros"])

        ativa = arm.base_ativa(self.settings)
        if ativa and not ativa.legado:
            try:
                anterior = duckdb.connect(str(ativa.caminho), read_only=True)
                try:
                    antes = {
                        t: anterior.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                        for t in ("empresas", "estabelecimentos", "socios")
                    }
                finally:
                    anterior.close()
            except duckdb.Error as e:
                antes = {}
                self._log(f"Nao foi possivel comparar com a versao ativa: {e}", "aviso")
            for t, n_antes in antes.items():
                n_novo = self.relatorio.contagens.get(t, 0)
                var = 100 * (n_novo - n_antes) / n_antes if n_antes else 0.0
                self.relatorio.comparacao[t] = {"anterior": n_antes, "nova": n_novo, "variacao_pct": round(var, 2)}
                if var < -self.settings.receita_queda_maxima_pct:
                    msg = (
                        f"{t}: {n_novo:,} registros contra {n_antes:,} na versao ativa ({var:.1f}%). "
                        "Queda acima do limite; a base nova NAO foi publicada."
                    )
                    if self.forcar:
                        self._log(msg + " (ignorado por --forcar)", "aviso")
                    else:
                        erros.append(msg)
        rejeitadas = sum(int(i.get("rejeitadas") or 0) for i in self.relatorio.arquivos.values())
        if rejeitadas:
            self._log(f"Total de linhas rejeitadas na importacao: {rejeitadas:,}", "aviso")
        if erros:
            raise PipelineError("Validacao falhou; a versao ativa continua em uso. " + " | ".join(erros))
        self._log("Validacao concluida: " + ", ".join(f"{t}={n:,}" for t, n in self.relatorio.contagens.items()))

    # ---------- publicacao e limpeza ----------

    def _publicar(self) -> None:
        assert self.mes
        anterior = arm.base_ativa(self.settings)
        destino = arm.publicar(self.final_db, self.mes, self.settings)
        self.relatorio.publicada = destino.name
        self.relatorio.anterior = anterior.caminho.name if anterior else None
        self._log(f"Publicada {destino.name}" + (f"; anterior mantida: {anterior.caminho.name}" if anterior else ""))

    def _limpar(self, tudo: bool) -> None:
        if tudo and self.settings.receita_manter_zips and self.zips.exists():
            destino = arm.data_dir(self.settings) / "zips" / (self.mes or "")
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(destino, ignore_errors=True)
            shutil.move(str(self.zips), destino)
        shutil.rmtree(self.dir, ignore_errors=True)

    def descartar_trabalho(self, manter_downloads: bool = True) -> None:
        """Cancelamento: apaga o que foi importado; ZIPs verificados podem ficar."""
        for p in (self.bruto_db, self.final_db):
            p.unlink(missing_ok=True)
            Path(str(p) + ".wal").unlink(missing_ok=True)
        for d in (self.extraido, self.dir / "tmp"):
            shutil.rmtree(d, ignore_errors=True)
        if not manter_downloads:
            shutil.rmtree(self.dir, ignore_errors=True)


class _ZipCorrompido(Exception):
    pass


def validar_base(conn: duckdb.DuckDBPyConnection, settings: Settings | None = None) -> dict:
    """Checagens de uma base (nova ou ativa): esquema, contagens, orfaos e
    consultas de amostra. Nao altera nada."""
    settings = settings or get_settings()
    erros: list[str] = []
    avisos: list[str] = []
    existentes = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    for tabela, cols in COLUNAS_OBRIGATORIAS.items():
        if tabela not in existentes:
            erros.append(f"tabela {tabela} ausente")
            continue
        tem = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [tabela]).fetchall()}
        if faltam := [c for c in cols if c not in tem]:
            erros.append(f"{tabela}: colunas ausentes {faltam}")
    if erros:
        return {"erros": erros, "avisos": avisos, "contagens": {}, "orfaos": {}}

    contagens = {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("empresas", "estabelecimentos", "socios", "simples", "cnaes", "municipios")
    }
    for t in ("empresas", "estabelecimentos", "socios", "simples", "cnaes", "municipios"):
        if not contagens[t]:
            erros.append(f"{t} esta vazia")
    orfaos = {
        "estabelecimentos_sem_empresa": conn.execute(
            "SELECT count(*) FROM estabelecimentos WHERE NOT tem_empresa").fetchone()[0],
        "socios_sem_empresa": conn.execute(
            "SELECT count(*) FROM socios s WHERE NOT EXISTS "
            "(SELECT 1 FROM empresas e WHERE e.cnpj_basico = s.cnpj_basico)").fetchone()[0],
        "empresas_sem_estabelecimento": conn.execute(
            "SELECT count(*) FROM empresas e WHERE NOT EXISTS "
            "(SELECT 1 FROM estabelecimentos s WHERE s.cnpj_basico = e.cnpj_basico)").fetchone()[0],
    }
    limite = settings.receita_orfaos_max_pct
    for chave, base in (("estabelecimentos_sem_empresa", "estabelecimentos"), ("socios_sem_empresa", "socios")):
        n = orfaos[chave]
        if n and contagens[base]:
            pct = 100 * n / contagens[base]
            msg = f"{n:,} {chave.replace('_', ' ')} ({pct:.2f}%)"
            if pct > limite:
                erros.append(msg + f" -- acima do limite de {limite}%")
            else:
                avisos.append(msg)
    dup = conn.execute("SELECT count(*) - count(DISTINCT cnpj_completo) FROM estabelecimentos").fetchone()[0]
    if dup:
        erros.append(f"{dup:,} CNPJs repetidos em estabelecimentos")
    amostra = conn.execute(
        "SELECT cnpj_completo FROM estabelecimentos USING SAMPLE 5 ROWS"
    ).fetchall()
    for (cnpj,) in amostra:
        if not conn.execute("SELECT 1 FROM estabelecimentos WHERE cnpj_completo = ?", [cnpj]).fetchone():
            erros.append("consulta de amostra por CNPJ falhou")
            break
    return {"erros": erros, "avisos": avisos, "contagens": contagens, "orfaos": orfaos}
