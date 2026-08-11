# Deployment Audit

Review Linux deployment without executing destructive installation, update or migration actions.

Check `install.sh`, `update.sh`, `setup_nginx.sh`, `finish_setup.sh`, `autosub-server.service` and `nginx-example.conf` for:

- source/version selection and backup behavior;
- virtualenv and dependency installation;
- `.env` and `config.json` preservation;
- file permissions and systemd service identity;
- Nginx certificate paths, proxy locations and `nginx -t` usage;
- restart/health-check ordering;
- rollback feasibility and backup location.

Use `bash -n` and static inspection where possible. Clearly separate Windows checks from Linux-only systemd/Nginx smoke tests.
