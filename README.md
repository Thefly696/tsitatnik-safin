# Цитатник Сафина — серверная часть и CI/CD

Репозиторий: https://github.com/Thefly696/tsitatnik-safin, ветка main.

## Состав

- docs/.nojekyll — пустой маркер GitHub Pages.
- docs/widget_data.json — публичный JSON виджета, цитат с тематическими тегами и версии приложения.
- AGENTS.md — инструкции для AI-агентов по серверному пайплайну и контракту данных.
- scripts/requirements.txt — только requests и beautifulsoup4.
- scripts/update_widget.py — Telegram-парсер и Gemini REST-суммаризатор.
- scripts/tests/test_update_widget.py — unit-тесты unittest.
- .github/workflows/update_content.yml — ежечасное обновление цитат.
- .github/workflows/build_release.yml — сборка APK и публикация релиза.
- android/ — сюда загружается проект, экспортированный из Google AI Studio.

## 1. GitHub Pages

Откройте Settings → Pages → Build and deployment. Выберите Deploy from a branch, ветку main и папку /docs, нажмите Save.

URL JSON после публикации:
https://thefly696.github.io/tsitatnik-safin/widget_data.json

## 2. Секреты

В Settings → Secrets and variables → Actions → New repository secret добавьте:

| Secret | Назначение |
|---|---|
| LLM_API_KEY | Обязательный Gemini API key |
| KEYSTORE_BASE64 | Keystore в Base64 |
| KEYSTORE_PASSWORD | Пароль keystore |
| KEY_ALIAS | Alias ключа |
| KEY_PASSWORD | Пароль приватного ключа |

Все четыре секрета подписи нужны для release APK. Если отсутствует хотя бы один, workflow соберёт debug APK.

Создание keystore (сохраните файл и резервную копию в безопасном месте):

    keytool -genkeypair -v -keystore release.jks -keyalg RSA -keysize 2048 -validity 10000 -alias tsitatnik
    base64 -w 0 release.jks

Вывод Base64 целиком поместите в KEYSTORE_BASE64, остальные значения — в три соответствующих секрета. Потеря keystore может помешать выпускать совместимые обновления уже установленного приложения.

CI передаёт Android Gradle-проекту переменные RELEASE_KEYSTORE_PATH, RELEASE_STORE_PASSWORD, RELEASE_KEY_ALIAS и RELEASE_KEY_PASSWORD. Android-проект должен самостоятельно использовать их в release signingConfig; CI не переписывает Gradle-файлы. После загрузки исходников проверьте, что signingConfig подключён к этим env-переменным.

## 3. Обновление Telegram

Workflow Update Telegram content запускается каждый час на 17-й минуте (17 * * * *) или вручную через Actions → Update Telegram content → Run workflow.

Параметры: LLM_API_KEY из Secrets, TG_CHANNEL=safin4, GEMINI_MODEL=gemini-4-hydrogen. Переменная GEMINI_FALLBACK_MODELS задаёт список резервных моделей через запятую; по умолчанию: gemini-4-hydrogen, gemini-3.5-flash-lite, gemini-3.1-flash-lite, gemini-3.8-flash. GEMINI_MODEL всегда ставится первым, дубликаты удаляются.

Скрипт загружает https://t.me/s/{TG_CHANNEL} с браузерным User-Agent, Accept-Language: ru-RU,ru;q=0.9, timeout 30 секунд. Извлекает цифровой post ID, URL, дату и текст, заменяет HTML <br> на перевод строки. Для фильтра копия текста очищается от ссылок, @упоминаний, #хештегов и пробелов; после очистки тексты короче 50 символов пропускаются.

Первый запуск (пустой quotes, last_checked_post_id="0") делает backfill за 30 дней: максимум 12 страниц через ?before=<min_post_id>, пауза 1,5 секунды. В штатном режиме читается только первая страница и берутся новые текстовые посты. Если подходящих новых постов нет, код завершения 0, Gemini не вызывается, JSON не переписывается.

