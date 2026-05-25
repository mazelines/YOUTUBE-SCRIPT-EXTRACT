"""Core extraction logic (transcripts + MP3 audio). No GUI dependencies live
here so this module stays unit-testable and reusable from a CLI or other
front-ends."""

from __future__ import annotations

import re
import json
import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request
from urllib.error import URLError

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    CouldNotRetrieveTranscript,
)


# --------------------------------------------------------------------------- #
# URL / video-id parsing
# --------------------------------------------------------------------------- #

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


class ExtractionError(Exception):
    """Raised when a transcript cannot be extracted, with a friendly message."""


def extract_video_id(url_or_id: str) -> str:
    """Return the 11-char YouTube video id from a URL or a bare id.

    Handles watch?v=, youtu.be/, /embed/, /shorts/, /live/, and raw ids.
    Raises ExtractionError when nothing video-id-like can be found.
    """
    s = (url_or_id or "").strip()
    if not s:
        raise ExtractionError("빈 입력입니다.")

    # Bare id.
    if _VIDEO_ID_RE.match(s):
        return s

    if "://" not in s:
        s = "https://" + s  # let urlparse handle e.g. "youtu.be/xxxx"

    parsed = urlparse(s)
    host = parsed.netloc.lower()
    if host not in _HOSTS:
        # Last resort: maybe an 11-char id is embedded somewhere.
        m = re.search(r"([A-Za-z0-9_-]{11})", s)
        if m:
            return m.group(1)
        raise ExtractionError(f"YouTube URL이 아닙니다: {url_or_id}")

    # youtu.be/<id>
    if host.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
        if _VIDEO_ID_RE.match(candidate):
            return candidate

    # /watch?v=<id>
    qs = parse_qs(parsed.query)
    if "v" in qs and _VIDEO_ID_RE.match(qs["v"][0]):
        return qs["v"][0]

    # /embed/<id>, /shorts/<id>, /live/<id>, /v/<id>
    parts = [p for p in parsed.path.split("/") if p]
    for i, part in enumerate(parts):
        if part in ("embed", "shorts", "live", "v") and i + 1 < len(parts):
            if _VIDEO_ID_RE.match(parts[i + 1]):
                return parts[i + 1]

    # Fallback: any 11-char token in the path.
    for part in parts:
        if _VIDEO_ID_RE.match(part):
            return part

    raise ExtractionError(f"영상 ID를 찾을 수 없습니다: {url_or_id}")


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #

@dataclass
class VideoMeta:
    video_id: str
    title: str
    author: str = ""


