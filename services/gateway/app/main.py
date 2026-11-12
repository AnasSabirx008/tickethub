"""gateway: the ONLY public service. No business logic.

- authenticates the caller with X-API-Key and passes the identity down as X-User-ID
- adds/propagates the correlation ID (X-Request-ID)
- forwards to inventory (events) and booking (bookings), with timeouts
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .faults import FaultMiddleware, Faults
from .lifecycle import Lifecycle
from .observability import APP_INFO, ObservabilityMiddleware, log, metrics_endpoint, request_id_var, setup_logging

SERVICE = "gateway"


def parse_keys(raw):
    """API_KEYS="key1:alice,key2:bob" -> {"key1": "alice", "key2": "bob"}"""
    return dict(pair.split(":", 1) for pair in raw.split(",") if ":" in pair)


def api_keys_from_env():
    """API_KEYS_FILE (a mounted secret) wins over the API_KEYS env var."""
    path = os.getenv("API_KEYS_FILE")
    if path:
        with open(path) as f:
            return f.read().strip()
    return os.getenv("API_KEYS", "")


def create_app(inventory_url=None, booking_url=None, api_keys=None):
    setup_logging(SERVICE)
    version = os.getenv("APP_VERSION", "dev")
    faults = Faults.from_env()
    life = Lifecycle()
    keys = parse_keys(api_keys if api_keys is not None else api_keys_from_env())
    if not keys:
        log(logging.WARNING, "no API_KEYS configured: every authenticated call will be refused")
    upstreams = {
        "inventory": inventory_url or os.getenv("INVENTORY_URL", "http://localhost:8001"),
        "booking": booking_url or os.getenv("BOOKING_URL", "http://localhost:8002"),
    }
    timeout = httpx.Timeout(float(os.getenv("UPSTREAM_TIMEOUT_S", "3")))
    clients = {}

    @asynccontextmanager
    async def lifespan(app):
        APP_INFO.labels(SERVICE, version).set(1)
        for name, url in upstreams.items():
            clients[name] = httpx.AsyncClient(base_url=url, timeout=timeout)
        task = asyncio.create_task(life.finish_startup(faults.startup_delay_s))
        log(logging.INFO, "started", version=version, **upstreams)
        yield
        task.cancel()
        for c in clients.values():
            await c.aclose()

    app = FastAPI(title="TicketHub API", version=version, lifespan=lifespan)
    app.add_middleware(FaultMiddleware, faults=faults)
    app.add_middleware(ObservabilityMiddleware)

    def current_user(x_api_key: str = Header(default="")):
        user = keys.get(x_api_key)
        if not user:
            raise HTTPException(401, "invalid or missing API key")
        return user

    async def forward(upstream, method, path, request: Request, user=None):
        headers = {"x-request-id": request_id_var.get()}
        if user:
            headers["x-user-id"] = user
        body = await request.body()
        if body:
            headers["content-type"] = "application/json"
        try:
            r = await clients[upstream].request(method, path, headers=headers, content=body or None)
        except httpx.TimeoutException:
            log(logging.WARNING, "upstream timeout", upstream=upstream, path=path)
            return JSONResponse({"detail": f"{upstream} timeout"}, status_code=504)
        except httpx.HTTPError as e:
            log(logging.WARNING, "upstream unreachable", upstream=upstream, path=path, error=str(e))
            return JSONResponse({"detail": f"{upstream} unavailable"}, status_code=502)
        return Response(r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"))

    # ---- probes ----
    @app.get("/healthz")
    def healthz():
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz():
        # Does NOT check upstreams: if booking is down, events must still be served.
        # If every service failed readiness when a dependency fails, one outage would
        # remove the whole chain from load balancing (cascading failure).
        if not life.started:
            return JSONResponse({"status": "starting"}, status_code=503)
        return {"status": "ready"}

    app.add_api_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)

    # ---- public API ----
    @app.get("/api/me")
    def me(user: str = Depends(current_user)):
        return {"user": user}

    @app.get("/api/events")
    async def list_events(request: Request):
        return await forward("inventory", "GET", "/events", request)

    @app.get("/api/events/{event_id}")
    async def get_event(event_id: str, request: Request):
        return await forward("inventory", "GET", f"/events/{event_id}", request)

    @app.post("/api/bookings")
    async def create_booking(request: Request, user: str = Depends(current_user)):
        return await forward("booking", "POST", "/bookings", request, user)

    @app.get("/api/bookings")
    async def my_bookings(request: Request, user: str = Depends(current_user)):
        return await forward("booking", "GET", "/bookings", request, user)

    @app.get("/api/bookings/{booking_id}")
    async def get_booking(booking_id: str, request: Request, user: str = Depends(current_user)):
        return await forward("booking", "GET", f"/bookings/{booking_id}", request, user)

    @app.post("/api/bookings/{booking_id}/cancel")
    async def cancel(booking_id: str, request: Request, user: str = Depends(current_user)):
        return await forward("booking", "POST", f"/bookings/{booking_id}/cancel", request, user)

    return app


app = create_app()
