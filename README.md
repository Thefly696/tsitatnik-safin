# Цитатник Сафина — серверная часть и CI/CD

Репозиторий: https://github.com/Thefly696/tsitatnik-safin, ветка `main`.

## Структура

- `docs/.nojekyll` — отключает Jekyll.
- `docs/widget_data.json` — публичный JSON для приложения.
- `scripts/requirements.txt` — requests и beautifulsoup4.
- `scripts/update_widget.py` — Telegram → Gemini REST → JSON.
- `scripts/tests/test_update_widget.py` — unit-тесты unittest.
- `.github/workflows/update_content.yml` — ежечасное обновление.
- `.github/workflows/build_release.yml` — Android APK и GitHub Releases.
- `android/` — Android Gradle-проект добавляется владельцем.

## GitHub Pages

Откройте **Settings → Pages**. В разделе Build and deployment выберите **Deploy from a branch**, ветку `main` и папку `/docs`. После публикации JSON будет доступен:

`https://thefly696.github.io/tsitatnik-safin/widget_data.json`

## Секреты

В **Settings → Secrets and variables → Actions** добавьте:

| Secret | Назначение |
|---|---|
| `LLM_API_KEY` | Обязательный Gemini API key |
| `KEYSTORE_BASE64` | Keystore в Base64 |
| `KEYSTORE_PASSWORD` | Пароль keystore |
| `KEY_ALIAS` | Alias ключа |
| `KEY_PASSWORD` | Пароль ключа |

Последние четыре секрета нужны для подписанного release APK. Если хотя бы один отсутствует, собирается debug APK. Не помещайте секреты в код или JSON.

Пример создания keystore (резервную копию храните безопасно):

```bash
keytool -genkeypair -v -keystore release.jks -keyalg RSA -keysize 2048 -validity 10000 -alias tsitatnik
base64 -w 0 release.jks
```

Android Gradle-проект должен самостоятельно использовать переменные `ANDROID_KEYSTORE_PATH`, `KEYSTORE_PASSWORD`, `KEY_ALIAS`, `KEY_PASSWORD` в release signingConfig. Workflow декодирует ключ и передаёт переменные, но не меняет Gradle-конфигурацию приложения.

## Обновление контента

Workflow `Update Telegram content` запускается каждый час на 17-й минуте и вручную через **Actions → Update Telegram content → Run workflow**.

Скрипт загружает `https://t.me/s/{TG_CHANNEL}` с браузерным User-Agent, русским Accept-Language и timeout 30 секунд. По умолчанию `TG_CHANNEL=safin4`, `GEMINI_MODEL=gemini-4-hydrogen`; при необходимости измените env в workflow.

Первый запуск выполняет backfill за 30 дней: максимум 12 страниц с паузой 1,5 секунды. Штатный запуск проверяет первую страницу и новые ID. URL, @упоминания и #хештеги удаляются для фильтра длины; тексты короче 50 символов пропускаются. Gemini вызывается через REST API, backfill отправляется батчами по 8 с паузой 4,5 секунды. На HTTP 429/503 предусмотрен один повтор через 8 секунд.

Модель отбирает содержательные психологические публикации, исключая рекламу, курсы, вебинары, организационные объявления и видео/стримы без самостоятельной мысли. Резюме — 1–2 предложения на русском, до 160 символов. Данные дедуплицируются по post_id, сортируются по убыванию ID и ограничиваются последними 30 днями (с сохранением минимум одной самой свежей записи).

JSON записывается атомарно через временный файл и `os.replace`. Если новых подходящих публикаций нет, Gemini не вызывается и файл не переписывается. Ошибка сети, пустой HTML или сбой API завершают скрипт с ненулевым кодом без изменения прежнего JSON.

## Локальный запуск и тесты

Python 3.12+:

```bash
python -m pip install -r scripts/requirements.txt
export LLM_API_KEY="ваш_ключ"
python -m unittest discover -s scripts/tests
python scripts/update_widget.py
```

PowerShell:

```powershell
$env:LLM_API_KEY = "ваш_ключ"
python -m unittest discover -s scripts/tests
```

## Android build / release

Загрузите Gradle Android-проект в `/android`. Сборка использует Temurin JDK 17 и Gradle 8.11.1. Триггеры: push изменений `android/**`, push тега `v*`, ручной запуск с boolean `publish_release` (default false).

При наличии всех секретов подписи создаётся `TsitatnikSafina-release.apk`, иначе — `TsitatnikSafina-debug.apk`. APK загружается как Actions artifact на 30 дней. При теге `v*` или ручном запуске с `publish_release=true` APK публикуется в GitHub Releases.

Пример релиза:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Если `/android` отсутствует или ещё не содержит Gradle-проект, workflow сообщает об этом и пропускает сборку.

## Важное перед production

1. Убедитесь, что Google Gemini API действительно предоставляет проекту модель с ID `gemini-4-hydrogen`. При 404 укажите доступный ID через `GEMINI_MODEL`; молчаливой подмены нет.
2. Проверьте права GitHub Actions на запись в Contents и создание Releases (**Settings → Actions → General**).
3. Проверьте signingConfig в Android-проекте.
4. JSON публичен — не помещайте туда персональные данные или секреты.