def fetch_video_meta(video_id: str, timeout: float = 10.0) -> VideoMeta:
    """Fetch the video title/author cheaply via YouTube's oEmbed endpoint.

    Falls back to the video id as the title if the request fails (e.g. offline
    or age-restricted), so extraction can still proceed.
    """
    oembed = (
        "https://www.youtube.com/oembed?url="
        f"https://www.youtube.com/watch?v={video_id}&format=json"
    )
    try:
        req = Request(oembed, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return VideoMeta(
            video_id=video_id,
            title=data.get("title") or video_id,
            author=data.get("author_name", ""),
        )
    except (URLError, ValueError, OSError, json.JSONDecodeError):
        return VideoMeta(video_id=video_id, title=video_id, author="")


# --------------------------------------------------------------------------- #
# Transcript fetching
# --------------------------------------------------------------------------- #

@dataclass
class TranscriptResult:
    meta: VideoMeta
    language: str
    language_code: str
    is_generated: bool
    snippets: list = field(default_factory=list)  # list of (text, start, duration)


def _select_transcript(transcript_list, preferred_langs, prefer_manual):
    """Pick the best transcript from a TranscriptList following preferences.

    Order: manual(preferred) -> generated(preferred) -> manual(any) ->
    generated(any) -> translate-to-first-preferred. Returns a Transcript.
    """
    langs = [l.strip() for l in preferred_langs if l.strip()]

    if prefer_manual:
        order = [
            lambda: transcript_list.find_manually_created_transcript(langs),
            lambda: transcript_list.find_generated_transcript(langs),
        ]
    else:
        order = [lambda: transcript_list.find_transcript(langs)]

    if langs:
        for finder in order:
            try:
                return finder()
            except (NoTranscriptFound, Exception):
                continue

    # Fall back to whatever exists, preferring manual.
    manual, generated = [], []
    for t in transcript_list:
        (generated if t.is_generated else manual).append(t)
    for pool in (manual, generated):
        if pool:
            chosen = pool[0]
            # Try to translate into a preferred language when possible.
            if langs and chosen.is_translatable:
                target = langs[0]
                if any(tl.language_code == target
                       for tl in chosen.translation_languages):
                    try:
                        return chosen.translate(target)
                    except Exception:
                        pass
            return chosen

    raise NoTranscriptFound(transcript_list.video_id, langs, transcript_list)


def fetch_transcript(
    video_id: str,
    preferred_langs=("ko", "en"),
    prefer_manual: bool = True,
    meta: VideoMeta | None = None,
) -> TranscriptResult:
    """Fetch a transcript for `video_id`, raising ExtractionError on failure."""
    api = YouTubeTranscriptApi()
    try:
        transcript_list = api.list(video_id)
        transcript = _select_transcript(
            transcript_list, list(preferred_langs), prefer_manual
        )
        fetched = transcript.fetch()
    except TranscriptsDisabled:
        raise ExtractionError("이 영상은 자막이 비활성화되어 있습니다.")
    except NoTranscriptFound:
        raise ExtractionError("요청한 언어의 자막을 찾을 수 없습니다.")
    except VideoUnavailable:
        raise ExtractionError("영상을 사용할 수 없습니다 (비공개/삭제됨).")
    except CouldNotRetrieveTranscript as e:
        raise ExtractionError(f"자막을 가져오지 못했습니다: {e}")
    except Exception as e:  # network errors, IP blocks, etc.
        raise ExtractionError(f"자막 추출 실패: {e}")

    snippets = [(s.text, s.start, s.duration) for s in fetched]
    if meta is None:
        meta = VideoMeta(video_id=video_id, title=video_id)
    return TranscriptResult(
        meta=meta,
        language=transcript.language,
        language_code=transcript.language_code,
        is_generated=transcript.is_generated,
        snippets=snippets,
    )


# --------------------------------------------------------------------------- #
# Markdown formatting & saving
# --------------------------------------------------------------------------- #

def format_timestamp(seconds: float) -> str:
    """Seconds -> H:MM:SS or M:SS."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _clean_text(text: str) -> str:
    # Collapse internal newlines/whitespace introduced by line-wrapped captions.
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def build_markdown(result: TranscriptResult, include_timestamps: bool = True) -> str:
    """Render a TranscriptResult as a markdown document string."""
    meta = result.meta
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    kind = "자동 생성" if result.is_generated else "수동 작성"

    lines = [
        f"# {meta.title}",
        "",
        f"- **URL**: {canonical_url(meta.video_id)}",
        f"- **영상 ID**: {meta.video_id}",
    ]
    if meta.author:
        lines.append(f"- **채널**: {meta.author}")
    lines += [
        f"- **자막 언어**: {result.language} (`{result.language_code}`, {kind})",
        f"- **추출 시각**: {now}",
        "",
        "---",
        "",
        "## Transcript",
        "",
    ]

    if include_timestamps:
        for text, start, _dur in result.snippets:
            clean = _clean_text(text)
            if clean:
                lines.append(f"`[{format_timestamp(start)}]` {clean}")
    else:
        paragraph = " ".join(
            _clean_text(t) for t, _s, _d in result.snippets if _clean_text(t)
        )
        lines.append(paragraph)

    lines.append("")
    return "\n".join(lines)


_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(title: str, video_id: str, max_len: int = 80) -> str:
    """Build a filesystem-safe markdown filename from a title + video id."""
    base = _INVALID_FS.sub("_", title).strip().strip(".")
    base = re.sub(r"\s+", " ", base)
    if len(base) > max_len:
        base = base[:max_len].rstrip()
    if not base:
        base = "transcript"
    return f"{base} [{video_id}].md"


def save_markdown(content: str, out_dir: str | Path, filename: str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / filename
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# One-shot convenience used by the GUI worker
# --------------------------------------------------------------------------- #

def extract_to_markdown(
    url_or_id: str,
    out_dir: str | Path,
    preferred_langs=("ko", "en"),
    prefer_manual: bool = True,
    include_timestamps: bool = True,
    progress=None,
) -> Path:
    """Full pipeline: parse -> meta -> transcript -> markdown -> file.

    `progress` is an optional callable(str) for status messages. Returns the
    written file path. Raises ExtractionError on any handled failure.
    """
    def report(msg):
        if progress:
            progress(msg)

    report("영상 ID 분석 중…")
    video_id = extract_video_id(url_or_id)

    report("영상 정보 가져오는 중…")
    meta = fetch_video_meta(video_id)

    report("자막 추출 중…")
    result = fetch_transcript(
        video_id, preferred_langs, prefer_manual, meta=meta
    )

    report("마크다운 저장 중…")
    content = build_markdown(result, include_timestamps=include_timestamps)
    filename = safe_filename(meta.title, video_id)
    return save_markdown(content, out_dir, filename)


# --------------------------------------------------------------------------- #
# MP3 audio extraction (yt-dlp + bundled ffmpeg)
# --------------------------------------------------------------------------- #

def _ffmpeg_location():
    """Return a directory containing an ffmpeg binary yt-dlp can use, or None.

    Prefers the binary bundled by imageio-ffmpeg so users don't have to install
    ffmpeg system-wide. That binary has a versioned name (e.g.
    ``ffmpeg-win-x86_64-v7.1.exe``) which yt-dlp won't recognise, so we copy it
    once into a cache dir under a plain ``ffmpeg``/``ffmpeg.exe`` name. Returns
    None to let yt-dlp fall back to whatever ffmpeg is on PATH.
    """
    import shutil
    import tempfile

    try:
        import imageio_ffmpeg
        src = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if not src.exists():
            return None
        exe_name = "ffmpeg.exe" if src.suffix.lower() == ".exe" else "ffmpeg"
        shim_dir = Path(tempfile.gettempdir()) / "yt_extractor_ffmpeg"
        shim_dir.mkdir(parents=True, exist_ok=True)
        dst = shim_dir / exe_name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
        return str(shim_dir)
    except Exception:
        return None  # let yt-dlp find ffmpeg on PATH


def extract_audio_mp3(
    url_or_id: str,
    out_dir: str | Path,
    bitrate: str = "192",
    progress=None,
) -> Path:
    """Download a video's audio and convert it to MP3.

    `bitrate` is a kbps string ("128", "192", "320"). `progress` is an optional
    callable(str). Returns the written .mp3 path. Raises ExtractionError.
    """
    try:
        import yt_dlp
    except ImportError:
        raise ExtractionError(
            "MP3 추출에는 yt-dlp가 필요합니다 (pip install yt-dlp)."
        )

    def report(msg):
        if progress:
            progress(msg)

    report("영상 ID 분석 중…")
    video_id = extract_video_id(url_or_id)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Track the final filename reported by the postprocessor.
    final_path = {"path": None}

    def progress_hook(d):
        status = d.get("status")
        if status == "downloading":
            pct = d.get("_percent_str", "").strip()
            spd = d.get("_speed_str", "").strip()
            report(f"음원 다운로드 중… {pct} {spd}".strip())
        elif status == "finished":
            report("MP3로 변환 중…")

    def postproc_hook(d):
        if d.get("status") == "finished":
            info = d.get("info_dict") or {}
            fp = info.get("filepath")
            if fp:
                final_path["path"] = fp

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out / "%(title)s [%(id)s].%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(bitrate),
        }],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postproc_hook],
    }
    ffloc = _ffmpeg_location()
    if ffloc:
        ydl_opts["ffmpeg_location"] = ffloc

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(canonical_url(video_id), download=True)
    except Exception as e:
        msg = str(e)
        if "ffmpeg" in msg.lower() or "ffprobe" in msg.lower():
            raise ExtractionError(
                "MP3 변환을 위한 ffmpeg를 찾을 수 없습니다. "
                "`pip install imageio-ffmpeg`로 해결하거나 ffmpeg를 설치하세요."
            )
        raise ExtractionError(f"음원 추출 실패: {msg}")

    # Resolve the produced .mp3 path.
    path = final_path["path"]
    if not path:
        # Derive from the template using the info dict.
        title = (info or {}).get("title", video_id)
        base = out / safe_filename(title, video_id)
        path = str(base.with_suffix(".mp3"))
    mp3_path = Path(path).with_suffix(".mp3")
    if not mp3_path.exists():
        raise ExtractionError("MP3 파일이 생성되지 않았습니다.")

    report("MP3 저장 완료")
    return mp3_path
