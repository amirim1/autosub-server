# AutoSub Server

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**AutoSub Server** — это локальный прокси-сервер JSON-подписок для панели **3x-ui**.

Он перехватывает запросы `/json/<subId>` и `/sub/<subId>` с внешнего порта прокси, получает оригинальную JSON-подписку от 3x-ui, генерирует и добавляет разрешенные профили автоматического выбора нод (Autoselect / LeastPing) в начало списка и возвращает оригинальные ноды без изменений после них.

Для старых ссылок `/sub/<subId>` сервер автоматически различает запросы: веб-браузерам отображает красивую родную HTML-страницу 3x-ui (со статистикой трафика и кнопками), а VPN-клиентам отдает обновленную JSON-подписку с автовыбором.

> [English documentation is available in [README_EN.md](README_EN.md).]

---

## 🌟 Главные возможности (Features)

- **Настраиваемая умная балансировка**: Выбор `leastPing` или `leastLoad` для каждого профиля автовыбора.
- **Обход блокировок (RU Bypass)**: Встроенные правила `direct` маршрутизации для популярных российских сервисов (Яндекс, Госуслуги, банки, маркетплейсы) в обход VPN.
- **Метаданные подписки**: Выборочная передача стандартного заголовка `hide-settings` для настроенных групп клиентов.
- **Современная админ-панель**: Управление клиентами, балансировщиками и настройками безопасности через красивый Glassmorphism веб-интерфейс.

---

## 📋 Содержание

