#!/usr/bin/env python3
from __future__ import annotations
import json, os, re, sys, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "docs" / "widget_data.json"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
URL_RE = re.compile(r"https?://\S+", re.I)
MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{2,}")
HASHTAG_RE = re.compile(r"(?<!\w)#[^\s#]+")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
SYSTEM_INSTRUCTION = "<role>\nТы редактор сборника практической психологии. Твоя задача — извлечь из каждого поста главную мысль для осмысления, сохранив авторскую глубину и вывод.\n</role>\n\n<rules>\n1. Отбор (is_meaningful): ставь true только если в посте есть самостоятельная психологическая идея. Ставь false для рекламы, анонсов курсов, оргпостов и пустых ссылок.\n2. Читай пост до последнего абзаца: главная развязка и практический вывод автора почти всегда находятся в финале, а не в начале.\n3. Сначала заполни служебные поля-черновики по порядку:\n   - hidden_mechanism: назови скрытую психологическую причину или иллюзию из середины/конца текста (1 короткая фраза).\n   - final_takeaway: выпиши главный вывод или ориентир из финальных абзацев (1 короткая фраза).\n4. Затем напиши summary (1–2 законченных предложения, строго до 185 символов), соединив hidden_mechanism и final_takeaway в цельный афоризм:\n   - Пиши от лица самой мысли (прямое утверждение с глаголом, без упоминания поста, текста или автора).\n   - Используй ясный живой русский язык без канцеляризмов и без кавычек вокруг текста.\n   - Сохраняй смысл оригинала, не выдумывай факты, которых нет в посте.\n5. Теги (tags): выбери 1–2 наиболее точных тега строго из списка:\n   Границы, Отношения, Семья, Тревога, Самооценка, Эмоции, Ответственность, Выбор, Зависимость, Осознанность.\n</rules>\n\n<examples>\n<example>\n<post>\nКогда возникает сильное чувство, в воображении уже строится совместное будущее, и туда вкладывается огромная жизненная энергия. При отказе рушится целый мир. Попытки быстро переключиться лишь усиливают напряжение. Человек реагирует на потерю той версии будущего, куда вложил силы. Когда боль признаётся нормальной частью процесса, появляется возможность постепенно вернуть силы в свою жизнь.\n</post>\n<output>\n{\n  \"post_id\": \"101\",\n  \"is_meaningful\": true,\n  \"hidden_mechanism\": \"При отказе человек оплакивает не столько другого, сколько вложенную в воображаемое будущее энергию.\",\n  \"final_takeaway\": \"Признание боли закономерной позволяет постепенно вернуть внимание и силы в собственную жизнь.\",\n  \"summary\": \"При отказе мы оплакиваем не человека, а вложенную в образ будущего энергию. Признавая эту боль закономерной, мы даём себе время вернуть силы в собственную жизнь.\",\n  \"tags\": [\"Отношения\", \"Эмоции\"]\n}\n</output>\n</example>\n\n<example>\n<post>\nВчера клиентка рассказала, как 10 лет молча мыла посуду за мужем, обижалась, а потом срывалась из-за забытого хлеба. Ей казалось, что он сам должен догадаться. Но пока мы ждём, что близкие угадают наши желания, мы общаемся не с реальным человеком, а со своей фантазией, и сами лишаем его шанса услышать нас.\n</post>\n<output>\n{\n  \"post_id\": \"102\",\n  \"is_meaningful\": true,\n  \"hidden_mechanism\": \"Ожидание, что близкий сам угадает желания, подменяет живого человека фантазией и копит скрытую обиду.\",\n  \"final_takeaway\": \"Прямой разговор даёт партнёру реальный шанс услышать вас вместо молчаливых претензий.\",\n  \"summary\": \"Пока мы ждём, что близкие сами угадают наши желания, мы общаемся со своей фантазией и лишаем реального человека шанса нас услышать.\",\n  \"tags\": [\"Отношения\", \"Границы\"]\n}\n</output>\n</example>\n</examples>"
ALLOWED_TAGS = ["Границы", "Отношения", "Семья", "Тревога", "Самооценка", "Эмоции", "Ответственность", "Выбор", "Зависимость", "Осознанность"]


class UpdateError(RuntimeError):
    pass


