import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    with TestClient(create_app(fakeredis.FakeRedis(decode_responses=True))) as c:
        yield c


def seats(c, eid="evt-3"):
    return c.get(f"/events/{eid}").json()["seats_left"]


def test_seeded_events(client):
    assert len(client.get("/events").json()) == 3


def test_reserve_and_release(client):
    before = seats(client)
    assert client.post("/events/evt-3/reserve", json={"booking_id": "b1", "quantity": 2}).status_code == 200
    assert seats(client) == before - 2
    assert client.post("/events/evt-3/release", json={"booking_id": "b1"}).json()["status"] == "released"
    assert seats(client) == before


def test_reserve_is_idempotent(client):
    before = seats(client)
    for _ in range(3):  # retries after a timeout must not reserve 3 times
        client.post("/events/evt-3/reserve", json={"booking_id": "b2", "quantity": 4})
    assert seats(client) == before - 4


def test_release_is_idempotent_and_safe(client):
    before = seats(client)
    assert client.post("/events/evt-3/release", json={"booking_id": "never"}).json()["status"] == "nothing_to_release"
    client.post("/events/evt-3/reserve", json={"booking_id": "b3", "quantity": 1})
    client.post("/events/evt-3/release", json={"booking_id": "b3"})
    client.post("/events/evt-3/release", json={"booking_id": "b3"})
    assert seats(client) == before


def test_sold_out_and_unknown(client):
    assert client.post("/events/evt-3/reserve", json={"booking_id": "big", "quantity": 10}).status_code == 200
    for i in range(4):
        client.post("/events/evt-3/reserve", json={"booking_id": f"x{i}", "quantity": 10})
    assert client.post("/events/evt-3/reserve", json={"booking_id": "last", "quantity": 1}).status_code == 409
    assert client.post("/events/nope/reserve", json={"booking_id": "z", "quantity": 1}).status_code == 404


def test_probes_and_metrics(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    client.get("/events/evt-1")
    m = client.get("/metrics").text
    assert 'route="/events/{event_id}"' in m      # template, not raw path
    assert "evt-1" not in m


def test_slow_start_fault(monkeypatch):
    monkeypatch.setenv("FAULT_STARTUP_DELAY_S", "30")
    with TestClient(create_app(fakeredis.FakeRedis(decode_responses=True))) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 503


def test_error_fault(monkeypatch):
    monkeypatch.setenv("FAULT_ERROR_RATE", "1.0")
    with TestClient(create_app(fakeredis.FakeRedis(decode_responses=True))) as c:
        assert c.get("/events").status_code == 500
        assert c.get("/healthz").status_code == 200   # probes are never broken
