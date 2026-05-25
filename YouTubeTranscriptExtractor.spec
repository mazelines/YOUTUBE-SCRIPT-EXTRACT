# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the YouTube 자막 추출기 release build.

Bundles, in addition to the Python code and PySide6:
  - the MazeLine banner image (yt_extractor/img/),
  - the ffmpeg binary shipped by imageio-ffmpeg (so MP3 works with no
    separate ffmpeg install),
  - aria2c for faster (multi-connection) downloads, IF you drop the binary
    at yt_extractor/bin/aria2c.exe (optional — yt-dlp falls back to its own
    concurrent-fragment downloader when it's absent),
  - all yt-dlp submodules (its extractors are imported dynamically).

Build:  pyinstaller YouTubeTranscriptExtractor.spec
Output: dist/YouTubeTranscriptExtractor.exe  (single file, no console)
"""

import os

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

datas = [("yt_extractor/img/mazelinebanner.jpg", "yt_extractor/img")]
datas += collect_data_files("imageio_ffmpeg")        # ffmpeg binary

# aria2c: bundled only when present so the build never fails on a fresh clone.
# Extracted to <_MEIPASS>/bin at runtime, where _ensure_aria2c_on_path finds it.
_aria2c = os.path.join("yt_extractor", "bin", "aria2c.exe")
if os.path.exists(_aria2c):
    datas += [(_aria2c, "bin")]

hiddenimports = collect_submodules("yt_dlp")          # dynamic extractors

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="YouTubeTranscriptExtractor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
