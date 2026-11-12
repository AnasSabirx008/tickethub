"""inventory-service: owns the event catalog and the seats left. INTERNAL ONLY."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import redis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .faults import FaultMiddleware, Faults
from .lifecycle import Lifecycle
from .observability import APP_INFO, ObservabilityMiddleware, log, metrics_endpoint, setup_logging
from .store import Store

SERVICE = "inventory-service"


class ReserveIn(BaseModel):
    booking_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1, le=10)


class ReleaseIn(BaseModel):
    booking_id: str = Field(min_length=1, max_length=64)


def create_app(redis_client=None):
    setup_logging(SERVICE)
    version = os.getenv("APP_VERSION", "dev")
    faults = Faults.from_env()
    life = Lifecycle()
    client = redis_client or redis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True, socket_timeout=1, socket_connect_timeout=1,
        # a dropped connection is retried (safe: reserve/release are idempotent)
        retry=Retry(ExponentialBackoff(cap=0.5), 2),
        retry_on_error=[redis.ConnectionError, redis.TimeoutError],
        health_check_interval=30)
    store = Store(client)

    @asynccontextmanager
    async def lifespan(app):
        APP_INFO.labels(SERVICE, version).set(1)
        if os.getenv("SEED_EVENTS", "true") == "true":
            try:
                store.seed()
            except redis.RedisError as e:
                log(logging.ERROR, "seed failed", error=str(e))
        task = asyncio.create_task(life.finish_startup(faults.startup_delay_s))
        log(logging.INFO, "started", version=version)
        yield
        task.cancel()

    app = FastAPI(title=SERVICE, version=version, lifespan=lifespan)
    app.add_middleware(FaultMiddleware, faults=faults)
    app.add_middleware(ObservabilityMiddleware)   # added last = outermost: measures faults too

    @app.exception_handler(redis.RedisError)
    async def redis_down(_, exc):
        log(logging.ERROR, "redis error", error=str(exc))
        return JSONResponse({"detail": "storage unavailable"}, status_code=503)

    # ---- probes ----
    @app.get("/healthz")
    def healthz():
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz():
        if not life.started:
            return JSONResponse({"status": "starting"}, status_code=503)
        try:
            store.ping()
        except redis.RedisError:
            return JSONResponse({"status": "redis unreachable"}, status_code=503)
        return {"status": "ready"}

    app.add_api_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)

    # ---- business ----
    @app.get("/events")
    def list_events():
        return store.list_events()

    @app.get("/events/{event_id}")
    def get_event(event_id: str):
        e = store.get_event(event_id)
        if not e:
            raise HTTPException(404, "event not found")
        return e

    @app.post("/events/{event_id}/reserve")
    def reserve(event_id: str, body: ReserveIn):
        res = store.reserve(event_id, body.booking_id, body.quantity)
        if res == -1:
            raise HTTPException(404, "event not found")
        if res == 0:
            raise HTTPException(409, "not enough seats")
        if res == -2:
            raise HTTPException(409, "booking_id already used")
        return {"status": "reserved", "booking_id": body.booking_id}

    @app.post("/events/{event_id}/release")
    def release(event_id: str, body: ReleaseIn):
        res = store.release(event_id, body.booking_id)
        return {"status": "released" if res == 1 else "nothing_to_release",
                "booking_id": body.booking_id}

    return app


app = create_app()
