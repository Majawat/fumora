import os

# Unraid's umask, set before fumora is imported (perms.UMASK is read at import):
# the permission tests then check 664/775, not whatever the shell happens to use.
os.umask(0o002)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fumora.config import Config  # noqa: E402
from fumora.main import create_app  # noqa: E402


@pytest.fixture
def config(tmp_path):
    return Config(config_dir=tmp_path / "config", port=8792, scheduler=False, download_monitor=False)


@pytest.fixture
def app(config):
    app = create_app(config)
    app.state.metron_limiter.min_interval = 0  # no pacing delay against mocked Metron
    app.state.metron_limiter.jitter = 0
    return app


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def logged_in(client):
    r = client.post(
        "/setup",
        data={"username": "admin", "password": "correct horse", "password2": "correct horse"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return client
