# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the YouTube 자막 추출기 release build.

Bundles, in addition to the Python code and PySide6:
  - the MazeLine banner image (yt_extractor/img/),
  - the ffmpeg binary shipped by imageio-ffmpeg (so MP3 works with no
    separate ffmpeg install),
  - all yt-dlp submodules (its extractors are imported dynamically).

Build:  pyinstaller YouTubeTranscriptExtractor.spec
Output: dist/YouTubeTranscriptExtractor/ (folder bundle, no console)
"""

from PyInstaller.utils.hooks import (
    collect_submodules, collect_data_files, collect_dynamic_libs,
)
from pathlib import Path
import shutil
import subprocess
from urllib.request import urlopen

datas = [("yt_extractor/img/mazelinebanner.jpg", "yt_extractor/img")]
datas += [("yt_extractor/img/chevron-down.svg", "yt_extractor/img"),
          ("yt_extractor/img/chevron-up.svg", "yt_extractor/img")]
datas += collect_data_files("imageio_ffmpeg")        # ffmpeg binary

hiddenimports = collect_submodules("yt_dlp")          # dynamic extractors
hiddenimports += collect_submodules("yt_dlp_ejs")
datas += collect_data_files("yt_dlp_ejs")

# YouTube player challenges require a JS runtime even in a frozen build.
# Ship Node and its exact-version license so end users need no extra install.
node = shutil.which("node")
if not node:
    raise RuntimeError("Install Node.js 22+ and add it to PATH before building.")
node_version = subprocess.check_output([node, "--version"], text=True).strip()
if int(node_version.lstrip("v").split(".")[0]) < 22:
    raise RuntimeError("Node.js 22+ is required for YouTube downloads.")
license_path = Path(".zcode") / "node-runtime" / node_version / "LICENSE"
if not license_path.exists():
    license_path.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(f"https://raw.githubusercontent.com/nodejs/node/{node_version}/LICENSE", timeout=30) as resp:
        license_path.write_bytes(resp.read())
datas += [(str(license_path), "node")]

# Chat HTML renderer: markdown loads extensions by name string and pygments
# loads its style/lexer modules dynamically — PyInstaller's static scan misses
# both, so bundle the whole packages.
hiddenimports += collect_submodules("markdown")
hiddenimports += collect_submodules("pygments")

# Built-in CPU LLM: bundle llama-cpp-python's compiled library + data, but only
# if it's installed (keeps builds working on clones without the optional dep).
# The GGUF model is NOT bundled — it's downloaded from Hugging Face on first use.
binaries = [(node, "node")]
try:
    binaries += collect_dynamic_libs("llama_cpp")
    datas += collect_data_files("llama_cpp")
    hiddenimports += collect_submodules("llama_cpp")
except Exception:
    pass

# STT fallback (pywhispercpp / whisper.cpp): same optional-dep pattern as
# llama_cpp. Only bundled when installed. The Whisper GGML model is NOT
# bundled — downloaded from Hugging Face on first use. Our build links
# whisper.cpp+ggml statically into the extension module (no separate ggml
# DLLs — that's deliberate, to avoid DLL-name collisions with llama_cpp's
# own ggml copies), so collecting the package itself is sufficient.
try:
    binaries += collect_dynamic_libs("pywhispercpp")
    datas += collect_data_files("pywhispercpp")
    hiddenimports += collect_submodules("pywhispercpp")
    hiddenimports += ["_pywhispercpp"]
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
