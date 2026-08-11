# Code Review

Проверяй изменения по порядку: корректность и регрессии; безопасность (секреты, auth, CSRF, TLS, forwarded headers); производительность и async resource lifecycle; читаемость; соответствие архитектуре; тесты и документацию.

Каждое замечание должно указывать файл/контекст, влияние, приоритет и конкретный способ исправления. Не требуй рефакторинга или новых абстракций без связи с риском.

Перед review установи scope через `git diff` и `git status`. Для runtime-изменений сопоставь тесты `test_builder.py`, `test_storage.py`, `test_api_client.py`, `test_server.py`, `test_dashboard.py`, `test_fingerprint.py`; для deployment-изменений проверь shell syntax и документацию.
