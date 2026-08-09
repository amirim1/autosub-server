# AutoSub Server - Структура проекта и Git Workflow

## 1. Ветки Git и их назначение

- **`dev`** (Рабочая ветка):
  - Используется для активной разработки, добавления новых функций и исправления багов.
  - Все промежуточные коммиты и тестирование проводятся здесь.
  - Команда обновления dev: `curl -sSL https://raw.githubusercontent.com/amirim1/autosub-server/dev/update.sh | AUTOSUB_VERSION=dev bash`

- **`main`** (Стабильная ветка / Production):
  - Содержит проверенный, стабильный код для продакшена.
  - Сюда вливается ветка `dev` перед созданием нового релиза.
  - Команда обновления пром: `curl -sSL https://raw.githubusercontent.com/amirim1/autosub-server/main/update.sh | bash`

---

## 2. Версионирование и Теги (Releases)

- Формат тегов: `vX.Y.Z` (Semantic Versioning)
- Существующие релизы:
  - `v1.0.0` — базовый релиз AutoSub Сервера
  - `v1.1.0` / `v1.1.1` — поддержка каналов обновления и превью
  - `v1.1.2` — исправление совместимости с Happ Proxy / v2rayNG (vnext/servers структура для TCP Ping и задержки)
  - `v1.2.0` — поддержка старых `/sub/` ссылок, умное проксирование HTML веб-страницы 3x-ui для браузеров и исправление ошибки "Socket closed"

---

## 3. Регламент проведения релизов (Release Process)

1. Убедиться, что все изменения внесены, протестированы (`python -m pytest`) и закоммичены в ветку `dev`.
2. Запушить ветку `dev` на GitHub (`git push origin dev`).
3. Переключиться на ветку `main` (`git checkout main`).
4. Влить ветку `dev` в `main` (`git merge dev`).
5. Проставить аннотированный тег (`git tag -a vX.Y.Z -m "Release vX.Y.Z: <описание>"`).
6. Запушить ветку `main` и тег на GitHub (`git push origin main && git push origin vX.Y.Z`).
7. Вернуться на рабочую ветку `dev` (`git checkout dev`).

---

## 4. Обзор структуры файлов репозитория

- `builder.py` — Логика генерации подписок, автовыбор (leastPing балансировщик), обогащение `address`/`port`, нормализация `vnext`/`servers` для VLESS/VMess/Trojan.
- `autosub_server.py` — FastAPI веб-сервер, роуты `/json/{sub_id}`, `/admin`, `/health`, ограничение частоты запросов (rate limiting).
- `rate_limiter.py` — Bounded sliding-window limiter и trusted-proxy resolution.
- `storage.py` — Работа с SQLite базой данных (`data.db`), хранение групп клиентов, правил и пресетов автовыбора.
- `api_client.py` — Взаимодействие с API 3x-ui / XUI панелей.
- `subscription_cache.py` — Ограниченный LRU/TTL-кэш готовых публичных подписок,
  single-flight и stale-if-error.
- `fingerprint.py` — Генерация уникальных идентификаторов нод (канонические хэши).
- `config.py` — Загрузка конфигурации и переменных окружения (`.env`).
- `dashboard.py` / `templates/` / `static/` — Панель администратора (HTML/JS/CSS).
- `update.sh` — Скрипт автоматического обновления сервера с GitHub.
- `install.sh` / `setup_nginx.sh` — Скрипты первичной установки и настройки Nginx.
- `tests/` — Модульные тесты (`test_builder.py`, `test_fingerprint.py`, `test_storage.py`).
