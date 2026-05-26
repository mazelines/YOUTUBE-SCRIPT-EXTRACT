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

# Cap attached transcript text so a long video can't overflow the prompt; the
# excess is dropped with a visible "…생략" marker in the system message. Keep
# this in sync with the built-in model's n_ctx (local_llm._get_llm): for Korean
# ~1 char ≈ 1 token, so 12000 chars ≈ 12k tokens, which must fit under n_ctx.
MAX_CONTEXT_CHARS = 12000


def build_messages(user_text: str, history=None, transcripts=None,
                   system_prompt: str = DEFAULT_SYSTEM_PROMPT) -> list[dict]:
    """Assemble the OpenAI `messages` array for one turn.

    `history` is prior [{"role","content"}, ...] turns (user/assistant only).
    `transcripts` is an iterable of (name, text); each is appended to the system
    message as a labelled, length-capped block.
    """
    system = system_prompt
    for name, text in (transcripts or []):
        text = text or ""
        if len(text) > MAX_CONTEXT_CHARS:
            text = text[:MAX_CONTEXT_CHARS] + "\n…(이하 생략: 자막이 길어 일부만 포함됨)"
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
