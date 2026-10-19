$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot 'apps\api')
if (!(Test-Path '.venv\Scripts\python.exe')) {
    & py -3.11 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 is required.' }
}
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-workspace.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if (!(Test-Path '.env')) { Copy-Item '.env.example' '.env' }
& .\.venv\Scripts\python.exe -m uvicorn app.v2_main:app --host 127.0.0.1 --port 8002
