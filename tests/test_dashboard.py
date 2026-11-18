"""Tests for dashboard and main.py DATA_DIR integration."""
import os
import json
import pytest


class TestDataDir:
    def test_main_uses_data_dir_env(self, monkeypatch, tmp_path):
        """main.py constants should use DATA_DIR env var."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import importlib
        import main
        importlib.reload(main)
        assert main.CONFIG_FILE == os.path.join(str(tmp_path), "config.yaml")
        assert main.PROCESSED_IDS_FILE == os.path.join(str(tmp_path), "processed", "processed_ids.json")
        assert main.LOG_FILE == os.path.join(str(tmp_path), "logs", "app.log")

    def test_main_defaults_to_current_dir(self, monkeypatch):
        """Without DATA_DIR, main.py should use current directory."""
        monkeypatch.delenv("DATA_DIR", raising=False)
        import importlib
        import main
        importlib.reload(main)
        assert main.CONFIG_FILE == os.path.join(".", "config.yaml")


@pytest.fixture
def app(monkeypatch, tmp_path):
    """Create test Flask app with env vars set."""
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASS", "testpass123")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_URL", "https://test.up.railway.app")
    monkeypatch.setenv("TESTING", "1")

    import os
    os.makedirs(tmp_path / "logs", exist_ok=True)
    os.makedirs(tmp_path / "processed", exist_ok=True)
    os.makedirs(tmp_path / "tokens", exist_ok=True)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("polling_interval_minutes: 5\naccounts: []\n")

    import importlib
    import main
    importlib.reload(main)
    import dashboard
    importlib.reload(dashboard)
    dashboard.app.config["TESTING"] = True
    dashboard.app.config["DATA_DIR"] = str(tmp_path)
    return dashboard.app


@pytest.fixture
def client(app):
    return app.test_client()


class TestLogin:
    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_login_page_renders(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_login_with_correct_credentials(self, client):
        resp = client.post("/login", data={
            "username": "admin",
            "password": "testpass123",
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/"

    def test_login_with_wrong_password(self, client):
        resp = client.post("/login", data={
            "username": "admin",
            "password": "wrong",
        })
        assert resp.status_code == 200

    def test_api_returns_401_when_not_logged_in(self, client):
        resp = client.get("/api/status")
        assert resp.status_code in (302, 401)

    def test_oauth_callback_accessible_without_login(self, client):
        resp = client.get("/oauth/callback")
        # Should not redirect to login — returns 400 because no code/state
        assert resp.status_code == 400

    def test_logout(self, client):
        client.post("/login", data={
            "username": "admin",
            "password": "testpass123",
        })
        resp = client.get("/api/status")
        assert resp.status_code == 200
        resp = client.get("/logout")
        assert resp.status_code == 302
        resp = client.get("/api/status")
        assert resp.status_code in (302, 401)


class TestOAuthFlow:
    def _login(self, client):
        client.post("/login", data={
            "username": "admin",
            "password": "testpass123",
        })

    def test_auth_start_uses_public_url(self, client, app):
        self._login(client)
        import os
        data_dir = app.config.get("DATA_DIR") or os.environ.get("DATA_DIR", ".")
        creds_path = os.path.join(data_dir, "credentials.json")
        with open(creds_path, "w") as f:
            json.dump({
                "web": {
                    "client_id": "test-id",
                    "client_secret": "test-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }, f)
        config_path = os.path.join(data_dir, "config.yaml")
        with open(config_path, "w") as f:
            f.write(
                "polling_interval_minutes: 5\naccounts:\n"
                "  - email: test@gmail.com\n"
                "    token_file: tokens/token_test.json\n"
                "    sheet_id: sid\n"
            )

        resp = client.post("/api/auth/start",
                           json={"email": "test@gmail.com"},
                           content_type="application/json")
        data = resp.get_json()
        assert resp.status_code == 200
        assert "auth_url" in data
        assert "test.up.railway.app" in data["auth_url"]


class TestRobustness:
    def _login(self, client):
        client.post("/login", data={"username": "admin", "password": "testpass123"})

    def test_healthz_needs_no_login(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_corrupt_config_is_not_overwritten(self, client, app):
        self._login(client)
        data_dir = os.environ["DATA_DIR"]
        config_path = os.path.join(data_dir, "config.yaml")
        with open(config_path, "w") as f:
            f.write("accounts: [unclosed\n")
        resp = client.put("/api/etsy/config/polling", json={"etsy_polling_interval_minutes": 7})
        assert resp.status_code == 500
        with open(config_path) as f:
            assert f.read() == "accounts: [unclosed\n"

    def test_config_save_is_atomic_and_valid(self, client):
        self._login(client)
        resp = client.post("/api/accounts", json={"email": "a@x.com", "sheet_id": "sid"})
        assert resp.status_code == 200
        import yaml
        with open(os.path.join(os.environ["DATA_DIR"], "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        assert cfg["accounts"][0]["email"] == "a@x.com"

    def test_poll_now_runs_as_job_and_rejects_concurrent(self, client, monkeypatch):
        self._login(client)
        import threading
        import etsy_poller
        release = threading.Event()
        monkeypatch.setattr(etsy_poller, "poll_all_accounts_job", lambda **kw: release.wait(2) and {"new_orders": 0})
        try:
            first = client.post("/api/etsy/poll-now", json={})
            assert first.status_code == 202
            second = client.post("/api/etsy/sync-sheet")
            assert second.status_code == 409
            status = client.get("/api/etsy/status").get_json()
            assert status["poll_running"] is True
            assert status["job"]["kind"] == "poll_all"
            assert "orders_version" in status
        finally:
            release.set()

    def test_dismiss_unparsed_email(self, client, monkeypatch):
        self._login(client)
        import etsy_poller
        calls = []
        monkeypatch.setattr(etsy_poller, "dismiss_unparsed_email",
                            lambda acc, mid: calls.append((acc, mid)) or mid == "m1")
        assert client.post("/api/etsy/unparsed/dismiss", json={}).status_code == 400
        ok = client.post("/api/etsy/unparsed/dismiss", json={"account": "a@x.com", "message_id": "m1"})
        assert ok.status_code == 200
        missing = client.post("/api/etsy/unparsed/dismiss", json={"account": "a@x.com", "message_id": "m2"})
        assert missing.status_code == 404
        assert calls == [("a@x.com", "m1"), ("a@x.com", "m2")]

    def test_poll_now_unknown_account_404(self, client):
        self._login(client)
        resp = client.post("/api/etsy/poll-now/nobody@x.com", json={})
        assert resp.status_code == 404
