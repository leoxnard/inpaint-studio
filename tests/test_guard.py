import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "RUNS", tmp_path)
    return TestClient(server.app)


def test_local_origin():
    assert server.local_origin("127.0.0.1:7380", None)
    assert server.local_origin("localhost:7381", "http://localhost:7380")
    assert server.local_origin("[::1]:7380", "http://[::1]:7380")
    assert not server.local_origin("evil.example", None)
    assert not server.local_origin("127.0.0.1:7380", "https://evil.example")
    assert not server.local_origin(None, None)


def test_api_guard(client):
    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/runs", headers={"Origin": "http://localhost:7380"}).status_code == 200
    r = client.get("/api/runs", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json() == {"detail": "forbidden origin"}
    assert client.get("/api/runs", headers={"host": "evil.example"}).status_code == 403
    assert client.post("/api/runs/x/hide", headers={"Origin": "https://evil.example"}).status_code == 403


def test_ws_guard(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/jobs", headers={"Origin": "https://evil.example"}):
            pass
    for headers in ({}, {"Origin": "http://127.0.0.1:7380"}):
        with client.websocket_connect("/ws/jobs", headers=headers) as ws:
            assert ws.receive_json()["type"] == "snapshot"
