"""Trava de processo unico para as tarefas da base local.

So um processo de trabalho pode usar a area de trabalho (DuckDB nao aceita dois
escritores no mesmo arquivo). A trava e do sistema operacional (flock no
Linux/macOS, msvcrt no Windows): se o processo morrer, ela e liberada sozinha,
sem arquivo "esquecido". Fica na pasta temporaria local da maquina (e nao no
disco de dados, que pode ser um disco externo ou de rede sem suporte a travas),
com o nome derivado da pasta de dados.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

from app.config import Settings, get_settings


def caminho_trava(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    dados = str(Path(settings.receita_data_dir).expanduser().resolve())
    chave = hashlib.sha256(dados.encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"cnpj-consulta-base-{chave}.lock"


class TravaProcesso:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or caminho_trava()
        self._fd: int | None = None

    def adquirir(self) -> bool:
        """True se conseguiu a trava (sem esperar)."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def liberar(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> TravaProcesso:
        return self

    def __exit__(self, *exc) -> None:
        self.liberar()


def em_uso(settings: Settings | None = None) -> int | None:
    """PID do processo que segura a trava (ou -1 se desconhecido); None se livre."""
    path = caminho_trava(settings)
    if not path.exists():
        return None
    t = TravaProcesso(path)
    if t.adquirir():
        t.liberar()
        return None
    try:
        return int(path.read_text().strip() or -1)
    except (OSError, ValueError):
        return -1
