# Запуск студии на этом компьютере (Windows PowerShell 5.1 и PowerShell 7).
#
#   .\scripts\studio.ps1              # студия на http://127.0.0.1:8765, браузер откроется сам
#   .\scripts\studio.ps1 -NoAuth      # без входа (TESTGEN_AUTH=off) — для локальной отладки
#   .\scripts\studio.ps1 -Port 8800 -NoBrowser
#   .\scripts\studio.ps1 -Setup       # заново поставить зависимости и Chromium
#
# venv лежит в %LOCALAPPDATA%\aitestgen\venv (короткий путь: драйвер Playwright не запускается при пути
# длиннее 260 символов) и создаётся при первом запуске; зависимости ставятся заново, когда меняется
# requirements.txt. База — TESTGEN_DATABASE_URL, без неё — локальная testgen:testgen@127.0.0.1:5432.

param(
    [int]$Port = 8765,
    [switch]$NoAuth,
    [switch]$NoBrowser,
    [switch]$Setup
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $env:LOCALAPPDATA "aitestgen\venv"
$python = Join-Path $venv "Scripts\python.exe"
$url = "http://127.0.0.1:$Port"

function Fail($text) {
    Write-Host $text -ForegroundColor Red
    exit 1
}

function Port-Open($hostName, [int]$portNumber) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $wait = $client.BeginConnect($hostName, $portNumber, $null, $null)
        return $wait.AsyncWaitHandle.WaitOne(1000) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# Студия уже работает на этом порту — только открыть её.
if (Port-Open "127.0.0.1" $Port) {
    Write-Host "Студия уже запущена: $url"
    if (-not $NoBrowser) { Start-Process $url }
    exit 0
}

# venv и зависимости
if (-not (Test-Path $python)) {
    Write-Host "Создаю venv: $venv"
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv $venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv $venv
    } else {
        Fail "Не найден Python 3.12: поставьте его с python.org и запустите скрипт снова."
    }
    if (-not (Test-Path $python)) { Fail "Не удалось создать venv в $venv" }
    $Setup = $true
}

$requirements = Join-Path $root "requirements.txt"
$stamp = Join-Path $venv ".requirements.sha256"
$hash = (Get-FileHash $requirements -Algorithm SHA256).Hash
if ($Setup -or -not (Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $hash) {
    Write-Host "Ставлю зависимости из requirements.txt (в первый раз — несколько минут)…"
    & $python -m pip install --disable-pip-version-check --progress-bar on -r $requirements
    if ($LASTEXITCODE -ne 0) { Fail "pip install завершился с ошибкой." }
    Write-Host "Ставлю Chromium для Playwright…"
    & $python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { Fail "playwright install chromium завершился с ошибкой." }
    Set-Content -Path $stamp -Value $hash -Encoding ascii
}

# База
if (-not $env:TESTGEN_DATABASE_URL) {
    $env:TESTGEN_DATABASE_URL = "postgresql+psycopg://testgen:testgen@127.0.0.1:5432/testgen"
}
$db = [regex]::Match($env:TESTGEN_DATABASE_URL, "@([^:/?]+)(?::(\d+))?")
$dbHost = if ($db.Success) { $db.Groups[1].Value } else { "127.0.0.1" }
$dbPort = if ($db.Success -and $db.Groups[2].Value) { [int]$db.Groups[2].Value } else { 5432 }
if (-not (Port-Open $dbHost $dbPort)) {
    Fail ("PostgreSQL не отвечает на ${dbHost}:${dbPort}. Запустите службу (например, " +
          "Start-Service postgresql-x64-17) или задайте TESTGEN_DATABASE_URL.")
}

$env:PORT = "$Port"
if ($NoAuth) { $env:TESTGEN_AUTH = "off" }

# Браузер — когда студия начнёт отвечать.
if (-not $NoBrowser) {
    Start-Job -ArgumentList $url -ScriptBlock {
        param($address)
        for ($i = 0; $i -lt 120; $i++) {
            try {
                Invoke-WebRequest "$address/api/ready" -UseBasicParsing -TimeoutSec 2 | Out-Null
                Start-Process $address
                return
            } catch {
                Start-Sleep -Seconds 1
            }
        }
    } | Out-Null
}

Write-Host "Запускаю студию: $url (остановить — Ctrl+C)"
Push-Location $root
try {
    & $python server.py
    exit $LASTEXITCODE
} finally {
    Pop-Location
    Get-Job | Remove-Job -Force
}
