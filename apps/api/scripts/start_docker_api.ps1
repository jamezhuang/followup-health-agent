param(
    [string]$Image = 'carebrief-api:0.1.0-check',
    [string]$Container = 'carebrief-api-check',
    [string]$DbContainer = 'ai-consultation-v1-rag-db-1',
    [int]$Port = 18000
)

$ErrorActionPreference = 'Stop'
$apiRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $apiRoot '.venv\Scripts\python.exe'
$prepare = Join-Path $PSScriptRoot 'prepare_docker_runtime.py'

if (!(Test-Path -LiteralPath $python)) {
    throw 'API Python virtual environment not found.'
}
if (!(Test-Path -LiteralPath $prepare)) {
    throw 'prepare_docker_runtime.py not found.'
}

docker image inspect $Image --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'Backend image not found.' }

$networkJson = docker inspect $DbContainer --format '{{json .NetworkSettings.Networks}}'
if ($LASTEXITCODE -ne 0) { throw 'Database container not found.' }
$networks = $networkJson | ConvertFrom-Json
$networkNames = @($networks.PSObject.Properties.Name | Where-Object { $_ -ne 'bridge' -and $_ -ne 'host' -and $_ -ne 'none' })
if ($networkNames.Count -eq 0) { throw 'Database requires a user-defined Docker network.' }
$dbNetwork = $networkNames[0]

$existing = docker ps -a --filter "name=^/$Container`$" --format '{{.ID}}'
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect containers.' }
if ($existing) { throw "Container $Container already exists. Inspect its logs before replacing it." }

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) { throw "Port $Port is occupied. Stop the original backend with Ctrl+C, then run this script again." }

& $python $prepare --api-root $apiRoot --db-host $DbContainer
if ($LASTEXITCODE -ne 0) { throw 'Runtime configuration preparation failed.' }

$cachePath = Join-Path $apiRoot '.cache'
$dataPath = Join-Path $apiRoot 'data'
$envPath = Join-Path $cachePath 'docker-runtime\api.env'
$dotenvPath = Join-Path $cachePath 'docker-runtime\api.dotenv'
foreach ($path in @($cachePath, $dataPath, $dotenvPath)) {
    if ($path.Contains(',')) { throw 'Mount paths containing commas require a separate configuration.' }
}

$runArguments = @(
    'run', '-d', '--init', '--pull=never',
    '--name', $Container,
    '--network', $dbNetwork,
    '--env-file', $envPath,
    '--mount', "type=bind,source=$cachePath,target=/app/.cache",
    '--mount', "type=bind,source=$dataPath,target=/app/data",
    '--mount', "type=bind,source=$dotenvPath,target=/app/.env,readonly",
    '-p', "127.0.0.1:${Port}:8000",
    $Image
)
docker @runArguments
if ($LASTEXITCODE -ne 0) { throw 'Backend container creation failed.' }

$ready = $false
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    $running = docker inspect $Container --format '{{.State.Running}}'
    if ($LASTEXITCODE -ne 0 -or $running -ne 'true') { break }
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/openapi.json" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        # Readiness only; no POST is sent or retried.
    }
    Start-Sleep -Seconds 1
}

if (!$ready) {
    Write-Host 'Backend not ready. Recent startup logs:'
    docker logs --tail 80 $Container
    throw 'Startup verification did not pass. The container is retained for diagnosis.'
}

Write-Host "Backend HTTP ready: http://127.0.0.1:$Port/docs"
Write-Host 'Workspace: http://127.0.0.1:3100/workspace'
Write-Host 'Login, stored files, retrieval, chat and restart persistence still require functional verification.'
