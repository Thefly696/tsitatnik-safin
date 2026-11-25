import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.update_widget import (
    UpdateError,
    atomic_write_json,
    clean_for_filter,
    enforce_summary_limit,
    load_data,
    parse_channel_html,
    validate_model_results,
)


HTML = """
<div class="tgme_widget_message" data-post="safin4/123">
  <div class="tgme_widget_message_date"><time datetime="2026-11-24T10:00:00+00:00"></time></div>
  <div class="tgme_widget_message_text">Первая строка<br>Вторая строка и продолжение текста.</div>
</div>
<div class="tgme_widget_message" data-post="safin4/122">
  <div class="tgme_widget_message_date"><time datetime="2026-11-23T10:00:00+00:00"></time></div>
  <div class="tgme_widget_message_text">Коротко</div>
</div>
"""


class ParserTests(unittest.TestCase):
    def test_extracts_post_fields_and_line_breaks(self):
        posts = parse_channel_html(HTML, "safin4")
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["post_id"], "123")
        self.assertEqual(posts[0]["source_url"], "https://t.me/safin4/123")
        self.assertEqual(posts[0]["published_at"], "2026-11-24T10:00:00+00:00")
        self.assertIn("\n", posts[0]["text"])

    def test_empty_html_fails_closed(self):
        with self.assertRaises(UpdateError):
            parse_channel_html("  ", "safin4")

    def test_html_without_message_blocks_fails_closed(self):
        with self.assertRaises(UpdateError):
            parse_channel_html("<html><body>blocked</body></html>", "safin4")


class FilterTests(unittest.TestCase):
    def test_removes_urls_mentions_hashtags_and_whitespace(self):
        cleaned = clean_for_filter("Текст https://example.com @person #тег   продолжение")
        self.assertEqual(cleaned, "Текст продолжение")

    def test_summary_is_limited_to_160_characters(self):
        result = enforce_summary_limit("слово " * 40)
        self.assertLessEqual(len(result), 160)
        self.assertTrue(result.endswith("…"))

    def test_short_summary_is_preserved(self):
        self.assertEqual(enforce_summary_limit("Короткая мысль."), "Короткая мысль.")


class JsonSafetyTests(unittest.TestCase):
    def test_atomic_write_produces_valid_utf8_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "widget_data.json"
            payload = {"schema_version": 1, "quotes": [{"summary": "Психология"}]}
            atomic_write_json(path, payload)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_invalid_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "widget_data.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(UpdateError):
                load_data(path)

    def test_model_cannot_return_unknown_or_missing_posts(self):
        expected = {"1", "2"}
        with self.assertRaises(UpdateError):
            validate_model_results(
                [{"post_id": "1", "is_meaningful": True, "summary": "Текст."}],
                expected,
            )
        with self.assertRaises(UpdateError):
            validate_model_results(
                [
                    {"post_id": "1", "is_meaningful": True, "summary": "Текст."},
                    {"post_id": "3", "is_meaningful": False, "summary": ""},
                ],
                expected,
            )


if __name__ == "__main__":
    unittest.main()
