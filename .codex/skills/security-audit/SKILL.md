# Security Audit

Audit AutoSub Server without changing runtime code unless explicitly requested.

Check:

- secrets, `.env`, logs and hard-coded credentials;
- `AUTOSUB_ADMIN_PASSWORD`, Basic Auth and CSRF token lifecycle;
- trusted proxy parsing and forwarded-header spoofing;
- rate limiting and its process-local limitations;
- `XUI_API_TOKEN`, login/password fallback and TLS verification;
- SQLite parameterization, migrations and sensitive data exposure;
- dashboard routes, templates and static asset exposure;
- Nginx TLS paths, public locations, permissions and systemd service user.

Report Critical/High/Medium/Low findings with file evidence, impact and minimal remediation. Never print or copy real secrets.
