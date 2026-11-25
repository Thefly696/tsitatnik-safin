#!/usr/bin/env python3
"""Fetch Telegram channel posts, summarize meaningful content, and atomically update Pages JSON."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "docs" / "widget_data.json"
TELEGRAM_BASE = "https://t.me/s/"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{2,}")
HASHTAG_RE = re.compile(r"(?<!\w)#[^\s#]+")
POST_ID_RE = re.compile(r"^[A-Za-z0-9_]+/(\d+)$")


class UpdateError(RuntimeError):
    """Raised when remote data cannot be trusted or processed."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def clean_for_filter(text: str) -> str:
    text = URL_RE.sub(" ", text)
    text = MENTION_RE.sub(" ", text)
    text = HASHTAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_channel_html(html: str, channel: str) -> list[dict[str, str]]:
    if not html or not html.strip():
        raise UpdateError("Telegram вернул пустой HTML")
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select(".tgme_widget_message")
    if not blocks:
        raise UpdateError("В HTML Telegram не найдены блоки .tgme_widget_message")

    posts: list[dict[str, str]] = []
    for block in blocks:
        data_post = (block.get("data-post") or "").strip()
        match = POST_ID_RE.fullmatch(data_post)
        if not match:
            continue
        post_id = match.group(1)
        time_node = block.select_one(".tgme_widget_message_date time[datetime]")
        if time_node is None:
            continue
        published_at = time_node.get("datetime", "").strip()
        try:
            parse_datetime(published_at)
        except (ValueError, TypeError):
            continue

        text_node = block.select_one(".tgme_widget_message_text")
        raw_text = ""
        if text_node is not None:
            # Convert HTML line breaks to newlines before extracting visible text.
            for br in text_node.find_all("br"):
                br.replace_with("\n")
            raw_text = text_node.get_text(separator="", strip=False)
            raw_text = re.sub(r"[ \t]+\n", "\n", raw_text).strip()

        posts.append({
            "post_id": post_id,
            "source_url": f"https://t.me/{data_post}",
            "published_at": published_at,
            "text": raw_text,
            "channel": channel,
        })
    return posts


def telegram_headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def fetch_telegram_page(channel: str, before: str | None = None) -> str:
    url = TELEGRAM_BASE + channel
    if before:
        url += "?" + urlencode({"before": before})
    response = requests.get(url, headers=telegram_headers(), timeout=30)
    response.raise_for_status()
    if not response.text or not response.text.strip():
        raise UpdateError("Telegram вернул пустой HTML")
    return response.text


def fetch_posts(channel: str, existing: dict[str, Any], now: datetime) -> tuple[list[dict[str, str]], int]:
    last_id = str(existing.get("last_checked_post_id", "0"))
    initial_backfill = not existing.get("quotes") and last_id == "0"
    cutoff = now - timedelta(days=30)
    collected: dict[str, dict[str, str]] = {}
    max_checked_id = int(last_id) if last_id.isdigit() else 0
    before: str | None = None
    page_limit = 12 if initial_backfill else 1

    for page_index in range(page_limit):
        html = fetch_telegram_page(channel, before)
        page_posts = parse_channel_html(html, channel)
        if not page_posts:
            raise UpdateError("На странице Telegram нет корректно разобранных публикаций")

        page_ids: list[int] = []
        for post in page_posts:
            post_num = int(post["post_id"])
            page_ids.append(post_num)
            if post["text"].strip():
                max_checked_id = max(max_checked_id, post_num)
            # Backfill is bounded by the 30-day window; normal mode only checks new IDs.
            if initial_backfill:
                if parse_datetime(post["published_at"]) >= cutoff and len(clean_for_filter(post["text"])) >= 50:
                    collected[post["post_id"]] = post
            elif post_num > int(last_id) and len(clean_for_filter(post["text"])) >= 50:
                collected[post["post_id"]] = post

        if not initial_backfill:
            break
        oldest_id = min(page_ids)
        oldest_date = min(parse_datetime(p["published_at"]) for p in page_posts)
        if oldest_date < cutoff or page_index == page_limit - 1:
            break
        before = str(oldest_id)
        time.sleep(1.5)

    return sorted(collected.values(), key=lambda p: int(p["post_id"])), max_checked_id


