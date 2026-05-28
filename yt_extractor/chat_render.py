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
  body { font-family: 'Segoe UI', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
         font-size: 13px; color: #222; background: #f6f6f6; }
  .turn { padding: 10px 14px; margin: 6px 0; }
  .turn-user { background: #eef5ff; border-left: 3px solid #1769aa; }
  .turn-ai   { background: #f6f6f6; border-left: 3px solid #1b7f3b; }
  .turn-err  { background: #fdecea; border-left: 3px solid #b00020; color: #7a0014; }
  .role      { font-weight: bold; margin-bottom: 4px; }
  .role-user { color: #1769aa; }
  .role-ai   { color: #1b7f3b; }
  .role-err  { color: #b00020; }
  .body      { line-height: 1.5; }
  .body p    { margin: 4px 0; }
  .body h1, .body h2, .body h3, .body h4 { margin: 10px 0 4px; }
  .body h1   { font-size: 18px; }
  .body h2   { font-size: 16px; }
  .body h3   { font-size: 14px; }
  .body ul, .body ol { margin: 4px 0 4px 20px; }
  .body li   { margin: 2px 0; }
  .body blockquote { margin: 6px 0; padding: 4px 10px;
                     background: #eef2f7; border-left: 3px solid #99a; color: #444; }
  .body code { background: #e9ecef; padding: 0 3px;
               font-family: 'Consolas', 'Menlo', 'D2Coding', monospace; }
  .body pre  { background: #1e1e1e; color: #dcdcdc; padding: 10px;
               font-family: 'Consolas', 'Menlo', 'D2Coding', monospace; }
  .body pre code { background: transparent; padding: 0; }
  .body table { border-collapse: collapse; margin: 6px 0; }
  .body th, .body td { border: 1px solid #ccc; padding: 4px 8px; }
  .body th   { background: #eee; }
  .cursor    { color: #888; }
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
            parts.append(_render_user(text))
        elif role == "assistant":
            tail = streaming and (i == len(turns) - 1)
            parts.append(_render_assistant(text, tail))
    if error:
        parts.append(_render_error(error))
    return f"<html><head>{_DOC_STYLE}</head><body>{''.join(parts)}</body></html>"
