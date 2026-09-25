#!/usr/bin/env bash
# ===========================================================
# start.sh - inicia o app CNPJ Consulta (Linux/Mac/Git Bash)
# ===========================================================
#
# Auto-suficiente: cria venv, instala deps, prepara .env,
# cria diretorios, inicializa banco, sobe o servidor.
#
# Para parar: Ctrl+C
# ===========================================================

set -uo pipefail
cd "$(dirname "$0")"

HOST="${APP_HOST:-127.0.0.1}"
PORT="${APP_PORT:-8000}"

pause_on_error() {
    if [ -t 0 ]; then
        echo
        read -rp "Pressione Enter para fechar..."
    fi
}

# ---------- 0. Verifica Python ----------
# Primeiro Python 3.12+ disponivel (o "python3" do sistema pode ser antigo).
PY=""
for cand in python3.14 python3.13 python3.12 python3 python py; do
    if command -v "$cand" >/dev/null 2>&1 \
        && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        PY="$cand"
        break
    fi
done
if [ -z "$PY" ]; then
    echo
    echo "  [ERRO] Python 3.12+ nao encontrado no PATH."
    echo
    echo "  Instale Python 3.12 ou superior."
    echo "  No Windows: https://www.python.org/downloads/"
    echo "  (marque 'Add python.exe to PATH' no instalador)"
    pause_on_error
    exit 1
fi

# Confirma versao >= 3.12
PY_VERSION="$("$PY" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [ "$(printf '%s\n' "3.12" "$PY_VERSION" | sort -V | head -n1)" != "3.12" ]; then
    echo "[ERRO] Python 3.12+ necessario. Encontrado: $PY_VERSION"
    pause_on_error
    exit 1
fi

# ---------- 1. Cria venv se faltar ----------
if [ -x ".venv/Scripts/python.exe" ]; then
    VENV_PY=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
    VENV_PY=".venv/bin/python"
else
    echo "[1/5] Criando ambiente virtual em .venv ..."
    if ! "$PY" -m venv .venv; then
        echo "[ERRO] Falha ao criar venv."
        pause_on_error
        exit 1
    fi
    if [ -x ".venv/Scripts/python.exe" ]; then
        VENV_PY=".venv/Scripts/python.exe"
    elif [ -x ".venv/bin/python" ]; then
        VENV_PY=".venv/bin/python"
    else
        echo "[ERRO] venv criada mas python nao encontrado dentro dela."
        pause_on_error
        exit 1
    fi
    echo "[1/5] Ambiente virtual criado."
fi

# Ativa venv (para mensagens de erro do pip mostrarem o python certo)
if [ -f ".venv/Scripts/activate" ]; then
    # shellcheck disable=SC1091
    source ".venv/Scripts/activate"
elif [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source ".venv/bin/activate"
fi

# ---------- 2. Instala deps se faltar ----------
if ! "$VENV_PY" -c "import fastapi, uvicorn, duckdb, jinja2, httpx" >/dev/null 2>&1; then
    echo "[2/5] Instalando dependencias (pode levar 1-2 min) ..."
    if ! "$VENV_PY" -m pip install --upgrade pip --quiet; then
        echo "[ERRO] Falha ao atualizar pip."
        pause_on_error
        exit 1
    fi
    if ! "$VENV_PY" -m pip install -e . --quiet; then
        echo "[ERRO] Falha ao instalar dependencias."
        echo "      Verifique sua conexao com a internet."
        pause_on_error
        exit 1
    fi
    echo "[2/5] Dependencias instaladas."
else
    echo "[2/5] Dependencias OK."
fi

# ---------- 3. .env ----------
if [ ! -f .env ]; then
    echo "[3/5] Criando .env a partir de .env.example ..."
    cp .env.example .env
else
    echo "[3/5] .env OK."
fi

# ---------- 4. Diretorios data/ ----------
mkdir -p data data/cache data/receita
echo "[4/5] Diretorios OK."

# ---------- 5. Banco SQLite ----------
if [ ! -f data/app.db ]; then
    echo "[5/5] Inicializando banco SQLite ..."
    if ! "$VENV_PY" -m app.cli init-db; then
        echo "[ERRO] Falha ao inicializar banco."
        pause_on_error
        exit 1
    fi
else
    echo "[5/5] Banco OK."
fi

echo
echo "  >> Servidor iniciando em http://${HOST}:${PORT}"
echo "     UI:  http://${HOST}:${PORT}/"
echo "     API: http://${HOST}:${PORT}/docs"
echo "     CLI: ${VENV_PY} -m app.cli --help"
echo
echo "  Pressione Ctrl+C para parar."
echo

exec "$VENV_PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --reload

status=$?
echo
echo "[ERRO] uvicorn saiu com codigo $status" >&2
pause_on_error
exit $status
