"""Core extraction logic (transcripts + MP3 audio). No GUI dependencies live
here so this module stays unit-testable and reusable from a CLI or other
front-ends."""

from __future__ import annotations

import os
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
    """Original one-shot pipeline (transcript -> markdown). Kept for backward
    compatibility; new callers should use ``extract_to_markdown_with_fallback``
    which dispatches to this when no fallback is requested.
    """
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
# Fallback: polling for not-yet-generated captions + STT for caption-less
# videos (including live streams). All optional; only invoked when the caller
# explicitly asks via extract_to_markdown_with_fallback.
# --------------------------------------------------------------------------- #

# Per-process cache of is_live_video() answers; a live status never flips back
# mid-run and the probe is an extra HTTP round-trip, so memoise it.
_live_cache: dict[str, bool] = {}


def fetch_transcript_or_none(
    video_id: str,
    preferred_langs=("ko", "en"),
    prefer_manual: bool = True,
    meta: VideoMeta | None = None,
) -> TranscriptResult | None:
    """Like fetch_transcript, but returns ``None`` on any failure instead of
    raising ExtractionError. Used by the fallback pipeline to distinguish
    "captions aren't there yet" from hard failures without try/except sprawl.
    """
    try:
        return fetch_transcript(
            video_id, preferred_langs, prefer_manual, meta=meta
        )
    except ExtractionError:
        return None


def fetch_transcript_with_polling(
    video_id: str,
    *,
    max_wait_min: float = 30.0,
    poll_interval_sec: float = 60.0,
    preferred_langs=("ko", "en"),
    prefer_manual: bool = True,
    meta: VideoMeta | None = None,
    progress=None,
    should_cancel=None,
) -> TranscriptResult | None:
    """Retry fetch_transcript periodically until it succeeds or the budget
    runs out. Intended for live streams where YouTube hasn't generated the
    auto-caption yet but will shortly. Returns ``None`` on timeout.

    The first attempt is immediate (no initial wait) so a race between "added
    to queue" and "caption went live" still resolves instantly.
    """
    import time

    if max_wait_min <= 0:
        return fetch_transcript_or_none(
            video_id, preferred_langs, prefer_manual, meta=meta
        )

    def report(msg):
        if progress:
            progress(msg)

    deadline = time.monotonic() + max_wait_min * 60.0
    attempt = 0
    while True:
        if should_cancel and should_cancel():
            return None
        attempt += 1
        result = fetch_transcript_or_none(
            video_id, preferred_langs, prefer_manual, meta=meta
        )
        if result is not None:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        waited_min = attempt * poll_interval_sec / 60.0
        report(
            f"자막 생성 대기 중… ({waited_min:.0f}/{max_wait_min:.0f}분, "
            f"다음 시도 {poll_interval_sec:.0f}초 후)"
        )
        # Sleep in small slices so a cancel is noticed within a second.
        sleep_remaining = min(poll_interval_sec, remaining)
        slept = 0.0
        while slept < sleep_remaining:
            if should_cancel and should_cancel():
                return None
            step = min(1.0, sleep_remaining - slept)
            time.sleep(step)
            slept += step


def is_live_video(video_id: str, timeout: float = 8.0) -> bool:
    """Probe whether ``video_id`` is a live stream (or upcoming premiere).

    Uses yt-dlp with download disabled — the lightest way to read the
    ``is_live`` / ``live_status`` fields. Memoised per process; on any failure
    (network, private video, etc.) returns ``False`` so callers fall through to
    the VOD path instead of refusing to process a possibly-live video.
    """
    if video_id in _live_cache:
        return _live_cache[video_id]
    try:
        import yt_dlp
    except ImportError:
        _live_cache[video_id] = False
        return False
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(canonical_url(video_id), download=False) or {}
    except Exception:
        _live_cache[video_id] = False
        return False
    live = bool(info.get("is_live")) or info.get("live_status") in (
        "is_live", "is_upcoming",
    )
    _live_cache[video_id] = live
    return live


