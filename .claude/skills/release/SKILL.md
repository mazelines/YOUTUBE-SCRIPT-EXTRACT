---
name: release
description: Build the YouTube 자막 추출기 release exe with PyInstaller and publish it to the shared Google Drive folder. Use when the user asks to release, publish, ship, deploy, upload to Drive, or "릴리스/배포/업로드" the app — anything that means "make a build users can download". The Drive folder is mounted locally via Google Drive Desktop at I:\GoogleDireve\PublicShare\YoutubeExtractor, so publishing is a local file copy (no API/auth).
---

# Release skill

Run the project's release pipeline: clean → PyInstaller build → `--selftest` verification → copy to the Google Drive shared folder.

## How to invoke

Run the PowerShell script from the project root:

```powershell
.\scripts\release.ps1
```

Add `-Versioned` if the user wants a timestamped archive copy in addition to the `latest` overwrite:

```powershell
.\scripts\release.ps1 -Versioned
```

Use `-SkipBuild` when the user just wants to re-publish the existing `dist/` (e.g. they already built and only the copy step is missing).

## What the script does

1. Wipes `build/` and `dist/`.
2. Runs `pyinstaller YouTubeTranscriptExtractor.spec --noconfirm`.
3. Launches the built exe with `--selftest` (windowed mode writes the result to `SELFTEST_OUT`) and aborts if it doesn't print `SELFTEST OK`.
4. Copies `dist\YouTubeTranscriptExtractor.exe` to `I:\GoogleDireve\PublicShare\YoutubeExtractor\YouTubeTranscriptExtractor.exe`. Google Drive Desktop then syncs it to the shared folder ID `1om09imjPWcrBJSUFYO87FJ4ibHnfNbgp` (https://drive.google.com/drive/folders/1om09imjPWcrBJSUFYO87FJ4ibHnfNbgp).

## When to stop and ask

- The Drive target is missing (e.g. Drive Desktop isn't running, or the drive letter changed). Report the missing path; don't try to guess a new letter.
- Selftest fails — don't publish. Surface the failure to the user.
- The user invokes the skill while uncommitted work-in-progress is in the tree. Confirm they want to publish anyway.

## Reporting back

After a successful run, tell the user:

- The exe size (so they can spot accidental bloat — typical is ~135 MB).
- That Drive Desktop will propagate the upload within a minute.
- The folder URL for verification.