- [Архитектура](#-архитектура)
- [Быстрый старт](#-быстрый-старт)
  - [Установка в одну команду](#установка-в-одну-команду-curl)
  - [Ручная установка](#ручная-установка)
- [Первичная настройка](#-первичная-настройка)
- [Конфигурация (.env)](#-конфигурация-env)
- [Панель управления (Dashboard)](#-панель-управления-dashboard)
- [Настройка Nginx](#-настройка-nginx)
- [Обновление](#-обновление)
- [Разработка и зависимости](#-разработка-и-воспроизводимые-зависимости)
- [Устранение неполадок](#-устранение-неполадок)

---

## 🏗️ Архитектура

```text
Внешний запрос клиента:
https://sub.your-domain.com:2097/json/SUB_ID  (или /sub/SUB_ID)

3x-ui (Оригинальная подписка):
https://sub.your-domain.com:2096/sub/SUB_ID  (Base64 / Web HTML)
https://sub.your-domain.com:2096/json/SUB_ID (JSON Upstream)

AutoSub Server (Локальный прокси):
http://127.0.0.1:25500/json/SUB_ID
http://127.0.0.1:25500/sub/SUB_ID

Настройка в панели 3x-ui:
JSON URI обратного прокси = https://sub.your-domain.com:2097/json/
```

### Проксирование HTTP заголовков
AutoSub передает клиентам важные метаданные подписки:
- `Announce`: Текст объявления, отображаемый в шапке клиента (например, v2rayNG, Happ).
- `Routing`, `Routing-Enable`: Правила маршрутизации от upstream.
- `hide-settings`: Выдаётся как `1` только для групп, выбранных в админке AutoSub.

> `hide-settings` — это метаданные клиента, а не шифрование JSON. В Happ скрытие настроек требует отдельного [ProviderID](https://www.happ.su/main/dev-docs/provider-id), который AutoSub не настраивает. Зашифрованные ссылки Happ используют формат [crypt5](https://www.happ.su/main/dev-docs/crypto-link); AutoSub его не генерирует и всегда возвращает обычную JSON-подписку.

### ⚡ Совместимость и пинг (Client Compatibility)
Формат Xray JSON-подписок (который генерирует AutoSub) полностью совместим с популярными клиентами (Happ, v2rayNG, NekoBox и др.).
- ✅ **TCP Ping & Latency**: Благодаря автоматической нормализации исходящих соединений (структуры `vnext` для VLESS/VMess и `servers` для Trojan), стандартные тесты задержки и TCP-пинг работают корректно "из коробки" без бесконечной загрузки.
- 💡 **HTTP GET / Real Ping**: Для протокола VLESS Reality по-прежнему рекомендуется использовать тест реальной задержки (Real Ping / via proxy get), так как серверы Reality могут блокировать пустые TCP-запросы (handshake) для защиты от активного сканирования цензорами.

---

## 🚀 Быстрый старт

**Стабильный релиз (Рекомендуется):**
```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/main/install.sh | bash
```

**Ветка разработки (Dev):**
```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/dev/install.sh | AUTOSUB_VERSION=dev bash
```

### Ручная установка

Требуется сервер на Ubuntu/Debian.

1. **Установка системных пакетов:**
   ```bash
   apt update
   apt install -y python3 python3-pip python3-venv nginx git curl
   ```

2. **Клонирование репозитория:**
   ```bash
   git clone https://github.com/amirim1/autosub-server.git /opt/autosub-server
   cd /opt/autosub-server
   ```

3. **Запуск скрипта установки:**
   Этот скрипт создаст виртуальное окружение, установит зафиксированные Python-зависимости и настроит системный сервис `systemd`.
   ```bash
   bash install.sh
   ```

---

## ⚙️ Первичная настройка (Пошагово)

После успешной установки (скрипт `install.sh` автоматически создаст папку `/opt/autosub-server` и запустит сервис), необходимо выполнить следующие шаги:

### Шаг 1: Подключение к 3x-ui (.env)
Откройте файл конфигурации:
```bash
nano /opt/autosub-server/.env
```
Укажите данные для подключения к вашей панели 3x-ui (потребуется `XUI_SUB_URL`, `XUI_API_URL` и либо логин/пароль, либо `XUI_API_TOKEN`). Задайте `AUTOSUB_ADMIN_PASSWORD` для защиты админ-панели AutoSub.
После сохранения перезапустите сервис:
```bash
systemctl restart autosub-server
```

### Шаг 2: Настройка Nginx прокси
AutoSub работает локально на порту `25500`. Чтобы ваши клиенты могли скачивать подписки, нужно настроить Nginx.
Запустите скрипт автонастройки (замените домен и порт на ваши):
```bash
bash /opt/autosub-server/setup_nginx.sh sub.your-domain.com 2097
```
*(Либо настройте Nginx вручную, пример конфигурации лежит в `nginx-example.conf`)*

### Шаг 3: Настройка внутри 3x-ui
Откройте вашу панель 3x-ui. Перейдите в Настройки панели (Panel Settings) и найдите параметр **"JSON URI обратного прокси"** (JSON Reverse Proxy URI). 
Установите его равным адресу вашего Nginx из Шага 2, например: `https://sub.your-domain.com:2097/json/`.
Теперь клиенты при нажатии кнопки "Обновить подписку" будут направляться через AutoSub.

### Шаг 4: Настройка балансировщиков в админке AutoSub
1. Пробросьте порт для безопасного доступа к локальной админке:
   ```bash
   ssh -L 25500:127.0.0.1:25500 root@YOUR_SERVER_IP
   ```
2. Откройте в браузере `http://127.0.0.1:25500/admin`
3. Перейдите на вкладку **Ноды** и нажмите **Node Discovery**, вставив Subscription ID любого активного клиента, чтобы AutoSub просканировал все ваши серверы.
4. Перейдите на вкладку **Автовыбор**, создайте балансировщик, выберите для него нужные серверы и сохраните. Готово!

---

## ⚙️ Конфигурация (.env)

Файл параметров расположен по адресу `/opt/autosub-server/.env`:

```env
AUTOSUB_HOST=127.0.0.1
AUTOSUB_PORT=25500

# Доверенные reverse-proxy (IP/CIDR через запятую). Только от них принимаются X-Real-IP/X-Forwarded-For
AUTOSUB_TRUSTED_PROXIES=127.0.0.1/32,::1/128

# Пароль для доступа к админ-панели /admin (оставьте пустым для отключения авторизации)
AUTOSUB_ADMIN_PASSWORD=your_secure_admin_password

# Адрес оригинальной JSON-подписки 3x-ui
XUI_SUB_URL=https://sub.your-domain.com:2096

# Адрес панели 3x-ui для работы API (укажите секретный путь, если используется)
XUI_API_URL=https://panel.your-domain.com:54321

# API-токен 3x-ui (Settings -> Security -> API Token)
XUI_API_TOKEN=

# Резервный URL 3x-ui
XUI_URL=https://sub.your-domain.com:2096

# Проверка TLS-сертификатов (true/false)
XUI_TLS_VERIFY=true

# Логин и пароль 3x-ui (если не используется API-токен)
XUI_USERNAME=admin
XUI_PASSWORD=change_me

# Кастомный заголовок подписки (опционально)
# SUB_TITLE=Мой VPN
```

---

## 📊 Панель управления (Dashboard)

Админ-панель работает локально для безопасности. Для подключения используйте SSH-туннель:

```bash
ssh -L 25500:127.0.0.1:25500 root@YOUR_SERVER_IP
```

После создания туннеля откройте в браузере: `http://127.0.0.1:25500/admin`

### Приоритет назначения групп клиентам:

1. **Локальная SQLite БД (`client_groups`)** — ручное и самое стабильное назначение через админ-панель.
2. **Оверрайды (`client_group_overrides`)** — переопределения групп по email/sub_id.
3. **3x-ui API (fallback)** — автоматическое получение групп клиентов из 3x-ui.

---

## 🌐 Настройка Nginx

### Автоматическая настройка

```bash
bash /opt/autosub-server/setup_nginx.sh sub.your-domain.com 2097
```

### Ручная настройка

Скопируйте пример конфигурационного файла `/opt/autosub-server/nginx-example.conf`:

```nginx
server {
    listen 2097 ssl http2;
    server_name sub.your-domain.com;

    ssl_certificate /etc/letsencrypt/live/sub.your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sub.your-domain.com/privkey.pem;

    # JSON subscriptions
    location /json/ {
        proxy_pass http://127.0.0.1:25500/json/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Legacy Base64 subscription URLs (serves HTML to web browsers, JSON to VPN apps)
    location /sub/ {
        proxy_pass http://127.0.0.1:25500/sub/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        return 404;
    }
}
```

Если Nginx расположен не на том же хосте, добавьте его IP или подсеть в `AUTOSUB_TRUSTED_PROXIES`. Не добавляйте публичные клиентские сети: forwarded-заголовки от недоверенных адресов намеренно игнорируются.

Активируйте конфиг и перезапустите Nginx:

```bash
ln -s /etc/nginx/sites-available/autosub-json /etc/nginx/sites-enabled/autosub-json
nginx -t
systemctl reload nginx
```

---

## 🔄 Обновление

### Однострочное обновление (curl)

```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/main/update.sh | bash
```

При обновлении создается автоматическая резервная копия базы данных и конфигурации в `/opt/autosub-server-backups/`.

---

## 🧰 Разработка и воспроизводимые зависимости

Поддерживается Python 3.10 и новее. Установка production-зависимостей из lock-файла:

```bash
python -m pip install --require-hashes -r requirements.txt
```

Для разработки используйте отдельное виртуальное окружение и dev lock:

```bash
python -m venv .venv
python -m pip install --require-hashes -r requirements-dev.txt
```

Основные проверки:

```bash
pytest
pytest --cov=. --cov-report=term-missing --cov-fail-under=55
ruff check .
ruff format --check .
pyright
pip-audit -r requirements.txt --require-hashes
bandit -c pyproject.toml -r . -x tests -ll
```

`ruff format --check .` пока является диагностической локальной проверкой: существующий код еще не имеет единого formatter baseline, поэтому CI не запускает ее до отдельного форматирующего PR.

После изменения `requirements.in` или `requirements-dev.in` пересоберите оба lock-файла из чистого dev-окружения:

```bash
python -m piptools compile --upgrade --generate-hashes --resolver=backtracking --strip-extras --no-emit-index-url --output-file=requirements.txt requirements.in
python -m piptools compile --upgrade --generate-hashes --resolver=backtracking --strip-extras --no-emit-index-url --allow-unsafe --output-file=requirements-dev.txt requirements-dev.in
```

---

## 🛠️ Устранение неполадок

- **Проверка статуса службы:** `systemctl status autosub-server`
- **Просмотр логов в реальном времени:** `journalctl -u autosub-server -f`
- **Проверка здоровья приложения:** `curl http://127.0.0.1:25500/health`
- **Тестирование проксирования:** `curl -k https://sub.your-domain.com:2097/json/CLIENT_SUB_ID`
