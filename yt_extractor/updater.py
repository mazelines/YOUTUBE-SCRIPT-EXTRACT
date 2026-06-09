"""Background update checker — fetches latest.yml from the update server."""

from __future__ import annotations

import urllib.request

from PySide6.QtCore import QThread, Signal

from yt_extractor import __version__

_UPDATE_URL = (
    "https://mazeline.tech/updates/youtubeTranscriptExtractor/latest.yml"
)


def _ver_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except ValueError:
        return (0,)


def _parse_version(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None


class UpdateChecker(QThread):
    """Emits update_available(new_version_str) when a newer release is found."""

    update_available = Signal(str)

    def run(self) -> None:
        try:
            with urllib.request.urlopen(_UPDATE_URL, timeout=6) as resp:
                text = resp.read().decode("utf-8")
            remote = _parse_version(text)
            if remote and _ver_tuple(remote) > _ver_tuple(__version__):
                self.update_available.emit(remote)
        except Exception:
            pass
