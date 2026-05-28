"""Core extraction logic (transcripts + MP3 audio). No GUI dependencies live
here so this module stays unit-testable and reusable from a CLI or other
front-ends."""

from __future__ import annotations

import re
import sys
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


def fetch_translated_transcript(
    video_id: str,
    target_lang: str = "ko",
    meta: VideoMeta | None = None,
) -> TranscriptResult | None:
    """Fetch any available transcript translated into ``target_lang`` via YouTube.

    Picks a translatable source (preferring manual over auto-generated) that
    YouTube offers ``target_lang`` for, calls ``.translate(target_lang).fetch()``,
    and returns the result. Returns ``None`` when no translatable transcript
    advertises the target language (most often: the video has no captions, or
    the only caption is a manually-uploaded one YouTube refuses to translate).
    """
    api = YouTubeTranscriptApi()
    try:
        transcript_list = api.list(video_id)
    except Exception:
        return None

    chosen = None
    for t in transcript_list:
        if not t.is_translatable:
            continue
        if not any(tl.language_code == target_lang
                   for tl in t.translation_languages):
            continue
        if chosen is None or (chosen.is_generated and not t.is_generated):
            chosen = t
            if not t.is_generated:
                break  # manual + translatable: best possible source
    if chosen is None:
        return None

    try:
        translated = chosen.translate(target_lang)
        fetched = translated.fetch()
    except Exception:
        return None

    snippets = [(s.text, s.start, s.duration) for s in fetched]
    if meta is None:
        meta = VideoMeta(video_id=video_id, title=video_id)
    return TranscriptResult(
        meta=meta,
        language=translated.language,
        language_code=translated.language_code,
        is_generated=True,  # YouTube translations are machine-produced
        snippets=snippets,
    )


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

TRANSCRIPT_TIMESTAMPED = "timestamped"
TRANSCRIPT_SENTENCES = "sentences"
TRANSCRIPT_PARAGRAPHS = "paragraphs"
TRANSCRIPT_FORMATS = (
    TRANSCRIPT_TIMESTAMPED,
    TRANSCRIPT_SENTENCES,
    TRANSCRIPT_PARAGRAPHS,
)

# Gaps between caption cues (seconds). Auto-captions rarely use punctuation.
_PAUSE_SENTENCE = 1.2
_PAUSE_PARAGRAPH = 2.5
# Latin (. ! ?) and CJK (。！？…) sentence terminators, optionally followed by
# closing quotes/brackets.
_SENTENCE_END_RE = re.compile(r'[.!?。！？…][\"\'\)\]」』）】]*$')
_CJK_LANG_PREFIXES = ("ja", "zh", "yue")  # use 。 instead of .


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


def _join_snippet_text(parts: list[str]) -> str:
    return " ".join(parts)


def _ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END_RE.search(text.strip()))


def _terminator_for(language_code: str) -> str:
    """Sentence terminator to append for a given language ('.' or CJK '。')."""
    lc = (language_code or "").lower()
    if any(lc.startswith(p) for p in _CJK_LANG_PREFIXES):
        return "。"
    return "."


def _finalize_sentence(text: str, terminator: str) -> str:
    """Capitalize the sentence start (Latin) and ensure it ends with punctuation.

    Auto-generated captions are lowercase and unpunctuated, so we add a period
    (or CJK 。) when a sentence doesn't already end with terminating punctuation.
    """
    text = text.strip()
    if not text:
        return text
    # Capitalize a leading ASCII lowercase letter (no-op for Korean/CJK).
    if text[0].isascii() and text[0].islower():
        text = text[0].upper() + text[1:]
    if not _ends_sentence(text):
        text += terminator
    return text


def _split_into_sentences(snippets: list) -> list[tuple]:
    """Group caption cues into (text, start, end) sentence tuples.

    A sentence ends at existing terminating punctuation or a >= _PAUSE_SENTENCE
    gap between cues.
    """
    sentences: list[tuple] = []
    buf: list[str] = []
    buf_start: float | None = None
    prev_end: float | None = None

    for text, start, dur in snippets:
        clean = _clean_text(text)
        if not clean:
            continue

        gap = (start - prev_end) if prev_end is not None else None
        if buf and gap is not None and gap >= _PAUSE_SENTENCE:
            sentences.append((_join_snippet_text(buf), buf_start, prev_end))
            buf = []

        if not buf:
            buf_start = start
        buf.append(clean)
        prev_end = start + dur

        if _ends_sentence(_join_snippet_text(buf)):
            sentences.append((_join_snippet_text(buf), buf_start, prev_end))
            buf = []

    if buf:
        sentences.append((_join_snippet_text(buf), buf_start, prev_end))

    return sentences


