# Release pipeline: clean -> build -> selftest -> zip -> publish to Google Drive.
#
# The Drive folder is mounted locally by Google Drive Desktop, so "upload" is
# just a Copy-Item — no API, no auth. Override the target with -DriveDir if
# the user has Drive mounted on a different letter.
#
# Usage:
#   .\scripts\release.ps1                    # clean build, zip, copy as latest
#   .\scripts\release.ps1 -Versioned         # also drop a timestamped archive
#   .\scripts\release.ps1 -SkipBuild         # just re-zip and re-copy the existing dist/
#   .\scripts\release.ps1 -DriveDir 'X:\...' # override Drive target

[CmdletBinding()]
param(
    [string]$DriveDir   = 'I:\GoogleDireve\PublicShare\YoutubeExtractor',
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
        if (Test-Path $d) { Remove-Item -Recurse -Force $d }
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
    Start-Process -FilePath $exePath -ArgumentList '--selftest' -Wait
    Remove-Item Env:\SELFTEST_OUT
    if (-not (Test-Path $selftestOut)) {
        Write-Fail 'Selftest produced no result file'
        exit 1
    }
    $result = Get-Content $selftestOut -Raw
    Write-Host $result
    Remove-Item $selftestOut
    if ($result -notmatch 'SELFTEST OK') {
        Write-Fail 'Selftest did not pass — aborting publish'
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

# --- 5) Publish to Google Drive (local mount) --------------------------------
if (-not (Test-Path $DriveDir)) {
    Write-Fail "Drive target missing: $DriveDir (is Google Drive Desktop running?)"
    exit 1
}

Write-Step "Copying to $DriveDir"
Copy-Item -Path $zipPath -Destination (Join-Path $DriveDir $zipName) -Force
Write-Ok "Published: $DriveDir\$zipName"

if ($Versioned) {
    $stamp   = Get-Date -Format 'yyyyMMdd-HHmm'
    $archive = "$AppName-$stamp.zip"
    Copy-Item -Path $zipPath -Destination (Join-Path $DriveDir $archive) -Force
    Write-Ok "Archived: $DriveDir\$archive"
}

# --- 6) Report ---------------------------------------------------------------
$driveItem = Get-Item (Join-Path $DriveDir $zipName)
Write-Host ''
Write-Host '── Release published ─────────────────────────────────────────' -ForegroundColor Green
Write-Host ("  File : {0}" -f $driveItem.FullName)
Write-Host ("  Size : {0:N1} MB" -f ($driveItem.Length / 1MB))
Write-Host ("  Time : {0}" -f $driveItem.LastWriteTime)
Write-Host '  Sync : Google Drive Desktop will push to the shared folder automatically.'
Write-Host '──────────────────────────────────────────────────────────────'
