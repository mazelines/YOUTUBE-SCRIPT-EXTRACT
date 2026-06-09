"""Render the chat conversation as styled HTML for QTextBrowser.

The AI replies use markdown (headers, lists, fenced code), but QTextEdit's
``insertPlainText`` showed those as literal ``**bold**`` / ``# Header`` text.
This module converts a turn list to HTML once per re-render so the view shows
proper formatting (LM Studio-style bubbles, monokai code blocks, tables).

Qt's QTextDocument supports only a subset of HTML/CSS — notably it understands
``<style>`` blocks with class selectors and inline styles, so codehilite is run
with ``noclasses=True`` to inline pygments' colors.
"""

from __future__ import annotations

import html

import markdown


# Trailing marker on the in-flight assistant message so the user can tell the
# stream hasn't finished yet. Qt doesn't run CSS animations, so it stays static.
STREAMING_CURSOR = "▍"


# A single Markdown instance is reused (cheaper than re-constructing) and
# ``reset()`` between renders so per-document state (footnote/ref tables) does
# not leak between turns.
_MD = markdown.Markdown(
    extensions=["fenced_code", "tables", "sane_lists", "nl2br", "codehilite"],
    extension_configs={
        "codehilite": {
            # Inline pygments styles — QTextDocument can't resolve a stylesheet
            # class by name reliably across versions.
            "noclasses": True,
            "pygments_style": "monokai",
            "guess_lang": False,
        },
    },
    output_format="html5",
)


def _md_to_html(text: str) -> str:
    _MD.reset()
    return _MD.convert(text or "")


# Bubble + typography styling. Block-element selectors are scoped through
# ``.turn`` classes so they don't fight QTextDocument's defaults elsewhere.
_DOC_STYLE = """
<style>
  body { font-family: 'Gothic A1', 'Apple SD Gothic Neo', 'Malgun Gothic', system-ui, sans-serif;
         font-size: 13.5px; color: #2a2622; background: #efe8da; }
  .turn { padding: 10px 14px; margin: 8px 0; border-radius: 16px; }
  .turn-user { background: #2a2622; color: #ffffff; margin-left: 40px; }
  .turn-ai   { background: transparent; color: #1a1410; margin-right: 40px; }
  .turn-err  { background: #fde6de; border-left: 3px solid #e85d3e; color: #7a0014; }
  .role      { font-weight: bold; margin-bottom: 4px; font-size: 11px; letter-spacing: 0.04em; }
  .role-user { color: #b6b0a0; }
  .role-ai   { color: #7f8a3f; }
  .role-err  { color: #e85d3e; }
  .body      { line-height: 1.62; }
  .body p    { margin: 4px 0; }
  .body h1, .body h2, .body h3, .body h4 { margin: 10px 0 4px; }
  .body h1   { font-size: 18px; }
  .body h2   { font-size: 16px; }
  .body h3   { font-size: 14px; }
  .body ul, .body ol { margin: 4px 0 4px 20px; }
  .body li   { margin: 2px 0; }
  .body blockquote { margin: 6px 0; padding: 4px 10px;
                     background: #f6f2ea; border-left: 3px solid #b3cf4e; color: #575451; }
  .body code { background: #ece5d8; padding: 0 3px;
               font-family: 'JetBrains Mono', 'Consolas', 'Menlo', monospace; font-size: 12.5px; }
  .body pre  { background: #1e1e1e; color: #dcdcdc; padding: 10px; border-radius: 8px;
               font-family: 'JetBrains Mono', 'Consolas', 'Menlo', monospace; }
  .body pre code { background: transparent; padding: 0; }
  .body table { border-collapse: collapse; margin: 6px 0; }
  .body th, .body td { border: 1px solid #ece5d8; padding: 4px 8px; }
  .body th   { background: #f6f2ea; }
  .cursor    { color: #f47458; }
</style>
"""


def _render_user(text: str) -> str:
    safe = html.escape(text).replace("\n", "<br>")
    return (
        '<div class="turn turn-user">'
        '<div class="role role-user">나</div>'
        f'<div class="body"><p>{safe}</p></div>'
        '</div>'
    )


def _render_assistant(text: str, streaming_tail: bool) -> str:
    body = _md_to_html(text)
    if streaming_tail:
        body += f'<span class="cursor">{STREAMING_CURSOR}</span>'
    return (
        '<div class="turn turn-ai">'
        '<div class="role role-ai">AI</div>'
        f'<div class="body">{body}</div>'
        '</div>'
    )


def _render_error(msg: str) -> str:
    safe = html.escape(msg).replace("\n", "<br>")
    return (
        '<div class="turn turn-err">'
        '<div class="role role-err">⚠ 오류</div>'
        f'<div class="body"><p>{safe}</p></div>'
        '</div>'
    )


def render_conversation(turns, *, streaming: bool = False,
                        error: str | None = None) -> str:
    """Build the full chat HTML from ``[{role, content}, ...]``.

    ``streaming=True`` marks the last assistant turn as in-flight (so it gets a
    trailing cursor). ``error`` appends a final error block.
    """
    parts: list[str] = []
    for i, t in enumerate(turns):
        role = t.get("role")
        text = t.get("content", "") or ""
        if role == "user":
            pass  # user prompt is a system instruction — don't display it
        elif role == "assistant":
            tail = streaming and (i == len(turns) - 1)
            parts.append(_render_assistant(text, tail))
    if error:
        parts.append(_render_error(error))
    return f"<html><head>{_DOC_STYLE}</head><body>{''.join(parts)}</body></html>"
