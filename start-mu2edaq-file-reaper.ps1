# start-mu2edaq-file-reaper.ps1 - run the reaper in the foreground on Windows
# (daemon mode needs fork(2); use a service wrapper such as NSSM for background use).
#   .\start-mu2edaq-file-reaper.ps1 [-Config config\mu2edaq-file-reaper.yaml] [-Port 5004] [extra args]
param(
    [string]$Config = "config\mu2edaq-file-reaper.yaml",
    [int]$Port = $(if ($env:CRS_PORT_HTTP) { [int]$env:CRS_PORT_HTTP } else { 5004 }),
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path "venv\Scripts\python.exe")) { throw "virtual environment not found; run .\bootstrap.ps1 first" }
$env:PYTHONPATH = "$PSScriptRoot\src;$env:PYTHONPATH"
New-Item -ItemType Directory -Force -Path data, logs | Out-Null
Write-Host "Starting File Reaper (http=$Port, config: $Config)"
& venv\Scripts\python.exe file_reaper.py --config $Config --port $Port @Rest
