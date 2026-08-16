# setup_autostart.ps1 — auto-start the always-on jobs at Windows log on.
# ============================================================================
# Puts two shortcuts in your Startup folder so these launch automatically every
# time you log in (surviving reboots / Windows updates), running hidden via
# pythonw.exe so no console windows clutter your desktop:
#
#   StorageStress-Scheduler : scripts/scheduler.py — fires the weekly `decide`
#                             and daily `mark` jobs on schedule.
#   StorageStress-Dashboard : the Streamlit dashboard on port 8501.
#
# WHY the Startup folder (not Task Scheduler / a Service)?
#   * Task Scheduler "at log on" tasks require ADMIN to register.
#   * A Windows Service runs in an isolated session and could not reach the
#     IB Gateway running in YOUR interactive session.
#   The Startup folder needs no elevation and runs in your session, right where
#   Gateway lives. It only triggers at LOG ON, which is exactly when Gateway is
#   available anyway.
#
# Re-running is safe (overwrites the shortcuts). To undo, delete the two .lnk
# files this prints, or run:  scripts\remove_autostart.ps1
# ----------------------------------------------------------------------------

$ErrorActionPreference = "Stop"

# --- resolve paths (this script lives in <project>\scripts) -----------------
$proj = Split-Path -Parent $PSScriptRoot
$venv = Join-Path (Split-Path -Parent $proj) ".venv\Scripts"
if (-not (Test-Path (Join-Path $venv "python.exe"))) {
    $venv = Join-Path $proj ".venv\Scripts"   # fallback: venv inside project
}
$py  = Join-Path $venv "python.exe"
$pyw = Join-Path $venv "pythonw.exe"          # windowless python for hidden run
if (-not (Test-Path $pyw)) { $pyw = $py }     # some venvs lack pythonw
if (-not (Test-Path $py))  { throw "python.exe not found under $venv" }
Write-Host "project : $proj"
Write-Host "pythonw : $pyw"

# --- free port 8501 if a previous dashboard is still holding it -------------
$listeners = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
    Write-Host "freeing port 8501 (stopping previous dashboard instance)"
    $listeners | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

# --- helper: create a .lnk in the Startup folder ----------------------------
$startup = [Environment]::GetFolderPath("Startup")
$wsh = New-Object -ComObject WScript.Shell

function New-StartupShortcut($name, $arguments) {
    $lnk = Join-Path $startup "$name.lnk"
    $s = $wsh.CreateShortcut($lnk)
    $s.TargetPath = $pyw               # windowless interpreter
    $s.Arguments = $arguments
    $s.WorkingDirectory = $proj        # so config/ and data/ resolve
    $s.WindowStyle = 7                 # minimized/hidden
    $s.Description = "Storage-Stress $name (auto-start)"
    $s.Save()
    Write-Host "created $lnk"
}

New-StartupShortcut "StorageStress-Scheduler" "scripts\scheduler.py"
New-StartupShortcut "StorageStress-Dashboard" `
    "-m streamlit run dashboard\app.py --server.port 8501 --server.headless true"

# --- start both NOW (Startup only fires at the NEXT log on) -----------------
Write-Host "`nstarting both now..."
Start-Process -FilePath $pyw -ArgumentList "scripts\scheduler.py" `
    -WorkingDirectory $proj -WindowStyle Hidden
Start-Process -FilePath $pyw `
    -ArgumentList "-m","streamlit","run","dashboard\app.py","--server.port","8501","--server.headless","true" `
    -WorkingDirectory $proj -WindowStyle Hidden

Start-Sleep -Seconds 6

# --- report ------------------------------------------------------------------
$dash = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue
$procs = @(Get-Process pythonw -ErrorAction SilentlyContinue).Count
Write-Host "`n--- status ---"
Write-Host ("dashboard on 8501 : " + ($(if ($dash) {"LISTENING"} else {"not up yet (give it a few more seconds)"})))
Write-Host ("pythonw processes : $procs (expect >= 2: scheduler + dashboard)")
Write-Host "auto-start shortcuts installed in: $startup"
Write-Host "dashboard: http://localhost:8501"
