import csv
import hmac
import io
import math
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_HTTPS", "").lower() == "true",
)
DATABASE_PATH = Path(app.root_path) / "venues.db"

# Your published Google Sheet
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vROiTq-W_OdNM2ZoWur6yJFHOOYaMBxMxis4cybVBH6YPeNdpRzjpe2GGjHxk_ziAOTs4iGF7k3Ajpz/pub?gid=0&single=true&output=csv"
VENUE_COLUMNS = (
    "venue_name",
    "location",
    "status",
    "updated_at",
    "latitude",
    "longitude",
)

VALID_USERS = {
    "password": "admin123",
}
ADMIN_USERS = frozenset({"password"})
VENUE_TEXT_LIMITS = {"venue_name": 200, "location": 300}


def current_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def init_db():
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS venues (
                id INTEGER PRIMARY KEY,
                venue_name TEXT NOT NULL,
                location TEXT,
                status TEXT,
                updated_at TEXT,
                latitude TEXT,
                longitude TEXT
            )
            """
        )


init_db()


def import_venues(csv_text):
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise ValueError("CSV file is missing its header row.")

    missing_columns = set(VENUE_COLUMNS) - set(reader.fieldnames)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"CSV file is missing required columns: {missing}")

    rows = [
        tuple((row.get(column) or "").strip() for column in VENUE_COLUMNS)
        for row in reader
    ]

    placeholders = ", ".join("?" for _ in VENUE_COLUMNS)
    columns = ", ".join(VENUE_COLUMNS)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("DELETE FROM venues")
        connection.executemany(
            f"INSERT INTO venues ({columns}) VALUES ({placeholders})",
            rows,
        )
    return len(rows)


def get_venues():
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT id, {', '.join(VENUE_COLUMNS)} FROM venues ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def get_venue(venue_id):
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            f"SELECT id, {', '.join(VENUE_COLUMNS)} FROM venues WHERE id = ?",
            (venue_id,),
        ).fetchone()
    return dict(row) if row else None


def validate_venue_payload(payload, partial=False):
    if not isinstance(payload, dict):
        raise ValueError("Send venue details as a JSON object.")

    if partial:
        status = payload.get("status")
        if not isinstance(status, str) or status.strip().lower() not in {
            "free",
            "occupied",
            "unknown",
        }:
            raise ValueError("Status must be Free, Occupied, or Unknown.")
        return {"status": status.strip().title()}

    fields = {}
    for column, maximum_length in VENUE_TEXT_LIMITS.items():
        value = payload.get(column)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{column.replace('_', ' ').title()} is required.")
        value = value.strip()
        if len(value) > maximum_length:
            raise ValueError(
                f"{column.replace('_', ' ').title()} must be {maximum_length} characters or fewer."
            )
        fields[column] = value

    status = payload.get("status", "Unknown")
    if not isinstance(status, str) or status.strip().lower() not in {
        "free",
        "occupied",
        "unknown",
    }:
        raise ValueError("Status must be Free, Occupied, or Unknown.")
    fields["status"] = status.strip().title()
    fields["updated_at"] = current_timestamp()

    for coordinate, limit in (("latitude", 90), ("longitude", 180)):
        value = payload.get(coordinate, "")
        if value in (None, ""):
            fields[coordinate] = ""
            continue
        if isinstance(value, bool):
            raise ValueError(f"{coordinate.title()} must be a number.")
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{coordinate.title()} must be a number.") from error
        if not math.isfinite(number) or abs(number) > limit:
            raise ValueError(
                f"{coordinate.title()} must be between {-limit} and {limit}."
            )
        fields[coordinate] = str(number)

    return fields


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
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return None

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
    """Download the published CSV and replace the local SQLite venue data."""
    try:
        response = requests.get(SHEET_CSV_URL, timeout=15)
        response.raise_for_status()
        imported_count = import_venues(response.text)
    except (requests.RequestException, ValueError, sqlite3.Error) as error:
        raise RuntimeError(f"Could not import venue CSV: {error}") from error

    print(f"Imported {imported_count} venues into {DATABASE_PATH}.")


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if session.get("user") not in ADMIN_USERS:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Administrator access is required."}), 403
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if VALID_USERS.get(username) == password:
            session.clear()
            session["user"] = username
            csrf_token()
            return redirect(url_for("home"))

        error = "Invalid username or password."

    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def home():
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
        current_user=session.get("user"),
        is_admin=session.get("user") in ADMIN_USERS,
    )


@app.route("/api/venues")
@login_required
def api_venues():
    try:
        return jsonify({"venues": get_venues()})
    except sqlite3.Error as error:
        app.logger.exception("Could not read venues from SQLite: %s", error)
        return jsonify({"error": "Could not load venues from the database"}), 500


@app.route("/api/venues", methods=["POST"])
@admin_required
def create_venue():
    try:
        venue = validate_venue_payload(request.get_json(silent=True))
        columns = ", ".join(VENUE_COLUMNS)
        placeholders = ", ".join("?" for _ in VENUE_COLUMNS)
        with sqlite3.connect(DATABASE_PATH) as connection:
            cursor = connection.execute(
                f"INSERT INTO venues ({columns}) VALUES ({placeholders})",
                tuple(venue[column] for column in VENUE_COLUMNS),
            )
            venue_id = cursor.lastrowid
        return jsonify({"venue": get_venue(venue_id)}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except sqlite3.Error:
        app.logger.exception("Could not create venue")
        return jsonify({"error": "Could not save the venue."}), 500


@app.route("/api/venues/<int:venue_id>", methods=["PUT", "DELETE"])
@admin_required
def update_or_delete_venue(venue_id):
    if request.method == "DELETE":
        try:
            with sqlite3.connect(DATABASE_PATH) as connection:
                cursor = connection.execute(
                    "DELETE FROM venues WHERE id = ?", (venue_id,)
                )
            if cursor.rowcount == 0:
                return jsonify({"error": "Venue not found."}), 404
            return jsonify({"success": True})
        except sqlite3.Error:
            app.logger.exception("Could not delete venue %s", venue_id)
            return jsonify({"error": "Could not delete the venue."}), 500

    try:
        venue = validate_venue_payload(request.get_json(silent=True))
        assignments = ", ".join(f"{column} = ?" for column in VENUE_COLUMNS)
        with sqlite3.connect(DATABASE_PATH) as connection:
            cursor = connection.execute(
                f"UPDATE venues SET {assignments} WHERE id = ?",
                tuple(venue[column] for column in VENUE_COLUMNS) + (venue_id,),
            )
        if cursor.rowcount == 0:
            return jsonify({"error": "Venue not found."}), 404
        return jsonify({"venue": get_venue(venue_id)})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except sqlite3.Error:
        app.logger.exception("Could not update venue %s", venue_id)
        return jsonify({"error": "Could not update the venue."}), 500


@app.route("/api/venues/<int:venue_id>/availability", methods=["PATCH"])
@admin_required
def update_venue_availability(venue_id):
    try:
        fields = validate_venue_payload(request.get_json(silent=True), partial=True)
        updated_at = current_timestamp()
        with sqlite3.connect(DATABASE_PATH) as connection:
            cursor = connection.execute(
                "UPDATE venues SET status = ?, updated_at = ? WHERE id = ?",
                (fields["status"], updated_at, venue_id),
            )
        if cursor.rowcount == 0:
            return jsonify({"error": "Venue not found."}), 404
        return jsonify({"venue": get_venue(venue_id)})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except sqlite3.Error:
        app.logger.exception("Could not update availability for venue %s", venue_id)
        return jsonify({"error": "Could not update venue availability."}), 500


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "").lower() == "true")