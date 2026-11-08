# Builds the Windows app: dist\EQ Log Suite\EQ Log Suite.exe, plus a zip of
# that folder to share. Run from anywhere, in PowerShell:
#
#     powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
#
# Needs Python 3.11-3.15 from python.org (with the "py" launcher, the
# installer's default). Makes its own .venv in the project folder the first
# time; later runs reuse it. -Python picks a version (e.g. -Python 3.13).
# -SkipSmokeTest skips the check at the end that opens the window once.

param(
    [string]$Python = "3",
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Step "Creating .venv (Python $Python)"
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw "The 'py' launcher isn't installed -- install Python from https://www.python.org/downloads/windows/ (keep 'py launcher' ticked)."
    }
    & py "-$Python" -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "couldn't create .venv with 'py -$Python'" }
}

Step "Installing requirements + PyInstaller"
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install -r requirements.txt pyinstaller --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Step "Building with PyInstaller"
& $VenvPy -m PyInstaller --noconfirm packaging\eq-log-suite.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$AppDir = Join-Path $Root "dist\EQ Log Suite"
$Exe = Join-Path $AppDir "EQ Log Suite.exe"
if (-not (Test-Path $Exe)) { throw "build finished but $Exe isn't there" }

if (-not $SkipSmokeTest) {
    # The exe has no console, so its output goes to app.log in the per-user
    # data folder (packaging\launch.py). EQLS_SMOKE_TEST makes it open the
    # window, check the page rendered and can talk to the app, then exit.
    Step "Smoke test (a window opens and closes by itself)"
    $Log = Join-Path $env:LOCALAPPDATA "EQ Log Suite\logs\app.log"
    $Before = if (Test-Path $Log) { (Get-Item $Log).Length } else { 0 }
    $env:EQLS_SMOKE_TEST = "1"
    try {
        $p = Start-Process -FilePath $Exe -PassThru
        if (-not $p.WaitForExit(90000)) { $p.Kill(); throw "smoke test: the app didn't exit within 90s" }
    } finally {
        Remove-Item Env:\EQLS_SMOKE_TEST
    }
    $Out = ""
    if (Test-Path $Log) {
        $fs = [IO.File]::Open($Log, 'Open', 'Read', 'ReadWrite')
        $fs.Seek($Before, 'Begin') | Out-Null
        $Out = (New-Object IO.StreamReader($fs)).ReadToEnd()
        $fs.Close()
    }
    Write-Host $Out
    if ($Out -notmatch "SMOKE TEST: loaded=True .*bridge='object'") {
        throw "smoke test failed -- see the output above (from $Log)"
    }
    Write-Host "Smoke test passed." -ForegroundColor Green
}

Step "Zipping"
$Version = $null
if (Get-Command git -ErrorAction SilentlyContinue) {
    try { $Version = (& git describe --tags --always 2>$null) } catch { }   # PS 5.1 throws on redirected stderr
}
if (-not $Version) { $Version = Get-Date -Format "yyyyMMdd" }
$Zip = Join-Path $Root "dist\EQ-Log-Suite-windows-$Version.zip"
if (Test-Path $Zip) { Remove-Item $Zip }
Compress-Archive -Path $AppDir -DestinationPath $Zip

Write-Host "`nDone." -ForegroundColor Green
Write-Host "  App: $Exe"
Write-Host "  Zip: $Zip"
