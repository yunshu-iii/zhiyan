param(
    [Parameter(Position=0)]
    [ValidateSet('start','stop','status','ingest')]
    [string]$Action = 'status',
    [Parameter(Position=1)]
    [string]$InputPath
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$appRoot = $root
$python = Join-Path $root '.venv\Scripts\python.exe'
$stateFile = Join-Path $root '.local\state\apps.json'
$dockerArgs = @('compose','--project-name','zhiyan-local','--env-file',(Join-Path $root '.local\infra.env'),'-f',(Join-Path $root 'compose.local.yml'))
$appState = @{}
if (Test-Path -LiteralPath $stateFile) {
    $saved = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    foreach ($property in $saved.PSObject.Properties) { $appState[$property.Name] = $property.Value }
}

function Get-OwnedProcess($entry) {
    if ($null -eq $entry) { return $null }
    $process = Get-Process -Id $entry.Id -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    if ($process.StartTime.ToUniversalTime().Ticks.ToString() -ne $entry.StartTicks) { return $null }
    if ($process.Path -ne $entry.Executable) { return $null }
    return $process
}

function Start-LocalApp($name, $executable, $arguments, $workingDirectory, $port) {
    $existing = Get-OwnedProcess $appState[$name]
    if ($null -ne $existing) { return }
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use. No existing process was stopped."
    }
    $process = Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory $workingDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root ".local\logs\$name.stdout.log") -RedirectStandardError (Join-Path $root ".local\logs\$name.stderr.log") -PassThru
    $appState[$name] = @{Id=$process.Id;StartTicks=$process.StartTime.ToUniversalTime().Ticks.ToString();Executable=$executable}
    $appState | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $stateFile -Encoding utf8
}

switch ($Action) {
    'start' {
        & docker @dockerArgs up -d --wait --wait-timeout 240
        if ($LASTEXITCODE -ne 0) { throw 'Infrastructure failed to become healthy; inspect project logs.' }
        $env:PYTHONNOUSERSITE='1'
        $env:PYTHONUTF8='1'
        Start-LocalApp 'backend' $python @('-m','uvicorn','app_main:app','--host','127.0.0.1','--port','8000') (Join-Path $appRoot 'app') 8000
        $node = (Get-Command node.exe -ErrorAction Stop).Source
        $vite = Join-Path $appRoot 'frontend\node_modules\vite\bin\vite.js'
        Start-LocalApp 'frontend' $node @(('"'+$vite+'"'),'--host','127.0.0.1','--port','5173','--strictPort') (Join-Path $appRoot 'frontend') 5173
        $ready = $false
        for ($attempt=0; $attempt -lt 60; $attempt++) {
            try {
                $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2
                $front = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:5173/' -TimeoutSec 2
                if ($health.status -eq 'ok' -and $front.StatusCode -eq 200) { $ready=$true; break }
            } catch { Start-Sleep -Seconds 1 }
        }
        if (-not $ready) { throw 'Application startup incomplete; inspect .local/logs/backend.stderr.log and frontend.stderr.log.' }
        Write-Output 'Frontend: http://127.0.0.1:5173'
        Write-Output 'Backend:  http://127.0.0.1:8000/docs'
        Write-Output 'Attu:     http://127.0.0.1:18000'
    }
    'stop' {
        foreach ($name in @('frontend','backend')) {
            $process = Get-OwnedProcess $appState[$name]
            if ($null -ne $process) {
                & taskkill.exe /PID $process.Id /T /F | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "Could not stop the owned $name process tree." }
            }
        }
        & docker @dockerArgs stop
        if ($LASTEXITCODE -ne 0) { throw 'Project container stop failed.' }
        Write-Output 'This project is stopped. Its data is retained.'
    }
    'status' {
        & docker @dockerArgs ps --all
        if ($LASTEXITCODE -ne 0) { throw 'Docker status query failed.' }
        foreach ($name in @('backend','frontend')) {
            $process = Get-OwnedProcess $appState[$name]
            Write-Output "$name : $(if ($null -ne $process) {'running (PID '+$process.Id+')'} else {'stopped'})"
        }
    }
    'ingest' {
        if (-not $InputPath) { throw 'Supply a UTF-8 text/Markdown file or directory as the second argument.' }
        $resolvedInput = (Resolve-Path -LiteralPath $InputPath).Path
        $env:PYTHONNOUSERSITE='1'
        $env:PYTHONUTF8='1'
        Push-Location $appRoot
        try {
            & $python (Join-Path $appRoot 'app\mult_agents\rag\ingest.py') $resolvedInput
            if ($LASTEXITCODE -ne 0) { throw 'Ingestion failed.' }
        } finally { Pop-Location }
    }
}
