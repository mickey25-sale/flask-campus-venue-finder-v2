import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import smtplib
import ssl
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from threading import Thread as PasswordResetThread
from urllib.parse import quote, urljoin, urlsplit

import click
import requests
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_user,
    logout_user,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask.sessions import SessionInterface, SessionMixin
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import validates
from sqlalchemy import update
from werkzeug.datastructures import CallbackDict
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
DATABASE_PATH = Path(
    os.environ.get("VENUE_DATABASE_PATH")
    or Path(app.root_path) / "venues.db"
)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_HTTPS", "").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    REMEMBER_COOKIE_DURATION=timedelta(days=14),
    REMEMBER_COOKIE_SECURE=os.environ.get("FLASK_HTTPS", "").lower() == "true",
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE="Lax",
    SQLALCHEMY_DATABASE_URI=f"sqlite:///{DATABASE_PATH.as_posix()}",
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)
db = SQLAlchemy(app)
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
    headers_enabled=False,
)


def include_migration_object(obj, name, object_type, reflected, compare_to):
    sqlite_managed_tables = {"venues", "excluded_sheet_venues"}
    if object_type == "table" and name in sqlite_managed_tables:
        return not reflected or compare_to is not None
    if object_type == "index" and obj.table.name in sqlite_managed_tables:
        return False
    return True


migrate = Migrate(app, db, include_object=include_migration_object)
login_manager = LoginManager(app)
login_manager.login_view = "admin_login"
login_manager.session_protection = "strong"


@login_manager.unauthorized_handler
def unauthorized_admin_request():
    if request.path.startswith("/api/") or request.path == "/admin/profile":
        return jsonify({"error": "Administrator authentication is required."}), 401
    return redirect(url_for("admin_login"))


DUMMY_ADMIN_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))

# Your published Google Sheet
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vROiTq-W_OdNM2ZoWur6yJFHOOYaMBxMxis4cybVBH6YPeNdpRzjpe2GGjHxk_ziAOTs4iGF7k3Ajpz/pub?gid=0&single=true&output=csv"
SHEET_SYNC_INTERVAL_SECONDS = 30
VENUE_COLUMNS = (
    "venue_name",
    "location",
    "status",
    "updated_at",
    "latitude",
    "longitude",
)
VENUE_STATUSES = ("AVAILABLE", "OCCUPIED", "MAINTENANCE")
LEGACY_STATUS = "Unknown"
GOOGLE_SHEETS_API_URL = "https://sheets.googleapis.com/v4"
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
ADMIN_ROLE = "admin"
ADMIN_MANAGER_ROLE = "admin_manager"
ADMIN_ROLES = (ADMIN_ROLE, ADMIN_MANAGER_ROLE)
STATUS_ALIASES = {
    "available": "AVAILABLE",
    "free": "AVAILABLE",
    "occupied": "OCCUPIED",
    "maintenance": "MAINTENANCE",
    "unknown": LEGACY_STATUS,
    "": LEGACY_STATUS,
}

class SheetsIntegrationError(Exception):
    """A safe, user-facing Google Sheets integration failure."""


EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def validate_admin_email(email):
    normalized_email = email.strip().casefold() if isinstance(email, str) else ""
    local_part, separator, domain = normalized_email.partition("@")
    if (
        not separator
        or len(normalized_email) > 254
        or len(local_part) > 64
        or not EMAIL_PATTERN.fullmatch(normalized_email)
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
        or not domain
    ):
        raise click.ClickException("Enter a valid administrator email address.")
    return normalized_email


def validate_admin_password(password):
    error = admin_password_error(password)
    if error:
        raise click.ClickException(error)


def admin_password_error(password):
    if not isinstance(password, str):
        return "Password must be text."
    if (
        len(password) < 12
        or not any(character.isupper() for character in password)
        or not any(character.islower() for character in password)
        or not any(character.isdigit() for character in password)
        or not any(not character.isalnum() for character in password)
    ):
        return (
            "Password must be at least 12 characters and include uppercase and "
            "lowercase letters, a number, and a special character."
        )
    return None


def utc_now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Admin(UserMixin, db.Model):
    __tablename__ = "admins"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(320), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(32), nullable=False, default="admin", index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    last_login = db.Column(db.DateTime(timezone=True), nullable=True)
    auth_version = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    def get_id(self):
        return f"{self.id}:{self.auth_version}"

    @validates("email")
    def normalize_email(self, key, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"A valid administrator {key} is required.")
        return value.strip().casefold()

    def set_password(self, password):
        if not isinstance(password, str) or not password:
            raise ValueError("A non-empty password is required.")
        self.password_hash = generate_password_hash(password)
        self.auth_version = (self.auth_version or 0) + 1

    def check_password(self, password):
        if not isinstance(password, str):
            return False
        try:
            return check_password_hash(self.password_hash, password)
        except (TypeError, ValueError):
            app.logger.exception("An administrator password hash is invalid.")
            return False

    @property
    def can_manage_admins(self):
        return self.is_active and self.role == ADMIN_MANAGER_ROLE


