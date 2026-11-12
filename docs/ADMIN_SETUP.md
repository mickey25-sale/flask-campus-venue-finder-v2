# Administrator and Google Sheets setup

Public visitors can browse the published CSV-backed venue map without signing
in. Only an authenticated administrator can add, edit, change status, or delete
venues. Those changes use the Google Sheets API; the published CSV URL remains
read-only.

## Administrator accounts

Administrator accounts are stored in the application's existing `venues.db`
SQLite database. The `admins` table is managed by Flask-Migrate and shares the
same database file as the venue cache; the application does not create a
separate authentication database.

There are two administrator roles:

- `admin` can manage venues.
- `admin_manager` can manage venues and administrator accounts.

The migration promotes the oldest active account to `admin_manager` only when
no active manager exists, so an existing installation retains access to account
management. Run migrations after deployment and keep at least one active
manager account.

Apply the database migration before starting the application:

```powershell
flask --app app db upgrade
```

Administrator sessions are stored in the same SQLite database in the
`server_sessions` table. The browser receives only a signed, opaque session ID;
session contents stay server-side. Sessions expire after eight hours. Login
rotates the session ID, and login/logout forms require a CSRF token. The
optional “Keep me signed in” checkbox issues a 14-day persistent cookie with
HttpOnly, SameSite=Lax, and Secure enabled when `FLASK_HTTPS=true`.

Authentication routes:

- `GET /admin/login` and `POST /admin/login`
- `GET /admin/forgot-password` and `POST /admin/forgot-password`
- `GET /admin/reset-password` and `POST /admin/reset-password`
- `POST /admin/logout`
- `GET /admin/profile` (authenticated; returns safe profile fields only)
- `GET /admin/dashboard` (authenticated)

Password recovery returns the same confirmation for valid and unknown email
addresses. Requests are limited to five per minute and ten per hour per client
IP. Reset tokens are generated with a cryptographically secure random source,
only their SHA-256 hashes are stored, and they expire after 30 minutes. Issuing
a new link invalidates prior links; successful reset consumes the token and
invalidates existing sessions and remembered logins for that administrator.
Reset pages disable caching and referrers, and the token is carried in the
email URL fragment so it is not sent in the initial page request.

Configure outbound email and the canonical public application origin using
environment variables; never commit their values:

- `APP_PUBLIC_URL`: canonical HTTPS origin used to construct reset links.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`.
- `SMTP_USE_SSL=true` for implicit TLS (typically port 465). Otherwise the app
  requires STARTTLS (typically port 587).

Do not use plain SMTP without TLS. When SMTP is unavailable, the application
logs a delivery error without revealing account existence to the requester;
configure valid email settings before enabling recovery in production.

All `/api/admin/` mutation routes enforce administrator authorization in Flask
before processing the request and also use route-level authorization guards.
Anonymous writes receive HTTP 401; public `GET /` and `GET /api/venues` remain
available without authentication. Google Sheets write helpers are only called
from these guarded administrator endpoints.

Active managers can manage administrator accounts from the dashboard or via
the protected `/api/admin/accounts` API:

- `GET /api/admin/accounts` lists safe account fields and recent audit events.
- `POST /api/admin/accounts` creates an account.
- `PUT /api/admin/accounts/<id>` changes its name or email.
- `PATCH /api/admin/accounts/<id>/role` changes its role.
- `PATCH /api/admin/accounts/<id>/status` activates or deactivates it.
- `POST /api/admin/accounts/<id>/password` securely resets its password.

The API never returns password hashes or passwords. Account-management
operations are recorded in the audit log without credential values. The last
active manager cannot be deactivated or demoted; administrator accounts are
deactivated rather than deleted.

Login attempts are limited to five per minute per client IP. The default limiter
storage is in-process memory; configure `RATELIMIT_STORAGE_URI` to a shared
storage backend when running multiple application workers.

Create administrator accounts from the server terminal. The command prompts
for the password twice without echoing it, validates the email, and requires a
password of at least 12 characters with uppercase and lowercase letters, a
number, and a special character. Only a Werkzeug password hash is stored:

```powershell
flask --app app create-admin
```

There is no public registration endpoint. Additional administrators are created
by running the command again. Duplicate emails are rejected. Activate or
deactivate an account with:

```powershell
flask --app app set-admin-active admin@example.edu --active
flask --app app set-admin-active admin@example.edu --inactive
```

Configure `FLASK_SECRET_KEY` as a long random secret and set `FLASK_HTTPS=true`
when the app is served over HTTPS. Do not commit passwords, password hashes,
session secrets, or service-account keys.

## Google Sheets API writes

1. Enable the Google Sheets API in the service account's Google Cloud project.
2. Share the original spreadsheet with the service account's email as an editor.
3. Configure `GOOGLE_SHEETS_SPREADSHEET_ID` using the ID from the original
   spreadsheet URL (`/spreadsheets/d/<ID>/edit`), not the published CSV URL.
4. Configure `GOOGLE_SHEETS_GID` to the worksheet tab ID. It defaults to `0`,
   matching the published CSV source.
5. Configure `GOOGLE_APPLICATION_CREDENTIALS` to an access-restricted
   service-account JSON file outside the repository. Alternatively, provide
   the credential JSON in the server-only `GOOGLE_SERVICE_ACCOUNT_JSON`
   environment variable.

The worksheet must contain the CSV venue columns:
`venue_name, location, status, updated_at, latitude, longitude`. The app
validates admin input on the server and refreshes its venue cache from the
authenticated worksheet after a successful write. If the Google configuration
is missing or invalid, admin writes fail with an explicit error and public
read-only browsing continues.