Gemini REST: POST https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent. Таймаут 90 секунд; заголовки x-goog-api-key и Content-Type: application/json. В generationConfig передаются thinkingConfig: {thinkingLevel: "low"}, responseMimeType: "application/json" и responseSchema; temperature/topP/topK/thinkingBudget не передаются. При HTTP 429/503 или сетевом таймауте для каждой модели выполняются две повторные попытки с паузами 5 и 15 секунд; после исчерпания попыток скрипт автоматически переходит к следующей модели. Успешная модель выводится в лог. thinkingConfig: {thinkingLevel: low} передаётся только моделям gemini-4-*; для gemini-3.* поле не передаётся. Запрос сокращения длинного summary использует ту же fallback-цепочку. Backfill: батчи по 5, пауза 5 секунд; штатный режим — одним запросом.

Модель отбрасывает рекламу, курсы, вебинары, наборы на обучение, организационные объявления и видео/стримы без самостоятельной мысли. Для каждой содержательной цитаты возвращает 1–2 тематических тега из фиксированного словаря: ["Границы", "Отношения", "Семья", "Тревога", "Самооценка", "Эмоции", "Ответственность", "Выбор", "Зависимость", "Осознанность"]. Python удаляет ведущие # и пробелы, сопоставляет категории без учёта регистра и сохраняет каноническое написание; неизвестные значения отбрасываются, дубли удаляются с сохранением порядка, максимум два тега. Для содержательной цитаты отсутствие допустимых тегов считается ошибкой и не приводит к перезаписи JSON; для отклонённых постов tags всегда равен []. Для психологических публикаций возвращает 1–2 коротких предложения на русском языке без Markdown, эмодзи, хештегов и внешних кавычек. Переносы/кавычки очищаются; при длине больше 190 символов делается одна попытка сокращения до 175, затем остаток обрезается по последнему целому слову до 189 символов + ….

Каждая цитата в docs/widget_data.json содержит поле tags (массив строк); остальные поля JSON-контракта не меняются. Цитаты объединяются без дублей по post_id, сортируются по убыванию ID и ограничиваются 30 днями (если все старые — сохраняется самая свежая). last_checked_post_id — максимальный ID среди просмотренных текстовых постов, включая отсеянные объявления. schema_version и app_update сохраняются, updated_at обновляется в UTC ISO-8601.

Запись JSON атомарная, UTF-8 (ensure_ascii=False, indent=2). Ошибка сети, пустой HTML или сбой API завершают скрипт с ненулевым кодом без перезаписи прежнего JSON.

Локально (Python 3.12+):

    python -m pip install -r scripts/requirements.txt
    export LLM_API_KEY="ваш_ключ"
    python -m unittest discover -s scripts/tests
    python scripts/update_widget.py

## 4. Android APK / GitHub Releases

Загрузите проект Google AI Studio в /android. CI использует ubuntu-latest и Temurin JDK 17. Если есть android/gradlew, workflow делает chmod +x и использует именно wrapper (его Gradle/AGP версии). Без wrapper устанавливается системный Gradle 8.11.1 через gradle/actions/setup-gradle@v4.

Триггеры: push изменений android/**, push тега v*, ручной запуск. В ручном запуске есть publish_release (boolean, default false) и необязательный release_tag (например, v1.0.0). При публикации без указанного тега и без tag-trigger создаётся manual-<номер запуска>.

С четырьмя секретами подписи собирается assembleRelease и создаётся TsitatnikSafina-release.apk; без них — assembleDebug и TsitatnikSafina-debug.apk. APK всегда выгружается в Actions artifacts на 30 дней. При теге v* или publish_release=true он прикрепляется к GitHub Release.

Публикация по тегу:

    git tag v1.0.0
    git push origin v1.0.0

Если /android отсутствует, workflow успешно пропускает сборку с уведомлением.

## 5. Перед запуском

1. Добавьте LLM_API_KEY, запустите content workflow вручную.
2. Включите Pages: main → /docs.
3. В Settings → Actions → General разрешите Actions запись в Contents и создание Releases.
4. После загрузки Android-проекта проверьте signingConfig и wrapper.
5. Проверьте доступность gemini-4-hydrogen в вашем API-проекте и поддержку thinkingLevel="low". Без API-ключа live-запрос не выполнялся.

JSON публичен. Не добавляйте туда секреты и персональные данные.
