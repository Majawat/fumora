from sqlalchemy import select

from fumora import settings_store
from fumora.models import Setting


def test_health_is_public(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_first_run_redirects_to_setup(client):
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/setup"


def test_setup_rejects_short_or_mismatched_password(client):
    r = client.post("/setup", data={"username": "a", "password": "short", "password2": "short"})
    assert r.status_code == 400
    r = client.post("/setup", data={"username": "a", "password": "longenough1", "password2": "longenough2"})
    assert r.status_code == 400


def test_setup_then_login_flow(logged_in):
    assert logged_in.get("/").status_code == 200
    logged_in.post("/logout")
    assert logged_in.get("/", follow_redirects=False).headers["location"] == "/login"
    r = logged_in.post("/login", data={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    r = logged_in.post(
        "/login", data={"username": "admin", "password": "correct horse"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert logged_in.get("/").status_code == 200


def test_setup_closed_after_first_user(logged_in):
    r = logged_in.post(
        "/setup",
        data={"username": "x", "password": "another one", "password2": "another one"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/login"


def test_secret_is_saved_but_never_rendered(logged_in, app):
    logged_in.post("/settings", data={"metron.username": "cody", "metron.password": "s3cret-value"})
    page = logged_in.get("/settings").text
    assert "s3cret-value" not in page
    assert "Saved — leave blank to keep" in page
    assert 'value="cody"' in page

    # Blank secret keeps the saved value.
    logged_in.post("/settings", data={"metron.username": "cody", "metron.password": ""})
    with app.state.session_factory() as db:
        assert settings_store.get(db, "metron.password") == "s3cret-value"

    # Clear checkbox removes it.
    logged_in.post("/settings", data={"metron.password": "", "metron.password.clear": "1"})
    with app.state.session_factory() as db:
        assert not settings_store.is_set(db, "metron.password")


def test_unknown_setting_keys_are_ignored(logged_in, app):
    logged_in.post("/settings", data={"evil.key": "x"})
    with app.state.session_factory() as db:
        assert db.scalar(select(Setting).where(Setting.key == "evil.key")) is None


def test_metron_test_without_credentials(logged_in):
    r = logged_in.post("/settings/test/metron")
    assert "Save a Metron API token (or username and password) first." in r.text
