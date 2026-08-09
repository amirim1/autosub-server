# AutoSub Server API

## Public Routes

### `GET /`

Redirects to `/admin` with HTTP 307.

### `GET /health`

Returns plain text such as `AutoSub Server v<version> OK`.

### `GET /health/ready`

Returns `200 {"status":"ready"}` only after FastAPI startup has opened/migrated
SQLite and initialized the HTTP manager, subscription cache, rate limiter, proxy
resolver, and CSRF manager. Before or after lifespan it returns
`503 {"status":"not_ready"}`. It does not contact 3x-ui or expose internal paths.

### `GET /json/{sub_id}`

Fetches the original 3x-ui subscription and returns generated JSON. `sub_id` is the subscription/client identifier. This endpoint remains JSON even when `Accept: text/html` or a browser User-Agent is supplied. Query parameters are passed to the builder/upstream path as supported by the existing implementation. Responses may include subscription metadata headers. Rate-limit failures return HTTP 429 with a JSON error body and `Retry-After`; unexpected failures return HTTP 500.

### `GET /sub/{sub_id}`

Legacy-compatible route with two representations:

- `?format=json` returns the same generated JSON as the historical machine path.
- `?format=html` returns a local AutoSub landing page. The page validates subscription
  readiness through the existing cache but never embeds or returns upstream HTML.

Explicit `format` has priority over weighted `Accept`; `application/json` and
`text/plain` select JSON, while `text/html` selects local HTML. Known subscription
clients and unknown/default `*/*` requests fall back to JSON. A Mozilla User-Agent is
used only as a final browser fallback. Unsupported or repeated `format` values return
HTTP 400 without reflecting the value. AutoSub has no separate raw/base64 representation.

The HTML response uses a local autoescaped template, strict self-only CSP,
`Cache-Control: no-store`, `Referrer-Policy: no-referrer`, and no external assets.
Upstream failures and redirects become a generic local HTTP 502 page with request ID.
Its fixed stylesheet is served from `/sub/_assets/subscription.css`, so the existing
public Nginx `/sub/` location is sufficient and admin assets remain unexposed.

### `GET /static/{path}`

Serves dashboard assets when the configured static directory exists.

## Admin Routes

All admin routes apply the admin-auth rate policy before `verify_admin`. If
`AUTOSUB_ADMIN_PASSWORD` is empty, access is unauthenticated but still rate-limited;
production deployments should set it.

### Read routes

- `GET /admin` — dashboard and reusable signed CSRF token.
- `GET /admin/preview?sub_id=<id>` — subscription preview; missing `sub_id` redirects to `/admin`.
- `GET /admin/api-test` — tests the configured XUI API and returns JSON.
- `GET /admin/debug?sub_id=<id>` — debug information; missing identifier returns HTTP 400.

### Mutating form routes

All require form field `_csrf` and normally return HTTP 303 redirects. Invalid or expired CSRF tokens return HTTP 403 with a request ID.

- `POST /admin/save` — saves dashboard configuration parsed from the submitted form.
- `POST /admin/discover` — form field `sub_id`; discovers nodes from a subscription.
- `POST /admin/set-client-group` — form fields `sub_id`, `email`, `groups`.
- `POST /admin/delete-client-group` — form field `sub_id`.
- `POST /admin/add-autoselect` — form fields `autoselect_id`, `name`, `strategy`; supported strategies are `leastPing` and `leastLoad`.
- `POST /admin/delete-autoselect` — form field `autoselect_id`.

## Authentication and Middleware-like Controls

FastAPI dependencies enforce admin authentication. CSRF uses expiring reusable
HMAC-SHA256 tokens and keeps no server-side token store. Client IP resolution trusts
`X-Real-IP`/`X-Forwarded-For` only when the direct peer belongs to
`AUTOSUB_TRUSTED_PROXIES`. Rate limiting is process-local and therefore not a
distributed protection mechanism. Public, admin-auth and expensive-admin operations
use separate policies. Rejected requests return HTTP 429 with `Retry-After`,
`X-Request-ID`, and `Cache-Control: no-store` before handler/cache/upstream work.

## Upstream Configuration

`XUI_SUB_URL` identifies the subscription upstream. `XUI_API_URL` identifies the panel API. `XUI_URL` is a backward-compatible fallback. API auth prefers `XUI_API_TOKEN`, otherwise uses `XUI_USERNAME` and `XUI_PASSWORD`.
