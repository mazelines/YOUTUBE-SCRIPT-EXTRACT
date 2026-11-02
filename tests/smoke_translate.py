"""End-to-end smoke test for chunked translation with the REAL built-in model.

Not part of the unit suite (it loads the GGUF model and is slow). It exercises
the exact production path the GUI's BuiltinTranslateWorker._run_chunked uses --
llm.split_for_translation -> build_messages(TRANSLATE_SYSTEM_PROMPT) ->
local_llm.stream_local_chat -- to confirm a long source is translated all the
way to the end with no truncation.

Completeness is measured with DISTINCT numbered markers, one per source line
(M0001, M0002, ...). Numbers survive translation verbatim, so the fraction of
markers that reappear in the output is a precise coverage signal: a chunk that
got truncated mid-way would drop its tail markers. The fixture is deliberately
non-repetitive -- repeated identical text makes the model de-duplicate it,
which masks truncation.

Run:  python tests/smoke_translate.py
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:  # make Korean/streamed output legible regardless of the console codepage
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from yt_extractor import local_llm
from yt_extractor.llm import (
    build_messages, split_for_translation, transcript_body,
    TRANSLATE_SYSTEM_PROMPT, TRANSLATE_CHUNK_CHARS,
)

# Vocabulary pools used to compose lexically-unique lines. A small model
# collapses repetitive input (it de-duplicates near-identical sentences), which
# masks truncation -- so every line must differ in its CONTENT words, not just a
# trailing marker token (the model treats a bare marker as droppable noise).
SUBJECTS = ["The engineer", "A researcher", "The pilot", "Our teacher",
            "The farmer", "A novelist", "The surgeon", "A diplomat",
            "The architect", "A musician", "The sailor", "A geologist"]
VERBS = ["examined", "rebuilt", "questioned", "celebrated", "measured",
         "abandoned", "redesigned", "documented", "praised", "dismantled"]
OBJECTS = ["the ancient bridge", "a frozen river", "the copper telescope",
           "an overgrown garden", "the silent harbor", "a cracked mosaic",
           "the desert highway", "an old lighthouse", "the marble staircase",
           "a hidden valley", "the iron foundry", "a coral reef"]
PLACES = ["in Lisbon", "near the canyon", "before sunrise", "during the storm",
          "after the harvest", "beyond the ridge", "under the bridge",
          "across the strait", "inside the cavern", "along the coastline"]


def build_source(n_lines: int = 120) -> tuple[str, list[str]]:
    """Return (source_text, ordered_markers): n_lines lexically-distinct
    sentences, each ending in a unique Mxxxx marker, spanning several chunks."""
    lines, markers = [], []
    for i in range(1, n_lines + 1):
        m = f"M{i:04d}"
        markers.append(m)
        sentence = (f"{SUBJECTS[i % len(SUBJECTS)]} "
                    f"{VERBS[i % len(VERBS)]} "
                    f"{OBJECTS[(i * 3) % len(OBJECTS)]} "
                    f"{PLACES[(i * 7) % len(PLACES)]}, marker {m}.")
        lines.append(sentence)
    return "\n".join(lines) + "\n", markers


def _translate_chunks(chunks: list[str]) -> tuple[list[str], float]:
    """Replicate BuiltinTranslateWorker._run_chunked without Qt; return
    (per-chunk outputs, elapsed seconds)."""
    print("\nloading model (first call may take a while)...")
    local_llm.ensure_loaded(on_status=lambda msg: print(f"  [{msg}]"))
    outputs: list[str] = []
    t0 = time.time()
    for i, chunk in enumerate(chunks, 1):
        print(f"\n--- translating chunk {i}/{len(chunks)} ({len(chunk)} chars) ---")
        messages = build_messages(chunk, system_prompt=TRANSLATE_SYSTEM_PROMPT)
        out = local_llm.stream_local_chat(
            messages, on_token=lambda t: print(t, end="", flush=True),
        )
        outputs.append(out)
        print()
        assert out.strip(), f"chunk {i} produced empty output"
    return outputs, time.time() - t0


def run_synthetic() -> int:
    """Marker-based run: precise end-to-end coverage on a controlled source."""
    source, markers = build_source()
    body = transcript_body(source)
    chunks = split_for_translation(body)

    print(f"source chars : {len(body)}")
    print(f"markers       : {len(markers)} ({markers[0]}..{markers[-1]})")
    print(f"chunk count  : {len(chunks)}")
    print(f"chunk sizes  : {[len(c) for c in chunks]}")
    assert len(chunks) >= 3, "source should split into several chunks"
    for c in chunks:
        assert len(c) <= TRANSLATE_CHUNK_CHARS, "a chunk exceeded the cap"

    outputs, elapsed = _translate_chunks(chunks)
    joined = "\n\n".join(outputs)

    found = set(re.findall(r"M\d{4}", joined))
    present = [m for m in markers if m in found]
    missing = [m for m in markers if m not in found]
    coverage = len(present) / len(markers)

    print("\n" + "=" * 60)
    print(f"chunks translated : {len(outputs)}/{len(chunks)}")
    print(f"output chars      : {len(joined)}")
    print(f"elapsed           : {elapsed:.1f}s")
    print(f"marker coverage   : {len(present)}/{len(markers)} ({coverage:.0%})")
    if missing:
        print(f"missing markers   : {missing[:20]}{' ...' if len(missing) > 20 else ''}")
    print(f"last marker {markers[-1]} present : {markers[-1] in found}")

    ok = (len(outputs) == len(chunks)
          and all(o.strip() for o in outputs)
          and markers[-1] in found
          and coverage >= 0.80)
    print("\nSMOKE TEST:", "PASS" if ok else "REVIEW")
    return 0 if ok else 1


def run_file(path: Path) -> int:
    """Real-transcript run: translate an extracted .md end to end.

    No synthetic markers exist, so completeness is judged by (1) every chunk
    producing output, (2) no chunk's Korean output collapsing to a tiny
    fraction of its input (a sign of de-dup/truncation), and (3) the final
    chunk being printed in full so the human can confirm the closing lines of
    the source were translated.
    """
    raw = path.read_text(encoding="utf-8")
    body = transcript_body(raw)
    chunks = split_for_translation(body)

    print(f"file         : {path.name}")
    print(f"source chars : {len(body)} (raw {len(raw)})")
    print(f"chunk count  : {len(chunks)}")
    print(f"chunk sizes  : {[len(c) for c in chunks]}")
    assert chunks, "transcript produced no chunks"
    for c in chunks:
        assert len(c) <= TRANSLATE_CHUNK_CHARS, "a chunk exceeded the cap"
    # Show the true tail of the source so the final-chunk translation can be
    # checked against it by eye.
    print(f"source tail  : ...{body.rstrip()[-160:]!r}")

    outputs, elapsed = _translate_chunks(chunks)
    joined = "\n\n".join(outputs)

    # Per-chunk collapse check: Korean prose is usually a similar order of
    # magnitude in characters to its English source. A chunk whose output is a
    # tiny fraction of its input likely got de-duplicated or cut short.
    print("\n" + "=" * 60)
    print("per-chunk output/input char ratio:")
    suspicious = []
    for i, (c, o) in enumerate(zip(chunks, outputs), 1):
        ratio = len(o) / max(1, len(c))
        flag = "  <-- LOW" if ratio < 0.35 else ""
        if ratio < 0.35:
            suspicious.append(i)
        print(f"  chunk {i:2d}: in={len(c):4d} out={len(o):4d} ratio={ratio:.2f}{flag}")

    print(f"\nchunks translated : {len(outputs)}/{len(chunks)}")
    print(f"output chars      : {len(joined)}")
    print(f"elapsed           : {elapsed:.1f}s")
    if suspicious:
        print(f"low-ratio chunks  : {suspicious} (review for collapse/truncation)")

    ok = (len(outputs) == len(chunks)
          and all(o.strip() for o in outputs)
          and not suspicious)
    print("\nSMOKE TEST:", "PASS" if ok else "REVIEW")
    print("(Confirm the LAST chunk above translates the source tail to the end.)")
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) > 1:
        return run_file(Path(sys.argv[1]))
    return run_synthetic()


if __name__ == "__main__":
    raise SystemExit(main())
