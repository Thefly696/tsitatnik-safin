import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import update_widget as app
from scripts.update_widget import (
    UpdateError, atomic_write_json, build_updated_data, clean_for_filter,
    deduplicate_quotes, enforce_summary_limit, is_text_candidate,
    load_data, parse_channel_html, prune_quotes, validate_model_results,
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
<div class="tgme_widget_message" data-post="another/999">
  <div class="tgme_widget_message_date"><time datetime="2026-11-23T10:00:00+00:00"></time></div>
  <div class="tgme_widget_message_text">Пост другого канала игнорируется.</div>
</div>
"""


class ParserTests(unittest.TestCase):
    def test_extracts_fields_and_br_newline(self):
        posts = parse_channel_html(HTML, "safin4")
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["post_id"], "123")
        self.assertEqual(posts[0]["source_url"], "https://t.me/safin4/123")
        self.assertEqual(posts[0]["published_at"], "2026-11-24T10:00:00+00:00")
        self.assertIn("\n", posts[0]["text"])

    def test_empty_blocked_and_wrong_channel_html_fail(self):
        for html in ("", " ", "<html>blocked</html>",
                     '<div class="tgme_widget_message" data-post="other/1"></div>'):
            with self.subTest(html=html), self.assertRaises(UpdateError):
                parse_channel_html(html, "safin4")


class FilterTests(unittest.TestCase):
    def test_strips_urls_mentions_hashtags_and_whitespace(self):
        self.assertEqual(clean_for_filter("Текст https://x.test @person #тег   продолжение"),
                         "Текст продолжение")

    def test_filters_media_and_short_text(self):
        self.assertFalse(is_text_candidate("https://t.me/demo @channel #video"))
        self.assertFalse(is_text_candidate("Короткий текст на 20 символов"))
        self.assertTrue(is_text_candidate("Содержательная психологическая публикация длиной более пятидесяти символов."))

    def test_summary_cleans_quotes_and_newlines(self):
        self.assertEqual(enforce_summary_limit(' «Первая\n\nстрока.» '), "Первая строка.")
        self.assertEqual(enforce_summary_limit(' "«Смысл мысли.»" '), "Смысл мысли.")
        self.assertEqual(enforce_summary_limit('"Обычная цитата"'), "Обычная цитата")

    def test_summary_accepts_up_to_190_characters(self):
        self.assertEqual(len(enforce_summary_limit("я" * 190)), 190)

    def test_prompt_requires_insight_not_plot_description(self):
        prompt = app.summarize_prompt([{"post_id": "1", "text": "История о выборе и ответственности."}])
        self.assertIn("извлеки главный психологический инсайт", prompt)
        self.assertIn("не пересказывай сюжет", prompt)
        self.assertIn("не более 190 символов", prompt)
        self.assertIn("СТРОГО ЗАПРЕЩЕНЫ", prompt)

    def test_summary_limit_and_whole_word_truncation(self):
        result = enforce_summary_limit("слово " * 40)
        self.assertLessEqual(len(result), 190)
        self.assertTrue(result.endswith("…"))
        self.assertFalse(result.endswith("сло…"))

    def test_long_single_word_is_bounded(self):
        result = enforce_summary_limit("я" * 200)
        self.assertEqual(len(result), 190)
        self.assertEqual(result[-1], "…")


class GeminiTests(unittest.TestCase):
    @patch.object(app.time, "sleep")
    @patch.object(app.requests, "post")
    def test_payload_timeout_and_retry(self, post_mock, sleep_mock):
        retry = type("Response", (), {"status_code": 429})()
        ok = type("Response", (), {
            "status_code": 200, "raise_for_status": lambda self: None,
            "json": lambda self: {"candidates": [{"content": {"parts": [{"text": "[]"}]}}]},
        })()
        post_mock.side_effect = [retry, ok]
        self.assertEqual(app.gemini_request("prompt", "secret", "gemini-4-hydrogen", app.RESULT_SCHEMA), [])
        self.assertEqual(post_mock.call_count, 2)
        sleep_mock.assert_called_once_with(5)
        args = post_mock.call_args.kwargs
        self.assertEqual(args["timeout"], 90)
        self.assertEqual(args["headers"]["x-goog-api-key"], "secret")
        config = args["json"]["generationConfig"]
        self.assertEqual(config["thinkingConfig"], {"thinkingLevel": "low"})
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertIn("responseSchema", config)
        for key in ("temperature", "topP", "topK", "thinkingBudget"):
            self.assertNotIn(key, config)

    @patch.object(app.time, "sleep")
    @patch.object(app.requests, "post")
    @patch.dict("os.environ", {
        "GEMINI_FALLBACK_MODELS": "gemini-4-hydrogen,gemini-3.5-flash-lite,gemini-3.1-flash-lite"
    })
    def test_503_switches_to_fallback_without_thinking_config(self, post_mock, sleep_mock):
        unavailable = type("Response", (), {"status_code": 503})()
        success = type("Response", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"candidates": [{"content": {"parts": [{"text": "[]"}]}}]},
        })()
        post_mock.side_effect = [unavailable, unavailable, unavailable, success]

        result = app.gemini_request(
            "prompt", "test-key", "gemini-4-hydrogen", app.RESULT_SCHEMA
        )

        self.assertEqual(result, [])
        self.assertEqual(post_mock.call_count, 4)
        self.assertEqual([call.args[0].rsplit("/", 1)[-1].split(":")[0]
                          for call in post_mock.call_args_list],
                         ["gemini-4-hydrogen"] * 3 + ["gemini-3.5-flash-lite"])
        self.assertEqual([call.kwargs["json"]["generationConfig"].get("thinkingConfig")
                          for call in post_mock.call_args_list],
                         [{"thinkingLevel": "low"}] * 3 + [None])
        self.assertEqual([call.kwargs["timeout"] for call in post_mock.call_args_list], [90] * 4)
        self.assertEqual([call.kwargs["json"]["generationConfig"]["responseMimeType"]
                          for call in post_mock.call_args_list], ["application/json"] * 4)
        self.assertEqual([call.kwargs["json"]["generationConfig"]["responseSchema"]
                          for call in post_mock.call_args_list], [app.RESULT_SCHEMA] * 4)
        self.assertEqual([call.kwargs["headers"]["x-goog-api-key"]
                          for call in post_mock.call_args_list], ["test-key"] * 4)
        self.assertEqual([call.kwargs["json"]["generationConfig"].get("thinkingConfig")
                          for call in post_mock.call_args_list][-1], None)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [5, 15])

    def test_tags_are_normalized_filtered_deduplicated_and_limited_to_two(self):
        meaningful = {"post_id": "1", "is_meaningful": True, "summary": "Текст",
                      "tags": [" #Границы ", "отношения", "НЕИЗВЕСТНЫЙ", "ГРАНИЦЫ", "Семья"]}
        rejected = {"post_id": "2", "is_meaningful": False, "summary": "",
                    "tags": ["Отношения"]}
        result = validate_model_results([meaningful, rejected], {"1", "2"})
        self.assertEqual(result["1"]["tags"], ["Границы", "Отношения"])
        self.assertEqual(result["2"]["tags"], [])
        self.assertEqual(app.normalize_tags(["## границы ", "#ОТНОШЕНИЯ"]), ["Границы", "Отношения"])
        self.assertEqual(app.normalize_tags(["Посторонний", None, "Семья"]), ["Семья"])
        self.assertEqual(app.RESULT_SCHEMA["items"]["properties"]["tags"]["type"], "ARRAY")
        self.assertIn("tags", app.RESULT_SCHEMA["items"]["required"])
        prompt = app.summarize_prompt([{"post_id": "1", "text": "Содержательный текст"}])
        for tag in app.ALLOWED_TAGS:
            self.assertIn(tag, prompt)

    def test_meaningful_result_without_allowed_tags_is_rejected(self):
        item = {"post_id": "1", "is_meaningful": True, "summary": "Текст",
                "tags": ["неизвестная категория"]}
        with self.assertRaises(UpdateError):
            validate_model_results([item], {"1"})

    def test_model_results_are_complete_and_unique(self):
        a = {"post_id": "1", "is_meaningful": True, "summary": "Текст", "tags": ["Эмоции"]}
        b = {"post_id": "2", "is_meaningful": False, "summary": "", "tags": []}
        self.assertEqual(set(validate_model_results([a, b], {"1", "2"})), {"1", "2"})
        for items in ([a], [a, {**b, "post_id": "3"}], [a, a]):
            with self.subTest(items=items), self.assertRaises(UpdateError):
                validate_model_results(items, {"1", "2"})


class FeedTests(unittest.TestCase):
    def test_deduplicates_sorts_and_prefers_new_entry(self):
        old = [{"post_id": "10", "summary": "old"}, {"post_id": "9", "summary": "nine"}]
        new = [{"post_id": "10", "summary": "updated"}, {"post_id": "11", "summary": "eleven"}]
        result = deduplicate_quotes(old, new)
        self.assertEqual([q["post_id"] for q in result], ["11", "10", "9"])
        self.assertEqual(result[1]["summary"], "updated")

    def test_prunes_old_quotes_but_keeps_newest_if_all_old(self):
        now = datetime(2026, 11, 25, tzinfo=timezone.utc)
        recent = {"post_id": "2", "published_at": "2026-11-24T00:00:00Z"}
        old = {"post_id": "1", "published_at": "2026-10-01T00:00:00Z"}
        self.assertEqual(prune_quotes([recent, old], now), [recent])
        self.assertEqual(prune_quotes([old], now), [old])

    def test_update_preserves_app_update_and_schema(self):
        metadata = {"latest_version_code": 7, "latest_version_name": "1.2.3",
                    "download_url": "https://example.test/release.apk"}
        original = {"schema_version": 1, "last_checked_post_id": "10",
                    "updated_at": "2026-11-25T00:00:00Z", "app_update": metadata, "quotes": []}
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        updated = build_updated_data(original, [
            {"post_id": "11", "published_at": "2026-11-25T11:00:00Z", "summary": "Смысл",
             "tags": ["Границы"]}
        ], 11, now)
        self.assertEqual(updated["app_update"], metadata)
        self.assertEqual(updated["schema_version"], 1)
        self.assertEqual(updated["last_checked_post_id"], "11")
        self.assertEqual(updated["quotes"][0]["tags"], ["Границы"])
        self.assertEqual(updated["quotes"][1]["tags"], [])


class JsonSafetyTests(unittest.TestCase):
    def test_force_regenerate_flag_is_case_insensitive(self):
        for value in ("true", "TRUE", "1", "yes", "YES", " on ", "false", "0", ""):
            with self.subTest(value=value):
                self.assertEqual(app.is_force_regenerate(value),
                                 value.strip().casefold() in {"true", "1", "yes", "on"})

    def test_atomic_utf8_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "widget_data.json"
            payload = {"schema_version": 1, "quotes": [{"summary": "Психология"}]}
            atomic_write_json(path, payload)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            tagged = {"schema_version": 1, "quotes": [{"post_id": "1", "tags": ["Границы"]}]}
            atomic_write_json(path, tagged)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["quotes"][0]["tags"], ["Границы"])
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_invalid_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "widget_data.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(UpdateError):
                load_data(path)

    @patch.object(app, "gemini_request", side_effect=UpdateError("Gemini unavailable"))
    @patch.object(app, "fetch_posts")
    def test_force_regenerate_ignores_old_feed_and_preserves_json_on_api_error(self, fetch_mock, _gemini_mock):
        original = {
            "schema_version": 1, "last_checked_post_id": "987",
            "updated_at": "2026-11-25T00:00:00Z",
            "app_update": {"latest_version_code": 3, "latest_version_name": "1.2.0"},
            "quotes": [{"post_id": "987", "published_at": "2026-11-25T00:00:00Z",
                        "source_url": "https://t.me/safin4/987", "summary": "Старая цитата"}],
        }
        post = {"post_id": "1000", "published_at": "2026-11-26T00:00:00Z",
                "source_url": "https://t.me/safin4/1000", "text": "Содержательный текст публикации " * 4}
        fetch_mock.return_value = ([post], 1000)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "widget_data.json"
            path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            before = path.read_bytes()
            with patch.object(app, "DATA_PATH", path), patch.dict("os.environ", {
                "LLM_API_KEY": "test-key", "FORCE_REGENERATE": "true"
            }):
                with self.assertRaises(UpdateError):
                    app.run()
            passed_data = fetch_mock.call_args.args[1]
            self.assertEqual(passed_data["quotes"], [])
            self.assertEqual(passed_data["last_checked_post_id"], "0")
            self.assertEqual(path.read_bytes(), before)

    @patch.object(app, "fetch_posts", side_effect=RuntimeError("network error"))
    def test_network_failure_does_not_overwrite_json(self, _mock):
        original = {"schema_version": 1, "last_checked_post_id": "0",
                    "updated_at": "2026-11-25T00:00:00Z", "app_update": {"latest_version_code": 1},
                    "quotes": []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "widget_data.json"
            path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            before = path.read_bytes()
            with patch.object(app, "DATA_PATH", path), patch.dict("os.environ", {"LLM_API_KEY": "test-key"}):
                with self.assertRaises(RuntimeError):
                    app.run()
            self.assertEqual(path.read_bytes(), before)

    def test_failed_replace_leaves_existing_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "widget_data.json"
            path.write_text('{"old": true}\n', encoding="utf-8")
            with patch.object(app.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    atomic_write_json(path, {"new": True})
            self.assertEqual(path.read_text(encoding="utf-8"), '{"old": true}\n')
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
