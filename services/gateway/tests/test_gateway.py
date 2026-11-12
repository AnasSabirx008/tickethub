import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app

INV, BOOK = "http://inventory.test", "http://booking.test"
KEY = {"x-api-key": "k1"}


@pytest.fixture
def client():
    with TestClient(create_app(INV, BOOK, "k1:alice")) as c:
        yield c


@respx.mock
def test_events_are_public(client):
    respx.get(f"{INV}/events").respond(200, json=[{"id": "evt-1"}])
    assert client.get("/api/events").json() == [{"id": "evt-1"}]


def test_bookings_need_api_key(client):
    assert client.post("/api/bookings", json={"event_id": "evt-1", "quantity": 1}).status_code == 401
    assert client.get("/api/bookings", headers={"x-api-key": "wrong"}).status_code == 401


@respx.mock
def test_identity_and_request_id_forwarded(client):
    route = respx.post(f"{BOOK}/bookings").respond(201, json={"id": "b1"})
    r = client.post("/api/bookings", json={"event_id": "evt-1", "quantity": 1},
                    headers={**KEY, "x-request-id": "trace-123"})
    assert r.status_code == 201
    sent = route.calls.last.request
    assert sent.headers["x-user-id"] == "alice"
    assert sent.headers["x-request-id"] == "trace-123"
    assert r.headers["x-request-id"] == "trace-123"


@respx.mock
def test_upstream_status_passed_through(client):
    respx.post(f"{BOOK}/bookings").respond(409, json={"detail": "not enough seats"})
    assert client.post("/api/bookings", json={"event_id": "evt-1", "quantity": 1}, headers=KEY).status_code == 409


@respx.mock
def test_upstream_down_and_timeout(client):
    respx.get(f"{INV}/events").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{INV}/events/evt-1").mock(side_effect=httpx.ReadTimeout("slow"))
    assert client.get("/api/events").status_code == 502
    assert client.get("/api/events/evt-1").status_code == 504


def test_ready_without_upstreams(client):
    assert client.get("/readyz").status_code == 200


def test_me(client):
    assert client.get("/api/me", headers=KEY).json() == {"user": "alice"}
    assert client.get("/api/me").status_code == 401
