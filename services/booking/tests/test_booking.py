import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app

INV = "http://inventory.test"
U = {"x-user-id": "alice"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(f"sqlite:///{tmp_path}/b.db", INV)) as c:
        yield c


@respx.mock
def test_booking_confirmed(client):
    respx.post(f"{INV}/events/evt-1/reserve").respond(200, json={"status": "reserved"})
    r = client.post("/bookings", json={"event_id": "evt-1", "quantity": 2}, headers=U)
    assert r.status_code == 201 and r.json()["status"] == "CONFIRMED"
    assert client.get("/bookings", headers=U).json()[0]["status"] == "CONFIRMED"


@respx.mock
def test_sold_out_marks_failed(client):
    respx.post(f"{INV}/events/evt-1/reserve").respond(409, json={"detail": "not enough seats"})
    assert client.post("/bookings", json={"event_id": "evt-1", "quantity": 2}, headers=U).status_code == 409
    assert client.get("/bookings", headers=U).json()[0]["status"] == "FAILED"


@respx.mock
def test_inventory_down_triggers_compensation(client):
    respx.post(f"{INV}/events/evt-1/reserve").mock(side_effect=httpx.ReadTimeout("timeout"))
    release = respx.post(f"{INV}/events/evt-1/release").respond(200, json={"status": "released"})
    assert client.post("/bookings", json={"event_id": "evt-1", "quantity": 2}, headers=U).status_code == 503
    assert release.called                                   # saga compensation happened
    assert client.get("/bookings", headers=U).json()[0]["status"] == "FAILED"


@respx.mock
def test_compensation_fails_leaves_pending(client):
    respx.post(f"{INV}/events/evt-1/reserve").mock(side_effect=httpx.ConnectError("down"))
    respx.post(f"{INV}/events/evt-1/release").mock(side_effect=httpx.ConnectError("down"))
    assert client.post("/bookings", json={"event_id": "evt-1", "quantity": 1}, headers=U).status_code == 503
    assert client.get("/bookings", headers=U).json()[0]["status"] == "PENDING"   # visible, reconcilable


@respx.mock
def test_cancel_flow_and_ownership(client):
    respx.post(f"{INV}/events/evt-1/reserve").respond(200, json={})
    respx.post(f"{INV}/events/evt-1/release").respond(200, json={"status": "released"})
    bid = client.post("/bookings", json={"event_id": "evt-1", "quantity": 1}, headers=U).json()["id"]
    assert client.post(f"/bookings/{bid}/cancel", headers={"x-user-id": "bob"}).status_code == 404
    assert client.post(f"/bookings/{bid}/cancel", headers=U).json()["status"] == "CANCELLED"
    assert client.post(f"/bookings/{bid}/cancel", headers=U).status_code == 409


def test_validation_and_missing_user(client):
    assert client.post("/bookings", json={"event_id": "evt-1", "quantity": 50}, headers=U).status_code == 422
    assert client.post("/bookings", json={"event_id": "evt-1", "quantity": 1}).status_code == 422


def test_probes(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert "bookings_total" in client.get("/metrics").text