def gemini_request(prompt: str, api_key: str, model: str, response_schema: dict[str, Any]) -> list[dict[str, Any]]:
    url = GEMINI_BASE.format(model=model)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }
    for attempt in range(2):
        response = requests.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=45,
        )
        if response.status_code in (429, 503) and attempt == 0:
            time.sleep(8)
            continue
        response.raise_for_status()
        try:
            body = response.json()
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise UpdateError(f"Некорректный JSON-ответ Gemini: {exc}") from exc
        if not isinstance(parsed, list):
            raise UpdateError("Gemini вернул не массив результатов")
        return parsed
    raise UpdateError("Gemini не ответил после повторной попытки")


RESULT_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "post_id": {"type": "STRING"},
            "is_meaningful": {"type": "BOOLEAN"},
            "summary": {"type": "STRING"},
        },
        "required": ["post_id", "is_meaningful", "summary"],
    },
}


def summarize_prompt(posts: list[dict[str, str]]) -> str:
    instructions = (
        "Ты редактор психологического Telegram-канала. Для каждой публикации верни объект с post_id, "
        "is_meaningful и summary. Отсеивай рекламу, анонсы очных и онлайн-курсов, вебинаров, наборов "
        "на обучение, ссылки на видео/стримы без самостоятельной мысли и организационные объявления "
        "(is_meaningful=false). Для содержательных психологических постов (true) передай суть по-русски "
        "в 1–2 коротких законченных предложениях, строго до 160 символов. Только чистый текст: без "
        "Markdown, эмодзи, хештегов, крайних кавычек и вводных фраз вроде «Автор пишет» или «В этом посте». "
        "Не добавляй фактов, которых нет в публикации. Верни результат для каждого post_id ровно один раз."
    )
    return instructions + "\n\nПубликации (JSON):\n" + json.dumps(
        [{"post_id": p["post_id"], "text": p["text"]} for p in posts],
        ensure_ascii=False,
    )


def validate_model_results(results: list[dict[str, Any]], expected_ids: set[str]) -> dict[str, dict[str, Any]]:
    validated: dict[str, dict[str, Any]] = {}
    for item in results:
        if not isinstance(item, dict):
            raise UpdateError("Gemini вернул элемент неверного типа")
        post_id = str(item.get("post_id", ""))
        if post_id not in expected_ids or post_id in validated:
            raise UpdateError(f"Gemini вернул неожиданный или повторный post_id: {post_id}")
        if not isinstance(item.get("is_meaningful"), bool) or not isinstance(item.get("summary"), str):
            raise UpdateError(f"Gemini вернул некорректные поля для post_id={post_id}")
        validated[post_id] = item
    if set(validated) != expected_ids:
        missing = sorted(expected_ids - set(validated))
        raise UpdateError(f"Gemini не вернул все публикации: {missing}")
    return validated


def shorten_summaries(results: dict[str, dict[str, Any]], api_key: str, model: str) -> None:
    too_long = [pid for pid, item in results.items() if item["is_meaningful"] and len(item["summary"].strip()) > 160]
    if not too_long:
        return
    schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {"post_id": {"type": "STRING"}, "summary": {"type": "STRING"}},
            "required": ["post_id", "summary"],
        },
    }
    prompt = (
        "Сократи каждую русскую формулировку до максимум 145 символов, сохранив основную мысль. "
        "Верни JSON-массив объектов post_id и summary. Без Markdown и кавычек.\n"
        + json.dumps([{"post_id": pid, "summary": results[pid]["summary"]} for pid in too_long], ensure_ascii=False)
    )
    shortened = gemini_request(prompt, api_key, model, schema)
    mapped: dict[str, str] = {}
    for item in shortened:
        if not isinstance(item, dict) or str(item.get("post_id")) not in too_long or not isinstance(item.get("summary"), str):
            raise UpdateError("Gemini вернул некорректный ответ при сокращении")
        mapped[str(item["post_id"])] = item["summary"].strip()
    if set(mapped) != set(too_long):
        raise UpdateError("Gemini не сократил все длинные формулировки")
    for pid, summary in mapped.items():
        results[pid]["summary"] = summary


