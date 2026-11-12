"""booking-service: owns bookings. INTERNAL ONLY (called by the gateway).

Booking = saga:  save PENDING -> reserve seats -> CONFIRMED
                                       |  sold out / unknown event -> FAILED
                                       |  inventory error/timeout  -> release (compensation) -> FAILED
                                       |                              release also fails     -> stays PENDING (to reconcile)
The user identity comes from the X-User-ID header set by the gateway.
"""
import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import Counter
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from .db import Base, Booking, Status, make_session_factory
from .faults import FaultMiddleware, Faults
from .inventory_client import InventoryClient
from .lifecycle import Lifecycle
from .observability import APP_INFO, ObservabilityMiddleware, log, metrics_endpoint, setup_logging

SERVICE = "booking-service"

# Business metric: what happened to each booking attempt
BOOKINGS = Counter("bookings_total", "Booking attempts by outcome", ["outcome"])


class BookingIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1, le=10)


def database_url_from_env():
    """DATABASE_URL without the password + DATABASE_PASSWORD_FILE (a mounted secret) -> full URL.
    The password never sits in an env var (visible with `docker inspect`)."""
    url = os.getenv("DATABASE_URL", "postgresql+psycopg://tickethub:tickethub@localhost:5432/bookings")
    password_file = os.getenv("DATABASE_PASSWORD_FILE")
    if password_file:
        with open(password_file) as f:
            url = make_url(url).set(password=f.read().strip()).render_as_string(hide_password=False)
    return url


def create_app(database_url=None, inventory_url=None):
    setup_logging(SERVICE)
    version = os.getenv("APP_VERSION", "dev")
    faults = Faults.from_env()
    life = Lifecycle()
    engine, Session = make_session_factory(
        database_url or database_url_from_env())
    inventory = InventoryClient(inventory_url or os.getenv("INVENTORY_URL", "http://localhost:8001"),
                                float(os.getenv("INVENTORY_TIMEOUT_S", "2")))

    @asynccontextmanager
    async def lifespan(app):
        APP_INFO.labels(SERVICE, version).set(1)
        for attempt in range(10):           # DB may start after us
            try:
                Base.metadata.create_all(engine)
                break
            except SQLAlchemyError as e:
                log(logging.WARNING, "database not reachable yet", attempt=attempt, error=str(e)[:200])
                time.sleep(2)
        task = asyncio.create_task(life.finish_startup(faults.startup_delay_s))
        log(logging.INFO, "started", version=version)
        yield
        task.cancel()
        inventory.close()

    app = FastAPI(title=SERVICE, version=version, lifespan=lifespan)
    app.add_middleware(FaultMiddleware, faults=faults)
    app.add_middleware(ObservabilityMiddleware)

    @app.exception_handler(SQLAlchemyError)
    async def db_down(_, exc):
        log(logging.ERROR, "database error", error=str(exc)[:200])
        return JSONResponse({"detail": "database unavailable"}, status_code=503)

    # ---- probes ----
    @app.get("/healthz")
    def healthz():
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz():
        # Checks ITS OWN database only. Not inventory: if inventory is down we can still
        # list and read bookings; failing readiness would remove us for nothing.
        if not life.started:
            return JSONResponse({"status": "starting"}, status_code=503)
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "database unreachable"}, status_code=503)
        return {"status": "ready"}

    app.add_api_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)

    # ---- helpers ----
    def set_status(booking_id, status):
        with Session.begin() as s:
            s.get(Booking, booking_id).status = status

    def compensate(b):
        """Undo a reservation that may or may not have happened (release is idempotent)."""
        try:
            r = inventory.release(b.event_id, b.id)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log(logging.ERROR, "compensation failed, booking left PENDING", booking_id=b.id, error=str(e))
            BOOKINGS.labels("pending_unresolved").inc()
            raise HTTPException(503, {"message": "booking pending, will be reconciled", "booking_id": b.id})
        set_status(b.id, Status.FAILED)
        BOOKINGS.labels("failed_inventory_error").inc()
        raise HTTPException(503, "inventory unavailable, booking cancelled")

    # ---- business ----
    @app.post("/bookings", status_code=201)
    def create_booking(body: BookingIn, x_user_id: str = Header(...)):
        b = Booking(id=str(uuid.uuid4()), user_id=x_user_id, event_id=body.event_id,
                    quantity=body.quantity, status=Status.PENDING)
        with Session.begin() as s:          # 1. record the intent first
            s.add(b)

        try:                                # 2. take the seats
            r = inventory.reserve(b.event_id, b.id, b.quantity)
        except httpx.HTTPError as e:
            log(logging.WARNING, "inventory call failed", booking_id=b.id, error=str(e))
            compensate(b)

        if r.status_code == 200:            # 3. confirm
            set_status(b.id, Status.CONFIRMED)
            BOOKINGS.labels("confirmed").inc()
            b.status = Status.CONFIRMED
            log(logging.INFO, "booking confirmed", booking_id=b.id, event_id=b.event_id, qty=b.quantity)
            return b.to_dict()
        if r.status_code in (404, 409):
            set_status(b.id, Status.FAILED)
            BOOKINGS.labels("failed_sold_out" if r.status_code == 409 else "failed_unknown_event").inc()
            raise HTTPException(r.status_code, r.json().get("detail", "rejected"))
        compensate(b)                       # 5xx from inventory: did it reserve or not? undo to be safe

    @app.get("/bookings")
    def my_bookings(x_user_id: str = Header(...)):
        with Session() as s:
            rows = s.scalars(select(Booking).where(Booking.user_id == x_user_id)
                             .order_by(Booking.created_at.desc())).all()
            return [r.to_dict() for r in rows]

    @app.get("/bookings/{booking_id}")
    def get_booking(booking_id: str, x_user_id: str = Header(...)):
        with Session() as s:
            b = s.get(Booking, booking_id)
            if not b or b.user_id != x_user_id:   # 404, not 403: don't reveal others' bookings exist
                raise HTTPException(404, "booking not found")
            return b.to_dict()

    @app.post("/bookings/{booking_id}/cancel")
    def cancel(booking_id: str, x_user_id: str = Header(...)):
        with Session.begin() as s:
            b = s.get(Booking, booking_id)
            if not b or b.user_id != x_user_id:
                raise HTTPException(404, "booking not found")
            if b.status != Status.CONFIRMED:
                raise HTTPException(409, f"cannot cancel a {b.status} booking")
            b.status = Status.CANCELLING
        try:
            inventory.release(b.event_id, b.id).raise_for_status()
        except httpx.HTTPError as e:
            set_status(b.id, Status.CONFIRMED)    # nothing changed: user can retry
            log(logging.WARNING, "cancel failed", booking_id=b.id, error=str(e))
            raise HTTPException(503, "inventory unavailable, try again")
        set_status(b.id, Status.CANCELLED)
        BOOKINGS.labels("cancelled").inc()
        b.status = Status.CANCELLED
        return b.to_dict()

    return app


app = create_app()
