# bin/ — bundled native binaries

Drop optional native executables here so the release build (and a source run)
can find them. These are **not** committed to the repo (see `.gitignore`); each
developer/CI fetches them locally.

## aria2c (faster MP3 downloads)

`aria2c` opens many parallel connections, which is the biggest win for MP3
extraction speed (the download is the bottleneck — MP3 encoding is CPU-only and
near-instant). It is **optional**: when absent, yt-dlp falls back to its own
concurrent-fragment downloader, so nothing breaks.

To enable it:

1. Get `aria2c.exe` (Windows x86_64):
   - `winget install aria2.aria2`, or `choco install aria2`, then copy the exe
     here; **or**
   - download a release from https://github.com/aria2/aria2/releases and copy
     `aria2c.exe` into this folder.
2. Place it at `yt_extractor/bin/aria2c.exe`.

At runtime `core._ensure_aria2c_on_path()` resolves it in this order:
PATH → `<_MEIPASS>/bin` (frozen build) → this folder. The PyInstaller spec
bundles `aria2c.exe` into the one-file build automatically **if** it is present
here at build time.
