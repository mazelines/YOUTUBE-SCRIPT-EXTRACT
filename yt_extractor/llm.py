"""OpenAI-compatible chat client — GUI-independent, like core.py.

Talks to any server exposing the OpenAI `/v1/chat/completions` endpoint with
Server-Sent-Events streaming: Ollama, SGLang, vLLM, llama.cpp's server, LM
Studio, or the OpenAI API itself. The GUI never needs to know which backend is
behind the URL, so the local-serving story (llama.cpp, etc.) can be swapped in
later without touching the chat UI.
"""

from __future__ import annotations

import json

import requests


class LLMError(Exception):
    """A chat request could not be completed (connection or server error)."""


# A small, instruction-tuned model is the target, so keep the persona focused on
# the one job this app feeds it: reasoning over extracted YouTube transcripts.
DEFAULT_SYSTEM_PROMPT = (
    "당신은 YouTube 자막을 분석하는 유능한 도우미입니다. "
    "첨부된 자막이 있으면 그 내용에 근거해 한국어로 정확하게 답하세요. "
    "요약은 핵심 위주로 간결하게, 번역은 자연스럽게, 질문에는 자막에 있는 "
    "사실만으로 답하고 근거가 없으면 모른다고 말하세요."
)

# Strict, translation-only persona for the chunked translation path. The
# general prompt above lets the model editorialize — it was observed adding
# titles ("## 번역"), prefaces ("다음은 번역입니다…"), and even self-generated
# Q&A after each chunk, which then leaked into the joined result. This one
# constrains it to emit ONLY the translated text so chunks concatenate cleanly.
TRANSLATE_SYSTEM_PROMPT = (
    "당신은 정확한 번역기입니다. 사용자가 보낸 텍스트를 자연스러운 한국어로 "
    "번역한 결과만 출력하세요. 제목, 머리말, 인사말, 설명, 요약, 질문과 답변, "
    "구분선 등 번역문 이외의 내용은 절대 추가하지 마세요. 이미 한국어인 부분은 "
    "그대로 두고, 번역된 본문만 응답하세요."
)

# Marker that separates the .md metadata header from the actual caption body
# (see core.build_markdown). Translation should only touch the body.
_TRANSCRIPT_MARKER = "## Transcript"


def transcript_body(text: str) -> str:
    """Return just the caption body of an extracted .md, dropping the header.

    The metadata header (title, URL, extraction time) isn't worth translating
    and confuses the model (it replies "nothing to translate" for a header-only
    chunk). Everything after the ``## Transcript`` marker is the body; if the
    marker is absent (e.g. raw pasted text) the input is returned unchanged.
    """
    idx = text.find(_TRANSCRIPT_MARKER)
    if idx == -1:
        return text
    return text[idx + len(_TRANSCRIPT_MARKER):].lstrip("\n")

# Cap attached transcript text so a long video can't overflow the prompt; the
# excess is dropped with a visible "…생략" marker in the system message. Keep
# this in sync with the built-in model's n_ctx (local_llm._get_llm): for Korean
# ~1 char ≈ 1 token, so 12000 chars ≈ 12k tokens, which must fit under n_ctx.
MAX_CONTEXT_CHARS = 12000

# Per-chunk input size for the built-in model's chunked translation. Translation
# needs room for BOTH the input and a similar-length output under n_ctx=16384, so
# this is kept well below MAX_CONTEXT_CHARS: ~4000 input chars + ~4000+ output +
# system prompt all fit comfortably with margin.
TRANSLATE_CHUNK_CHARS = 4000


def _wrap_long_line(line: str, max_chars: int) -> list[str]:
    """Break a single over-cap line into pieces no longer than ``max_chars``.

    Prefers word (whitespace) boundaries; a single word longer than the cap
    (e.g. spaceless CJK auto-captions) is sliced at character boundaries. This
    guarantees the cap even when a transcript is one long punctuation-less line.
    """
    pieces: list[str] = []
    current = ""
    for word in line.split(" "):
        if len(word) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            for i in range(0, len(word), max_chars):
                pieces.append(word[i:i + max_chars])
            continue
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= max_chars:
            current = candidate
        else:
            pieces.append(current)
            current = word
    if current:
        pieces.append(current)
    return pieces


def split_for_translation(text: str,
                          max_chars: int = TRANSLATE_CHUNK_CHARS) -> list[str]:
    """Split transcript text into translation chunks, each no longer than ``max_chars``.

    Accumulates whole lines until adding the next would exceed ``max_chars``,
    then starts a new chunk — so a sentence/line is never cut mid-way (the
    transcript .md is normally line-broken per sentence/paragraph). A single
    line longer than the cap (auto-captions can be one giant punctuation-less
    line) is broken further at word, then character, boundaries via
    ``_wrap_long_line`` so every chunk fits the model's context window. Returns
    ``[]`` for empty or whitespace-only input.
    """
    if not text or not text.strip():
        return []
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_wrap_long_line(line, max_chars))
            continue
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks


def build_messages(user_text: str, history=None, transcripts=None,
                   system_prompt: str = DEFAULT_SYSTEM_PROMPT,
                   max_chars: int | None = MAX_CONTEXT_CHARS) -> list[dict]:
    """Assemble the OpenAI `messages` array for one turn.

    `history` is prior [{"role","content"}, ...] turns (user/assistant only).
    `transcripts` is an iterable of (name, text); each is appended to the system
    message as a labelled, length-capped block.
    `max_chars` caps each transcript block (None = no limit, for external LLMs
    with large context windows; built-in model uses MAX_CONTEXT_CHARS).
    """
    system = system_prompt
    for name, text in (transcripts or []):
        text = text or ""
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars] + "\n…(이하 생략: 자막이 길어 일부만 포함됨)"
        system += f"\n\n=== 자막: {name} ===\n{text}"

    messages = [{"role": "system", "content": system}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_text})
    return messages


def stream_chat(messages, base_url: str, model: str, *, api_key: str = "",
                temperature: float = 0.3, on_token=None, should_cancel=None,
                timeout: int = 120) -> str:
    """Stream a chat completion, calling `on_token(str)` per delta.

    Returns the full assembled response text. Raises LLMError on any connection
    or server-side failure, with a message safe to show the user. Honours
    `should_cancel()` between chunks for prompt cancellation.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }

    try:
        resp = requests.post(url, json=payload, headers=headers,
                             stream=True, timeout=timeout)
    except requests.exceptions.ConnectionError:
        raise LLMError(
            f"LLM 백엔드에 연결할 수 없습니다: {url}\n"
            "서버가 실행 중인지, 주소/포트가 맞는지 확인하세요."
        )
    except requests.RequestException as e:
        raise LLMError(f"요청 실패: {e}")

    if resp.status_code != 200:
        snippet = resp.text[:300].strip()
        resp.close()
        raise LLMError(f"백엔드 오류 (HTTP {resp.status_code}): {snippet}")

    parts: list[str] = []
    try:
        for line in resp.iter_lines(decode_unicode=True):
            if should_cancel and should_cancel():
                break
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or [{}]
            delta = choices[0].get("delta") or {}
            chunk = delta.get("content")
            if chunk:
                parts.append(chunk)
                if on_token:
                    on_token(chunk)
    except requests.RequestException as e:
        raise LLMError(f"스트리밍 중 연결이 끊겼습니다: {e}")
    finally:
        resp.close()

    return "".join(parts)
