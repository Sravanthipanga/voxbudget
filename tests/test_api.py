from fastapi.testclient import TestClient

from voxbudget.api import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_budget_endpoint_sums():
    r = client.get("/budget")
    body = r.json()
    assert body["total_ms"] == 800.0
    assert body["critical_path_subtotal_ms"] == 750.0


def test_turn_endpoint():
    r = client.post("/turn", json={"text": "what is a latency budget", "as_voice": True})
    assert r.status_code == 200
    body = r.json()
    assert body["transcript"] == "what is a latency budget"
    assert body["timing"]["first_audio_ms"] < 800


def test_stats_and_metrics():
    client.post("/turn", json={"text": "hello stats", "as_voice": True})
    assert client.get("/stats").json()["stats"]["first_audio"]["count"] >= 1
    text = client.get("/metrics").text
    assert "voxbudget_turns_total" in text


def test_chaos_clear():
    assert client.post("/chaos", json={"clear": True}).json()["chaos"] == "cleared"


def test_websocket_turn_streams_done():
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"text": "hello websocket", "as_voice": True})
        kinds = []
        while True:
            msg = ws.receive_json()
            kinds.append(msg["kind"])
            if msg["kind"] == "done":
                assert msg["data"]["transcript"] == "hello websocket"
                break
    assert "transcript" in kinds
