# start.ps1 - inicia o servidor web no Windows
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (Test-Path .venv\Scripts\Activate.ps1) {
    & .venv\Scripts\Activate.ps1
}

$Host_ = if ($env:APP_HOST) { $env:APP_HOST } else { "127.0.0.1" }
$Port = if ($env:APP_PORT) { $env:APP_PORT } else { "8000" }

Write-Host ">> Iniciando servidor em http://$Host_`:$Port" -ForegroundColor Cyan
python -m uvicorn app.main:app --host $Host_ --port $Port --reload
