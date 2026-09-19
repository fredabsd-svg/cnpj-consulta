# setup.ps1 - instalacao para Windows PowerShell
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$Python = $env:PYTHON
if (-not $Python) { $Python = "python" }
$VenvDir = ".venv"

Write-Host ">> Verificando Python 3.12+..." -ForegroundColor Cyan
$pyVersion = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$parts = $pyVersion.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 12)) {
    Write-Error "Python 3.12+ necessario. Encontrado: $pyVersion"
    exit 1
}

Write-Host ">> Criando ambiente virtual em $VenvDir..." -ForegroundColor Cyan
& $Python -m venv $VenvDir

& "$VenvDir\Scripts\Activate.ps1"

Write-Host ">> Atualizando pip..." -ForegroundColor Cyan
python -m pip install --upgrade pip wheel setuptools

Write-Host ">> Instalando dependencias..." -ForegroundColor Cyan
python -m pip install -e ".[dev]"

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "   .env criado. Edite conforme necessario." -ForegroundColor Yellow
}

Write-Host ">> Criando diretorios de dados..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path data\receita, data\cache | Out-Null

Write-Host ">> Inicializando banco SQLite..." -ForegroundColor Cyan
python -m app.cli init-db

Write-Host ""
Write-Host "OK! Para iniciar o servidor:" -ForegroundColor Green
Write-Host "    .\$VenvDir\Scripts\Activate.ps1"
Write-Host "    .\start.ps1"
Write-Host ""
Write-Host "Para usar a CLI diretamente:" -ForegroundColor Green
Write-Host "    python -m app.cli --help"
Write-Host ""
Write-Host "Para baixar a base da Receita Federal (opcional, ~10 GB):" -ForegroundColor Green
Write-Host "    python -m app.cli sync-receita --mes 2024-08"
