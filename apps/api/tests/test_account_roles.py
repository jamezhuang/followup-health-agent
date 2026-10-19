from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import create_app
from app.storage import Store
from test_v0 import settings


@pytest.fixture
def role_app(settings, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ENABLED", "false")
    monkeypatch.setenv("HEALTH_MEMORY_ENABLED", "false")
    app = create_app(settings)
    app.state.store.register("admin001", "Admin123", role="admin")
    app.state.store.register("user001", "123456")
    app.state.workspace_documents = SimpleNamespace(repo=SimpleNamespace(
        runs=lambda *a, **kw: [{"id": "test-run"}],
        run_detail=lambda *a, **kw: {"id": "test-run", "events": []},
    ))
    # Avoid initializing a real document service; account and route checks are real.
    return app


def login(client, username, password):
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("username,password", [
    ("123456", "654321"), ("abcdef", "ABCDEF"),
    ("Abc123", "Def456"), ("a" * 20, "9" * 20),
])
def test_registration_accepts_requested_rules(settings, monkeypatch, username, password):
    monkeypatch.setenv("WORKSPACE_ENABLED", "false")
    client = TestClient(create_app(settings))
    response = client.post("/auth/register", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    assert response.json()["role"] == "user"
    assert response.json()["username"] == username.lower()


@pytest.mark.parametrize("field,value", [("username", "short"), ("password", "12345"),
    ("username", "a" * 21), ("password", "b" * 21),
    ("username", "user_001"), ("password", "Pass word"), ("username", "测试账号123")])
def test_registration_rejects_invalid_credentials(settings, monkeypatch, field, value):
    monkeypatch.setenv("WORKSPACE_ENABLED", "false")
    client = TestClient(create_app(settings))
    payload = {"username": "user001", "password": "123456", field: value}
    assert client.post("/auth/register", json=payload).status_code == 422


def test_registration_cannot_self_assign_admin(role_app):
    client = TestClient(role_app)
    assert client.post("/auth/register", json={"username": "rogue001", "password": "123456", "role": "admin"}).status_code == 422
    assert client.post("/auth/register", json={"username": "rogue001", "password": "123456"}).json()["role"] == "user"


@pytest.mark.parametrize("path", ["/workspace/runs", "/workspace/runs/test-run",
    "/workspace/admin/runs", "/workspace/admin/runs/test-run", "/dev/runs/test-run", "/dev/chat-integration"])
def test_anonymous_and_user_cannot_read_any_logs(role_app, path):
    client = TestClient(role_app)
    headers = {"X-Dev-Token": role_app.state.settings.dev_token}
    assert client.get(path, headers=headers).status_code == 401
    account = login(client, "user001", "123456")
    assert account["role"] == "user"
    assert client.get("/workspace/status").json()["admin"] is False
    assert client.get(path, headers=headers).status_code == 403


def test_admin_can_read_logs_and_status(role_app):
    client = TestClient(role_app)
    account = login(client, "admin001", "Admin123")
    assert account["role"] == "admin"
    assert client.get("/auth/me").json()["user"]["role"] == "admin"
    assert client.get("/workspace/status").json()["admin"] is True
    for path in ["/workspace/runs", "/workspace/admin/runs", "/workspace/runs/test-run", "/workspace/admin/runs/test-run"]:
        assert client.get(path).status_code == 200
    headers = {"X-Dev-Token": role_app.state.settings.dev_token}
    assert client.get("/dev/chat-integration", headers=headers).status_code == 200
    assert client.get("/dev/runs/test-run", headers=headers).status_code == 200
    client.post("/auth/logout")
    assert client.get("/workspace/admin/runs").status_code == 401


def test_role_checked_on_each_request_and_allowlist_cannot_elevate(role_app):
    client = TestClient(role_app)
    account = login(client, "admin001", "Admin123")
    role_app.state.workspace_admins = {"admin001", "user001"}
    with role_app.state.store.engine.begin() as connection:
        connection.execute(text("UPDATE accounts SET role='user' WHERE id=:id"), {"id": account["id"]})
    assert client.get("/workspace/admin/runs").status_code == 403
    assert client.get("/workspace/status").json()["admin"] is False


def test_existing_account_schema_migrates_without_data_loss(tmp_path):
    url = "sqlite:///" + str(tmp_path / "old.db")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE accounts (id VARCHAR(64) PRIMARY KEY, email VARCHAR(160) UNIQUE, password TEXT, profile JSON)"))
        connection.execute(text("INSERT INTO accounts VALUES ('legacy', 'old@example.com', 'unchanged-hash', '{}')"))
    engine.dispose()
    store = Store(url)
    with store.engine.connect() as connection:
        row = connection.execute(text("SELECT email, password, role FROM accounts")).one()
    assert tuple(row) == ("old@example.com", "unchanged-hash", "user")
    store.engine.dispose()


def test_legacy_email_accounts_keep_login_and_data(role_app):
    legacy = role_app.state.store.register("old@example.com", "old-password-123")
    role_app.state.store.set_profile(legacy["id"], {"age": "28"}, "manual")
    client = TestClient(role_app)
    account = login(client, "old@example.com", "old-password-123")
    assert account["role"] == "user"
    assert client.get("/profile").json()["fields"]["age"]["value"] == "28"
    assert client.get("/workspace/runs").status_code == 403
