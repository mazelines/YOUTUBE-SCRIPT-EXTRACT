---
name: release
description: Build and verify the YouTube transcript extractor Windows EXE with PyInstaller, then create a local ZIP. Use for release, build, package, or deployment preparation requests.
---

# Release skill

Run the local release pipeline from the project root:

```powershell
.\.venv\Scripts\Activate.ps1
.\scripts\release.ps1
```

The environment must have PyInstaller installed. The script cleans `build/` and
`dist/`, builds `YouTubeTranscriptExtractor.spec`, runs the EXE with `--selftest`,
and creates `dist/YouTubeTranscriptExtractor.zip` containing the EXE and its
required libraries. A failed selftest must stop packaging.

- Use `-SkipBuild` to verify and repackage an existing bundle.
- Use `-Versioned` to also save a timestamped ZIP inside local `dist/`.
- Report the local ZIP path, its size, and the selftest result.

Release ends with local artifacts. Do not copy to a Drive folder, upload to
cloud storage, or wait for cloud synchronization as part of this procedure.
Git commits, pushes, GitHub Releases, and the legacy FTP tool are separate
operations, performed only when the user explicitly requests them.
