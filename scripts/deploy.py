"""FTP-deploy the YouTubeTranscriptExtractor release.

The release artifact `dist/YouTubeTranscriptExtractor.zip` (the installer,
produced by `scripts/build_installer.ps1`) must already exist. This script does
NOT build or repack it; it only:
    1. manifest write dist/latest.yml (version from yt_extractor/__init__.py,
       plus the zip's size + sha512 and a release timestamp)
    2. upload  latest.yml + the zip to the FTP host

Credentials and the upload list come from `ftp-config.json` (gitignored — copy
`ftp-config.example.json` and fill in the real password). Plain FTP by default
(`"secure": false`); set `"secure": true` for FTPS (FTP over TLS).

Usage (from repo root):
    py -3.10 scripts/deploy.py            # manifest + upload
    py -3.10 scripts/deploy.py --check    # login + list only
    py -3.10 scripts/deploy.py --dry-run  # manifest only, no upload

Config keys (ftp-config.json):
    host        FTP server hostname            e.g. "mazeline.tech"
    user        FTP username
    password    FTP password
    remotePath  remote directory to upload into (default "/")
    secure      true -> FTPS (FTP_TLS), false -> plain FTP
    uploads     local file paths to upload (relative to repo root)
"""

from __future__ import annotations

import argparse
import datetime
import ftplib
import hashlib
import json
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG = _REPO_ROOT / "ftp-config.json"
_EXAMPLE = "ftp-config.example.json"

_ZIP = _REPO_ROOT / "dist" / "YouTubeTranscriptExtractor.zip"
_MANIFEST = _REPO_ROOT / "dist" / "latest.yml"


def _human(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb:.1f} MB" if mb >= 1 else f"{n / 1024:.0f} KB"


def _app_version() -> str:
    init = _REPO_ROOT / "yt_extractor" / "__init__.py"
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', init.read_text("utf-8"))
    if not m:
        raise SystemExit("yt_extractor/__init__.py에서 __version__을 찾지 못했습니다.")
    return m.group(1)


def _sha512(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_manifest() -> None:
    if not _ZIP.is_file():
        raise SystemExit(
            f"릴리스 zip이 없습니다: {_ZIP}\n"
            "먼저 인스톨러를 빌드하세요: scripts\\build_installer.ps1"
        )
    version = _app_version()
    size = _ZIP.stat().st_size
    sha = _sha512(_ZIP)
    released = datetime.datetime.now().isoformat(timespec="seconds")
    text = (
        f"version: {version}\n"
        f"zip: YouTubeTranscriptExtractor.zip\n"
        f"size: {size}\n"
        f"sha512: {sha}\n"
        f"releaseDate: '{released}'\n"
    )
    _MANIFEST.write_text(text, encoding="utf-8")
    print(f"매니페스트: {_MANIFEST.relative_to(_REPO_ROOT)} (version {version})")


def _load_config(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(
            f"설정 파일이 없습니다: {path}\n"
            f"`{_EXAMPLE}`를 복사해 `{path.name}`로 만들고 비밀번호를 채우세요."
        )
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"설정 파일 JSON 파싱 실패: {path}\n{e}")
    for key in ("host", "user", "password"):
        if not cfg.get(key):
            raise SystemExit(f"설정에 '{key}'가 비어 있습니다: {path}")
    cfg.setdefault("remotePath", "/")
    cfg.setdefault("secure", False)
    cfg.setdefault("uploads", [])
    return cfg


def _ensure_remote_dir(ftp: ftplib.FTP, path: str) -> None:
    if path.startswith("/"):
        try:
            ftp.cwd("/")
        except ftplib.all_errors:
            pass
    for seg in (s for s in path.split("/") if s):
        try:
            ftp.cwd(seg)
        except ftplib.all_errors:
            ftp.mkd(seg)
            ftp.cwd(seg)


def _connect(cfg: dict) -> ftplib.FTP:
    cls = ftplib.FTP_TLS if cfg["secure"] else ftplib.FTP
    ftp = cls()
    ftp.connect(cfg["host"], timeout=30)
    ftp.login(cfg["user"], cfg["password"])
    if isinstance(ftp, ftplib.FTP_TLS):
        ftp.prot_p()
    _ensure_remote_dir(ftp, cfg["remotePath"] or "/")
    return ftp


def _upload(ftp: ftplib.FTP, local: Path) -> None:
    size = local.stat().st_size
    sent = 0
    last_pct = -1

    def _cb(block: bytes) -> None:
        nonlocal sent, last_pct
        sent += len(block)
        pct = int(sent * 100 / size) if size else 100
        if pct != last_pct:
            last_pct = pct
            print(f"\r  {local.name}: {pct:3d}%  ({_human(sent)}/{_human(size)})",
                  end="", flush=True)

    with local.open("rb") as fh:
        ftp.storbinary(f"STOR {local.name}", fh, blocksize=65536, callback=_cb)
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FTP-deploy YouTubeTranscriptExtractor release (latest.yml + installer zip).")
    ap.add_argument("files", nargs="*", help="업로드 목록 직접 지정 (repo root 기준 상대 경로)")
    ap.add_argument("--config", default=str(_DEFAULT_CONFIG), help="ftp-config.json 경로")
    ap.add_argument("--check", action="store_true", help="로그인 후 원격 디렉터리 목록만 출력")
    ap.add_argument("--dry-run", action="store_true", help="manifest만, 업로드 안 함")
    args = ap.parse_args(argv)

    cfg = _load_config(Path(args.config))

    if not args.check:
        _write_manifest()

    upload_names = args.files or cfg["uploads"]
    targets: list[Path] = []
    if not args.check:
        if not upload_names:
            raise SystemExit("업로드할 파일이 없습니다. ftp-config.json의 'uploads'를 채우세요.")
        for name in upload_names:
            p = Path(name)
            if not p.is_absolute():
                p = _REPO_ROOT / p
            if not p.is_file():
                raise SystemExit(f"업로드 대상 파일이 없습니다: {p}")
            targets.append(p)

    scheme = "FTPS" if cfg["secure"] else "FTP"
    print(f"[{scheme}] {cfg['user']}@{cfg['host']}{cfg['remotePath']}")

    if args.dry_run:
        print("DRY-RUN - 연결하지 않습니다. 업로드 예정:")
        for t in targets:
            print(f"  {t.relative_to(_REPO_ROOT)}  ({_human(t.stat().st_size)})")
        return 0

    try:
        ftp = _connect(cfg)
    except ftplib.all_errors as e:
        raise SystemExit(f"FTP 연결/로그인 실패: {e}")

    try:
        if args.check:
            print("로그인 성공. 원격 디렉터리 목록:")
            ftp.retrlines("LIST")
            return 0
        for t in targets:
            print(f"업로드 중: {t.relative_to(_REPO_ROOT)} ({_human(t.stat().st_size)})")
            _upload(ftp, t)
        print("배포 완료 [OK]")
        return 0
    except ftplib.all_errors as e:
        raise SystemExit(f"FTP 작업 실패: {e}")
    finally:
        try:
            ftp.quit()
        except ftplib.all_errors:
            ftp.close()


if __name__ == "__main__":
    raise SystemExit(main())
