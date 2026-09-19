@echo off
REM ============================================================
REM start.bat - inicia o app CNPJ Consulta (Windows)
REM ============================================================
REM
REM Auto-suficiente: cria venv, instala deps, prepara .env,
REM cria diretorios, inicializa banco, sobe o servidor.
REM Duplo-clique no Explorer ou execute em terminal.
REM ============================================================

setlocal enableextensions enabledelayedexpansion
cd /d "%~dp0"

set "HOST=127.0.0.1"
if not "%APP_HOST%"=="" set "HOST=%APP_HOST%"
set "PORT=8000"
if not "%APP_PORT%"=="" set "PORT=%APP_PORT%"

REM ---------- 0. Verifica Python no PATH ----------
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo  [ERRO] Python nao encontrado no PATH.
    echo.
    echo  Instale Python 3.12 ou superior:
    echo    https://www.python.org/downloads/
    echo.
    echo  IMPORTANTE: marque "Add python.exe to PATH" no instalador.
    echo.
    pause
    exit /b 1
)

REM Resolve Python (venv se existir, senao sistema)
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if "%PY%"=="" set "PY=python"

REM Confirma versao >= 3.12 via exit code
"%PY%" -c "import sys;sys.exit(0 if sys.version_info>=(3,12) else 1)" >nul 2>&1
if errorlevel 1 (
    "%PY%" -c "import sys;print(sys.version_info.major,sys.version_info.minor)" 1>&2
    echo  [ERRO] Python 3.12+ necessario. Veja versao acima.
    pause
    exit /b 1
)

REM ---------- 1. Cria venv se faltar ----------
if not exist ".venv\Scripts\python.exe" (
    echo [1/5] Criando ambiente virtual em .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERRO] Falha ao criar venv. Verifique permissoes.
        pause
        exit /b 1
    )
    set "PY=.venv\Scripts\python.exe"
) else (
    echo [1/5] Ambiente virtual OK.
)

REM ---------- 2. Instala deps se faltar ----------
"%PY%" -c "import fastapi, uvicorn, duckdb, jinja2, httpx" >nul 2>&1
if errorlevel 1 goto :install_deps
echo [2/5] Dependencias OK.
goto :deps_done
:install_deps
echo [2/5] Instalando dependencias - pode levar 1-2 min ...
"%PY%" -m pip install --upgrade pip --quiet
if errorlevel 1 (
    echo [ERRO] Falha ao atualizar pip.
    pause
    exit /b 1
)
"%PY%" -m pip install -e . --quiet
if errorlevel 1 (
    echo [ERRO] Falha ao instalar dependencias.
    echo        Verifique sua conexao com a internet.
    pause
    exit /b 1
)
:deps_done

REM ---------- 3. .env ----------
if not exist ".env" (
    echo [3/5] Criando .env a partir de .env.example ...
    copy /y ".env.example" ".env" >nul
) else (
    echo [3/5] .env OK.
)

REM ---------- 4. Diretorios data/ ----------
if not exist "data"        mkdir data
if not exist "data\cache"  mkdir data\cache
if not exist "data\receita" mkdir data\receita
echo [4/5] Diretorios OK.

REM ---------- 5. Banco SQLite ----------
if not exist "data\app.db" (
    echo [5/5] Inicializando banco SQLite ...
    "%PY%" -m app.cli init-db
    if errorlevel 1 (
        echo [ERRO] Falha ao inicializar banco.
        pause
        exit /b 1
    )
) else (
    echo [5/5] Banco OK.
)

echo.
echo  ^>^> Servidor iniciando em http://%HOST%:%PORT%
echo     UI:  http://%HOST%:%PORT%/
echo     API: http://%HOST%:%PORT%/docs
echo.
echo  Pressione Ctrl+C para parar.
echo.

"%PY%" -m uvicorn app.main:app --host %HOST% --port %PORT% --reload
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [ERRO] uvicorn encerrou com codigo %RC%
    pause
)

endlocal & exit /b %RC%