def format_transcript_snippets(
    snippets: list,
    style: str = TRANSCRIPT_SENTENCES,
    language_code: str = "",
) -> list[str]:
    """Merge short caption cues into readable, punctuated lines.

    *sentences* — one punctuated sentence per line.
    *paragraphs* — blank-line-separated blocks (>= ~2.5s pause between topics),
    each sentence inside still punctuated.

    Because auto-generated captions carry no punctuation, sentence boundaries
    are inferred from existing punctuation and speech pauses, then a terminator
    ('.' or CJK '。') is appended so the text reads as proper sentences.
    """
    if style not in (TRANSCRIPT_SENTENCES, TRANSCRIPT_PARAGRAPHS):
        raise ValueError(f"unsupported style: {style}")

    terminator = _terminator_for(language_code)
    sentences = _split_into_sentences(snippets)
    final = [
        (_finalize_sentence(txt, terminator), start, end)
        for txt, start, end in sentences
        if _finalize_sentence(txt, terminator)
    ]

    if style == TRANSCRIPT_SENTENCES:
        return [txt for txt, _s, _e in final]

    # Paragraphs: join consecutive sentences, breaking on long pauses.
    joiner = "" if terminator == "。" else " "
    paragraphs: list[str] = []
    current: list[str] = []
    prev_end: float | None = None
    for txt, start, end in final:
        if current and prev_end is not None and (start - prev_end) >= _PAUSE_PARAGRAPH:
            paragraphs.append(joiner.join(current))
            current = []
        current.append(txt)
        prev_end = end
    if current:
        paragraphs.append(joiner.join(current))

    return paragraphs


def build_markdown(
    result: TranscriptResult,
    transcript_format: str = TRANSCRIPT_SENTENCES,
    *,
    include_timestamps: bool | None = None,
) -> str:
    """Render a TranscriptResult as a markdown document string.

    ``transcript_format`` is one of ``timestamped``, ``sentences``, or
    ``paragraphs``. ``include_timestamps`` is deprecated; when set, it overrides
    ``transcript_format`` (True -> timestamped, False -> sentences).
    """
    if include_timestamps is not None:
        transcript_format = (
            TRANSCRIPT_TIMESTAMPED if include_timestamps else TRANSCRIPT_SENTENCES
        )
    if transcript_format not in TRANSCRIPT_FORMATS:
        raise ValueError(f"unsupported transcript_format: {transcript_format}")
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

    if transcript_format == TRANSCRIPT_TIMESTAMPED:
        for text, start, _dur in result.snippets:
            clean = _clean_text(text)
            if clean:
                lines.append(f"`[{format_timestamp(start)}]` {clean}")
    else:
        units = format_transcript_snippets(
            result.snippets, transcript_format,
            language_code=result.language_code,
        )
        for i, unit in enumerate(units):
            lines.append(unit)
            if (
                transcript_format == TRANSCRIPT_PARAGRAPHS
                and i + 1 < len(units)
            ):
                lines.append("")

    lines.append("")
    return "\n".join(lines)


