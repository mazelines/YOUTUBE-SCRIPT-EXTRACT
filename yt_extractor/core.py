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
    # Where the captions came from: "youtube" (official/auto-caption) or
    # "stt" (faster-whisper on captured audio). Lets the markdown builder
    # decide the output suffix (".stt.md" vs ".md") without string-matching
    # the language label, which is fragile under localisation.
    origin: str = "youtube"


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
            except Exception:
                # NoTranscriptFound is a subclass of Exception; listing both
                # in the tuple was redundant. Any miss just means "try the
                # next finder" (manual -> generated -> ...).
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

    Original one-shot path (no fallback). New callers usually want
    ``extract_to_markdown_with_fallback``, which dispatches here when no
    polling/STT fallback is requested.

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

# Per-process cache of is_live_video() answers, keyed by video id, with the
# time.monotonic() timestamp at which the answer was obtained. The probe is an
# extra HTTP round-trip, so memoising avoids re-probing during a batch; but a
# live stream eventually ends, so we expire entries after _LIVE_CACHE_TTL_SEC
# — otherwise a long-running GUI session would keep treating a finished stream
# as live forever.
_live_cache: dict[str, tuple[bool, float]] = {}
_LIVE_CACHE_TTL_SEC = 300.0  # 5 minutes


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
    start = time.monotonic()
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
        # Report actual elapsed time since polling started, not attempt*n.
        # The first attempt happens immediately (0s elapsed), so the message
        # correctly reads "(0/30분)" right after the first miss rather than
        # the old "(1/30분)" which implied a minute had already passed.
        elapsed_min = (time.monotonic() - start) / 60.0
        report(
            f"자막 생성 대기 중… ({elapsed_min:.0f}/{max_wait_min:.0f}분, "
            f"다음 시도 {poll_interval_sec:.0f}초 후, {attempt}회 시도)"
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
    ``is_live`` / ``live_status`` fields. Memoised per process with a TTL
    (``_LIVE_CACHE_TTL_SEC``): a live stream can transition to a VOD while
    the app stays open, so a permanent cache would mis-route finished streams
    to the live-capture path forever. On any failure (network, private video,
    etc.) returns ``False`` so callers fall through to the VOD path instead of
    refusing to process a possibly-live video.
    """
    import time

    cached = _live_cache.get(video_id)
    if cached is not None:
        value, ts = cached
        if time.monotonic() - ts < _LIVE_CACHE_TTL_SEC:
            return value
        # expired — fall through and re-probe
    try:
        import yt_dlp
    except ImportError:
        _live_cache[video_id] = (False, time.monotonic())
        return False
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(canonical_url(video_id), download=False) or {}
    except Exception:
        _live_cache[video_id] = (False, time.monotonic())
        return False
    live = bool(info.get("is_live")) or info.get("live_status") in (
        "is_live", "is_upcoming",
    )
    _live_cache[video_id] = (live, time.monotonic())
    return live


# MP3 bitrate for STT-bound audio. Speech recognition gains nothing from
# higher bitrates, so we keep STT downloads small to bound the faster-whisper
# input size and the live-capture file growth.
STT_AUDIO_BITRATE = "128"


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
    # Delegate to the same _download pipeline extract_audio_mp3 uses, so the
    # VOD path gets the same cancel/ffmpeg-error handling as MP3 extraction
    # instead of a duplicated yt-dlp invocation here. 128 kbps is plenty for
    # speech (STT doesn't benefit from higher bitrates).
    return extract_audio_mp3(
        url_or_id, out_dir,
        bitrate=STT_AUDIO_BITRATE,
        progress=progress,
        should_cancel=should_cancel,
    )


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
    # Write to a .part sidecar then atomically rename, so a half-finished
    # capture (or a capture that crashes mid-stream) never overwrites an
    # existing good .live.mp3 the user may be re-running against. Matches the
    # pattern yt-dlp itself uses for downloads.
    part_path = out_path.with_suffix(out_path.suffix + ".part")

    duration_sec = str(int(float(capture_minutes) * 60))
    # Build ffmpeg args. -user_agent + -headers carry the cookies/auth-style
    # headers yt-dlp would send; without them YouTube's CDN returns 403.
    # Strip CR/LF from keys+values defensively: a maliciously crafted header
    # value could otherwise inject extra headers via ffmpeg's -headers blob
    # (yt-dlp is the source so this is defence-in-depth, not a real vector).
    def _scrub(s: str) -> str:
        return (s or "").replace("\r", "").replace("\n", "")

    header_blob = "".join(
        f"{_scrub(k)}: {_scrub(v)}\r\n" for k, v in headers.items()
    )
    user_agent = _scrub(headers.get("User-Agent", "")) or "Mozilla/5.0"
    cmd = [
        ffmpeg_exe, "-y",
        "-user_agent", user_agent,
    ]
    if header_blob:
        cmd += ["-headers", header_blob]
    cmd += [
        "-i", stream_url,
        "-t", duration_sec,
        "-vn", "-acodec", "libmp3lame", "-b:a", f"{STT_AUDIO_BITRATE}k",
        "-rtbufsize", "64M",   # live buffer; avoids "real-time buffer too full"
        str(part_path),
    ]

    report(f"라이브 캡처 중… ({capture_minutes:.0f}분, ffmpeg 직접 녹음)")
    # Initialise up front: on TimeoutExpired the `proc = subprocess.run(...)`
    # assignment never happens, so the post-check must still be able to
    # reference `proc` without a NameError masking the real failure.
    proc = None
    run_kwargs = {
        "capture_output": True,
        # Give a generous wall-clock budget: ffmpeg finishes faster than
        # real-time once it has buffered, but the live stream is real-time
        # so we allow capture_minutes + 120s slack + startup.
        "timeout": float(capture_minutes) * 60.0 + 120.0,
        "check": False,
    }
    # In a PyInstaller --noconsole (windowed) build, a subprocess that wants
    # a console allocates its own black cmd window — ffmpeg would sit on the
    # user's screen for up to `capture_minutes`. CREATE_NO_WINDOW prevents
    # that. Mirrors _force_kill_process_tree in app.py.
    if os.name == "nt":
        run_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(cmd, **run_kwargs)
    except subprocess.TimeoutExpired as e:
        # ffmpeg ran past the budget — usually means the stream stalled.
        # Keep whatever partial output we have rather than failing outright.
        # `e` carries the partial output/stderr if any was captured.
        report("라이브 캡처 시간 초과 — 받은 구간으로 STT 진행")
    except Exception as e:
        raise ExtractionError(f"라이브 캡처 실행 실패: {e}")

    # ffmpeg wrote to part_path; promote it to the final name on success, or
    # clean it up on failure so a stale .part never litters the output dir.
    if not part_path.exists() or part_path.stat().st_size == 0:
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        stderr_tail = ""
        if proc is not None and proc.stderr:
            stderr_tail = proc.stderr.decode("utf-8", "replace")[-400:]
        rc = proc.returncode if proc is not None else "?"
        raise ExtractionError(
            "라이브 캡처가 음성을 생성하지 못했습니다. "
            f"(ffmpeg rc={rc}; {stderr_tail})"
        )
    # os.replace is atomic on the same filesystem: either the rename fully
    # succeeds (out_path becomes the new capture) or it fails (the previous
    # out_path is left untouched).
    os.replace(part_path, out_path)
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
        is_generated=True,  # STT is machine produced, like auto-captions
        snippets=snippets,
        origin="stt",
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
                "자막이 없습니다. STT 폴백을 사용하려면 faster-whisper 설치가 "
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

        # Ensure the Whisper checkpoint exists BEFORE downloading the audio:
        # if the model fetch fails we don't want to have wasted time/bandwidth
        # on audio we then can't transcribe. The first run pulls the model
        # (~480MB for 'small') with progress reported through the same channel
        # as everything else. Subsequent runs hit the cache and skip this.
        try:
            from . import stt as _stt_mod
            if not _stt_mod.is_model_present(stt_model_size):
                report(
                    f"[STT] 모델({stt_model_size}) 다운로드 중… (최초 1회, "
                    f"이후 캐시 사용)"
                )
                _stt_mod.download_model(
                    stt_model_size,
                    should_cancel=should_cancel,
                )
        except _stt_mod.STTError as e:
            raise ExtractionError(str(e))
        except Exception as e:
            raise ExtractionError(f"STT 모델 준비 실패: {e}")

        report("[STT] 음성 다운로드 중…")
        audio_path: Path | None = None
        stt_result = None
        try:
            audio_path = _download_for_stt(
                url_or_id, out_dir,
                live_capture_minutes=live_capture_minutes,
                progress=lambda m: report(f"[STT] {m}"),
                should_cancel=should_cancel,
            )

            # Pick the STT source language from the preferred list (first
            # entry), but only when it's a single-letter-base code Whisper
            # understands.
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
        finally:
            # Clean up the intermediate audio unless the user wants it kept.
            # Doing this in `finally` guarantees the temp mp3 doesn't linger
            # in out_dir even if STT raised — previously the unlink lived
            # after the _run_stt call, so an exception would leak the file.
            if audio_path is not None and not keep_audio:
                try:
                    audio_path.unlink(missing_ok=True)
                except OSError:
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
    # official YouTube captions when the user opens them later. We key off
    # result.origin ("stt" vs "youtube") rather than the language label, so
    # the check survives translations of the "음성 인식" string.
    is_stt_origin = result.origin == "stt"
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

def _ffmpeg_location():
    """Return a directory containing an ffmpeg binary yt-dlp/ffmpeg can use,
    or None.

    Prefers the binary bundled by imageio-ffmpeg so users don't have to install
    ffmpeg system-wide. That binary has a versioned name (e.g.
    ``ffmpeg-win-x86_64-v7.1.exe``) which yt-dlp won't recognise, so we copy it
    once into a cache dir under a plain ``ffmpeg``/``ffmpeg.exe`` name. Returns
    None to let yt-dlp fall back to whatever ffmpeg is on PATH.

    As a side effect, the returned directory is also prepended to ``PATH`` for
    the current process. yt-dlp's ``ffmpeg_location`` option is *not* consulted
    by every code path — notably ``FFmpegFD.available()`` (used to decide
    whether section downloads / live-from-start are even attempted) checks the
    default ``FFmpegPostProcessor().available``, which only looks at PATH.
    Without our dir on PATH, ``download_ranges`` fails with "ffmpeg is not
    installed" even though ``ffmpeg_location`` points right at it. Adding it to
    PATH makes both detection paths agree.

    Note on ffprobe: imageio-ffmpeg ships ffmpeg *only*, but none of our
    current code paths need ffprobe — VOD audio extraction and the live-stream
    ffmpeg capture both work with ffmpeg alone. If a future feature needs
    ffprobe (e.g. precise timestamp cuts), add a separate fetch then; for now
    it would be dead code.
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
