$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot 'apps\web')
if (!(Test-Path 'node_modules')) {
    & npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
if (!(Test-Path '.env.local')) { Copy-Item '.env.local.example' '.env.local' }
& npm.cmd run dev -- --port 3002