def enforce_summary_limit(summary: str) -> str:
    summary = re.sub(r"\s+", " ", summary).strip().strip('"«»“”')
    if len(summary) <= 160:
        return summary
    prefix = summary[:159]
    if " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0]
    return prefix.rstrip(" ,;:-") + "…"


def load_data(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UpdateError(f"Не найден файл данных: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Не удалось прочитать JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("quotes"), list):
        raise UpdateError("widget_data.json имеет неверную структуру")
    if "schema_version" not in data or not isinstance(data.get("app_update"), dict):
        raise UpdateError("В widget_data.json отсутствуют обязательные поля")
    return data


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def run() -> bool:
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        raise UpdateError("Не задана обязательная переменная окружения LLM_API_KEY")
    channel = os.environ.get("TG_CHANNEL", "safin4").strip().lstrip("@")
    model = os.environ.get("GEMINI_MODEL", "gemini-4-hydrogen").strip()
    if not channel or not model:
        raise UpdateError("TG_CHANNEL и GEMINI_MODEL не должны быть пустыми")

    data = load_data(DATA_PATH)
    now = utc_now()
    posts, max_checked_id = fetch_posts(channel, data, now)
    if not posts:
        print("Новых подходящих текстовых публикаций нет; файл не изменён.")
        return False

    initial_backfill = not data.get("quotes") and str(data.get("last_checked_post_id", "0")) == "0"
    batch_size = 8 if initial_backfill else len(posts)
    additions: list[dict[str, str]] = []
    for offset in range(0, len(posts), batch_size):
        batch = posts[offset:offset + batch_size]
        raw_results = gemini_request(summarize_prompt(batch), api_key, model, RESULT_SCHEMA)
        mapped = validate_model_results(raw_results, {p["post_id"] for p in batch})
        shorten_summaries(mapped, api_key, model)
        by_id = {p["post_id"]: p for p in batch}
        for post_id, result in mapped.items():
            summary = enforce_summary_limit(result["summary"])
            if result["is_meaningful"] and summary:
                post = by_id[post_id]
                additions.append({
                    "post_id": post_id,
                    "published_at": post["published_at"],
                    "source_url": post["source_url"],
                    "summary": summary,
                })
        if initial_backfill and offset + batch_size < len(posts):
            time.sleep(4.5)

    by_id: dict[str, dict[str, Any]] = {}
    for quote in data["quotes"] + additions:
        if isinstance(quote, dict) and quote.get("post_id") is not None:
            by_id[str(quote["post_id"])] = quote
    merged = sorted(by_id.values(), key=lambda q: int(q["post_id"]), reverse=True)
    cutoff = now - timedelta(days=30)
    fresh = []
    for quote in merged:
        try:
            is_recent = parse_datetime(str(quote["published_at"])) >= cutoff
        except (KeyError, ValueError, TypeError):
            is_recent = False
        if is_recent:
            fresh.append(quote)
    if not fresh and merged:
        fresh = [merged[0]]

    updated = dict(data)
    updated["quotes"] = fresh
    updated["last_checked_post_id"] = str(max_checked_id)
    updated["updated_at"] = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    atomic_write_json(DATA_PATH, updated)
    print(f"Обновлено: добавлено {len(additions)} цитат; всего в ленте {len(fresh)}.")
    return True


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        sys.exit(1)