_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(
    title: str, video_id: str, max_len: int = 80, lang_tag: str = ""
) -> str:
    """Build a filesystem-safe markdown filename from a title + video id.

    `lang_tag` (e.g. ``"ko"``) yields ``"... [id].ko.md"`` — used to save a
    translated variant next to the original without colliding with it.
    """
    base = _INVALID_FS.sub("_", title).strip().strip(".")
    base = re.sub(r"\s+", " ", base)
    if len(base) > max_len:
        base = base[:max_len].rstrip()
    if not base:
        base = "transcript"
    tag = f".{lang_tag}" if lang_tag else ""
    return f"{base} [{video_id}]{tag}.md"


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
    transcript_format: str = TRANSCRIPT_SENTENCES,
    include_timestamps: bool | None = None,
    translate_to: str | None = None,
    progress=None,
) -> list[Path]:
    """Full pipeline: parse -> meta -> transcript -> markdown -> file.

    When ``translate_to`` is a language code (e.g. ``"ko"``) and the source
    transcript is in a different language, a second ``.md`` is written next to
    the original with a ``.{lang}.md`` suffix, using YouTube's built-in
    translation. Translation is best-effort: if the video has no translatable
    caption, only the original is saved (no error raised).

    `progress` is an optional callable(str) for status messages. Returns the
    list of written file paths (1 or 2). Raises ExtractionError on any handled
    failure of the primary extraction.
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
    content = build_markdown(
        result,
        transcript_format=transcript_format,
        include_timestamps=include_timestamps,
    )
    written = [save_markdown(content, out_dir, safe_filename(meta.title, video_id))]

    if translate_to:
        target = translate_to.strip().lower()
        src = (result.language_code or "").lower()
        # Skip when the source already matches (no point re-translating ko→ko),
        # treating "ko" and "ko-KR" as equivalent for this check.
        if target and target.split("-")[0] != src.split("-")[0]:
            report(f"{target} 번역 가져오는 중…")
            translated = fetch_translated_transcript(video_id, target, meta=meta)
            if translated is not None:
                report(f"{target} 번역 저장 중…")
                t_content = build_markdown(
                    translated,
                    transcript_format=transcript_format,
                    include_timestamps=include_timestamps,
                )
                t_name = safe_filename(meta.title, video_id, lang_tag=target)
                written.append(save_markdown(t_content, out_dir, t_name))
            else:
                report(f"{target} 번역 자막을 사용할 수 없습니다 (원문만 저장)")
    return written


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


def _bundled_bin_dirs():
    """Directories that may hold binaries we ship (e.g. aria2c).

    Covers a PyInstaller one-file build (extracted under sys._MEIPASS) and a
    plain source checkout (yt_extractor/bin/).
    """
    dirs = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass) / "bin")
        dirs.append(Path(meipass))
    dirs.append(Path(__file__).resolve().parent / "bin")
    return dirs


def _ensure_aria2c_on_path():
    """Make an aria2c executable discoverable; return its directory or None.

    Prefers one already on PATH; otherwise, if we bundle aria2c under bin/, we
    prepend that directory to PATH so yt-dlp's executable lookup finds it (a
    frozen app's bundled binaries are not on PATH by default).
    """
    import os
    import shutil

    found = shutil.which("aria2c")
    if found:
        return os.path.dirname(found)

    exe = "aria2c.exe" if os.name == "nt" else "aria2c"
    for base in _bundled_bin_dirs():
        if (base / exe).exists():
            os.environ["PATH"] = str(base) + os.pathsep + os.environ.get("PATH", "")
            return str(base)
    return None


def _download_accel_opts():
    """yt-dlp options that speed up the download (the real bottleneck).

    MP3 encoding is CPU-only and near-instant; GPU codecs apply to video, not
    audio. So the win is in the network step: prefer aria2c with many parallel
    connections when available (bundled or on PATH), otherwise fall back to
    yt-dlp's built-in concurrent fragment downloads. Returns a dict to merge
    into ydl_opts.
    """
    if _ensure_aria2c_on_path():
        return {
            "external_downloader": "aria2c",
            # -x/-s: up to 16 connections/splits per download; -k1M: split size.
            "external_downloader_args": ["-x16", "-s16", "-k1M"],
        }
    # aria2c unavailable: still parallelize fragmented (DASH/HLS) streams.
    return {"concurrent_fragment_downloads": 4}


class _Cancelled(Exception):
    """Internal signal raised from a yt-dlp hook to abort an in-flight download."""


def extract_audio_mp3(
    url_or_id: str,
    out_dir: str | Path,
    bitrate: str = "192",
    progress=None,
    should_cancel=None,
) -> Path:
    """Download a video's audio and convert it to MP3.

    `bitrate` is a kbps string ("128", "192", "320"). `progress` is an optional
    callable(str). `should_cancel` is an optional callable() -> bool; when it
    returns True (e.g. the app is shutting down) the download is aborted from
    inside yt-dlp's hooks so the worker thread returns promptly instead of
    blocking process exit. Returns the written .mp3 path. Raises ExtractionError.
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

    def cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    if cancelled():
        raise _Cancelled()

    report("영상 ID 분석 중…")
    video_id = extract_video_id(url_or_id)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Track the final filename reported by the postprocessor.
    final_path = {"path": None}

    def progress_hook(d):
        # Raising here is yt-dlp's supported way to abort a running download.
        if cancelled():
            raise _Cancelled()
        status = d.get("status")
        if status == "downloading":
            pct = d.get("_percent_str", "").strip()
            spd = d.get("_speed_str", "").strip()
            report(f"음원 다운로드 중… {pct} {spd}".strip())
        elif status == "finished":
            report("MP3로 변환 중…")

    def postproc_hook(d):
        if cancelled():
            raise _Cancelled()
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
    ydl_opts.update(_download_accel_opts())
    ffloc = _ffmpeg_location()
    if ffloc:
        ydl_opts["ffmpeg_location"] = ffloc

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(canonical_url(video_id), download=True)
    except _Cancelled:
        raise ExtractionError("작업이 취소되었습니다.")
    except Exception as e:
        # yt-dlp wraps our _Cancelled in a DownloadError; unwrap that case.
        if isinstance(e.__cause__, _Cancelled) or isinstance(
            getattr(e, "exc_info", [None])[1] if hasattr(e, "exc_info") else None,
            _Cancelled,
        ):
            raise ExtractionError("작업이 취소되었습니다.")
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
