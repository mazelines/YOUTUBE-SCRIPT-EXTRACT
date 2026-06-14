# Build the Inno Setup installer from the existing PyInstaller onedir bundle,
# then compress the resulting setup.exe into dist\YouTubeTranscriptExtractor.zip
# (the distribution artifact name the FTP deploy expects).
#
# Run PyInstaller first (e.g. pyinstaller YouTubeTranscriptExtractor.spec) so
# dist\YouTubeTranscriptExtractor\ exists. Version comes from
# yt_extractor/__init__.py unless overridden with -Version.
#
# Usage:
#   .\scripts\build_installer.ps1
#   .\scripts\build_installer.ps1 -Version 1.2.3

[CmdletBinding()]
param([string]$Version)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $Version) {
    $init = Get-Content "yt_extractor\__init__.py" -Raw
    if ($init -notmatch '__version__\s*=\s*["'']([^"'']+)["'']') {
        throw "yt_extractor\__init__.py에서 __version__을 찾지 못했습니다."
    }
    $Version = $Matches[1]
}

$bundle = "dist\YouTubeTranscriptExtractor"
if (-not (Test-Path $bundle)) {
    throw "onedir 번들이 없습니다: $bundle`n먼저 PyInstaller 빌드를 하세요 (pyinstaller YouTubeTranscriptExtractor.spec)."
}

$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "ISCC.exe를 찾지 못했습니다. Inno Setup을 설치하세요 (winget install JRSoftware.InnoSetup)." }

Write-Host "==> ISCC build (v$Version)" -ForegroundColor Cyan
& $iscc "/DMyAppVersion=$Version" "installer\YouTubeTranscriptExtractor.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC 빌드 실패 (exit $LASTEXITCODE)" }

$setup = "dist\installer\YouTubeTranscriptExtractor-Setup-$Version.exe"
if (-not (Test-Path $setup)) { throw "빌드된 설치 파일이 없습니다: $setup" }

$zip = "dist\YouTubeTranscriptExtractor.zip"
if (Test-Path $zip) { Remove-Item $zip }
Write-Host "==> Zipping installer -> $zip" -ForegroundColor Cyan
Compress-Archive -Path $setup -DestinationPath $zip

$s = (Get-Item $setup).Length / 1MB
$z = (Get-Item $zip).Length / 1MB
Write-Host ("OK  setup.exe {0:N1} MB  ->  {1} ({2:N1} MB)" -f $s, $zip, $z) -ForegroundColor Green
