# bootstrap.ps1 - set up the mu2edaq-file-reaper environment on Windows 11.
#   powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1 [-Extras]
param([switch]$Extras)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path "venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment"
    python -m venv venv
}
& venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& venv\Scripts\pip.exe install -r requirements.txt -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }
& venv\Scripts\pip.exe install -e .
if ($LASTEXITCODE -ne 0) { throw "package installation failed" }
if ($Extras) { & venv\Scripts\pip.exe install pyzmq zstandard }
foreach ($pair in @(@("mu2edaq_discovery", "..\mu2edaq-discovery"), @("mu2edaq_notify", "..\mu2edaq-phone-notification-system"))) {
    & venv\Scripts\python.exe -c "import $($pair[0])" 2>$null
    if ($LASTEXITCODE -ne 0 -and (Test-Path $pair[1])) { & venv\Scripts\pip.exe install -e $pair[1] }
}
New-Item -ItemType Directory -Force -Path data, logs, config | Out-Null
Write-Host "mu2edaq-file-reaper environment ready.  Next: .\start-mu2edaq-file-reaper.ps1"
