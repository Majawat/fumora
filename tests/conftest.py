import pytest
from fastapi.testclient import TestClient

from fumora.config import Config
from fumora.main import create_app


@pytest.fixture
def config(tmp_path):
    return Config(config_dir=tmp_path / "config", port=8792, scheduler=False)


@pytest.fixture
def app(config):
    return create_app(config)


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
