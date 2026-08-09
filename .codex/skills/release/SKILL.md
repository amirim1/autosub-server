# Release

## Checklist

- Проверить diff, `git status`, версию и `CHANGELOG.md`.
- Запустить `python -m pytest -q` и доступные статические проверки.
- Проверить документацию, install/update scripts, миграции и обратимость.
- Сверить `CHANGELOG.md`, `README.md`, `README_EN.md`, версию/тег и совместимость `.env`/SQLite migration.
- Проверить `python -m compileall -q *.py`; на Linux — `bash -n` для scripts и `nginx -t` для Nginx.
- Для Linux deployment отдельно отметить непроверенные systemd/nginx smoke tests.

## Git Safety

Не выполнять commit, push, force push, удаление тегов или переписывание истории без прямого запроса. Не обновлять существующий release tag принудительно; для исправлений использовать новый согласованный релизный процесс.
