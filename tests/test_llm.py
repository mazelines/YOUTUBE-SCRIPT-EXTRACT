import unittest

from yt_extractor.llm import (
    build_messages, split_for_translation, transcript_body,
    MAX_CONTEXT_CHARS, TRANSLATE_SYSTEM_PROMPT,
)


class TranscriptBodyTests(unittest.TestCase):
    def test_strips_metadata_header_before_transcript_marker(self):
        md = (
            "# Some Title\n\n"
            "- **URL**: https://youtu.be/x\n"
            "- **추출 시각**: 2026-05-25\n\n"
            "---\n\n"
            "## Transcript\n\n"
            "[Music]\nhello world this is the body\n"
        )
        body = transcript_body(md)
        self.assertNotIn("URL", body)
        self.assertNotIn("추출 시각", body)
        self.assertIn("hello world this is the body", body)
        self.assertFalse(body.startswith("\n"))

    def test_returns_full_text_when_no_marker(self):
        plain = "just some text\nwith no header"
        self.assertEqual(transcript_body(plain), plain)


class SplitForTranslationTests(unittest.TestCase):
    def test_empty_or_blank_returns_no_chunks(self):
        self.assertEqual(split_for_translation(""), [])
        self.assertEqual(split_for_translation("   \n  \n"), [])

    def test_short_text_is_a_single_chunk(self):
        text = "first line\nsecond line"
        self.assertEqual(split_for_translation(text, max_chars=4000), [text])

    def test_splits_at_line_boundaries_without_cutting_a_line(self):
        lines = [f"line number {i}" for i in range(10)]
        text = "\n".join(lines)
        chunks = split_for_translation(text, max_chars=30)

        self.assertGreater(len(chunks), 1)
        # No chunk exceeds the limit (every line here is < 30 chars).
        for c in chunks:
            self.assertLessEqual(len(c), 30)
        # Every original line survives intact, in order, with none split.
        rejoined_lines = []
        for c in chunks:
            rejoined_lines.extend(c.split("\n"))
        self.assertEqual(rejoined_lines, lines)

    def test_oversized_line_with_spaces_breaks_at_word_boundaries(self):
        # An auto-caption can be one long punctuation-less line. It must still
        # be broken down so NO chunk exceeds the cap (otherwise it overflows
        # the model's n_ctx and gets truncated).
        words = " ".join(f"word{i}" for i in range(200))  # one long line
        chunks = split_for_translation(words, max_chars=40)

        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 40)
        # Words are never cut in half — every original word survives intact.
        produced = " ".join(chunks).split()
        self.assertEqual(produced, words.split())

    def test_oversized_line_without_spaces_is_hard_sliced(self):
        # CJK captions may have no spaces at all; fall back to character slices
        # so the cap is still respected.
        big = "가" * 100
        chunks = split_for_translation(big, max_chars=20)

        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 20)
        self.assertEqual("".join(chunks), big)  # nothing lost

    def test_every_chunk_respects_cap_for_mixed_content(self):
        text = "short line\n" + (" ".join(["alpha"] * 500)) + "\nanother short"
        chunks = split_for_translation(text, max_chars=50)
        for c in chunks:
            self.assertLessEqual(len(c), 50)


class BuildMessagesTests(unittest.TestCase):
    def test_default_caps_transcript_at_max_context_chars(self):
        long_text = "가" * (MAX_CONTEXT_CHARS + 500)
        msgs = build_messages("질문", transcripts=[("a", long_text)])
        system = msgs[0]["content"]
        self.assertIn("이하 생략", system)

    def test_none_max_chars_keeps_full_transcript(self):
        long_text = "가" * (MAX_CONTEXT_CHARS + 500)
        msgs = build_messages("질문", transcripts=[("a", long_text)], max_chars=None)
        system = msgs[0]["content"]
        self.assertNotIn("이하 생략", system)
        self.assertIn(long_text, system)


if __name__ == "__main__":
    unittest.main()