def parse_datetime(value: str) -> datetime:
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def clean_for_filter(value: str) -> str:
    value = URL_RE.sub(" ", value)
    value = MENTION_RE.sub(" ", value)
    value = HASHTAG_RE.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()


def is_text_candidate(value: str) -> bool:
    return len(clean_for_filter(value)) >= 50


def normalize_summary(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    quote_chars = '"\'«»“”„‟'
    while value and value[0] in quote_chars:
        value = value[1:].lstrip()
    while value and value[-1] in quote_chars:
        value = value[:-1].rstrip()
    return value


def parse_channel_html(html: str, channel: str) -> list[dict[str, str]]:
    if not html or not html.strip():
        raise UpdateError("Telegram вернул пустой HTML")
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select(".tgme_widget_message")
    if not blocks:
        raise UpdateError("Не найдены блоки .tgme_widget_message")
    pattern = re.compile(rf"^{re.escape(channel)}/(\d+)$")
    posts = []
    for block in blocks:
        match = pattern.fullmatch((block.get("data-post") or "").strip())
        if not match:
            continue
        time_node = block.select_one(".tgme_widget_message_date time[datetime]")
        if time_node is None:
            continue
        date = time_node.get("datetime", "").strip()
        try:
            parse_datetime(date)
        except (ValueError, TypeError):
            continue
        text_node = block.select_one(".tgme_widget_message_text")
        text = ""
        if text_node:
            for br in text_node.find_all("br"):
                br.replace_with("\n")
            text = re.sub(r"[ \t]+\n", "\n", text_node.get_text(separator="", strip=False)).strip()
        data_post = block.get("data-post", "").strip()
        posts.append({"post_id": match.group(1), "source_url": f"https://t.me/{data_post}",
                      "published_at": date, "text": text})
    if not posts:
        raise UpdateError("Не найдены корректные публикации указанного канала")
    return posts


def fetch_telegram_page(channel: str, before: str | None = None) -> str:
    url = f"https://t.me/s/{channel}"
    if before:
        url += "?" + urlencode({"before": before})
    response = requests.get(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }, timeout=30)
    response.raise_for_status()
    if not response.text or not response.text.strip():
        raise UpdateError("Telegram вернул пустой HTML")
    return response.text


def fetch_posts(channel: str, data: dict[str, Any], now: datetime) -> tuple[list[dict[str, str]], int]:
    last_text = str(data.get("last_checked_post_id", "0"))
    if not last_text.isdigit():
        raise UpdateError("last_checked_post_id должен быть числом")
    last_id = int(last_text)
    backfill = not data["quotes"] and last_text == "0"
    cutoff, before, max_id = now - timedelta(days=30), None, last_id
    found: dict[str, dict[str, str]] = {}
    for page in range(12 if backfill else 1):
        posts = parse_channel_html(fetch_telegram_page(channel, before), channel)
        ids, dates = [], []
        for post in posts:
            pid = int(post["post_id"])
            ids.append(pid)
            dates.append(parse_datetime(post["published_at"]))
            if post["text"].strip():
                max_id = max(max_id, pid)  # includes announcements later rejected by Gemini
            if not is_text_candidate(post["text"]):
                continue
            if (backfill and parse_datetime(post["published_at"]) >= cutoff) or (not backfill and pid > last_id):
                found[post["post_id"]] = post
        if not backfill or min(dates) < cutoff or page == 11:
            break
        before = str(min(ids))
        time.sleep(1.5)
    return sorted(found.values(), key=lambda p: int(p["post_id"])), max_id


RESULT_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "propertyOrdering": [
    "post_id", "is_meaningful", "hidden_mechanism", "final_takeaway", "summary", "tags"
], "properties": {
    "post_id": {"type": "STRING"},
    "is_meaningful": {"type": "BOOLEAN"},
    "hidden_mechanism": {"type": "STRING", "description": "Скрытая психологическая причина или иллюзия из текста, до 120 символов; пусто если is_meaningful=false"},
    "final_takeaway": {"type": "STRING", "description": "Главный практический вывод из финальных абзацев поста, до 120 символов; пусто если is_meaningful=false"},
    "summary": {"type": "STRING", "description": "Итоговый самостоятельный инсайт (1–2 предложения, до 185 символов), соединяющий hidden_mechanism и final_takeaway; без кавычек"},
    "tags": {"type": "ARRAY", "items": {"type": "STRING"}}
}, "required": ["post_id", "is_meaningful", "hidden_mechanism", "final_takeaway", "summary", "tags"]}}
SHORTEN_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "post_id": {"type": "STRING"}, "summary": {"type": "STRING"}}, "required": ["post_id", "summary"]}}


