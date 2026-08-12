# AutoSub Server — структура и Git workflow

## Ветки и каналы доставки

- `main` — production-ветка. `install.sh`/`update.sh` из `main` по умолчанию
  устанавливают последний опубликованный GitHub Release, а не произвольный HEAD.
- `dev` — интеграционная ветка. Для установки точного состояния ветки необходимо
  явно передать `AUTOSUB_VERSION=dev`.
- `codex/*`, `feature/*`, `fix/*` — короткоживущие рабочие ветки. После merge их
  следует удалять; прямые изменения `main` и `dev` не являются штатным процессом.
- `vX.Y.Z` — неизменяемый production-тег, соответствующий GitHub Release.

Команды каналов приведены в основном [README](../README.md#быстрый-старт).

## Pull request и release workflow

1. Создать рабочую ветку от актуального `dev`.
2. Внести одно связное изменение, обновить тесты и документацию.
3. Запустить локальные доступные gates и открыть PR в `dev`.
4. Дождаться обязательных CI-проверок и review, затем выполнить merge без переписывания
   опубликованной истории.
5. Для релиза подготовить версию и changelog отдельным PR `dev` → `main`.
6. После зелёного CI выполнить merge, создать подписанный/аннотированный `vX.Y.Z` и
   GitHub Release из содержимого changelog.
7. Проверить установку/обновление по тегу и production readiness. Не перемещать и не
   переиспользовать опубликованный тег.

Рекомендуемая защита `main` и `dev`: PR-only изменения, запрет force-push/delete,
strict required status checks (`Tests (Python 3.10)`, `Tests (Python 3.12)`,
`Tests (Python 3.14)`, `Lint and types`, `Dependency and source security`, `ShellCheck`,
`CodeQL (Python)`) и
автоматическое удаление merged-веток.

## Основные компоненты

- `autosub_server.py` — FastAPI lifecycle, HTTP-маршруты и границы безопасности.
- `builder.py` — разбор подписки, нормализация Xray и генерация autoselect-профилей.
- `api_client.py` — subscription/API интеграция 3x-ui и аутентификация.
- `http_clients.py`, `http_client_config.py`, `http_client_errors.py` — lifespan HTTP
  pools, лимиты, таймауты и безопасные сетевые ошибки.
- `subscription_representation.py` — выбор JSON или безопасного локального HTML.
- `subscription_cache.py` — bounded LRU/TTL, single-flight и stale-if-error.
- `storage.py`, `migrations.py`, `database_*` — SQLite, миграции и backups.
- `rate_limiter.py`, `csrf.py`, `http_security.py` — rate limit, trusted proxies,
  CSRF, request IDs, CSP и response headers.
- `dashboard.py`, `templates/`, `static/` — локальная админ-панель и subscription landing.
- `release_manager.py`, `install.sh`, `update.sh` — атомарное развёртывание и rollback.
- `setup_nginx.sh`, `nginx-example.conf`, `autosub-server.service` — production edge
  и systemd runtime.
- `runtime-manifest.txt` — единственный allowlist production payload.
- `tests/` — unit, regression, security и temporary-root deployment smoke tests.
- `.codex/` — отслеживаемые инструкции и инструменты проекта для AI-агентов; они не
  входят в production manifest.

## Production layout

```text
/opt/autosub-server/
├── current -> releases/<release-id>
├── releases/<release-id>/    # runtime allowlist + отдельный venv
├── update.sh                 # root-owned установленный updater
└── shared/                   # единственная записываемая runtime-область
    ├── .env
    ├── config.json
    ├── data.db
    ├── autosub.log
    └── backups/
```

Сервис остаётся root-managed, но unit лишён capabilities, видит код read-only и может
писать только в `shared`. Nginx публикует `/json/` и `/sub/`; admin port остаётся на
loopback. Обновлятор использует lock, staging markers, SQLite backup, атомарный symlink,
readiness gate, rollback кода и recovery после прерывания.

## Что не входит в runtime

`docs/`, `tests/`, `.github/`, `.codex/`, локальные окружения, секреты, базы и логи
хранятся или создаются отдельно и не копируются в release. Любое добавление runtime
модуля или asset требует одновременного обновления `runtime-manifest.txt` и его тестов.
