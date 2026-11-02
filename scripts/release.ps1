# Release pipeline: clean -> build -> selftest -> local ZIP.
# Usage:
#   .\scripts\release.ps1             # build, verify, package in dist/
#   .\scripts\release.ps1 -Versioned  # also create a timestamped ZIP in dist/
#   .\scripts\release.ps1 -SkipBuild  # verify and repackage existing dist/

[CmdletBinding()]
param(
    [string]$AppName    = 'YouTubeTranscriptExtractor',
    [string]$SpecFile   = 'YouTubeTranscriptExtractor.spec',
    [switch]$Versioned,
    [switch]$SkipBuild,
    [switch]$SkipSelftest
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "OK  $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "ERR $msg" -ForegroundColor Red }

# --- 1) Clean previous build artifacts ---------------------------------------
if (-not $SkipBuild) {
    Write-Step 'Cleaning build/ and dist/'
    foreach ($d in @('build', 'dist')) {
        $target = [IO.Path]::GetFullPath((Join-Path $projectRoot $d))
        if ((Split-Path -Parent $target) -ne [IO.Path]::GetFullPath($projectRoot)) {
            throw "Cleanup target outside project: $target"
        }
        if (Test-Path -LiteralPath $target) {
            if ((Get-Item -LiteralPath $target).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing to clean a linked directory: $target"
            }
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }
}

# --- 2) PyInstaller build (onedir) -------------------------------------------
if (-not $SkipBuild) {
    Write-Step "PyInstaller $SpecFile"
    & pyinstaller $SpecFile --noconfirm --log-level WARN
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "PyInstaller failed (exit $LASTEXITCODE)"
        exit 1
    }
}

# The real exe lives inside the onedir bundle folder.
$bundleDir = Join-Path $projectRoot "dist\$AppName"
$exePath   = Join-Path $bundleDir "$AppName.exe"
if (-not (Test-Path $exePath)) {
    Write-Fail "Built exe not found: $exePath"
    exit 1
}

$exeSize = (Get-Item $exePath).Length
$dirSize = (Get-ChildItem $bundleDir -Recurse | Measure-Object -Property Length -Sum).Sum
Write-Ok ("Bundle: {0} ({1:N1} MB total, exe stub {2:N1} MB)" -f $bundleDir, ($dirSize / 1MB), ($exeSize / 1MB))

# --- 3) Selftest the built exe -----------------------------------------------
if (-not $SkipSelftest) {
    Write-Step 'Running --selftest against the built exe'
    $selftestOut = Join-Path $projectRoot '_selftest_release.txt'
    if (Test-Path $selftestOut) { Remove-Item $selftestOut }
    $env:SELFTEST_OUT = $selftestOut
    Start-Process -FilePath $exePath -ArgumentList '--selftest' -WindowStyle Hidden -Wait
    Remove-Item Env:\SELFTEST_OUT
    if (-not (Test-Path $selftestOut)) {
        Write-Fail 'Selftest produced no result file'
        exit 1
    }
    $result = Get-Content $selftestOut -Raw
    Write-Host $result
    Remove-Item $selftestOut
    if ($result -notmatch 'SELFTEST OK') {
        Write-Fail 'Selftest did not pass — aborting packaging'
        exit 1
    }
    Write-Ok 'Selftest passed'
}

# --- 4) Zip the bundle folder ------------------------------------------------
$zipName = "$AppName.zip"
$zipPath = Join-Path $projectRoot "dist\$zipName"
Write-Step "Zipping bundle -> $zipPath"
if (Test-Path $zipPath) { Remove-Item $zipPath }
Compress-Archive -Path "$bundleDir\*" -DestinationPath $zipPath
$zipSize = (Get-Item $zipPath).Length
Write-Ok ("Zip: {0} ({1:N1} MB)" -f $zipPath, ($zipSize / 1MB))

# --- 5) Optional local archive ----------------------------------------------
if ($Versioned) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $archivePath = Join-Path $projectRoot "dist\$AppName-$stamp.zip"
    Copy-Item -LiteralPath $zipPath -Destination $archivePath -Force
    Write-Ok "Archived: $archivePath"
}

# --- 6) Report ---------------------------------------------------------------
$zipItem = Get-Item -LiteralPath $zipPath
Write-Host ''
Write-Host 'Release package ready' -ForegroundColor Green
Write-Host ("  File : {0}" -f $zipItem.FullName)
Write-Host ("  Size : {0:N1} MB" -f ($zipItem.Length / 1MB))
Write-Host ("  Time : {0}" -f $zipItem.LastWriteTime)