class AdminPasswordResetToken(db.Model):
    __tablename__ = "admin_password_reset_tokens"

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(
        db.Integer,
        db.ForeignKey("admins.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)


class AdminAuditLog(db.Model):
    __tablename__ = "admin_audit_log"

    id = db.Column(db.Integer, primary_key=True)
    actor_admin_id = db.Column(
        db.Integer,
        db.ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    target_admin_id = db.Column(
        db.Integer,
        db.ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    action = db.Column(db.String(64), nullable=False, index=True)
    details = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    actor = db.relationship("Admin", foreign_keys=[actor_admin_id])
    target = db.relationship("Admin", foreign_keys=[target_admin_id])


def admin_account_data(admin):
    return {
        "id": admin.id,
        "full_name": admin.full_name,
        "email": admin.email,
        "role": admin.role,
        "is_active": admin.is_active,
        "created_at": admin.created_at.isoformat(),
        "updated_at": admin.updated_at.isoformat(),
        "last_login": admin.last_login.isoformat() if admin.last_login else None,
    }


def record_admin_action(action, target_admin=None, details=None):
    db.session.add(
        AdminAuditLog(
            actor_admin_id=current_user.id,
            target_admin_id=target_admin.id if target_admin else None,
            action=action,
            details=json.dumps(details or {}, sort_keys=True),
        )
    )


class ServerSession(db.Model):
    __tablename__ = "server_sessions"

    session_id = db.Column(db.String(128), primary_key=True)
    data = db.Column(db.Text, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)


class DatabaseSession(CallbackDict, SessionMixin):
    def __init__(self, initial=None, sid=None, new=False):
        self.sid = sid
        self.new = new
        self.modified = False
        self.accessed = False
        super().__init__(initial, self._mark_modified)

    def _mark_modified(self, mapping):
        if mapping is self:
            self.modified = True

    def __getitem__(self, key):
        self.accessed = True
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.accessed = True
        return super().get(key, default)

    def __contains__(self, key):
        self.accessed = True
        return super().__contains__(key)


class DatabaseSessionInterface(SessionInterface):
    session_class = DatabaseSession
    salt = "campus-venue-server-session"

    def _serializer(self, app):
        return URLSafeTimedSerializer(app.secret_key, salt=self.salt)

    def _new_session(self):
        return self.session_class(sid=secrets.token_urlsafe(32), new=True)

    def open_session(self, app, req):
        cookie = req.cookies.get(self.get_cookie_name(app))
        if not cookie:
            return self._new_session()

        try:
            session_id = self._serializer(app).loads(
                cookie,
                max_age=int(app.permanent_session_lifetime.total_seconds()),
            )
        except BadSignature:
            return self._new_session()

        stored_session = db.session.get(ServerSession, session_id)
        if stored_session is None:
            return self._new_session()
        if stored_session.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
            db.session.delete(stored_session)
            db.session.commit()
            return self._new_session()

        try:
            data = json.loads(stored_session.data)
        except json.JSONDecodeError:
            app.logger.warning("Discarding a malformed server-side session.")
            db.session.delete(stored_session)
            db.session.commit()
            return self._new_session()
        if not isinstance(data, dict):
            app.logger.warning("Discarding a non-object server-side session.")
            db.session.delete(stored_session)
            db.session.commit()
            return self._new_session()
        return self.session_class(data, sid=session_id)

    def save_session(self, app, current_session, response):
        cookie_name = self.get_cookie_name(app)
        if current_session.accessed:
            response.vary.add("Cookie")

        if not current_session:
            if not current_session.new:
                db.session.execute(
                    db.delete(ServerSession).where(
                        ServerSession.session_id == current_session.sid
                    )
                )
                db.session.commit()
            response.delete_cookie(
                cookie_name,
                domain=self.get_cookie_domain(app),
                path=self.get_cookie_path(app),
                secure=self.get_cookie_secure(app),
                httponly=self.get_cookie_httponly(app),
                samesite=self.get_cookie_samesite(app),
            )
            return

        refresh_permanent_session = (
            current_session.permanent
            and app.config["SESSION_REFRESH_EACH_REQUEST"]
        )
        if not current_session.modified and not refresh_permanent_session:
            return

        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + (
            app.permanent_session_lifetime
        )
        stored_session = db.session.get(ServerSession, current_session.sid)
        if stored_session is None:
            stored_session = ServerSession(
                session_id=current_session.sid,
                data=json.dumps(dict(current_session)),
                expires_at=expires_at,
            )
            db.session.add(stored_session)
        else:
            stored_session.data = json.dumps(dict(current_session))
            stored_session.expires_at = expires_at
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            raise

        signed_session_id = self._serializer(app).dumps(current_session.sid)
        expires = self.get_expiration_time(app, current_session)
        response.set_cookie(
            cookie_name,
            signed_session_id,
            expires=expires,
            httponly=self.get_cookie_httponly(app),
            secure=self.get_cookie_secure(app),
            samesite=self.get_cookie_samesite(app),
            domain=self.get_cookie_domain(app),
            path=self.get_cookie_path(app),
        )

    def regenerate(self, current_session):
        db.session.execute(
            db.delete(ServerSession).where(
                ServerSession.session_id == current_session.sid
            )
        )
        db.session.commit()
        current_session.sid = secrets.token_urlsafe(32)
        current_session.new = True
        current_session.modified = True


app.session_interface = DatabaseSessionInterface()


@contextmanager
def database_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    with database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS venues (
                id INTEGER PRIMARY KEY,
                venue_name TEXT NOT NULL,
                location TEXT,
                status TEXT,
                updated_at TEXT,
                latitude TEXT,
                longitude TEXT,
                source_key TEXT,
                admin_modified INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(venues)")
        }
        if "source_key" not in columns:
            connection.execute("ALTER TABLE venues ADD COLUMN source_key TEXT")
        if "admin_modified" not in columns:
            connection.execute(
                "ALTER TABLE venues ADD COLUMN admin_modified INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS venues_source_key
            ON venues (source_key) WHERE source_key IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS excluded_sheet_venues (
                source_key TEXT PRIMARY KEY
            )
            """
        )
        connection.execute(
            """
            UPDATE venues
            SET status = CASE lower(trim(status))
                WHEN 'free' THEN 'AVAILABLE'
                WHEN 'available' THEN 'AVAILABLE'
                WHEN 'occupied' THEN 'OCCUPIED'
                WHEN 'maintenance' THEN 'MAINTENANCE'
                WHEN 'unknown' THEN 'Unknown'
                ELSE 'Unknown'
            END
            WHERE status IS NULL OR lower(trim(status)) NOT IN (
                'available', 'occupied', 'maintenance', 'unknown'
            )
            """
        )


init_db()

_sheet_sync_lock = threading.Lock()
_sheet_last_attempt = None
_sheet_sync_error = None


@login_manager.user_loader
def load_admin(admin_id):
    try:
        account_id, auth_version = admin_id.split(":", 1)
        admin = db.session.get(Admin, int(account_id))
    except (TypeError, ValueError):
        return None
    try:
        version_matches = admin and admin.auth_version == int(auth_version)
    except (TypeError, ValueError):
        return None
    return (
        admin
        if version_matches and admin.is_active and admin.role in ADMIN_ROLES
        else None
    )


def refresh_sheet_if_due(force=False):
    global _sheet_last_attempt, _sheet_sync_error

    if (
        not force
        and
        _sheet_last_attempt is not None
        and time.monotonic() - _sheet_last_attempt < SHEET_SYNC_INTERVAL_SECONDS
    ):
        return
    if not _sheet_sync_lock.acquire(blocking=False):
        return

    try:
        if (
            not force
            and
            _sheet_last_attempt is not None
            and time.monotonic() - _sheet_last_attempt < SHEET_SYNC_INTERVAL_SECONDS
        ):
            return
        _sheet_last_attempt = time.monotonic()
        try:
            response = requests.get(SHEET_CSV_URL, timeout=15)
            response.raise_for_status()
            import_venues(response.text)
            _sheet_sync_error = None
        except (requests.RequestException, ValueError, sqlite3.Error):
            _sheet_sync_error = (
                "Could not sync venues from Google Sheets. "
                "Showing saved venue data."
            )
            app.logger.exception("Could not sync venues from Google Sheets")
    finally:
        _sheet_sync_lock.release()


def import_venues(csv_text):
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise ValueError("CSV file is missing its header row.")

    reader.fieldnames = [
        field.strip().lstrip("\ufeff").casefold()
        for field in reader.fieldnames
    ]
    missing_columns = set(VENUE_COLUMNS) - set(reader.fieldnames)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"CSV file is missing required columns: {missing}")

    rows = []
    identity_counts = {}
    for raw_row in reader:
        row = {
            column: (raw_row.get(column) or "").strip()
            for column in VENUE_COLUMNS
        }
        if not row["venue_name"]:
            app.logger.warning("Skipping a Google Sheets row without a venue name.")
            continue
        try:
            row["status"] = normalize_venue_status(
                row["status"], allow_aliases=True
            )
        except ValueError:
            app.logger.warning(
                "Unknown status %r for venue %r; displaying Unknown.",
                row["status"],
                row["venue_name"],
            )
            row["status"] = LEGACY_STATUS
        identity = normalize_venue_identity(row["venue_name"], row["location"])
        occurrence = identity_counts.get(identity, 0)
        identity_counts[identity] = occurrence + 1
        source_key = hashlib.sha256(
            f"{identity}\x1f{occurrence}".encode("utf-8")
        ).hexdigest()
        rows.append((row, source_key))

    incoming_keys = {source_key for _, source_key in rows}
    with database_connection() as connection:
        connection.row_factory = sqlite3.Row
        for row, source_key in rows:
            existing = connection.execute(
                "SELECT id FROM venues WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            if existing:
                assignments = ", ".join(
                    f"{column} = ?" for column in VENUE_COLUMNS
                )
                connection.execute(
                    f"UPDATE venues SET {assignments}, admin_modified = 0 "
                    "WHERE id = ?",
                    tuple(row[column] for column in VENUE_COLUMNS)
                    + (existing["id"],),
                )
                continue

            local_rows = connection.execute(
                "SELECT id, venue_name, location FROM venues WHERE source_key IS NULL"
            ).fetchall()
            matching_local = next(
                (
                    local_row
                    for local_row in local_rows
                    if normalize_venue_identity(
                        local_row["venue_name"], local_row["location"]
                    )
                    == normalize_venue_identity(row["venue_name"], row["location"])
                ),
                None,
            )
            if matching_local:
                assignments = ", ".join(
                    f"{column} = ?" for column in VENUE_COLUMNS
                )
                connection.execute(
                    f"UPDATE venues SET {assignments}, source_key = ?, "
                    "admin_modified = 0 WHERE id = ?",
                    tuple(row[column] for column in VENUE_COLUMNS)
                    + (source_key, matching_local["id"]),
                )
                continue

            columns = ", ".join(VENUE_COLUMNS) + ", source_key, admin_modified"
            placeholders = ", ".join("?" for _ in range(len(VENUE_COLUMNS) + 2))
            connection.execute(
                f"INSERT INTO venues ({columns}) VALUES ({placeholders})",
                tuple(row[column] for column in VENUE_COLUMNS) + (source_key, 0),
            )

        current_sheet_rows = connection.execute(
            "SELECT id, source_key, admin_modified FROM venues "
            "WHERE source_key IS NOT NULL"
        ).fetchall()
        for sheet_row in current_sheet_rows:
            if sheet_row["source_key"] in incoming_keys:
                continue
            if sheet_row["admin_modified"]:
                connection.execute(
                    "UPDATE venues SET source_key = NULL WHERE id = ?",
                    (sheet_row["id"],),
                )
            else:
                connection.execute(
                    "DELETE FROM venues WHERE id = ?", (sheet_row["id"],)
                )
    return len(rows)


def normalize_venue_identity(venue_name, location):
    values = (venue_name or "", location or "")
    return "\x1f".join(
        " ".join(unicodedata.normalize("NFKC", value).casefold().split())
        for value in values
    )


def source_key_for_row(row, occurrence):
    identity = normalize_venue_identity(row["venue_name"], row["location"])
    return hashlib.sha256(
        f"{identity}\x1f{occurrence}".encode("utf-8")
    ).hexdigest()


def normalize_venue_status(status, allow_legacy=True, allow_aliases=False):
    if not isinstance(status, str):
        raise ValueError("Status must be a supported venue status.")

    status_value = status.strip().casefold()
    normalized = (
        STATUS_ALIASES.get(status_value)
        if allow_aliases
        else next(
            (
                allowed_status
                for allowed_status in VENUE_STATUSES
                if allowed_status.casefold() == status_value
            ),
            LEGACY_STATUS if status_value == LEGACY_STATUS.casefold() else None,
        )
    )
    if normalized is None or (normalized == LEGACY_STATUS and not allow_legacy):
        allowed = ", ".join(VENUE_STATUSES)
        raise ValueError(f"Status must be one of: {allowed}.")
    return normalized


def get_venues():
    with database_connection() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT id, {', '.join(VENUE_COLUMNS)} FROM venues "
            "WHERE source_key IS NOT NULL ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def get_status_filter_options():
    options = list(VENUE_STATUSES)
    with database_connection() as connection:
        rows = connection.execute(
            "SELECT DISTINCT status FROM venues "
            "WHERE status IS NOT NULL AND source_key IS NOT NULL"
        ).fetchall()
    for (status,) in rows:
        normalized = normalize_venue_status(status)
        if normalized not in options:
            options.append(normalized)
    return options


def csrf_token():
    token = session.get("_csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token}


@app.before_request
def protect_state_changes():
    if (
        request.path.startswith("/admin/")
        and request.url_rule is not None
        and request.endpoint
        not in {
            "admin_login",
            "admin_forgot_password",
            "admin_reset_password",
        }
        and not is_admin()
    ):
        if request.path == "/admin/profile":
            return jsonify({"error": "Administrator authentication is required."}), 401
        if request.method in {"GET", "HEAD"}:
            return redirect(url_for("admin_login"))
        return jsonify({"error": "Administrator authentication is required."}), 401

    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return None

    if request.path.startswith("/api/admin/") and not is_admin():
        return jsonify({"error": "Administrator authentication is required."}), 401
    if request.path == "/admin/logout" and not is_admin():
        return jsonify({"error": "Administrator authentication is required."}), 401

    expected_token = session.get("_csrf_token", "")
    supplied_token = request.headers.get("X-CSRF-Token")
    if supplied_token is None:
        supplied_token = request.form.get("csrf_token", "")
    if not expected_token or not supplied_token or not hmac.compare_digest(
        expected_token.encode("utf-8"), supplied_token.encode("utf-8")
    ):
        return jsonify({"error": "Invalid or missing CSRF token."}), 400
    return None


@app.cli.command("import-venues")
def import_venues_command():
    """Download the published CSV and synchronize local venue data."""
    try:
        response = requests.get(SHEET_CSV_URL, timeout=15)
        response.raise_for_status()
        imported_count = import_venues(response.text)
    except (requests.RequestException, ValueError, sqlite3.Error) as error:
        raise RuntimeError(f"Could not import venue CSV: {error}") from error

    print(f"Imported {imported_count} venues into {DATABASE_PATH}.")


@app.cli.command("create-admin")
def create_admin_command():
    """Create an administrator without exposing a public registration endpoint."""
    full_name = click.prompt("Administrator full name").strip()
    if not full_name or len(full_name) > 150:
        raise click.ClickException(
            "Administrator full name must be between 1 and 150 characters."
        )

    email = validate_admin_email(click.prompt("Administrator email"))
    if Admin.query.filter_by(email=email).first():
        raise click.ClickException("An administrator with that email already exists.")

    password = click.prompt(
        "Password", hide_input=True, confirmation_prompt=True
    )
    validate_admin_password(password)

    has_manager = Admin.query.filter_by(
        role=ADMIN_MANAGER_ROLE,
        is_active=True,
    ).first()
    role = ADMIN_ROLE if has_manager else ADMIN_MANAGER_ROLE
    admin = Admin(full_name=full_name, email=email, role=role, is_active=True)
    admin.set_password(password)
    db.session.add(admin)
    try:
        db.session.commit()
    except IntegrityError as error:
        db.session.rollback()
        raise click.ClickException(
            "An administrator with that email already exists."
        ) from error
    click.echo(f"Administrator account created for {admin.email} with role {role}.")


def set_admin_active(email, active):
    normalized_email = email.strip().casefold()
    admin = Admin.query.filter_by(email=normalized_email).first()
    if admin is None:
        raise click.ClickException("Administrator account was not found.")
    admin.is_active = active
    db.session.commit()
    state = "activated" if active else "deactivated"
    click.echo(f"Administrator account {state} for {admin.email}.")


@app.cli.command("set-admin-active")
@click.argument("email")
@click.option("--active/--inactive", required=True, help="Activate or deactivate this account.")
def set_admin_active_command(email, active):
    """Activate or deactivate an administrator account."""
    set_admin_active(email, active)


def is_admin():
    return (
        current_user.is_authenticated
        and current_user.is_active
        and current_user.role in ADMIN_ROLES
    )


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not is_admin():
            return jsonify({"error": "Administrator authentication is required."}), 401
        return view_func(*args, **kwargs)

    return wrapped


def admin_page_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not is_admin():
            return "Administrator authorization is required.", 403
        return view_func(*args, **kwargs)

    return wrapped


def admin_management_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Administrator authentication is required."}), 401
        if not current_user.can_manage_admins:
            return jsonify({"error": "Administrator management permission is required."}), 403
        return view_func(*args, **kwargs)

    return wrapped


@app.route("/admin/login", methods=["GET", "POST"], endpoint="admin_login")
@app.route("/login", methods=["GET", "POST"], endpoint="login")
@limiter.limit("5 per minute", methods=["POST"])
def admin_login():
    error = None
    success = (
        "Your password has been reset. You can now sign in."
        if request.args.get("reset") == "complete"
        else None
    )

    if request.method == "POST":
        email = (request.form.get("email") or request.form.get("username") or "")
        email = email.strip().casefold()
        password = request.form.get("password") or ""
        admin = Admin.query.filter_by(email=email).first()
        password_matches = (
            admin.check_password(password)
            if admin
            else check_password_hash(DUMMY_ADMIN_PASSWORD_HASH, password)
        )

        if admin and admin.is_active and admin.role in ADMIN_ROLES and password_matches:
            admin.last_login = datetime.now(timezone.utc)
            db.session.commit()
            app.session_interface.regenerate(session)
            session.clear()
            remember_admin = request.form.get("remember") == "on"
            login_user(admin, remember=remember_admin, fresh=True)
            session.permanent = True
            csrf_token()
            return redirect(url_for("admin_dashboard"))

        error = "Invalid email or password."

    if is_admin():
        return redirect(url_for("admin_dashboard"))
    return render_template("login.html", error=error, success=success)


PASSWORD_RESET_TOKEN_LIFETIME = timedelta(minutes=30)
PASSWORD_RESET_GENERIC_MESSAGE = (
    "If an active administrator account matches that email, "
    "password reset instructions will be sent."
)


def send_password_reset_email(admin_email, token):
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_username = os.environ.get("SMTP_USERNAME", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    smtp_sender = os.environ.get("SMTP_FROM", "").strip()
    public_url = os.environ.get("APP_PUBLIC_URL", "").strip()
    if not all((smtp_host, smtp_username, smtp_password, smtp_sender, public_url)):
        raise RuntimeError("Password-recovery SMTP settings are incomplete.")

    parsed_url = urlsplit(public_url)
    local_http = (
        parsed_url.hostname in {"localhost", "127.0.0.1"}
        and parsed_url.scheme == "http"
    )
    if (
        (parsed_url.scheme != "https" and not local_http)
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.path not in ("", "/")
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise RuntimeError("APP_PUBLIC_URL must be a trusted HTTPS origin.")

    use_ssl = os.environ.get("SMTP_USE_SSL", "").strip().lower() == "true"
    try:
        smtp_port = int(os.environ.get("SMTP_PORT", "465" if use_ssl else "587"))
    except ValueError as error:
        raise RuntimeError("SMTP_PORT must be a valid port number.") from error
    if not 1 <= smtp_port <= 65535:
        raise RuntimeError("SMTP_PORT must be a valid port number.")

    reset_url = (
        urljoin(public_url.rstrip("/") + "/", "admin/reset-password")
        + "#token="
        + quote(token, safe="")
    )
    message = EmailMessage()
    message["Subject"] = "Administrator password reset"
    message["From"] = smtp_sender
    message["To"] = admin_email
    message.set_content(
        "A password reset was requested for your administrator account.\n\n"
        f"Use this link within 30 minutes to choose a new password:\n{reset_url}\n\n"
        "If you did not request this change, you can ignore this email."
    )
    timeout = 10
    timeout = 10
    if use_ssl:
        with smtplib.SMTP_SSL(
            smtp_host,
            smtp_port,
            timeout=timeout,
            context=ssl.create_default_context(),
        ) as server:
            server.login(smtp_username, smtp_password)
            server.send_message(message)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(smtp_username, smtp_password)
            server.send_message(message)


def invalidate_password_reset_token(token_hash):
    with app.app_context():
        reset_token = AdminPasswordResetToken.query.filter_by(
            token_hash=token_hash,
            used_at=None,
        ).first()
        if reset_token:
            reset_token.used_at = utc_now_naive()
            db.session.commit()


def deliver_password_reset_email(admin_email, token, token_hash):
    try:
        send_password_reset_email(admin_email, token)
    except (OSError, RuntimeError, smtplib.SMTPException, ValueError):
        app.logger.exception(
            "Could not deliver an administrator password-recovery email."
        )
        invalidate_password_reset_token(token_hash)


@app.after_request
def secure_password_recovery_response(response):
    if request.endpoint in {"admin_forgot_password", "admin_reset_password"}:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/admin/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
@limiter.limit("10 per hour", methods=["POST"])
def admin_forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")

    email_value = request.form.get("email", "")
    try:
        email = validate_admin_email(email_value)
    except click.ClickException:
        email = None

    admin = (
        Admin.query.filter_by(email=email, is_active=True)
        .filter(Admin.role.in_(ADMIN_ROLES))
        .first()
        if email
        else None
    )
    if admin:
        now = utc_now_naive()
        db.session.execute(
            update(AdminPasswordResetToken)
            .where(
                AdminPasswordResetToken.admin_id == admin.id,
                AdminPasswordResetToken.used_at.is_(None),
            )
            .values(used_at=now)
        )
        token = secrets.token_urlsafe(32)
        reset_token = AdminPasswordResetToken(
            admin_id=admin.id,
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            expires_at=now + PASSWORD_RESET_TOKEN_LIFETIME,
        )
        db.session.add(reset_token)
        db.session.commit()
        try:
            delivery = PasswordResetThread(
                target=deliver_password_reset_email,
                args=(admin.email, token, reset_token.token_hash),
                daemon=True,
            )
            delivery.start()
        except (OSError, RuntimeError):
            app.logger.exception(
                "Could not queue an administrator password-recovery email."
            )
            invalidate_password_reset_token(reset_token.token_hash)

    return render_template(
        "forgot_password.html",
        message=PASSWORD_RESET_GENERIC_MESSAGE,
    )


def find_valid_password_reset_token(token):
    if not isinstance(token, str) or not token or len(token) > 256:
        return None, None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    reset_token = AdminPasswordResetToken.query.filter_by(
        token_hash=token_hash,
        used_at=None,
    ).first()
    if not reset_token or reset_token.expires_at <= utc_now_naive():
        return None, None
    admin = db.session.get(Admin, reset_token.admin_id)
    if not admin or not admin.is_active or admin.role not in ADMIN_ROLES:
        return None, None
    return reset_token, admin


@app.route("/admin/reset-password", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def admin_reset_password():
    if request.method == "GET":
        return render_template("reset_password.html")

    token = request.form.get("token", "")
    reset_token, admin = find_valid_password_reset_token(token)
    if not reset_token or not admin:
        return render_template(
            "reset_password.html",
            error="This password reset link is invalid or has expired. Request a new link.",
            token_valid=False,
        ), 400

    password = request.form.get("password", "")
    confirmation = request.form.get("confirm_password", "")
    password_error = admin_password_error(password)
    if password_error:
        return render_template(
            "reset_password.html",
            error=password_error,
            token_valid=True,
            token=token,
        ), 400
    if password != confirmation:
        return render_template(
            "reset_password.html",
            error="The passwords do not match.",
            token_valid=True,
            token=token,
        ), 400

    now = utc_now_naive()
    consumed = db.session.execute(
        update(AdminPasswordResetToken)
        .where(
            AdminPasswordResetToken.id == reset_token.id,
            AdminPasswordResetToken.used_at.is_(None),
            AdminPasswordResetToken.expires_at > now,
        )
        .values(used_at=now)
    )
    if consumed.rowcount != 1:
        db.session.rollback()
        return render_template(
            "reset_password.html",
            error="This password reset link is invalid or has expired. Request a new link.",
            token_valid=False,
        ), 400

    admin.set_password(password)
    db.session.execute(
        update(AdminPasswordResetToken)
        .where(
            AdminPasswordResetToken.admin_id == admin.id,
            AdminPasswordResetToken.id != reset_token.id,
            AdminPasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    db.session.commit()
    return redirect(url_for("admin_login", reset="complete"))


@app.route("/admin/logout", methods=["POST"], endpoint="admin_logout")
@app.route("/logout", methods=["POST"], endpoint="logout")
def admin_logout():
    logout_user()
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin/dashboard")
@admin_page_required
def admin_dashboard():
    return home()


@app.route("/admin/profile")
@admin_required
def admin_profile():
    response = jsonify(
        {
            "id": current_user.id,
            "full_name": current_user.full_name,
            "email": current_user.email,
            "role": current_user.role,
            "is_active": current_user.is_active,
            "created_at": current_user.created_at.isoformat(),
            "updated_at": current_user.updated_at.isoformat(),
            "last_login": (
                current_user.last_login.isoformat()
                if current_user.last_login
                else None
            ),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/admin/accounts", methods=["GET"])
@admin_management_required
def api_admin_accounts():
    admins = Admin.query.order_by(Admin.created_at, Admin.id).all()
    audit_logs = (
        AdminAuditLog.query.order_by(AdminAuditLog.created_at.desc())
        .limit(100)
        .all()
    )
    response = jsonify(
        {
            "admins": [admin_account_data(admin) for admin in admins],
            "audit_logs": [
                {
                    "id": entry.id,
                    "actor": entry.actor.full_name,
                    "target": entry.target.full_name if entry.target else None,
                    "action": entry.action,
                    "details": json.loads(entry.details),
                    "created_at": entry.created_at.isoformat(),
                }
                for entry in audit_logs
            ],
            "roles": list(ADMIN_ROLES),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/admin/accounts", methods=["POST"])
@admin_management_required
def api_admin_create_account():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A JSON administrator object is required."}), 400
    try:
        full_name = payload.get("full_name")
        if not isinstance(full_name, str) or not full_name.strip() or len(full_name.strip()) > 150:
            raise ValueError("Full name must be between 1 and 150 characters.")
        email = validate_admin_email(payload.get("email"))
        password = payload.get("password")
        password_error = admin_password_error(password)
        if password_error:
            raise ValueError(password_error)
        role = payload.get("role", ADMIN_ROLE)
        if role not in ADMIN_ROLES:
            raise ValueError("Choose a supported administrator role.")
        if Admin.query.filter_by(email=email).first():
            return jsonify({"error": "An administrator with that email already exists."}), 409

        admin = Admin(
            full_name=full_name.strip(),
            email=email,
            role=role,
            is_active=True,
        )
        admin.set_password(password)
        db.session.add(admin)
        db.session.flush()
        record_admin_action(
            "account_created",
            admin,
            {"role": role},
        )
        db.session.commit()
        return jsonify(
            {
                "message": "Administrator account created.",
                "admin": admin_account_data(admin),
            }
        ), 201
    except (ValueError, click.ClickException) as error:
        db.session.rollback()
        message = error.description if isinstance(error, click.ClickException) else str(error)
        return jsonify({"error": message}), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "An administrator with that email already exists."}), 409


@app.route("/api/admin/accounts/<int:admin_id>", methods=["PUT"])
@admin_management_required
def api_admin_update_account(admin_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A JSON administrator object is required."}), 400
    admin = db.session.get(Admin, admin_id)
    if admin is None:
        return jsonify({"error": "Administrator account was not found."}), 404
    changed_fields = []
    try:
        if "full_name" in payload:
            full_name = payload["full_name"]
            if not isinstance(full_name, str) or not full_name.strip() or len(full_name.strip()) > 150:
                raise ValueError("Full name must be between 1 and 150 characters.")
            if admin.full_name != full_name.strip():
                admin.full_name = full_name.strip()
                changed_fields.append("full_name")
        if "email" in payload:
            email = validate_admin_email(payload["email"])
            existing = Admin.query.filter(
                Admin.email == email,
                Admin.id != admin.id,
            ).first()
            if existing:
                return jsonify({"error": "An administrator with that email already exists."}), 409
            if admin.email != email:
                admin.email = email
                changed_fields.append("email")
        if not changed_fields:
            raise ValueError("Provide a changed full name or email address.")
        record_admin_action("account_updated", admin, {"fields": changed_fields})
        db.session.commit()
        return jsonify(
            {"message": "Administrator account updated.", "admin": admin_account_data(admin)}
        )
    except (ValueError, click.ClickException) as error:
        db.session.rollback()
        message = error.description if isinstance(error, click.ClickException) else str(error)
        return jsonify({"error": message}), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "An administrator with that email already exists."}), 409


@app.route("/api/admin/accounts/<int:admin_id>/role", methods=["PATCH"])
@admin_management_required
def api_admin_set_account_role(admin_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get("role") not in ADMIN_ROLES:
        return jsonify({"error": "Choose a supported administrator role."}), 400
    admin = db.session.get(Admin, admin_id)
    if admin is None:
        return jsonify({"error": "Administrator account was not found."}), 404
    role = payload["role"]
    if (
        admin.role == ADMIN_MANAGER_ROLE
        and role != ADMIN_MANAGER_ROLE
        and admin.is_active
        and Admin.query.filter_by(
            role=ADMIN_MANAGER_ROLE,
            is_active=True,
        ).count() <= 1
    ):
        return jsonify(
            {"error": "The last active administrator manager cannot be demoted."}
        ), 409
    if admin.role != role:
        previous_role = admin.role
        admin.role = role
        record_admin_action(
            "role_changed",
            admin,
            {"from": previous_role, "to": role},
        )
        db.session.commit()
    return jsonify(
        {"message": "Administrator role updated.", "admin": admin_account_data(admin)}
    )


@app.route("/api/admin/accounts/<int:admin_id>/status", methods=["PATCH"])
@admin_management_required
def api_admin_set_account_status(admin_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("is_active"), bool):
        return jsonify({"error": "A boolean is_active value is required."}), 400
    admin = db.session.get(Admin, admin_id)
    if admin is None:
        return jsonify({"error": "Administrator account was not found."}), 404
    is_active = payload["is_active"]
    if (
        admin.is_active
        and not is_active
        and admin.role == ADMIN_MANAGER_ROLE
        and Admin.query.filter_by(
            role=ADMIN_MANAGER_ROLE,
            is_active=True,
        ).count() <= 1
    ):
        return jsonify(
            {"error": "The last active administrator manager cannot be deactivated."}
        ), 409
    if admin.is_active != is_active:
        admin.is_active = is_active
        record_admin_action(
            "account_activated" if is_active else "account_deactivated",
            admin,
        )
        db.session.commit()
    return jsonify(
        {"message": "Administrator status updated.", "admin": admin_account_data(admin)}
    )


@app.route("/api/admin/accounts/<int:admin_id>/password", methods=["POST"])
@admin_management_required
def api_admin_reset_account_password(admin_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A JSON password object is required."}), 400
    password_error = admin_password_error(payload.get("password"))
    if password_error:
        return jsonify({"error": password_error}), 400
    admin = db.session.get(Admin, admin_id)
    if admin is None:
        return jsonify({"error": "Administrator account was not found."}), 404
    admin.set_password(payload["password"])
    record_admin_action("password_reset", admin)
    db.session.commit()
    return jsonify({"message": "Administrator password reset successfully."})


@app.route("/")
def home():
    refresh_sheet_if_due()
    try:
        venues = get_venues()
        error = None
    except sqlite3.Error:
        venues = []
        error = "Could not load venues from the local database."

    return render_template(
        "index.html",
        venues=venues,
        error=error,
        current_year=datetime.now(timezone.utc).year,
        current_user=(
            current_user.full_name if current_user.is_authenticated else None
        ),
        is_admin=is_admin(),
        can_manage_admins=(
            is_admin() and current_user.can_manage_admins
        ),
        sync_warning=_sheet_sync_error,
        status_options=VENUE_STATUSES,
        status_filter_options=get_status_filter_options(),
    )


@app.route("/api/venues")
def api_venues():
    refresh_sheet_if_due()
    try:
        response = jsonify(
            {
                "venues": get_venues(),
                "sync_warning": _sheet_sync_error,
                "status_options": VENUE_STATUSES,
                "status_filter_options": get_status_filter_options(),
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    except sqlite3.Error as error:
        app.logger.exception("Could not read venues from SQLite: %s", error)
        return jsonify({"error": "Could not load venues from the database"}), 500


def _google_api_session():
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    credentials_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not service_account_json and not credentials_file:
        raise SheetsIntegrationError(
            "Google Sheets write access is not configured on the server."
        )

    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        if service_account_json:
            credentials_info = json.loads(service_account_json)
            credentials = service_account.Credentials.from_service_account_info(
                credentials_info, scopes=[GOOGLE_SHEETS_SCOPE]
            )
        else:
            credentials = service_account.Credentials.from_service_account_file(
                credentials_file, scopes=[GOOGLE_SHEETS_SCOPE]
            )
        return AuthorizedSession(credentials)
    except (ImportError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SheetsIntegrationError(
            "Google Sheets service-account credentials are invalid or unavailable."
        ) from error


def google_sheets_request(method, path, params=None, body=None):
    spreadsheet_id = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        raise SheetsIntegrationError(
            "Google Sheets write access is not configured on the server."
        )

    url = f"{GOOGLE_SHEETS_API_URL}/{path.format(spreadsheet_id=quote(spreadsheet_id, safe=''))}"
    try:
        from google.auth.exceptions import GoogleAuthError

        response = _google_api_session().request(
            method,
            url,
            params=params,
            json=body,
            timeout=20,
        )
    except (GoogleAuthError, requests.RequestException, OSError) as error:
        raise SheetsIntegrationError("Google Sheets API request failed.") from error

    if not response.ok:
        app.logger.error("Google Sheets API returned HTTP %s.", response.status_code)
        raise SheetsIntegrationError(
            f"Google Sheets API rejected the request (HTTP {response.status_code})."
        )
    if response.status_code == 204 or not response.content:
        return {}
    try:
        return response.json()
    except ValueError as error:
        raise SheetsIntegrationError(
            "Google Sheets API returned an invalid response."
        ) from error


def google_sheet_table():
    metadata = google_sheets_request(
        "GET",
        "spreadsheets/{spreadsheet_id}",
        params={"fields": "sheets(properties(sheetId,title))"},
    )
    try:
        target_gid = int(os.environ.get("GOOGLE_SHEETS_GID", "0"))
        sheets = metadata["sheets"]
        sheet = next(
            item["properties"]
            for item in sheets
            if int(item["properties"]["sheetId"]) == target_gid
        )
        sheet_id = int(sheet["sheetId"])
        title = sheet["title"]
    except (KeyError, TypeError, ValueError, StopIteration) as error:
        raise SheetsIntegrationError(
            "The configured worksheet tab was not found in the Google Sheet."
        ) from error

    range_name = f"'{title.replace(chr(39), chr(39) * 2)}'!A:ZZ"
    values_response = google_sheets_request(
        "GET",
        "spreadsheets/{spreadsheet_id}/values/"
        + quote(range_name, safe="!:$'"),
        params={"valueRenderOption": "FORMATTED_VALUE"},
    )
    values = values_response.get("values", [])
    if not values:
        raise SheetsIntegrationError("The configured Google worksheet is empty.")

    headers = [
        str(value).strip().lstrip("\ufeff").casefold()
        for value in values[0]
    ]
    missing_columns = set(VENUE_COLUMNS) - set(headers)
    if missing_columns:
        raise SheetsIntegrationError(
            "The Google worksheet is missing required venue columns."
        )

    rows = []
    identity_counts = {}
    for row_number, raw_values in enumerate(values[1:], start=2):
        row = {
            column: str(raw_values[index]).strip()
            if index < len(raw_values) and raw_values[index] is not None
            else ""
            for column, index in (
                (column, headers.index(column)) for column in VENUE_COLUMNS
            )
        }
        if not row["venue_name"]:
            continue
        try:
            row["status"] = normalize_venue_status(
                row["status"], allow_aliases=True
            )
        except ValueError:
            row["status"] = LEGACY_STATUS

        identity = normalize_venue_identity(row["venue_name"], row["location"])
        occurrence = identity_counts.get(identity, 0)
        identity_counts[identity] = occurrence + 1
        rows.append(
            {
                "venue": row,
                "source_key": source_key_for_row(row, occurrence),
                "sheet_row": row_number,
                "raw_values": raw_values,
            }
        )
    return {
        "sheet_id": sheet_id,
        "title": title,
        "headers": headers,
        "rows": rows,
    }


def sheet_row_values(table, venue, existing_values=None):
    values = list(existing_values or [])
    if len(values) < len(table["headers"]):
        values.extend([""] * (len(table["headers"]) - len(values)))
    for column in VENUE_COLUMNS:
        values[table["headers"].index(column)] = venue[column]
    return values


def column_name(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def write_google_sheet_row(table, row_number, venue, existing_values=None):
    values = sheet_row_values(table, venue, existing_values)
    last_column = column_name(len(table["headers"]))
    range_name = (
        f"'{table['title'].replace(chr(39), chr(39) * 2)}'!"
        f"A{row_number}:{last_column}{row_number}"
    )
    google_sheets_request(
        "PUT",
        "spreadsheets/{spreadsheet_id}/values/"
        + quote(range_name, safe="!:$'"),
        params={"valueInputOption": "USER_ENTERED"},
        body={"values": [values]},
    )


def sync_sheet_table_to_cache():
    global _sheet_last_attempt, _sheet_sync_error

    try:
        table = google_sheet_table()
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=VENUE_COLUMNS)
        writer.writeheader()
        for row in table["rows"]:
            writer.writerow(row["venue"])
        with _sheet_sync_lock:
            import_venues(output.getvalue())
            _sheet_last_attempt = time.monotonic()
            _sheet_sync_error = None
    except (SheetsIntegrationError, sqlite3.Error) as error:
        raise SheetsIntegrationError(
            "The Google Sheet was changed, but the venue data cache could not be refreshed."
        ) from error


def validated_venue_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("A JSON venue object is required.")

    name = payload.get("venue_name")
    location = payload.get("location", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Venue name is required.")
    if len(name.strip()) > 200:
        raise ValueError("Venue name must be 200 characters or fewer.")
    if not isinstance(location, str) or len(location.strip()) > 300:
        raise ValueError("Location must be 300 characters or fewer.")

    status = normalize_venue_status(
        payload.get("status"), allow_legacy=False, allow_aliases=False
    )
    latitude = payload.get("latitude", "")
    longitude = payload.get("longitude", "")
    if (latitude in (None, "")) != (longitude in (None, "")):
        raise ValueError("Enter both latitude and longitude, or leave both blank.")
    if latitude in (None, ""):
        latitude_text = longitude_text = ""
    else:
        try:
            latitude_number = float(latitude)
            longitude_number = float(longitude)
        except (TypeError, ValueError) as error:
            raise ValueError("Coordinates must be valid numbers.") from error
        if (
            not math.isfinite(latitude_number)
            or not math.isfinite(longitude_number)
            or abs(latitude_number) > 90
            or abs(longitude_number) > 180
        ):
            raise ValueError("Coordinates are outside the valid latitude/longitude range.")
        latitude_text = format(latitude_number, ".8f").rstrip("0").rstrip(".")
        longitude_text = format(longitude_number, ".8f").rstrip("0").rstrip(".")

    return {
        "venue_name": name.strip(),
        "location": location.strip(),
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "latitude": latitude_text,
        "longitude": longitude_text,
    }


def get_venue_source_key(venue_id):
    with database_connection() as connection:
        row = connection.execute(
            "SELECT source_key FROM venues WHERE id = ? AND source_key IS NOT NULL",
            (venue_id,),
        ).fetchone()
    return row[0] if row else None


def admin_write_error(error):
    app.logger.error("Google Sheets administrator operation failed: %s", error)
    return jsonify({"error": str(error)}), 503


@app.route("/api/admin/venues", methods=["POST"])
@admin_required
def api_admin_create_venue():
    try:
        venue = validated_venue_payload(request.get_json(silent=True))
        table = google_sheet_table()
        values = sheet_row_values(table, venue)
        range_name = f"'{table['title'].replace(chr(39), chr(39) * 2)}'!A:ZZ"
        google_sheets_request(
            "POST",
            "spreadsheets/{spreadsheet_id}/values/"
            + quote(range_name, safe="!:$'")
            + ":append",
            params={
                "valueInputOption": "USER_ENTERED",
                "insertDataOption": "INSERT_ROWS",
            },
            body={"values": [values]},
        )
        sync_sheet_table_to_cache()
        return jsonify({"message": "Venue added to Google Sheets."}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except (SheetsIntegrationError, sqlite3.Error) as error:
        return admin_write_error(error)


@app.route("/api/admin/venues/<int:venue_id>", methods=["PUT"])
@admin_required
def api_admin_update_venue(venue_id):
    try:
        venue = validated_venue_payload(request.get_json(silent=True))
        source_key = get_venue_source_key(venue_id)
        if source_key is None:
            return jsonify({"error": "Venue was not found in Google Sheets."}), 404
        table = google_sheet_table()
        sheet_row = next(
            (row for row in table["rows"] if row["source_key"] == source_key),
            None,
        )
        if sheet_row is None:
            return jsonify({"error": "Venue was not found in Google Sheets."}), 404
        write_google_sheet_row(
            table,
            sheet_row["sheet_row"],
            venue,
            sheet_row["raw_values"],
        )
        sync_sheet_table_to_cache()
        return jsonify({"message": "Venue updated in Google Sheets."})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except (SheetsIntegrationError, sqlite3.Error) as error:
        return admin_write_error(error)


@app.route("/api/admin/venues/<int:venue_id>/status", methods=["PATCH"])
@admin_required
def api_admin_update_venue_status(venue_id):
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("A JSON status object is required.")
        status = normalize_venue_status(
            payload.get("status"),
            allow_legacy=False,
            allow_aliases=False,
        )
        source_key = get_venue_source_key(venue_id)
        if source_key is None:
            return jsonify({"error": "Venue was not found in Google Sheets."}), 404
        table = google_sheet_table()
        sheet_row = next(
            (row for row in table["rows"] if row["source_key"] == source_key),
            None,
        )
        if sheet_row is None:
            return jsonify({"error": "Venue was not found in Google Sheets."}), 404
        venue = dict(sheet_row["venue"], status=status)
        venue["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_google_sheet_row(
            table,
            sheet_row["sheet_row"],
            venue,
            sheet_row["raw_values"],
        )
        sync_sheet_table_to_cache()
        return jsonify({"message": "Venue status updated in Google Sheets."})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except (SheetsIntegrationError, sqlite3.Error) as error:
        return admin_write_error(error)


@app.route("/api/admin/venues/<int:venue_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_venue(venue_id):
    try:
        source_key = get_venue_source_key(venue_id)
        if source_key is None:
            return jsonify({"error": "Venue was not found in Google Sheets."}), 404
        table = google_sheet_table()
        sheet_row = next(
            (row for row in table["rows"] if row["source_key"] == source_key),
            None,
        )
        if sheet_row is None:
            return jsonify({"error": "Venue was not found in Google Sheets."}), 404
        google_sheets_request(
            "POST",
            "spreadsheets/{spreadsheet_id}:batchUpdate",
            body={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": table["sheet_id"],
                                "dimension": "ROWS",
                                "startIndex": sheet_row["sheet_row"] - 1,
                                "endIndex": sheet_row["sheet_row"],
                            }
                        }
                    }
                ]
            },
        )
        sync_sheet_table_to_cache()
        return jsonify({"message": "Venue deleted from Google Sheets."})
    except (SheetsIntegrationError, sqlite3.Error) as error:
        return admin_write_error(error)


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "").lower() == "true")