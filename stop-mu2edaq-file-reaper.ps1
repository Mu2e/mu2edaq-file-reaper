# stop-mu2edaq-file-reaper.ps1 - stop a foreground/background reaper started from this directory.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "file_reaper\.py|mu2edaq_file_reaper|mu2edaq-file-reaper" -and $_.CommandLine -match "python" }
if (-not $procs) { Write-Host "File Reaper not running"; exit 0 }
foreach ($p in $procs) { Write-Host "Stopping File Reaper (pid $($p.ProcessId))"; Stop-Process -Id $p.ProcessId -Force }
Remove-Item -ErrorAction SilentlyContinue file-reaper.pid
Write-Host "File Reaper stopped"
