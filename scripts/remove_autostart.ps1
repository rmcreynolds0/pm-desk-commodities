# remove_autostart.ps1 — undo setup_autostart.ps1.
# ============================================================================
# Deletes the two Startup-folder shortcuts so the scheduler and dashboard no
# longer launch at log on, and stops any currently-running instances.
# (Does NOT touch the ledger in data/live — your history is preserved.)
# ----------------------------------------------------------------------------

$ErrorActionPreference = "Continue"

# Remove the auto-start shortcuts.
$startup = [Environment]::GetFolderPath("Startup")
foreach ($name in "StorageStress-Scheduler", "StorageStress-Dashboard") {
    $lnk = Join-Path $startup "$name.lnk"
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "removed $lnk" }
    else { Write-Host "not present: $lnk" }
}

# Stop the running dashboard (whatever holds port 8501).
$listeners = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
    $listeners | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Write-Host "stopped dashboard on port 8501"
}

# Stop the scheduler: the pythonw process whose command line runs scheduler.py.
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*scheduler.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "stopped scheduler (pid $($_.ProcessId))" }

Write-Host "auto-start removed. Ledger and logs are untouched."
