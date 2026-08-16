# setup_xsec_autostart.ps1 — auto-start the cross-sectional scheduler + dashboard.
# =============================================================================
# Installs Startup-folder shortcuts (no admin needed, unlike Task Scheduler) and
# launches both immediately via pythonw so no console windows appear.
#
#   XSec-Scheduler : scripts/xsec_scheduler.py — fires rebalance + daily marks
#   XSec-Dashboard : Streamlit research/live dashboard on port 8502
#
# Startup-folder entries run in the user's INTERACTIVE session, which is where
# IB Gateway also lives — a Windows Service could not reach it.
#
# Undo: scripts\remove_autostart.ps1 (or delete the .lnk files printed below).
# -----------------------------------------------------------------------------

$ErrorActionPreference = "Stop"

$proj = Split-Path -Parent $PSScriptRoot
$venv = Join-Path (Split-Path -Parent $proj) ".venv\Scripts"
if (-not (Test-Path (Join-Path $venv "python.exe"))) {
    $venv = Join-Path $proj ".venv\Scripts"
}
$py  = Join-Path $venv "python.exe"
$pyw = Join-Path $venv "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
if (-not (Test-Path $py))  { throw "python.exe not found under $venv" }

Write-Host "project : $proj"

# --- stop any previous instances so we don't double-fire jobs ---------------
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*xsec_scheduler.py*' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "stopped previous scheduler (pid $($_.ProcessId))"
    }

# --- Startup shortcuts ------------------------------------------------------
$startup = [Environment]::GetFolderPath("Startup")
$wsh = New-Object -ComObject WScript.Shell

function New-Shortcut($name, $arguments) {
    $lnk = Join-Path $startup "$name.lnk"
    $s = $wsh.CreateShortcut($lnk)
    $s.TargetPath       = $pyw
    $s.Arguments        = $arguments
    $s.WorkingDirectory = $proj
    $s.WindowStyle      = 7
    $s.Description      = "QUANTT $name"
    $s.Save()
    Write-Host "created $lnk"
}

New-Shortcut "XSec-Scheduler" "scripts\xsec_scheduler.py"
New-Shortcut "XSec-Dashboard" `
    "-m streamlit run dashboard\pivot_a.py --server.port 8502 --server.headless true"

# --- start now (Startup only fires at the NEXT logon) -----------------------
Write-Host "`nstarting now..."
Start-Process -FilePath $pyw -ArgumentList "scripts\xsec_scheduler.py" `
    -WorkingDirectory $proj -WindowStyle Hidden

$dash = Get-NetTCPConnection -LocalPort 8502 -State Listen -ErrorAction SilentlyContinue
if (-not $dash) {
    Start-Process -FilePath $pyw `
        -ArgumentList "-m","streamlit","run","dashboard\pivot_a.py",
                      "--server.port","8502","--server.headless","true" `
        -WorkingDirectory $proj -WindowStyle Hidden
}

Start-Sleep -Seconds 6

# --- report -----------------------------------------------------------------
$sched = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -like '*xsec_scheduler.py*' }
Write-Host "`n--- status ---"
Write-Host ("scheduler        : " + $(if ($sched) { "RUNNING (pid $($sched.ProcessId))" } else { "NOT RUNNING" }))
$d = Get-NetTCPConnection -LocalPort 8502 -State Listen -ErrorAction SilentlyContinue
Write-Host ("dashboard :8502  : " + $(if ($d) { "LISTENING" } else { "not up" }))
Write-Host "autostart entries in: $startup"