DEFAULT_FALLBACK_MODELS = (
    "gemini-4-hydrogen,gemini-3.5-flash-lite,"
    "gemini-3.1-flash-lite,gemini-3.8-flash"
)


def build_model_chain(preferred_model: str) -> list[str]:
    """Put GEMINI_MODEL first, then unique fallback IDs in configured order."""
    fallback_value = os.environ.get("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS)
    configured = [part.strip() for part in fallback_value.split(",") if part.strip()]
    ordered = [preferred_model.strip(), *configured]
    result: list[str] = []
    seen: set[str] = set()
    for model_id in ordered:
        if model_id and model_id not in seen:
            seen.add(model_id)
            result.append(model_id)
    if not result:
        raise UpdateError("Список моделей Gemini пуст")
    return result


def gemini_request(prompt: str, api_key: str, model: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Try primary and fallback models; retry transient failures twice per model."""
    model_chain = build_model_chain(model)
    retry_delays = (5, 15)
    last_error: Exception | None = None

    for model_id in model_chain:
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        if model_id.startswith("gemini-4-"):
            payload["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}
        else:
            payload["generationConfig"]["temperature"] = 0.25

        for attempt in range(3):
            try:
                response = requests.post(
                    GEMINI_URL.format(model=model_id),
                    headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=90,
                )
            except requests.exceptions.Timeout as exc:
                last_error = exc
                transient_message = f"таймаут модели {model_id}: {exc}"
            else:
                if response.status_code not in (429, 503):
                    response.raise_for_status()
                    try:
                        result = json.loads(
                            response.json()["candidates"][0]["content"]["parts"][0]["text"]
                        )
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        raise UpdateError(f"Некорректный JSON Gemini ({model_id}): {exc}") from exc
                    if not isinstance(result, list):
                        raise UpdateError(f"Ответ Gemini ({model_id}) должен быть JSON-массивом")
                    print(f"[Gemini] Успешная модель: {model_id}")
                    return result

                last_error = requests.HTTPError(
                    f"{response.status_code} от модели {model_id}", response=response
                )
                transient_message = f"HTTP {response.status_code} от модели {model_id}"

            if attempt < 2:
                delay = retry_delays[attempt]
                print(f"[Gemini] {transient_message}; повтор {attempt + 1}/2 через {delay} сек.")
                time.sleep(delay)
            else:
                print(f"[Gemini] Модель {model_id} недоступна после 2 повторов; переключение.")

    raise UpdateError(
        f"Все модели Gemini недоступны. Последняя ошибка: {last_error}"
    ) from last_error

def summarize_prompt(posts: list[dict[str, str]]) -> str:
    items = [{"post_id": p["post_id"], "text": p["text"]} for p in posts]
    return (
        "<posts>\\n" + json.dumps(items, ensure_ascii=False) + "\\n</posts>\\n\\n"
        "На основе постов выше верни JSON по схеме, опираясь на финальный вывод каждого поста."
    )


REPHRASE_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "propertyOrdering": [
    "post_id", "hidden_mechanism", "final_takeaway", "summary"
], "properties": {
    "post_id": {"type": "STRING"},
    "hidden_mechanism": {"type": "STRING"},
    "final_takeaway": {"type": "STRING"},
    "summary": {"type": "STRING"}
}, "required": ["post_id", "hidden_mechanism", "final_takeaway", "summary"]}}


ANNOTATION_PREFIXES = (
    "Пример", "Иллюстрация", "Метафора", "Разница между", "Разбор",
    "Объяснение", "Мысль о", "В этом посте", "Автор"
)


def needs_summary_rephrase(summary: str) -> bool:
    normalized = normalize_summary(summary)
    return (len(normalized) > 190 or
            any(normalized.casefold().startswith(prefix.casefold())
                for prefix in ANNOTATION_PREFIXES))


def rephrase_invalid_summaries(results: dict[str, dict[str, Any]], api_key: str, model: str) -> None:
    """Make one targeted correction request per annotated or overlong quote."""
    for pid, item in results.items():
        if not item["is_meaningful"] or not needs_summary_rephrase(item["summary"]):
            continue
        prompt = (
            "<posts>\\n" + json.dumps([{
                "post_id": pid,
                "hidden_mechanism": item.get("hidden_mechanism", ""),
                "final_takeaway": item.get("final_takeaway", "")
            }], ensure_ascii=False) +
            "\\n</posts>\\nПереформулируй только summary этой цитаты как самостоятельный инсайт, "
            "опираясь на hidden_mechanism и final_takeaway. Не начинай с аннотации. "
            "Верни один объект по JSON-схеме."
        )
        corrected = gemini_request(prompt, api_key, model, REPHRASE_SCHEMA)
        if (len(corrected) != 1 or not isinstance(corrected[0], dict)
                or str(corrected[0].get("post_id", "")) != pid
                or not isinstance(corrected[0].get("summary"), str)):
            raise UpdateError(f"Некорректный повторный ответ для post_id={pid}")
        item["summary"] = normalize_summary(corrected[0]["summary"])


def normalize_tags(value: Any) -> list[str]:
    """Canonicalize model tags, whitelist them, preserve order and cap at two."""
    if not isinstance(value, list):
        return []
    canonical = {tag.casefold(): tag for tag in ALLOWED_TAGS}
    result: list[str] = []
    for raw_tag in value:
        if not isinstance(raw_tag, str):
            continue
        cleaned = raw_tag.strip().lstrip("#").strip()
        tag = canonical.get(cleaned.casefold())
        if tag and tag not in result:
            result.append(tag)
            if len(result) == 2:
                break
    return result


def is_force_regenerate(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"true", "1", "yes", "on"}


def validate_model_results(items: list[dict[str, Any]], expected: set[str]) -> dict[str, dict[str, Any]]:
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise UpdateError("Элемент Gemini должен быть объектом")
        pid = str(item.get("post_id", ""))
        if pid not in expected or pid in result:
            raise UpdateError(f"Неожиданный или повторный post_id: {pid}")
        if (not isinstance(item.get("is_meaningful"), bool) or not isinstance(item.get("summary"), str)
                or not isinstance(item.get("tags"), list)
                or not isinstance(item.get("hidden_mechanism"), str)
                or not isinstance(item.get("final_takeaway"), str)):
            raise UpdateError(f"Некорректные поля для post_id={pid}")
        item["tags"] = normalize_tags(item["tags"]) if item["is_meaningful"] else []
        if item["is_meaningful"] and not item["tags"]:
            raise UpdateError(f"Нет допустимых тематических тегов для post_id={pid}")
        result[pid] = item
    if set(result) != expected:
        raise UpdateError(f"Gemini не вернул все публикации: {sorted(expected - set(result))}")
    return result


def enforce_summary_limit(value: str) -> str:
    value = normalize_summary(value)
    if len(value) <= 190:
        return value
    prefix = value[:189]
    if " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0]
    return prefix.rstrip(" ,;:-") + "…"


def shorten_summaries(results: dict[str, dict[str, Any]], api_key: str, model: str) -> None:
    for item in results.values():
        item["summary"] = normalize_summary(item["summary"])
    long_ids = [pid for pid, item in results.items() if item["is_meaningful"] and len(item["summary"]) > 190]
    if not long_ids:
        return
    prompt = ("Сократи каждую формулировку до максимум 175 символов. Верни JSON-массив объектов post_id и summary, "
        "без Markdown и внешних кавычек:\n" + json.dumps(
            [{"post_id": pid, "summary": results[pid]["summary"]} for pid in long_ids], ensure_ascii=False))
    mapped = {}
    for item in gemini_request(prompt, api_key, model, SHORTEN_SCHEMA):
        if not isinstance(item, dict):
            raise UpdateError("Некорректный ответ при сокращении")
        pid, summary = str(item.get("post_id", "")), item.get("summary")
        if pid not in long_ids or pid in mapped or not isinstance(summary, str):
            raise UpdateError("Некорректный или повторный post_id при сокращении")
        mapped[pid] = normalize_summary(summary)
    if set(mapped) != set(long_ids):
        raise UpdateError("Gemini не сократил все длинные summary")
    for pid, value in mapped.items():
        results[pid]["summary"] = value


def deduplicate_quotes(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {}
    for quote in old + new:
        if not isinstance(quote, dict) or not str(quote.get("post_id", "")).isdigit():
            raise UpdateError("Некорректный post_id цитаты")
        normalized = dict(quote)
        normalized["tags"] = normalize_tags(normalized.get("tags", []))
        by_id[str(normalized["post_id"])] = normalized
    return sorted(by_id.values(), key=lambda q: int(q["post_id"]), reverse=True)


def prune_quotes(quotes: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=30)
    fresh = []
    for quote in quotes:
        try:
            if parse_datetime(str(quote["published_at"])) >= cutoff:
                fresh.append(quote)
        except (KeyError, ValueError, TypeError):
            continue
    return fresh if fresh else quotes[:1]


def build_updated_data(data: dict[str, Any], additions: list[dict[str, Any]], max_id: int, now: datetime) -> dict[str, Any]:
    updated = dict(data)
    updated["quotes"] = prune_quotes(deduplicate_quotes(data["quotes"], additions), now)
    updated["last_checked_post_id"] = str(max_id)
    updated["updated_at"] = now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return updated


def load_data(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UpdateError(f"Файл не найден: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Ошибка чтения JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("quotes"), list):
        raise UpdateError("Некорректная структура widget_data.json")
    if "schema_version" not in data or not isinstance(data.get("app_update"), dict):
        raise UpdateError("Отсутствуют schema_version или app_update")
    if not str(data.get("last_checked_post_id", "0")).isdigit():
        raise UpdateError("last_checked_post_id должен быть числом")
    return data


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def run() -> bool:
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        raise UpdateError("Не задана обязательная переменная LLM_API_KEY")
    channel = os.environ.get("TG_CHANNEL", "safin4").strip().lstrip("@")
    model = os.environ.get("GEMINI_MODEL", "gemini-4-hydrogen").strip()
    if not channel or not model:
        raise UpdateError("TG_CHANNEL и GEMINI_MODEL не должны быть пустыми")
    data = load_data(DATA_PATH)
    force_regenerate = is_force_regenerate(os.environ.get("FORCE_REGENERATE"))
    if force_regenerate:
        print("[Update] FORCE_REGENERATE=true: игнорируем старые quotes и last_checked_post_id.")
        data = dict(data)
        data["quotes"] = []
        data["last_checked_post_id"] = "0"
    now = datetime.now(timezone.utc)
    posts, max_checked = fetch_posts(channel, data, now)
    if not posts:
        print("Новых подходящих текстовых постов нет; JSON не изменён.")
        return False

    backfill = not data["quotes"] and str(data.get("last_checked_post_id", "0")) == "0"
    fallback_chain = build_model_chain(model)
    uses_flash_lite_fallback = any("flash-lite" in model_id for model_id in fallback_chain[1:])
    size, additions = (2 if backfill and uses_flash_lite_fallback else 5 if backfill else len(posts)), []
    for offset in range(0, len(posts), size):
        batch = posts[offset:offset + size]
        results = validate_model_results(gemini_request(summarize_prompt(batch), key, model, RESULT_SCHEMA),
                                         {p["post_id"] for p in batch})
        rephrase_invalid_summaries(results, key, model)
        shorten_summaries(results, key, model)
        by_id = {p["post_id"]: p for p in batch}
        for pid, item in results.items():
            summary = enforce_summary_limit(item["summary"])
            if item["is_meaningful"] and summary:
                post = by_id[pid]
                additions.append({"post_id": pid, "published_at": post["published_at"],
                                  "source_url": post["source_url"], "summary": summary, "tags": item["tags"]})
        if backfill and offset + size < len(posts):
            time.sleep(5)

    updated = build_updated_data(data, additions, max_checked, now)
    atomic_write_json(DATA_PATH, updated)
    print(f"Обработано {len(posts)} постов; добавлено цитат: {len(additions)}.")
    return True


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        sys.exit(1)
