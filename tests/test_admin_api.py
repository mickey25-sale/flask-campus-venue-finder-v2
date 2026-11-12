import csv
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_test_database_directory = tempfile.TemporaryDirectory()
_test_database_path = Path(_test_database_directory.name) / "test-venues.db"
_original_database_path = os.environ.get("VENUE_DATABASE_PATH")
os.environ["VENUE_DATABASE_PATH"] = str(_test_database_path)
try:
    import app as venue_app
finally:
    if _original_database_path is None:
        os.environ.pop("VENUE_DATABASE_PATH", None)
    else:
        os.environ["VENUE_DATABASE_PATH"] = _original_database_path


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        venue_app.limiter.reset()
        self.environment_patch = patch.dict(
            os.environ,
            {
                "GOOGLE_SHEETS_SPREADSHEET_ID": "",
                "GOOGLE_SERVICE_ACCOUNT_JSON": "",
                "GOOGLE_APPLICATION_CREDENTIALS": "",
            },
        )
        self.environment_patch.start()
        venue_app.app.config.update(TESTING=True, SECRET_KEY="test-session-secret")
        venue_app.init_db()
        with venue_app.app.app_context():
            venue_app.db.drop_all()
            venue_app.db.create_all()
            self.admin = venue_app.Admin(
                full_name="Campus Administrator",
                email="campus-admin@example.edu",
                role=venue_app.ADMIN_MANAGER_ROLE,
                is_active=True,
            )
            self.admin.set_password("TestingAdminPass-2026!")
            venue_app.db.session.add(self.admin)
            venue_app.db.session.commit()
            self.admin_id = self.admin.id
        venue_app._sheet_last_attempt = None
        venue_app._sheet_sync_error = None
        self.client = venue_app.app.test_client()
        self.venue = {
            "venue_name": "Library",
            "location": "Main campus",
            "status": "AVAILABLE",
            "updated_at": "2026-01-01T10:00:00+00:00",
            "latitude": "0.5",
            "longitude": "34.1",
        }
        source = io.StringIO()
        writer = csv.DictWriter(source, fieldnames=venue_app.VENUE_COLUMNS)
        writer.writeheader()
        writer.writerow(self.venue)
        venue_app.import_venues(source.getvalue())
        self.venue_id = venue_app.get_venues()[0]["id"]

    def tearDown(self):
        self.environment_patch.stop()

    @classmethod
    def tearDownClass(cls):
        with venue_app.app.app_context():
            venue_app.db.session.remove()
            for engine in venue_app.db.engines.values():
                engine.dispose()
        _test_database_directory.cleanup()

    def authenticate_admin(self):
        login_page = self.client.get("/admin/login")
        self.assertEqual(login_page.status_code, 200)
        with self.client.session_transaction() as user_session:
            csrf = user_session["_csrf_token"]
        response = self.client.post(
            "/admin/login",
            data={
                "csrf_token": csrf,
                "email": "campus-admin@example.edu",
                "password": "TestingAdminPass-2026!",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as user_session:
            self.csrf_token = user_session["_csrf_token"]

    def admin_headers(self):
        return {"X-CSRF-Token": self.csrf_token}

    @staticmethod
    def run_thread_immediately(target, args, daemon):
        class ImmediateThread:
            def start(self):
                target(*args)

        return ImmediateThread()

    def sheet_table(self):
        return {
            "sheet_id": 0,
            "title": "Venues",
            "headers": list(venue_app.VENUE_COLUMNS),
            "rows": [
                {
                    "venue": dict(self.venue),
                    "source_key": venue_app.source_key_for_row(self.venue, 0),
                    "sheet_row": 2,
                    "raw_values": list(self.venue.values()),
                }
            ],
        }

    def test_public_page_and_venue_api_do_not_require_login(self):
        class CsvResponse:
            text = (
                "venue_name,location,status,updated_at,latitude,longitude\n"
                "Library,Main campus,AVAILABLE,2026-01-01T10:00:00+00:00,0.5,34.1\n"
            )

            @staticmethod
            def raise_for_status():
                return None

        with patch.object(venue_app.requests, "get", return_value=CsvResponse()):
            page = self.client.get("/")
            response = self.client.get("/api/venues")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Admin sign in", page.data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["venues"][0]["venue_name"], "Library")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_admin_api_rejects_anonymous_mutations(self):
        requests = (
            ("POST", "/api/admin/venues", {}),
            ("PUT", "/api/admin/venues/1", {}),
            ("PATCH", "/api/admin/venues/1/status", {"status": "OCCUPIED"}),
            ("DELETE", "/api/admin/venues/1", None),
        )
        with (
            patch.object(venue_app, "google_sheet_table") as get_table,
            patch.object(venue_app, "google_sheets_request") as sheet_request,
            patch.object(venue_app, "write_google_sheet_row") as write_row,
        ):
            for method, path, payload in requests:
                response = self.client.open(path, method=method, json=payload)
                self.assertEqual(response.status_code, 401, (method, path))
                self.assertEqual(
                    response.get_json()["error"],
                    "Administrator authentication is required.",
                )
        get_table.assert_not_called()
        sheet_request.assert_not_called()
        write_row.assert_not_called()

    def test_admin_management_api_rejects_anonymous_and_non_manager(self):
        manager_endpoints = (
            ("GET", "/api/admin/accounts", None),
            ("POST", "/api/admin/accounts", {}),
            ("PUT", "/api/admin/accounts/1", {}),
            ("PATCH", "/api/admin/accounts/1/role", {"role": "admin"}),
            ("PATCH", "/api/admin/accounts/1/status", {"is_active": False}),
            ("POST", "/api/admin/accounts/1/password", {"password": "NewSecurePass-2026!"}),
        )
        for method, path, payload in manager_endpoints:
            response = self.client.open(path, method=method, json=payload)
            self.assertEqual(response.status_code, 401, (method, path))

        with venue_app.app.app_context():
            saved_admin = venue_app.db.session.get(venue_app.Admin, self.admin_id)
            saved_admin.role = venue_app.ADMIN_ROLE
            venue_app.db.session.commit()
        self.authenticate_admin()
        for method, path, payload in manager_endpoints:
            response = self.client.open(
                path,
                method=method,
                json=payload,
                headers=self.admin_headers() if method != "GET" else None,
            )
            self.assertEqual(response.status_code, 403, (method, path))

    def test_dashboard_management_controls_are_manager_only(self):
        self.authenticate_admin()
        manager_page = self.client.get("/admin/dashboard")
        self.assertEqual(manager_page.status_code, 200)
        self.assertIn(b'id="createAdminForm"', manager_page.data)
        self.assertIn(b"js/admin-management.js", manager_page.data)

        with venue_app.app.app_context():
            saved_admin = venue_app.db.session.get(venue_app.Admin, self.admin_id)
            saved_admin.role = venue_app.ADMIN_ROLE
            venue_app.db.session.commit()
        self.client = venue_app.app.test_client()
        self.authenticate_admin()
        regular_admin_page = self.client.get("/admin/dashboard")
        self.assertEqual(regular_admin_page.status_code, 200)
        self.assertNotIn(b'id="createAdminForm"', regular_admin_page.data)
        self.assertNotIn(b"js/admin-management.js", regular_admin_page.data)

    def test_manager_can_create_edit_change_role_status_reset_and_audit(self):
        self.authenticate_admin()
        listing = self.client.get("/api/admin/accounts")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.get_json()["admins"]), 1)
        self.assertNotIn("password_hash", listing.get_json()["admins"][0])

        created = self.client.post(
            "/api/admin/accounts",
            json={
                "full_name": "New Administrator",
                "email": "new-admin@example.edu",
                "password": "InitialSecurePass-2026!",
                "role": venue_app.ADMIN_ROLE,
            },
            headers=self.admin_headers(),
        )
        self.assertEqual(created.status_code, 201)
        created_account = created.get_json()["admin"]
        admin_id = created_account["id"]
        self.assertNotIn("password_hash", created_account)
        self.assertNotIn("InitialSecurePass-2026!", created.get_data(as_text=True))

        updated = self.client.put(
            f"/api/admin/accounts/{admin_id}",
            json={
                "full_name": "Updated Administrator",
                "email": "updated-admin@example.edu",
            },
            headers=self.admin_headers(),
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()["admin"]["email"], "updated-admin@example.edu")

        role = self.client.patch(
            f"/api/admin/accounts/{admin_id}/role",
            json={"role": venue_app.ADMIN_MANAGER_ROLE},
            headers=self.admin_headers(),
        )
        self.assertEqual(role.status_code, 200)
        self.assertEqual(role.get_json()["admin"]["role"], venue_app.ADMIN_MANAGER_ROLE)

        status = self.client.patch(
            f"/api/admin/accounts/{admin_id}/status",
            json={"is_active": False},
            headers=self.admin_headers(),
        )
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.get_json()["admin"]["is_active"])

        reset = self.client.post(
            f"/api/admin/accounts/{admin_id}/password",
            json={"password": "ResetSecurePass-2026!"},
            headers=self.admin_headers(),
        )
        self.assertEqual(reset.status_code, 200)
        self.assertNotIn("ResetSecurePass-2026!", reset.get_data(as_text=True))
        with venue_app.app.app_context():
            saved_account = venue_app.db.session.get(venue_app.Admin, admin_id)
            self.assertTrue(saved_account.check_password("ResetSecurePass-2026!"))

        logs = self.client.get("/api/admin/accounts").get_json()["audit_logs"]
        self.assertEqual(
            {entry["action"] for entry in logs},
            {
                "account_created",
                "account_updated",
                "role_changed",
                "account_deactivated",
                "password_reset",
            },
        )
        log_text = str(logs)
        self.assertNotIn("InitialSecurePass-2026!", log_text)
        self.assertNotIn("ResetSecurePass-2026!", log_text)
        self.assertNotIn("password_hash", log_text)

    def test_last_active_manager_cannot_deactivate_or_demote_self(self):
        self.authenticate_admin()
        role = self.client.patch(
            f"/api/admin/accounts/{self.admin_id}/role",
            json={"role": venue_app.ADMIN_ROLE},
            headers=self.admin_headers(),
        )
        self.assertEqual(role.status_code, 409)
        status = self.client.patch(
            f"/api/admin/accounts/{self.admin_id}/status",
            json={"is_active": False},
            headers=self.admin_headers(),
        )
        self.assertEqual(status.status_code, 409)
        self.assertEqual(
            self.client.delete(
                f"/api/admin/accounts/{self.admin_id}",
                headers=self.admin_headers(),
            ).status_code,
            405,
        )

    def test_admin_login_rotates_session_and_updates_last_login(self):
        login_page = self.client.get("/admin/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn(b"Alupe University", login_page.data)
        self.assertIn(b"name=\"remember\"", login_page.data)
        self.assertIn(b"id=\"togglePassword\"", login_page.data)
        old_cookie = self.client.get_cookie("session")
        with self.client.session_transaction() as user_session:
            csrf_token = user_session["_csrf_token"]
        response = self.client.post(
            "/admin/login",
            data={
                "csrf_token": csrf_token,
                "email": "campus-admin@example.edu",
                "password": "TestingAdminPass-2026!",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/dashboard"))
        new_cookie = self.client.get_cookie("session")
        self.assertNotEqual(old_cookie.value, new_cookie.value)
        with venue_app.app.app_context():
            saved_admin = venue_app.db.session.get(venue_app.Admin, self.admin_id)
            self.assertIsNotNone(saved_admin.last_login)
            session_rows = venue_app.db.session.query(
                venue_app.ServerSession
            ).all()
            self.assertEqual(len(session_rows), 1)
            self.assertIn("_user_id", session_rows[0].data)
            self.assertNotIn(saved_admin.password_hash, session_rows[0].data)

    def test_remember_me_sets_securely_configured_remember_cookie(self):
        self.client.get("/admin/login")
        with self.client.session_transaction() as user_session:
            csrf = user_session["_csrf_token"]
        with patch.dict(venue_app.app.config, {"REMEMBER_COOKIE_SECURE": True}):
            response = self.client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf,
                    "email": "campus-admin@example.edu",
                    "password": "TestingAdminPass-2026!",
                    "remember": "on",
                },
            )
        self.assertEqual(response.status_code, 302)
        remember_cookie = next(
            header
            for header in response.headers.getlist("Set-Cookie")
            if header.startswith("remember_token=")
        )
        self.assertIn("Secure", remember_cookie)
        self.assertIn("HttpOnly", remember_cookie)
        self.assertIn("SameSite=Lax", remember_cookie)
        self.assertTrue(venue_app.app.config["REMEMBER_COOKIE_HTTPONLY"])
        self.assertEqual(venue_app.app.config["REMEMBER_COOKIE_SAMESITE"], "Lax")

    def test_admin_profile_and_dashboard_require_login_and_hide_secrets(self):
        self.assertEqual(self.client.get("/admin/profile").status_code, 401)
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 302)
        self.authenticate_admin()
        profile = self.client.get("/admin/profile")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(
            profile.get_json()["email"],
            "campus-admin@example.edu",
        )
        self.assertNotIn("password_hash", profile.get_json())
        self.assertNotIn("secret", profile.get_json())
        self.assertEqual(profile.headers["Cache-Control"], "no-store")
        with patch.object(venue_app, "refresh_sheet_if_due"):
            self.assertEqual(
                self.client.get("/admin/dashboard").status_code,
                200,
            )

    def test_expired_session_is_rejected(self):
        self.authenticate_admin()
        with venue_app.app.app_context():
            saved_session = venue_app.db.session.query(
                venue_app.ServerSession
            ).one()
            saved_session.expires_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).replace(tzinfo=None)
            venue_app.db.session.commit()
        self.assertEqual(self.client.get("/admin/profile").status_code, 401)

    def test_changed_admin_role_invalidates_existing_session(self):
        self.authenticate_admin()
        with venue_app.app.app_context():
            saved_admin = venue_app.db.session.get(venue_app.Admin, self.admin_id)
            saved_admin.role = "viewer"
            venue_app.db.session.commit()
        self.assertEqual(self.client.get("/admin/profile").status_code, 401)
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 302)

    def test_login_rejects_bad_credentials_with_generic_message(self):
        self.client.get("/admin/login")
        with self.client.session_transaction() as user_session:
            csrf = user_session["_csrf_token"]
        for email, password in (
            ("missing@example.edu", "WrongPassword-2026!"),
            ("campus-admin@example.edu", "WrongPassword-2026!"),
        ):
            response = self.client.post(
                "/admin/login",
                data={"csrf_token": csrf, "email": email, "password": password},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Invalid email or password.", response.data)
            self.assertNotIn(b"not found", response.data.lower())

    def test_login_and_logout_require_csrf(self):
        self.client.get("/admin/login")
        invalid_login = self.client.post(
            "/admin/login",
            data={
                "email": "campus-admin@example.edu",
                "password": "TestingAdminPass-2026!",
            },
        )
        self.assertEqual(invalid_login.status_code, 400)
        self.assertEqual(self.client.post("/admin/logout").status_code, 401)

        self.authenticate_admin()
        self.assertEqual(
            self.client.post("/admin/logout").status_code,
            400,
        )
        logout = self.client.post(
            "/admin/logout",
            headers={"X-CSRF-Token": self.csrf_token},
        )
        self.assertEqual(logout.status_code, 302)
        self.assertTrue(logout.headers["Location"].endswith("/admin/login"))
        self.assertEqual(self.client.get("/admin/profile").status_code, 401)

    def test_login_rate_limit_blocks_sixth_attempt(self):
        self.client.get("/admin/login")
        with self.client.session_transaction() as user_session:
            csrf = user_session["_csrf_token"]
        responses = [
            self.client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf,
                    "email": "missing@example.edu",
                    "password": "WrongPassword-2026!",
                },
            )
            for _ in range(6)
        ]
        self.assertTrue(all(response.status_code == 200 for response in responses[:5]))
        self.assertEqual(responses[5].status_code, 429)

    def test_administrators_have_independent_sessions(self):
        with venue_app.app.app_context():
            second_admin = venue_app.Admin(
                full_name="Second Administrator",
                email="second-admin@example.edu",
                role="admin",
                is_active=True,
            )
            second_admin.set_password("AnotherSecurePass-2026!")
            venue_app.db.session.add(second_admin)
            venue_app.db.session.commit()

        first_client = self.client
        second_client = venue_app.app.test_client()
        first_client.get("/admin/login")
        second_client.get("/admin/login")
        with first_client.session_transaction() as first_session:
            first_csrf = first_session["_csrf_token"]
        with second_client.session_transaction() as second_session:
            second_csrf = second_session["_csrf_token"]
        first_login = first_client.post(
            "/admin/login",
            data={
                "csrf_token": first_csrf,
                "email": "campus-admin@example.edu",
                "password": "TestingAdminPass-2026!",
            },
        )
        second_login = second_client.post(
            "/admin/login",
            data={
                "csrf_token": second_csrf,
                "email": "second-admin@example.edu",
                "password": "AnotherSecurePass-2026!",
            },
        )
        self.assertEqual(first_login.status_code, 302)
        self.assertEqual(second_login.status_code, 302)
        self.assertNotEqual(
            first_client.get_cookie("session").value,
            second_client.get_cookie("session").value,
        )
        self.assertEqual(
            first_client.get("/admin/profile").get_json()["email"],
            "campus-admin@example.edu",
        )
        self.assertEqual(
            second_client.get("/admin/profile").get_json()["email"],
            "second-admin@example.edu",
        )

    def test_cli_creates_and_deactivates_additional_admins(self):
        runner = venue_app.app.test_cli_runner()
        created = runner.invoke(
            args=["create-admin"],
            input=(
                "Second Administrator\n"
                "second-admin@example.edu\n"
                "AnotherSecurePass-2026!\n"
                "AnotherSecurePass-2026!\n"
            ),
        )
        self.assertEqual(created.exit_code, 0, created.output)
        self.assertNotIn("AnotherSecurePass-2026!", created.output)
        disabled = runner.invoke(
            args=[
                "set-admin-active",
                "second-admin@example.edu",
                "--inactive",
            ]
        )
        self.assertEqual(disabled.exit_code, 0, disabled.output)
        with venue_app.app.app_context():
            additional_admin = venue_app.Admin.query.filter_by(
                email="second-admin@example.edu"
            ).one()
            self.assertFalse(additional_admin.is_active)
            self.assertNotEqual(
                additional_admin.password_hash,
                "AnotherSecurePass-2026!",
            )
            self.assertTrue(
                additional_admin.check_password("AnotherSecurePass-2026!")
            )

    def test_create_admin_rejects_invalid_email_and_weak_password(self):
        runner = venue_app.app.test_cli_runner()
        invalid_email = runner.invoke(
            args=["create-admin"],
            input="Invalid Email Admin\nnot-an-email\n",
        )
        self.assertNotEqual(invalid_email.exit_code, 0)
        self.assertIn("valid administrator email", invalid_email.output)

        weak_password = runner.invoke(
            args=["create-admin"],
            input=(
                "Weak Password Admin\n"
                "weak-password@example.edu\n"
                "short\n"
                "short\n"
            ),
        )
        self.assertNotEqual(weak_password.exit_code, 0)
        self.assertIn("at least 12 characters", weak_password.output)
        self.assertNotIn("short", weak_password.output)
        with venue_app.app.app_context():
            self.assertIsNone(
                venue_app.Admin.query.filter_by(
                    email="weak-password@example.edu"
                ).first()
            )

    def test_create_admin_rejects_duplicate_email_and_password_mismatch(self):
        runner = venue_app.app.test_cli_runner()
        duplicate = runner.invoke(
            args=["create-admin"],
            input=(
                "Duplicate Administrator\n"
                "CAMPUS-ADMIN@EXAMPLE.EDU\n"
            ),
        )
        self.assertNotEqual(duplicate.exit_code, 0)
        self.assertIn("already exists", duplicate.output)

        mismatch = runner.invoke(
            args=["create-admin"],
            input=(
                "Mismatch Administrator\n"
                "mismatch@example.edu\n"
                "FirstSecurePass-2026!\n"
                "SecondSecurePass-2026!\n"
            ),
        )
        self.assertNotEqual(mismatch.exit_code, 0)
        self.assertNotIn("FirstSecurePass-2026!", mismatch.output)
        self.assertNotIn("SecondSecurePass-2026!", mismatch.output)
        with venue_app.app.app_context():
            self.assertIsNone(
                venue_app.Admin.query.filter_by(
                    email="mismatch@example.edu"
                ).first()
            )

    def test_admin_password_is_hashed_and_email_is_case_insensitive_unique(self):
        with venue_app.app.app_context():
            saved_admin = venue_app.db.session.get(venue_app.Admin, self.admin_id)
            self.assertNotEqual(saved_admin.password_hash, "TestingAdminPass-2026!")
            self.assertTrue(saved_admin.check_password("TestingAdminPass-2026!"))
            duplicate = venue_app.Admin(
                full_name="Duplicate Account",
                email="CAMPUS-ADMIN@EXAMPLE.EDU",
            )
            duplicate.set_password("AnotherSecurePass-2026!")
            venue_app.db.session.add(duplicate)
            with self.assertRaises(venue_app.IntegrityError):
                venue_app.db.session.commit()
            venue_app.db.session.rollback()
            self.assertEqual(saved_admin.email, "campus-admin@example.edu")

    def test_deactivated_admin_cannot_sign_in(self):
        with venue_app.app.app_context():
            saved_admin = venue_app.db.session.get(venue_app.Admin, self.admin_id)
            saved_admin.is_active = False
            venue_app.db.session.commit()
        self.client.get("/admin/login")
        with self.client.session_transaction() as user_session:
            csrf = user_session["_csrf_token"]
        response = self.client.post(
            "/admin/login",
            data={
                "csrf_token": csrf,
                "email": "campus-admin@example.edu",
                "password": "TestingAdminPass-2026!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid email or password.", response.data)
        self.assertEqual(self.client.get("/admin/profile").status_code, 401)

    def test_registration_endpoint_does_not_exist(self):
        self.assertEqual(self.client.get("/register").status_code, 404)

    def test_password_recovery_hides_account_existence_and_sends_hashed_token(self):
        page = self.client.get("/admin/forgot-password")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'name="csrf_token"', page.data)
        with self.client.session_transaction() as user_session:
            csrf = user_session["_csrf_token"]

        with (
            patch.object(venue_app, "send_password_reset_email") as send_email,
            patch.object(
                venue_app,
                "PasswordResetThread",
                side_effect=self.run_thread_immediately,
            ),
        ):
            known = self.client.post(
                "/admin/forgot-password",
                data={"csrf_token": csrf, "email": "CAMPUS-ADMIN@EXAMPLE.EDU"},
            )
            unknown = self.client.post(
                "/admin/forgot-password",
                data={"csrf_token": csrf, "email": "nobody@example.edu"},
            )

        self.assertEqual(known.status_code, 200)
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(known.data, unknown.data)
        self.assertIn(venue_app.PASSWORD_RESET_GENERIC_MESSAGE.encode(), known.data)
        self.assertEqual(send_email.call_count, 1)
        token = send_email.call_args.args[1]
        self.assertNotIn(token, known.get_data(as_text=True))
        with venue_app.app.app_context():
            saved_admin = venue_app.db.session.get(venue_app.Admin, self.admin_id)
            self.assertEqual(saved_admin.email, "campus-admin@example.edu")
            saved_token = venue_app.AdminPasswordResetToken.query.one()
            self.assertEqual(saved_token.admin_id, self.admin_id)
            self.assertNotEqual(saved_token.token_hash, token)
            self.assertEqual(
                saved_token.token_hash,
                venue_app.hashlib.sha256(token.encode()).hexdigest(),
            )
            self.assertGreater(
                saved_token.expires_at,
                datetime.now(timezone.utc).replace(tzinfo=None),
            )
            self.assertLessEqual(
                saved_token.expires_at,
                datetime.now(timezone.utc).replace(tzinfo=None)
                + venue_app.PASSWORD_RESET_TOKEN_LIFETIME,
            )
        self.assertEqual(known.headers["Cache-Control"], "no-store")
        self.assertEqual(known.headers["Referrer-Policy"], "no-referrer")

    def test_password_recovery_resets_once_and_invalidates_sessions(self):
        self.authenticate_admin()
        second_client = venue_app.app.test_client()
        login_page = second_client.get("/admin/login")
        self.assertEqual(login_page.status_code, 200)
        with second_client.session_transaction() as user_session:
            second_csrf = user_session["_csrf_token"]
        second_login = second_client.post(
            "/admin/login",
            data={
                "csrf_token": second_csrf,
                "email": "campus-admin@example.edu",
                "password": "TestingAdminPass-2026!",
            },
        )
        self.assertEqual(second_login.status_code, 302)

        with (
            patch.object(venue_app, "send_password_reset_email") as send_email,
            patch.object(
                venue_app,
                "PasswordResetThread",
                side_effect=self.run_thread_immediately,
            ),
        ):
            self.client.get("/admin/forgot-password")
            with self.client.session_transaction() as user_session:
                csrf = user_session["_csrf_token"]
            sent = self.client.post(
                "/admin/forgot-password",
                data={"csrf_token": csrf, "email": "campus-admin@example.edu"},
            )
        self.assertEqual(sent.status_code, 200)
        token = send_email.call_args.args[1]

        reset_page = self.client.get("/admin/reset-password")
        self.assertEqual(reset_page.status_code, 200)
        self.assertIn(b"complete password reset link", reset_page.data)
        reset = self.client.post(
            "/admin/reset-password",
            data={
                "csrf_token": csrf,
                "token": token,
                "password": "ChangedSecurePass-2026!",
                "confirm_password": "ChangedSecurePass-2026!",
            },
        )
        self.assertEqual(reset.status_code, 302)
        self.assertIn("reset=complete", reset.headers["Location"])
        self.assertEqual(self.client.get("/admin/profile").status_code, 401)
        self.assertEqual(second_client.get("/admin/profile").status_code, 401)
        login_response = self.client.get(reset.headers["Location"])
        self.assertIn(b"Your password has been reset", login_response.data)

        with venue_app.app.app_context():
            saved_admin = venue_app.db.session.get(venue_app.Admin, self.admin_id)
            saved_token = venue_app.AdminPasswordResetToken.query.one()
            self.assertTrue(saved_admin.check_password("ChangedSecurePass-2026!"))
            self.assertIsNotNone(saved_token.used_at)

        reused = self.client.post(
            "/admin/reset-password",
            data={
                "csrf_token": csrf,
                "token": token,
                "password": "AnotherSecurePass-2026!",
                "confirm_password": "AnotherSecurePass-2026!",
            },
        )
        self.assertEqual(reused.status_code, 400)
        self.assertIn(b"invalid or has expired", reused.data)
        with venue_app.app.app_context():
            saved_admin = venue_app.db.session.get(venue_app.Admin, self.admin_id)
            self.assertTrue(saved_admin.check_password("ChangedSecurePass-2026!"))

    def test_password_reset_rejects_weak_mismatched_and_expired_tokens(self):
        page = self.client.get("/admin/forgot-password")
        with self.client.session_transaction() as user_session:
            csrf = user_session["_csrf_token"]

        with (
            patch.object(venue_app, "send_password_reset_email") as send_email,
            patch.object(
                venue_app,
                "PasswordResetThread",
                side_effect=self.run_thread_immediately,
            ),
        ):
            self.client.post(
                "/admin/forgot-password",
                data={"csrf_token": csrf, "email": "campus-admin@example.edu"},
            )
        token = send_email.call_args.args[1]

        weak = self.client.post(
            "/admin/reset-password",
            data={
                "csrf_token": csrf,
                "token": token,
                "password": "weak",
                "confirm_password": "weak",
            },
        )
        self.assertEqual(weak.status_code, 400)
        self.assertIn(b"at least 12 characters", weak.data)
        mismatch = self.client.post(
            "/admin/reset-password",
            data={
                "csrf_token": csrf,
                "token": token,
                "password": "StrongPassword-2026!",
                "confirm_password": "DifferentPassword-2026!",
            },
        )
        self.assertEqual(mismatch.status_code, 400)
        self.assertIn(b"passwords do not match", mismatch.data)

        with venue_app.app.app_context():
            saved_token = venue_app.AdminPasswordResetToken.query.one()
            saved_token.expires_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).replace(tzinfo=None)
            venue_app.db.session.commit()
        expired = self.client.post(
            "/admin/reset-password",
            data={
                "csrf_token": csrf,
                "token": token,
                "password": "StrongPassword-2026!",
                "confirm_password": "StrongPassword-2026!",
            },
        )
        self.assertEqual(expired.status_code, 400)
        self.assertIn(b"invalid or has expired", expired.data)
        with venue_app.app.app_context():
            saved_admin = venue_app.db.session.get(venue_app.Admin, self.admin_id)
            self.assertTrue(saved_admin.check_password("TestingAdminPass-2026!"))

    def test_password_recovery_rate_limits_requests(self):
        self.client.get("/admin/forgot-password")
        with self.client.session_transaction() as user_session:
            csrf = user_session["_csrf_token"]
        with patch.object(venue_app, "send_password_reset_email"):
            responses = [
                self.client.post(
                    "/admin/forgot-password",
                    data={
                        "csrf_token": csrf,
                        "email": "nobody@example.edu",
                    },
                )
                for _ in range(6)
            ]
        self.assertEqual([response.status_code for response in responses[:5]], [200] * 5)
        self.assertEqual(responses[5].status_code, 429)

    def test_password_recovery_delivery_failure_invalidates_token(self):
        self.client.get("/admin/forgot-password")
        with self.client.session_transaction() as user_session:
            csrf = user_session["_csrf_token"]
        with (
            patch.object(
                venue_app,
                "send_password_reset_email",
                side_effect=RuntimeError("SMTP settings are unavailable"),
            ),
            patch.object(
                venue_app,
                "PasswordResetThread",
                side_effect=self.run_thread_immediately,
            ),
        ):
            response = self.client.post(
                "/admin/forgot-password",
                data={"csrf_token": csrf, "email": "campus-admin@example.edu"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(venue_app.PASSWORD_RESET_GENERIC_MESSAGE.encode(), response.data)
        with venue_app.app.app_context():
            saved_token = venue_app.AdminPasswordResetToken.query.one()
            self.assertIsNotNone(saved_token.used_at)

    def test_password_reset_email_uses_configured_starttls_credentials(self):
        class FakeSMTP:
            def __init__(self):
                self.starttls_called = False
                self.login_args = None
                self.message = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def ehlo(self):
                return None

            def starttls(self, context):
                self.starttls_called = True
                self.tls_context = context

            def login(self, username, password):
                self.login_args = (username, password)

            def send_message(self, message):
                self.message = message

        fake_smtp = FakeSMTP()
        settings = {
            "SMTP_HOST": "smtp.example.edu",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "mailer@example.edu",
            "SMTP_PASSWORD": "smtp-test-secret",
            "SMTP_FROM": "Campus Venue Finder <no-reply@example.edu>",
            "APP_PUBLIC_URL": "https://venues.example.edu",
        }
        with (
            venue_app.app.app_context(),
            patch.dict(os.environ, settings),
            patch.object(venue_app.smtplib, "SMTP", return_value=fake_smtp) as smtp,
        ):
            venue_app.send_password_reset_email(
                "campus-admin@example.edu",
                "secure-reset-token",
            )

        smtp.assert_called_once_with("smtp.example.edu", 587, timeout=10)
        self.assertTrue(fake_smtp.starttls_called)
        self.assertEqual(
            fake_smtp.login_args,
            ("mailer@example.edu", "smtp-test-secret"),
        )
        self.assertEqual(fake_smtp.message["To"], "campus-admin@example.edu")
        self.assertIn(
            "https://venues.example.edu/admin/reset-password#token=secure-reset-token",
            fake_smtp.message.get_content(),
        )

    def test_invalid_venue_data_is_rejected_before_sheet_write(self):
        self.authenticate_admin()
        invalid_venue = dict(self.venue, status="FREE", latitude="91")
        with patch.object(venue_app, "google_sheet_table") as get_table:
            response = self.client.post(
                "/api/admin/venues",
                json=invalid_venue,
                headers=self.admin_headers(),
            )
        self.assertEqual(response.status_code, 400)
        get_table.assert_not_called()

    def test_add_update_status_and_delete_write_to_sheets(self):
        self.authenticate_admin()
        table = self.sheet_table()
        new_venue = {
            "venue_name": "Science Hall",
            "location": "East campus",
            "status": "MAINTENANCE",
            "latitude": "0.51",
            "longitude": "34.12",
        }
        with (
            patch.object(venue_app, "google_sheet_table", return_value=table),
            patch.object(venue_app, "google_sheets_request", return_value={}) as write,
            patch.object(venue_app, "sync_sheet_table_to_cache"),
        ):
            created = self.client.post(
                "/api/admin/venues",
                json=new_venue,
                headers=self.admin_headers(),
            )
            updated = self.client.put(
                f"/api/admin/venues/{self.venue_id}",
                json=dict(self.venue, venue_name="Learning Centre", status="OCCUPIED"),
                headers=self.admin_headers(),
            )
            status = self.client.patch(
                f"/api/admin/venues/{self.venue_id}/status",
                json={"status": "MAINTENANCE"},
                headers=self.admin_headers(),
            )
            deleted = self.client.delete(
                f"/api/admin/venues/{self.venue_id}",
                headers=self.admin_headers(),
            )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(write.call_count, 4)
        self.assertEqual(write.call_args_list[0].args[0], "POST")
        self.assertEqual(write.call_args_list[1].args[0], "PUT")
        self.assertEqual(write.call_args_list[2].args[0], "PUT")
        self.assertEqual(write.call_args_list[3].args[0], "POST")

    def test_write_fails_explicitly_when_google_configuration_is_missing(self):
        self.authenticate_admin()
        response = self.client.post(
            "/api/admin/venues",
            json={
                "venue_name": "New venue",
                "location": "Campus",
                "status": "AVAILABLE",
            },
            headers=self.admin_headers(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("not configured", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
