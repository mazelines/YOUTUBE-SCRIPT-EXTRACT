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

from PyInstaller.utils.hooks import (
    collect_submodules, collect_data_files, collect_dynamic_libs,
)

datas = [("yt_extractor/img/mazelinebanner.jpg", "yt_extractor/img")]
datas += collect_data_files("imageio_ffmpeg")        # ffmpeg binary

hiddenimports = collect_submodules("yt_dlp")          # dynamic extractors

# Chat HTML renderer: markdown loads extensions by name string and pygments
# loads its style/lexer modules dynamically — PyInstaller's static scan misses
# both, so bundle the whole packages.
hiddenimports += collect_submodules("markdown")
hiddenimports += collect_submodules("pygments")

# Built-in CPU LLM: bundle llama-cpp-python's compiled library + data, but only
# if it's installed (keeps builds working on clones without the optional dep).
# The GGUF model is NOT bundled — it's downloaded from Hugging Face on first use.
binaries = []
try:
    binaries += collect_dynamic_libs("llama_cpp")
    datas += collect_data_files("llama_cpp")
    hiddenimports += collect_submodules("llama_cpp")
except Exception:
    pass

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
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
    [],
    name="YouTubeTranscriptExtractor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="YouTubeTranscriptExtractor",
)
