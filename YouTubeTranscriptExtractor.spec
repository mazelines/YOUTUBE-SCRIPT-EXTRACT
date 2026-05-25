# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the YouTube 자막 추출기 release build.

Bundles, in addition to the Python code and PySide6:
  - the MazeLine banner image (yt_extractor/img/),
  - the ffmpeg binary shipped by imageio-ffmpeg (so MP3 works with no
    separate ffmpeg install),
  - all yt-dlp submodules (its extractors are imported dynamically).

Build:  pyinstaller YouTubeTranscriptExtractor.spec
Output: dist/YouTubeTranscriptExtractor.exe  (single file, no console)
"""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

datas = [("yt_extractor/img/mazelinebanner.jpg", "yt_extractor/img")]
datas += collect_data_files("imageio_ffmpeg")        # ffmpeg binary

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
