#!/usr/bin/env bash
# setup.sh - instalacao para Linux/Mac/Git Bash no Windows
set -euo pipefail

cd "$(dirname "$0")"

# Detecta python disponivel
PYTHON=""
for cand in python3 python py; do
    if command -v "$cand" >/dev/null 2>&1; then
        PYTHON="$cand"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "ERRO: Python nao encontrado. Instale Python 3.12 ou superior." >&2
    exit 1
fi

VENV_DIR=".venv"

echo ">> Verificando Python 3.12+..."
PY_VERSION="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [ "$(printf '%s\n' "3.12" "$PY_VERSION" | sort -V | head -n1)" != "3.12" ]; then
    echo "ERRO: Python 3.12+ necessario. Encontrado: $PY_VERSION" >&2
    exit 1
fi

echo ">> Criando ambiente virtual em $VENV_DIR..."
"$PYTHON" -m venv "$VENV_DIR"

# Ativacao portatil (Linux/Mac usam bin/, Windows Git Bash usa Scripts/)
if [ -f "$VENV_DIR/Scripts/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/Scripts/activate"
elif [ -f "$VENV_DIR/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
else
    echo "ERRO: venv criada mas activate nao encontrado em $VENV_DIR" >&2
    exit 1
fi

echo ">> Atualizando pip..."
python -m pip install --upgrade pip wheel setuptools

echo ">> Instalando dependencias..."
python -m pip install -e ".[dev]"

echo ">> Criando .env a partir de .env.example (se nao existir)..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "   .env criado. Edite conforme necessario."
fi

echo ">> Criando diretorios de dados..."
mkdir -p data/receita data/cache

echo ">> Inicializando banco SQLite..."
python -m app.cli init-db

echo ""
echo "OK! Para iniciar o servidor:"
if [ -f "$VENV_DIR/Scripts/activate" ]; then
    echo "    source $VENV_DIR/Scripts/activate"
else
    echo "    source $VENV_DIR/bin/activate"
fi
echo "    ./start.sh        (ou start.bat no Windows)"
echo ""
echo "Ou para usar a CLI diretamente:"
echo "    python -m app.cli --help"
echo ""
echo "Para baixar a base da Receita Federal (opcional, ~10 GB):"
echo "    python -m app.cli sync-receita --mes 2024-08"
