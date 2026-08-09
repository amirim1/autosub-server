# Debugging

## Method

1. Воспроизвести проблему минимальным сценарием.
2. Зафиксировать симптомы, входы и ожидаемое поведение.
3. Проследить поток от HTTP/API boundary через builder/storage до ответа.
4. Найти первопричину, отделив её от вторичных ошибок.
5. Предложить минимальное исправление без изменения unrelated behavior.
6. Добавить/обновить regression test и повторить релевантные проверки.

Базовые команды: `python -m pytest -q`, `python -m compileall -q *.py`. Для HTTP используй FastAPI `TestClient`; для SQLite — временную БД через pytest `tmp_path`. Не проверяй production upstream или реальные секреты без явного разрешения.

Для subscription и admin bugs отдельно проверяй security headers, trusted proxies, CSRF, rate limiting и совместимость формата JSON.
