"""Observability shared by every service: JSON logs, correlation ID, RED metrics.

(Identical copy in each service. In a real company this would be an internal package.)
"""
import contextvars
import json
import logging
import sys
import time
import uuid

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# Endpoints we don't measure or break (probes and scraping would only add noise)
SKIP_PATHS = {"/healthz", "/readyz", "/metrics"}

request_id_var = contextvars.ContextVar("request_id", default="-")

# --- RED metrics: Rate + Errors (http_requests_total by status) and Duration ---
HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request duration",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
HTTP_IN_FLIGHT = Gauge("http_requests_in_flight", "Requests currently being handled")
APP_INFO = Gauge("app_info", "Service version (always 1)", ["service", "version"])


class JsonFormatter(logging.Formatter):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def format(self, record):
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "service": self.service,
            "request_id": request_id_var.get(),
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "extra_fields", {}))
        return json.dumps(entry)


def setup_logging(service):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").disabled = True  # our middleware logs requests
    logging.getLogger("httpx").setLevel(logging.WARNING)


def log(level, msg, **fields):
    logging.getLogger("app").log(level, msg, extra={"extra_fields": fields})


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Correlation ID + metrics + one access log line per request."""

    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        path = request.url.path
        start = time.perf_counter()
        status = 500
        if path not in SKIP_PATHS:
            HTTP_IN_FLIGHT.inc()
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["x-request-id"] = rid
            return response
        finally:
            if path not in SKIP_PATHS:
                HTTP_IN_FLIGHT.dec()
                duration = time.perf_counter() - start
                route_obj = request.scope.get("route")
                # Route TEMPLATE (/bookings/{booking_id}), never the raw path: keeps cardinality low
                route = getattr(route_obj, "path", "unmatched")
                HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
                HTTP_LATENCY.labels(request.method, route).observe(duration)
                log(logging.INFO, "request", method=request.method, route=route,
                    status=status, duration_ms=round(duration * 1000, 1))
            request_id_var.reset(token)


def metrics_endpoint():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