def _download_for_stt(
    url_or_id: str,
    out_dir: str | Path,
    *,
    live_capture_minutes: float = 0.0,
    progress=None,
    should_cancel=None,
) -> Path:
    """Download best-audio for STT, returning an mp3 path.

    Two paths:

    * **VOD** — the normal yt-dlp audio download (same pipeline as
      ``extract_audio_mp3``). Captions-less finished videos come here.
    * **Live** — yt-dlp resolves the live HLS manifest URL (download=False),
      then we hand it to ffmpeg with ``-t <minutes*60>`` so ffmpeg captures
      exactly the requested window into a single mp3. This is far more
      reliable than yt-dlp's own live section download, which fails with
      "This format cannot be partially downloaded" on YouTube live audio and
      scatters thousands of fragment shards when cancelled mid-stream.

    The result is left in ``out_dir`` (the caller decides whether to keep the
    intermediate audio). Raises ExtractionError on failure.
    """
    try:
        import yt_dlp
    except ImportError:
        raise ExtractionError("다운로드에는 yt-dlp가 필요합니다 (pip install yt-dlp).")

    def report(msg):
        if progress:
            progress(msg)

    if should_cancel and should_cancel():
        raise ExtractionError("작업이 취소되었습니다.")

    video_id = extract_video_id(url_or_id)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    is_live = is_live_video(video_id)
    if is_live and live_capture_minutes <= 0:
        raise ExtractionError(
            "라이브 영상은 캡처 시간(분)을 지정해야 합니다. "
            "라이브 종료 후 다시 시도하거나 캡처 시간을 늘리세요."
        )

    ffloc = _ffmpeg_location() or ""
    ffmpeg_exe = (
        str(Path(ffloc) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
        if ffloc else "ffmpeg"
    )

    # ------------------------------------------------------------------ LIVE
    if is_live:
        return _capture_live_audio(
            ydl_module=yt_dlp,
            video_id=video_id,
            out_dir=out,
            capture_minutes=live_capture_minutes,
            ffmpeg_exe=ffmpeg_exe,
            progress=progress,
            should_cancel=should_cancel,
        )

    # ------------------------------------------------------------------- VOD
    final_path = {"path": None}

    def progress_hook(d):
        if should_cancel and should_cancel():
            raise _Cancelled()
        status = d.get("status")
        if status == "downloading":
            pct = d.get("_percent_str", "").strip()
            spd = d.get("_speed_str", "").strip()
            eta = d.get("_eta_str", "").strip()
            report(f"음성 다운로드 중… {pct} {spd} ETA {eta}".strip())
        elif status == "finished":
            report("음성 → 텍스트 변환 준비 중…")

    def postproc_hook(d):
        if should_cancel and should_cancel():
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
            "preferredquality": "128",   # speech needs little bandwidth
        }],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postproc_hook],
    }
    ydl_opts.update(_download_accel_opts())
    if ffloc:
        ydl_opts["ffmpeg_location"] = ffloc

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(canonical_url(video_id), download=True)
    except _Cancelled:
        raise ExtractionError("작업이 취소되었습니다.")
    except Exception as e:
        if isinstance(e.__cause__, _Cancelled) or isinstance(
            getattr(e, "exc_info", [None])[1] if hasattr(e, "exc_info") else None,
            _Cancelled,
        ):
            raise ExtractionError("작업이 취소되었습니다.")
        msg = str(e)
        if "ffmpeg" in msg.lower() or "ffprobe" in msg.lower():
            raise ExtractionError(
                "변환을 위한 ffmpeg를 찾을 수 없습니다. "
                "`pip install imageio-ffmpeg`로 해결하거나 ffmpeg를 설치하세요."
            )
        raise ExtractionError(f"음성 다운로드 실패: {msg}")

    path = final_path["path"]
    if not path:
        title = (info or {}).get("title", video_id)
        base = out / safe_filename(title, video_id)
        path = str(base.with_suffix(".mp3"))
    final = Path(path).with_suffix(".mp3")
    if not final.exists():
        raise ExtractionError("음성 파일이 생성되지 않았습니다.")
    report("음성 다운로드 완료")
    return final


