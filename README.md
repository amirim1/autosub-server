# AutoSub Server

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/amirim1/autosub-server/actions/workflows/ci.yml/badge.svg)](https://github.com/amirim1/autosub-server/actions/workflows/ci.yml)
[![Latest Release](https://img.shields.io/github/v/release/amirim1/autosub-server)](https://github.com/amirim1/autosub-server/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

**AutoSub Server** дополняет JSON-подписки панели 3x-ui профилями автоматического
выбора сервера. Пользователь получает обычную ссылку подписки, а его VPN-клиент —
готовые балансировщики `leastPing` или `leastLoad`, отфильтрованные по группе клиента.

AutoSub нужен владельцу 3x-ui, который хочет централизованно:

- добавлять в подписку один или несколько профилей автовыбора;
- выбирать узлы для каждого профиля и назначать разные правила группам клиентов;
- автоматически подхватывать существующие узлы и клиентов из 3x-ui;
- управлять настройками через локальную веб-панель;
- сохранить прежние ссылки `/sub/<subId>` и совместимость с VPN-клиентами.

Пример результата:

```text
Исходная подписка:  Германия, Нидерланды, Финляндия
Через AutoSub:      🚀 Основные авто, ⚡ Все ноды авто,
                    Германия, Нидерланды, Финляндия
```

AutoSub **не заменяет** 3x-ui, Xray, Nginx или VPN-клиент. Он не создаёт inbound,
не меняет исходные узлы и не преобразует Base64-подписки: на входе и выходе ожидается
JSON, который уже выдаёт 3x-ui.

[English version](README_EN.md)

Список сайтов, которые должны идти напрямую и обходить балансировщик, теперь можно
редактировать в админ-панели. После установки используется прежний встроенный список
российских сайтов. Поддерживаются правила Xray `domain:`, `full:`, `keyword:`,
`regexp:` и `geosite:` — по одному на строку. Пустой сохранённый список отключает
только доменное direct-правило; маршрутизация приватных IP и блокировки сохраняются.

## Как это работает

```text
VPN-клиент или браузер
        │  /json/SUB_ID или /sub/SUB_ID
        ▼
Nginx: TLS и публичные /json/ + /sub/
        │  http://127.0.0.1:25500
        ▼
AutoSub Server
        ├─ получает исходную JSON-подписку из 3x-ui
        ├─ определяет клиента и его группы
        ├─ добавляет разрешённые профили автовыбора
        └─ возвращает подписку и исходные метаданные
```

- `/json/<subId>` всегда возвращает JSON.
- `/sub/<subId>` автоматически различает тип клиента. Обычный браузер, включая
  Mozilla/WebView с JSON-предпочитающим `Accept`, получает безопасный локальный
  лендинг AutoSub. Известные VPN-клиенты получают JSON-подписку.
- `?format=json` и `?format=html` явно выбирают формат для `/sub/`; явный формат
  важнее эвристики браузера. Маршрут `/json/` нельзя переключить в HTML.
- Недоверенный HTML 3x-ui никогда не исполняется под origin AutoSub. Лендинг не
  использует внешние скрипты, аналитику или ресурсы панели.

## Возможности

- профили `leastPing` и `leastLoad` с выбранными узлами или шаблоном `*`;
- правила по группам 3x-ui и индивидуальные переопределения клиента;
- импорт каталога узлов и проверка API панели;
- bounded LRU/TTL-кэш, per-key single-flight и stale-if-error;
- отдельные HTTP-пулы для подписок и аутентифицированного API 3x-ui;
- ограничения размера upstream-ответа, таймауты и безопасные повторы GET;
- локальная админ-панель с Basic Auth, CSRF и rate limiting;
- trusted-proxy модель для корректного определения IP за Nginx;
- SQLite с транзакционными миграциями и резервной копией перед обновлением;
- атомарные release-каталоги, readiness-проверка и автоматический rollback кода;
- воспроизводимые lock-файлы с hashes и CI для Python 3.10/3.12/3.14.

## Требования

Штатная production-схема:

- Linux с systemd (проверяется на Debian/Ubuntu-подобном окружении);
- запуск установщика и обновлятора от `root`;
- Python 3.10 или новее;
- `git`, `curl`, `flock`, `systemctl` и не менее 512 MiB свободного места;
- работающая 3x-ui с JSON-подпиской;
- Nginx и TLS-сертификат для публичного доступа.

По умолчанию AutoSub слушает только `127.0.0.1:25500`. Публичными через Nginx
должны быть только `/json/` и `/sub/`; `/admin` открывайте через SSH-туннель.

## Быстрый старт

### Выбор канала

| Канал | Для чего | Что устанавливается |
|---|---|---|
| `main` | production | последний опубликованный GitHub Release |
| `dev` | тестовый сервер | точное текущее состояние ветки `dev` |
| `vX.Y.Z` | закреплённая версия | конкретный тег без fallback на ветку |

Не переключайте production между `main` и `dev` для проверки изменений. Для `dev`
используйте отдельный сервер или отдельный `AUTOSUB_ROOT` и systemd unit.

### Стабильная установка (`main`)

```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/main/install.sh | bash
```

Скрипт разрешает `latest` через GitHub Releases, загружает точный тег, создаёт
`/opt/autosub-server/{releases,shared}`, отдельный venv релиза и systemd unit. При
первой установке генерируются случайные admin- и CSRF-секреты.

### Тестовая установка (`dev`)

```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/dev/install.sh \
  | AUTOSUB_VERSION=dev bash
```

### Установка конкретной версии

```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/main/install.sh \
  | AUTOSUB_VERSION=v3.1.0 bash
```

### Первичная настройка

Откройте постоянный конфигурационный файл:

```bash
nano /opt/autosub-server/shared/.env
```

Минимально проверьте:

```dotenv
AUTOSUB_HOST=127.0.0.1
AUTOSUB_PORT=25500
AUTOSUB_TRUSTED_PROXIES=127.0.0.1/32,::1/128

XUI_SUB_URL=https://sub.example.com:2096
XUI_API_URL=https://panel.example.com:54321/secret-path
XUI_API_TOKEN=replace-with-3x-ui-api-token
XUI_TLS_VERIFY=true
```

`XUI_SUB_URL` — origin JSON-подписки. `XUI_API_URL` — адрес панели/API и обычно
использует другой порт или secret path. `XUI_URL` оставлен только как совместимый
fallback. API token предпочтительнее `XUI_USERNAME`/`XUI_PASSWORD`.

После изменения:

```bash
systemctl restart autosub-server
curl -fsS http://127.0.0.1:25500/health/ready
```

### Nginx

Сначала получите TLS-сертификат для домена. Затем:

```bash
bash /opt/autosub-server/current/setup_nginx.sh sub.example.com 2097
```

Пример конфигурации находится в [`nginx-example.conf`](nginx-example.conf). Скрипт
публикует только `/json/` и `/sub/`, проверяет конфигурацию через `nginx -t` и
перезагружает Nginx.

В 3x-ui укажите **JSON reverse proxy URI**:

```text
https://sub.example.com:2097/json/
```

Проверка:

```bash
curl -fsS http://127.0.0.1:25500/health
curl -fsS http://127.0.0.1:25500/health/ready
curl -I https://sub.example.com:2097/sub/REAL_SUB_ID
curl -fsS https://sub.example.com:2097/json/REAL_SUB_ID
```

Открытие `/sub/REAL_SUB_ID` в браузере должно показать локальный лендинг AutoSub;
последняя команда должна вернуть JSON с профилями автовыбора.

## Панель управления

Админ-панель остаётся локальной. С рабочего компьютера создайте туннель:

```bash
ssh -L 25500:127.0.0.1:25500 root@SERVER_IP
```

Откройте `http://127.0.0.1:25500/admin` и используйте
`AUTOSUB_ADMIN_USERNAME`/`AUTOSUB_ADMIN_PASSWORD` из `shared/.env`.

В панели можно:

1. проверить соединение с API 3x-ui;
2. найти доступные узлы;
3. создать профили автовыбора и выбрать стратегию;
4. назначить профили группам клиентов;
5. проверить итоговую подписку через preview.

## Обновление

Перед обновлением проверьте состояние:

```bash
systemctl status autosub-server --no-pager
curl -fsS http://127.0.0.1:25500/health/ready
```

### Stable (`main` → последний Release)

```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/main/update.sh | bash
```

Для уже установленного v3 layout эквивалентно:

```bash
/opt/autosub-server/update.sh
```

### Development (`dev`)

```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/dev/update.sh \
  | AUTOSUB_VERSION=dev bash
```

или:

```bash
AUTOSUB_VERSION=dev /opt/autosub-server/update.sh
```

### Закреплённая версия

```bash
AUTOSUB_VERSION=v3.1.0 /opt/autosub-server/update.sh
```

Обновлятор использует lock, проверяет свободное место и Python, создаёт backup
SQLite, устанавливает зависимости с hashes, переключает `current` атомарно и ждёт
`/health/ready`. При неуспешном запуске код автоматически возвращается на предыдущий
release. Постоянные данные не перезаписываются.

```text
/opt/autosub-server/
├── current -> releases/<release-id>
├── releases/                  # код и venv релизов
├── update.sh                  # root-owned установленный updater
└── shared/
    ├── .env
    ├── config.json
    ├── data.db
    ├── autosub.log
    └── backups/
```

Backup базы предназначен для ручного восстановления при повреждении данных. Обычный
rollback кода не откатывает SQLite после того, как обновлённый сервис уже принимал
запросы. Проверенные копии сохраняются в `/opt/autosub-server/shared/backups/`.

## Публичные маршруты

| Метод и путь | Назначение |
|---|---|
| `GET /json/{sub_id}` | JSON-подписка; формат выбирается по профилю клиента |
| `GET /json/{sub_id}?format=xray\|singbox\|clash\|links` | явный wire-формат (`links`/`base64` — base64-список share-ссылок) |
| `GET /json/{sub_id}?client=happ\|incy\|v2raytun` | переопределение профиля клиента (неизвестное значение → 400) |
| `GET /sub/{sub_id}` | browser landing или JSON по типу клиента |
| `GET /sub/{sub_id}?format=json` | явный JSON |
| `GET /sub/{sub_id}?format=html` | явный локальный HTML |
| `GET /health` | совместимая liveness-проверка |
| `GET /health/live` | явная liveness-проверка |
| `GET /health/ready` | готовность зависимостей процесса |

Профили клиентов (приоритет: `?client=` → User-Agent → generic):

| Профиль | Формат по умолчанию |
|---|---|
| Happ | sing-box JSON (selector + urltest) |
| Incy | sing-box JSON (selector + urltest) |
| v2RayTun | sing-box JSON (полная поддержка балансировщика) |
| Clash/Mihomo/Stash | Clash YAML |
| Generic (браузеры, curl, прочее) | Xray JSON (leastPing/leastLoad + observatory) |

Формат `links` не кодирует балансировочные группы — это ограничение формата
share-ссылок; используйте его только явно через `?format=`.

Ответы содержат `X-Request-ID`; rate limit возвращает `429` и `Retry-After`.
Подробный контракт: [`docs/API.md`](docs/API.md).

### Балансировка и гео-чувствительные сервисы

Сгенерированные балансировщики исключают «разрыв сессий» и скачки IP между странами:

- `sticky_domains` (админ-панель) маршрутизируются через фиксированную ноду — банки,
  стриминг и API с привязкой к IP получают стабильный egress; DNS идёт тем же путём.
- Режим **«Только одна страна»** у балансировщика ограничивает авто-выбор нодами
  одной страны (flag-эмодзи или название), поэтому переподключения не меняют страну.
- Интервал health-check принудительно не ниже 60s (`AUTOSUB_MIN_PROBE_INTERVAL`),
  чтобы узлы не переключались посреди сессии.

## Конфигурация

Основные переменные перечислены в [`.env.example`](.env.example):

| Переменная | Назначение |
|---|---|
| `AUTOSUB_HOST`, `AUTOSUB_PORT` | локальный bind сервера |
| `AUTOSUB_TRUSTED_PROXIES` | доверенные immediate proxy IP/CIDR |
| `AUTOSUB_ADMIN_USERNAME/PASSWORD` | Basic Auth панели |
| `AUTOSUB_SECRET_KEY` | HMAC-ключ CSRF |
| `XUI_SUB_URL` | origin JSON-подписок |
| `XUI_API_URL` | origin API панели |
| `XUI_API_TOKEN` | рекомендуемая API-аутентификация |
| `XUI_USERNAME/PASSWORD` | fallback login API |
| `XUI_TLS_VERIFY` | проверка TLS upstream |
| `SUB_TITLE`, `SUB_USERINFO` | необязательные overrides метаданных |
| `AUTOSUB_MIN_PROBE_INTERVAL` | пол интервала health-check в генерации (60s) |

Постоянные правила хранятся в SQLite. `shared/config.json` нужен для совместимости и
первичного импорта; миграция помечается только после успешной транзакции.

## Безопасность

- Не публикуйте `127.0.0.1:25500` и `/admin` напрямую в Интернет.
- Не добавляйте `.env`, `data.db`, логи или реальные subscription IDs в Git и issue.
- Оставляйте `XUI_TLS_VERIFY=true`; выключайте только для осознанного локального
  self-signed upstream.
- Указывайте в `AUTOSUB_TRUSTED_PROXIES` только реальный Nginx. Сеть `0.0.0.0/0`
  или `::/0` отвергается при запуске.
- Храните `/opt/autosub-server`, unit и updater под `root`; process unit изолирует
  файловую систему, устройства, kernel controls и разрешает запись только в `shared`.
- При публикации через CDN добавьте грубый внешний rate limit и настройте цепочку
  trusted proxies с учётом реального immediate peer.

Инструкции по уязвимостям: [`SECURITY.md`](SECURITY.md).

## Разработка

```bash
git clone https://github.com/amirim1/autosub-server.git
cd autosub-server
git switch dev
python3 -m venv .venv-development
source .venv-development/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest --cov=. --cov-branch --cov-fail-under=70
ruff check .
pyright
```

Production- и dev-зависимости задаются в `requirements.in` и
`requirements-dev.in`; `requirements*.txt` — сгенерированные lock-файлы с hashes.
Инструкция по обновлению locks и quality gates: [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

CI запускает тесты на Python 3.10, 3.12 и актуальном stable 3.14, Ruff, Pyright, pip-audit, Bandit,
ShellCheck, coverage и CodeQL. Runtime составляется только из allowlist
[`runtime-manifest.txt`](runtime-manifest.txt); docs, tests и `.codex/` хранятся в Git,
но не копируются в production release.

## Диагностика

```bash
systemctl status autosub-server --no-pager
journalctl -u autosub-server -n 200 --no-pager
tail -n 200 /opt/autosub-server/shared/autosub.log
curl -v http://127.0.0.1:25500/health/ready
nginx -t
```

Частые причины:

- `502` подписки — неверный `XUI_SUB_URL`, upstream вернул не JSON, timeout или
  ответ превысил безопасный лимит;
- API test не проходит — перепутаны `XUI_SUB_URL` и `XUI_API_URL`, неверный token
  или secret path панели;
- браузер видит JSON вместо лендинга — используйте `/sub/...`, а не `/json/...`, и
  уберите явный `?format=json`;
- балансировщиков нет — проверьте, что профили включены, в них выбраны существующие
  node IDs, а группа пользователя разрешает эти profile IDs;
- `429` — дождитесь времени из `Retry-After` и проверьте trusted-proxy настройку.

## Документация и история

- [Архитектура](docs/ARCHITECTURE.md)
- [API](docs/API.md)
- [База данных и миграции](docs/DATABASE.md)
- [Разработка](docs/DEVELOPMENT.md)
- [Структура и Git workflow](docs/structure.md)
- [CHANGELOG](CHANGELOG.md)
- [GitHub Releases](https://github.com/amirim1/autosub-server/releases)

Лицензия: [MIT](LICENSE).
