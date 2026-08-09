# Project Audit

## Purpose

Проводить глубокий, доказательный аудит AutoSub Server перед изменениями.

## Procedure

1. Проверить `git status`, дерево, `AGENTS.md`, `README.md`, `docs/`, зависимости и deployment scripts.
2. Найти entry points, маршруты, persistence, external integrations и security boundaries.
3. Проверить тесты, команды и несоответствия документации коду.
4. Не изменять файлы до завершения аудита и согласования, если запрос требует audit-first.
5. Сверить фактические маршруты, конфигурационные переменные, SQLite-таблицы, тестовые файлы и deployment-команды с AI-документацией.

## Result Format

Отчёт должен содержать стек, архитектуру, важные директории, команды и отдельные findings:

`Critical`, `High`, `Medium`, `Low` — каждый с доказательством, риском и минимальной рекомендацией.

Для AutoSub Server отдельно проверь subscription flow, legacy `/sub/` content negotiation, admin auth/CSRF, trusted proxies, XUI URL separation, SQLite migrations и Linux-only deployment limitations.