def _capture_live_audio(*, ydl_module, video_id, out_dir, capture_minutes,
                        ffmpeg_exe, progress=None, should_cancel=None) -> Path:
    """Capture a YouTube live stream directly with ffmpeg.

    yt-dlp resolves the live HLS manifest URL + the auth headers YouTube
    requires, then we hand both to ffmpeg with ``-t <seconds>`` so it records
    exactly the requested window into a single mp3 — no fragment shards, no
    post-merge step, no "partially downloaded" error. ffmpeg stops itself at
    the duration, so no external timer or cancel hook is needed.
    """
    import subprocess

    def report(msg):
        if progress:
            progress(msg)

    report(
        f"라이브 스트림 URL 확인 중… (최대 {capture_minutes:.0f}분 캡처)"
    )
    ydl_opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "format": "bestaudio/best",
    }
    try:
        with ydl_module.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(canonical_url(video_id), download=False)
    except Exception as e:
        raise ExtractionError(f"라이브 스트림 정보를 가져오지 못했습니다: {e}")

    sel = (info.get("requested_formats") or [info])[0]
    stream_url = sel.get("url")
    if not stream_url:
        raise ExtractionError("라이브 스트림 URL을 찾을 수 없습니다.")
    headers = sel.get("http_headers") or {}

    # Output filename: "<title> [id].live.mp3". The .live suffix keeps it
    # distinct from a normal full-audio download.
    title = info.get("title", video_id)
    out_name = safe_filename(title, video_id)
    out_name = out_name.replace(".md", ".live.mp3")
    if not out_name.endswith(".live.mp3"):
        out_name = out_name.rsplit(".", 1)[0] + ".live.mp3"
    out_path = Path(out_dir) / out_name

    duration_sec = str(int(float(capture_minutes) * 60))
    # Build ffmpeg args. -user_agent + -headers carry the cookies/auth-style
    # headers yt-dlp would send; without them YouTube's CDN returns 403.
    header_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    cmd = [
        ffmpeg_exe, "-y",
        "-user_agent", headers.get("User-Agent", "Mozilla/5.0"),
    ]
    if header_blob:
        cmd += ["-headers", header_blob]
    cmd += [
        "-i", stream_url,
        "-t", duration_sec,
        "-vn", "-acodec", "libmp3lame", "-b:a", "128k",
        "-rtbufsize", "64M",   # live buffer; avoids "real-time buffer too full"
        str(out_path),
    ]

    report(f"라이브 캡처 중… ({capture_minutes:.0f}분, ffmpeg 직접 녹음)")
    try:
        proc = subprocess.run(
            cmd, capture_output=True,
            # Give a generous wall-clock budget: ffmpeg finishes faster than
            # real-time once it has buffered, but the live stream is real-time
            # so we allow capture_minutes + 60s slack + startup.
            timeout=float(capture_minutes) * 60.0 + 120.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # ffmpeg ran past the budget — usually means the stream stalled.
        # Keep whatever partial output we have rather than failing outright.
        report("라이브 캡처 시간 초과 — 받은 구간으로 STT 진행")
    except Exception as e:
        raise ExtractionError(f"라이브 캡처 실행 실패: {e}")

    if not out_path.exists() or out_path.stat().st_size == 0:
        stderr_tail = proc.stderr.decode("utf-8", "replace")[-400:] if proc else ""
        raise ExtractionError(
            "라이브 캡처가 음성을 생성하지 못했습니다. "
            f"(ffmpeg rc={proc.returncode if proc else '?'}; {stderr_tail})"
        )
    report("라이브 캡처 완료")
    return out_path


def _run_stt(
    audio_path: Path,
    *,
    model_size: str = "small",
    language: str | None,
    progress=None,
    should_cancel=None,
) -> TranscriptResult | None:
    """Invoke faster-whisper on ``audio_path`` and wrap the output in a
    TranscriptResult. Returns ``None`` (not raises) when faster-whisper isn't
    installed — the caller decides how to surface that.
    """
    try:
        from .stt import transcribe_audio, STTError
    except ImportError:
        return None

    try:
        snippets = transcribe_audio(
            audio_path,
            model_size=model_size,
            language=language,
            on_progress=lambda n: progress and progress(
                f"음성 → 텍스트 변환 중… (segment {n})"
            ),
            should_cancel=should_cancel,
        )
    except STTError as e:
        raise ExtractionError(f"STT 변환 실패: {e}")

    if not snippets:
        return None
    # We don't know the source language reliably; detect lazily only if the
    # caller didn't ask for one. The header will read "auto-detected (STT)".
    lang_label = f"음성 인식 ({language or 'auto'})"
    return TranscriptResult(
        meta=None,  # filled in by the caller
        language=lang_label,
        language_code=language or "",
        is_generated=True,  # STT is machine-produced, like auto-captions
        snippets=snippets,
    )


def extract_to_markdown_with_fallback(
    url_or_id: str,
    out_dir: str | Path,
    *,
    preferred_langs=("ko", "en"),
    prefer_manual: bool = True,
    transcript_format: str = TRANSCRIPT_SENTENCES,
    include_timestamps: bool | None = None,
    translate_to: str | None = None,
    stt_fallback: bool = False,
    stt_model_size: str = "small",
    poll_wait_min: float = 0.0,
    poll_interval_sec: float = 60.0,
    live_capture_minutes: float = 0.0,
    keep_audio: bool = False,
    progress=None,
    should_cancel=None,
) -> list[Path]:
    """Full pipeline with fallbacks for caption-less / live videos.

    Order:
      1. fetch_transcript (immediate).
      2. If absent and ``poll_wait_min`` > 0, poll that long.
      3. If still absent and ``stt_fallback``:
           a. Live + ``live_capture_minutes`` > 0 -> capture window, then STT.
           b. Live + capture = 0 -> raise (caller shows "try after stream").
           c. VOD -> full audio download, then STT.

    STT requires faster-whisper; if it's missing the function raises an
    ExtractionError explaining how to install it, rather than silently
    producing nothing. Returns 1-2 written paths (original + translation when
    ``translate_to`` is set).
    """
    def report(msg):
        if progress:
            progress(msg)

    report("영상 ID 분석 중…")
    video_id = extract_video_id(url_or_id)

    report("영상 정보 가져오는 중…")
    meta = fetch_video_meta(video_id)

    # Step 1: immediate caption attempt.
    report("자막 추출 중…")
    result = fetch_transcript_or_none(
        video_id, preferred_langs, prefer_manual, meta=meta
    )

    # Step 2: poll if requested and the immediate attempt missed.
    if result is None and poll_wait_min > 0:
        report(
            f"자막이 아직 없습니다. YouTube 자동 자막 생성을 "
            f"최대 {poll_wait_min:.0f}분 대기합니다…"
        )
        result = fetch_transcript_with_polling(
            video_id,
            max_wait_min=poll_wait_min,
            poll_interval_sec=poll_interval_sec,
            preferred_langs=preferred_langs,
            prefer_manual=prefer_manual,
            meta=meta,
            progress=lambda m: report(f"[폴링] {m}"),
            should_cancel=should_cancel,
        )

    # Step 3: STT fallback.
    audio_tmp: Path | None = None
    if result is None and stt_fallback:
        if should_cancel and should_cancel():
            raise ExtractionError("작업이 취소되었습니다.")
        # Check that the STT backend is even importable before downloading
        # gigabytes of audio we then can't transcribe. We import the *module*
        # (not the symbol) so tests can monkeypatch stt.is_stt_available.
        try:
            from . import stt as _stt_mod
            _stt_available = _stt_mod.is_stt_available()
        except ImportError:
            _stt_available = False
        except Exception:
            _stt_available = False
        if not _stt_available:
            raise ExtractionError(
                "자막이 없습니다. STT 폴백을 사용하려면 faster-whisper 설치이 "
                "필요합니다: pip install -r requirements-stt.txt"
            )

        # Live requires a capture window; detect once here so we can give the
        # user an actionable message instead of a generic download failure.
        is_live = is_live_video(video_id)
        if is_live and live_capture_minutes <= 0:
            raise ExtractionError(
                "라이브 중인 영상은 자막이 아직 없습니다. "
                "캡처 시간(분)을 지정하면 일부 구간을 STT로 변환할 수 있고, "
                "라이브 종료 후 다시 시도하면 전체 자막을 받을 수 있습니다."
            )

        report("[STT] 음성 다운로드 중…")
        try:
            audio_path = _download_for_stt(
                url_or_id, out_dir,
                live_capture_minutes=live_capture_minutes,
                progress=lambda m: report(f"[STT] {m}"),
                should_cancel=should_cancel,
            )
        finally:
            pass
        audio_tmp = audio_path if not keep_audio else None

        # Pick the STT source language from the preferred list (first entry),
        # but only when it's a single-letter-base code Whisper understands.
        stt_lang = None
        if preferred_langs:
            first = (preferred_langs[0] or "").split("-")[0].lower()
            if first:
                stt_lang = first

        report("[STT] 음성 → 텍스트 변환 중…")
        stt_result = _run_stt(
            audio_path,
            model_size=stt_model_size,
            language=stt_lang,
            progress=lambda m: report(f"[STT] {m}"),
            should_cancel=should_cancel,
        )

        # Clean up the intermediate audio unless the user wants it kept.
        if audio_tmp is not None:
            try:
                audio_tmp.unlink(missing_ok=True)
            except Exception:
                pass

        if stt_result is None:
            raise ExtractionError(
                "STT로 텍스트를 추출하지 못했습니다. (음성이 너무 짧거나 "
                "모델이 설치되지 않았을 수 있습니다.)"
            )
        # Fill in the metadata the STT path doesn't have.
        stt_result.meta = meta
        # Persist the audio alongside the transcript when the user asked.
        result = stt_result

    if result is None:
        # No fallback path produced captions. Surface the most likely reason.
        raise ExtractionError(
            "자막을 찾을 수 없습니다. YouTube가 아직 자막을 생성하지 않았거나 "
            "비활성화된 영상일 수 있습니다. 옵션에서 STT 폴백을 켜거나 폴링 "
            "대기 시간을 늘려보세요."
        )

    report("마크다운 저장 중…")
    content = build_markdown(
        result,
        transcript_format=transcript_format,
        include_timestamps=include_timestamps,
    )
    # STT-originated files get a distinct suffix so they don't masquerade as
    # official YouTube captions when the user opens them later.
    is_stt_origin = result.language.startswith("음성 인식")
    lang_tag = ""
    if is_stt_origin:
        base_name = safe_filename(meta.title, video_id)
        # Insert ".stt" before the [id] suffix.
        stt_name = base_name.replace(
            f" [{video_id}].md", f" [{video_id}].stt.md"
        )
        written = [save_markdown(content, out_dir, stt_name)]
    else:
        written = [save_markdown(content, out_dir, safe_filename(meta.title, video_id))]

    if translate_to and not is_stt_origin:
        # Translation only makes sense for YouTube-sourced captions (we can
        # ask YouTube to translate). For STT text, leave translation to the
        # AI chat panel instead.
        target = translate_to.strip().lower()
        src = (result.language_code or "").lower()
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

def _ffprobe_download_url() -> str | None:
    """Resolve the ffprobe download URL for the current platform from
    ffbinaries.com's latest release.

    ffprobe isn't shipped by imageio-ffmpeg (which bundles ffmpeg only), so
    yt-dlp features that need it — ``download_ranges``, ``live_from_start``
    with cuts, metadata probing — fail with "ffmpeg not installed". We fetch
    ffprobe separately here. ffprobe is a self-contained binary (its own copy
    of libavformat/libavcodec), so a minor version skew from the bundled
    ffmpeg (7.x vs ffbinaries' 6.1) is fine for the metadata+cut use case.
    Returns None on any failure so callers can surface a clear message.
    """
    import platform

    machine = platform.machine().lower()
    is_64 = ("64" in machine) or (platform.architecture()[0] == "64bit")
    if platform.system().lower().startswith("win"):
        plat = "windows-64" if is_64 else "windows-32"
    elif platform.system().lower() == "darwin":
        plat = "osx-64"
    else:
        plat = "linux-64" if is_64 else "linux-32"

    try:
        import requests
        resp = requests.get(
            "https://ffbinaries.com/api/v1/version/latest", timeout=15,
            headers={"User-Agent": "YouTubeTranscriptExtractor"},
        )
        if resp.status_code != 200:
            return None
        entry = resp.json().get("bin", {}).get(plat, {})
        return entry.get("ffprobe")
    except Exception:
        return None


def _ensure_ffprobe(shim_dir: Path, *, on_progress=None,
                    should_cancel=None) -> Path | None:
    """Download+extract ffprobe into ``shim_dir`` if it's not already there.

    Pulls a ZIP from ffbinaries.com (the URL from ``_ffprobe_download_url``),
    writes the archive to a ``.zip.part`` file, then extracts just
    ``ffprobe(.exe)`` to a ``.exe.part`` file and atomically renames on
    success. This guarantees no half-written ffprobe ever looks valid — an
    interrupted download/extraction leaves only ``.part`` files behind, which
    the next call overwrites. Returns the path on success (cached or freshly
    fetched), or None if the download couldn't be completed.
    """
    import shutil
    import zipfile

    is_win = os.name == "nt"
    exe_name = "ffprobe.exe" if is_win else "ffprobe"
    dst = shim_dir / exe_name
    if dst.exists() and dst.stat().st_size > 0:
        return dst  # cached

    shim_dir.mkdir(parents=True, exist_ok=True)
    # A previous interrupted run may have left a zero-byte or partial ffprobe
    # (looks present but won't run). Remove it so we don't trust a bad file.
    if dst.exists():
        try:
            dst.unlink()
        except OSError:
            pass

    url = _ffprobe_download_url()
    if not url:
        return None

    tmp_zip = shim_dir / "ffprobe.zip.part"
    tmp_dst = shim_dir / (exe_name + ".part")
    try:
        import requests
        headers = {"User-Agent": "YouTubeTranscriptExtractor"}
        with requests.get(url, stream=True, timeout=60, headers=headers) as resp:
            if resp.status_code != 200:
                return None
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            with open(tmp_zip, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if should_cancel and should_cancel():
                        return None
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        # Extract only the ffprobe binary; the ZIP also contains ffmpeg/etc
        # which we already have via imageio-ffmpeg, so we skip them.
        with zipfile.ZipFile(tmp_zip) as zf:
            member = None
            for name in zf.namelist():
                low = name.lower().rsplit("/", 1)[-1]
                if low in ("ffprobe", "ffprobe.exe"):
                    member = name
                    break
            if not member:
                return None
            with zf.open(member) as src_f, open(tmp_dst, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f, length=1 << 20)
        if tmp_dst.stat().st_size == 0:
            return None
        # os.replace is atomic on the same filesystem: either the rename
        # fully succeeds (dst becomes valid ffprobe) or it fails (dst stays
        # absent — never half-written).
        os.replace(tmp_dst, dst)
    except Exception:
        return None
    finally:
        tmp_zip.unlink(missing_ok=True)
        tmp_dst.unlink(missing_ok=True)

    return dst if (dst.exists() and dst.stat().st_size > 0) else None


def _ffmpeg_location(*, ensure_ffprobe: bool = False,
                     on_progress=None, should_cancel=None):
    """Return a directory containing an ffmpeg binary yt-dlp can use, or None.

    Prefers the binary bundled by imageio-ffmpeg so users don't have to install
    ffmpeg system-wide. That binary has a versioned name (e.g.
    ``ffmpeg-win-x86_64-v7.1.exe``) which yt-dlp won't recognise, so we copy it
    once into a cache dir under a plain ``ffmpeg``/``ffmpeg.exe`` name. Returns
    None to let yt-dlp fall back to whatever ffmpeg is on PATH.

    When ``ensure_ffprobe`` is True, also download ffprobe into the same shim
    directory if it's missing (imageio-ffmpeg ships ffmpeg only). This is
    required by yt-dlp features that probe media metadata or cut at exact
    timestamps — ``download_ranges``, ``live_from_start``+cuts. Without ffprobe
    those fail with a misleading "ffmpeg is not installed" error. The download
    is best-effort: on failure we still return the shim dir (ffmpeg-only is
    enough for plain downloads), so the caller gets the best available setup.

    As a side effect, the returned directory is also prepended to ``PATH`` for
    the current process. yt-dlp's ``ffmpeg_location`` option is *not* consulted
    by every code path — notably ``FFmpegFD.available()`` (used to decide
    whether section downloads / live-from-start are even attempted) checks the
    default ``FFmpegPostProcessor().available``, which only looks at PATH.
    Without our dir on PATH, ``download_ranges`` fails with "ffmpeg is not
    installed" even though ``ffmpeg_location`` points right at it. Adding it to
    PATH makes both detection paths agree.
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
    except Exception:
        return None  # let yt-dlp find ffmpeg on PATH

    # ffprobe fetch is done OUTSIDE the try/except above for two reasons:
    #   1. A failure to fetch ffprobe must not mask the ffmpeg path we just
    #      resolved (the broad except above would have turned it into None).
    #   2. On Windows, doing the ffprobe download/copy immediately after
    #      shutil.copy2 of ffmpeg occasionally hits a transient file-lock that
    #      corrupts the extraction. Separating the steps sidesteps that.
    if ensure_ffprobe:
        try:
            _ensure_ffprobe(
                shim_dir, on_progress=on_progress, should_cancel=should_cancel,
            )
        except Exception:
            pass  # best-effort; ffmpeg-only is still usable for plain downloads

    # Prepend to PATH so yt-dlp's PATH-based detection (FFmpegFD.available and
    # FFmpegPostProcessor().available) also sees our ffmpeg, not just the
    # explicit ffmpeg_location option. Idempotent: only prepend when not
    # already at the front, to avoid unbounded PATH growth across calls.
    path_str = str(shim_dir)
    current_path = os.environ.get("PATH", "")
    parts = current_path.split(os.pathsep)
    if not parts or parts[0].lower() != path_str.lower():
        os.environ["PATH"] = path_str + os.pathsep + current_path

    return str(shim_dir)


def _download_accel_opts():
    """yt-dlp options that parallelize the network step (the real bottleneck).

    MP3 encoding is CPU-only and near-instant; the cost is downloading. We use
    yt-dlp's built-in concurrent fragment downloader for DASH/HLS streams.

    NOTE: aria2c as an external downloader was tried but reliably fails on
    YouTube on Windows — WSAENETUNREACH ("A socket operation was attempted to
    an unreachable network"), aria2c exits 1, every MP3/MP4 download fails.
    Native download is both reliable and starts near-instantly. Returns a dict
    to merge into ydl_opts.
    """
    return {"concurrent_fragment_downloads": 4}


class _Cancelled(Exception):
    """Internal signal raised from a yt-dlp hook to abort an in-flight download."""


def _download(
    url_or_id: str,
    out_dir: str | Path,
    *,
    fmt: str,
    postprocessors: list,
    out_ext: str,
    progress=None,
    should_cancel=None,
    **extra_opts,
) -> Path:
    """Download a video with yt-dlp and run `postprocessors` (audio extract or
    video remux), then return the written file.

    `fmt` is the yt-dlp format string; `out_ext` is the final extension
    ("mp3", "mp4"). `extra_opts` are merged into ydl_opts (e.g.
    merge_output_format). `progress` is an optional callable(str). `should_cancel` is
    an optional callable() -> bool; when it returns True (e.g. the app is
    shutting down) the download is aborted from inside yt-dlp's hooks so the
    worker thread returns promptly instead of blocking process exit. Shared by
    extract_audio_mp3 and extract_video. Raises ExtractionError.
    """
    try:
        import yt_dlp
    except ImportError:
        raise ExtractionError(
            "다운로드에는 yt-dlp가 필요합니다 (pip install yt-dlp)."
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
    label = out_ext.upper()

    def progress_hook(d):
        # Raising here is yt-dlp's supported way to abort a running download.
        if cancelled():
            raise _Cancelled()
        status = d.get("status")
        if status == "downloading":
            pct = d.get("_percent_str", "").strip()
            spd = d.get("_speed_str", "").strip()
            report(f"다운로드 중… {pct} {spd}".strip())
        elif status == "finished":
            report(f"{label}(으)로 변환 중…")

    def postproc_hook(d):
        if cancelled():
            raise _Cancelled()
        if d.get("status") == "finished":
            info = d.get("info_dict") or {}
            fp = info.get("filepath")
            if fp:
                final_path["path"] = fp

    ydl_opts = {
        "format": fmt,
        "outtmpl": str(out / "%(title)s [%(id)s].%(ext)s"),
        "postprocessors": postprocessors,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postproc_hook],
    }
    ydl_opts.update(extra_opts)
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
                "변환을 위한 ffmpeg를 찾을 수 없습니다. "
                "`pip install imageio-ffmpeg`로 해결하거나 ffmpeg를 설치하세요."
            )
        raise ExtractionError(f"다운로드 실패: {msg}")

    # Resolve the produced file path.
    path = final_path["path"]
    if not path:
        # Derive from the template using the info dict.
        title = (info or {}).get("title", video_id)
        base = out / safe_filename(title, video_id)
        path = str(base.with_suffix("." + out_ext))
    final = Path(path).with_suffix("." + out_ext)
    if not final.exists():
        raise ExtractionError(f"{label} 파일이 생성되지 않았습니다.")

    report(f"{label} 저장 완료")
    return final


def extract_audio_mp3(
    url_or_id: str,
    out_dir: str | Path,
    bitrate: str = "192",
    progress=None,
    should_cancel=None,
) -> Path:
    """Download a video's audio and convert it to MP3.

    `bitrate` is a kbps string ("128", "192", "320"). See _download for the
    shared options. Returns the written .mp3 path.
    """
    return _download(
        url_or_id, out_dir,
        fmt="bestaudio/best",
        postprocessors=[{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(bitrate),
        }],
        out_ext="mp3",
        progress=progress, should_cancel=should_cancel,
    )


def extract_video(
    url_or_id: str,
    out_dir: str | Path,
    height: int = 1080,
    progress=None,
    should_cancel=None,
) -> Path:
    """Download a video as MP4, capped at `height` pixels (0 = best available).

    Remuxes to an MP4 container without re-encoding (fast, lossless). See
    _download for the shared options. Returns the written .mp4 path.
    """
    h = f"[height<={height}]" if height else ""
    # Prefer H.264 (avc1) video — AV1/VP9 at 1080p plays as audio-only on
    # players without those decoders (e.g. Windows' built-in). Fall back to any
    # mp4, then any format, so download never fails outright.
    fmt = (
        f"bestvideo{h}[vcodec^=avc1]+bestaudio[ext=m4a]/"
        f"bestvideo{h}[ext=mp4]+bestaudio/"
        f"best{h}[ext=mp4]/best{h}"
    )
    return _download(
        url_or_id, out_dir,
        fmt=fmt,
        # preferedformat (single 'r') is yt-dlp's historical spelling.
        postprocessors=[{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        out_ext="mp4",
        merge_output_format="mp4",
        progress=progress, should_cancel=should_cancel,
    )
