import pytest


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    return "s3cret"


def test_writes_open_without_admin_token(client):
    r = client.post("/api/settings", json={"balance": 10})
    assert r.status_code == 200
    assert client.get("/api/auth").json() == {"auth_required": False, "token_valid": True}


def test_writes_rejected_without_token(client, token):
    for method, path in (("post", "/api/poller/cookie"), ("post", "/api/credentials"),
                         ("delete", "/api/traders/Tester"), ("post", "/api/push/mt5"),
                         ("delete", "/api/poller/cookie"), ("get", "/api/poller/test")):
        r = client.request(method.upper(), path, json={})
        assert r.status_code == 401, path


def test_wrong_token_rejected(client, token):
    r = client.post("/api/settings", json={"balance": 1}, headers={"X-Admin-Token": "nope"})
    assert r.status_code == 401


def test_admin_token_accepted(client, token):
    r = client.post("/api/settings", json={"balance": 42}, headers={"X-Admin-Token": token})
    assert r.status_code == 200
    assert r.json()["balance"] == 42


def test_sync_token_accepted_for_cookie_push(client, token, monkeypatch):
    monkeypatch.setenv("COOKIE_SYNC_TOKEN", "sync")
    r = client.post("/api/poller/cookie", json={"cookie": "a=b"},
                    headers={"X-Sync-Token": "sync"})
    assert r.status_code == 200 and r.json()["ok"]


def test_reads_stay_open(client, token):
    assert client.get("/api/widget").status_code == 200
    assert client.get("/api/journal").status_code == 200
    assert client.get("/healthz").json()["ok"] is True


def test_auth_status_reports_token_validity(client, token):
    assert client.get("/api/auth").json() == {"auth_required": True, "token_valid": False}
    assert client.get("/api/auth", headers={"X-Admin-Token": token}).json()["token_valid"]
