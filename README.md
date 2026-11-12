# TicketHub: the application (test subject for the DevSecOps / AIOps platform)

A web front-end and three small FastAPI services. The app is deliberately simple; the project is the
**platform** around it.

```
browser ──► frontend (nginx, PUBLIC) ──/api──► gateway ──► booking-service ──► inventory-service
              static site                                      │                     │
                                                       Postgres (bookings)    Redis (events + seats)
```

**frontend** (`services/frontend`): plain HTML/CSS/JS, no build step, no npm dependencies (nothing to patch).
nginx serves the page and proxies `/api/` to `${GATEWAY_URL}` (same origin, so no CORS), starts the
correlation ID, and adds security headers (CSP, X-Frame-Options...). It listens on **8080** so it can run as
non-root. `nginx.conf.template` is rendered at start by the official nginx images (put it in
`/etc/nginx/templates/default.conf.template`). Demo: sign in with a key from `API_KEYS`, book, cancel;
error messages show the request ID so you can find the request in the logs.

| Service | Port (dev) | Exposed | Owns | Readiness checks |
|---|---|---|---|---|
| frontend | 8080 (container) | **public** | nothing (static site + /api proxy) | nginx `/healthz` |
| gateway | 8080 | internal (behind frontend) | nothing (API key auth, correlation ID, routing) | itself only |
| booking-service | 8002 | internal | bookings (Postgres) | its DB only |
| inventory-service | 8001 | internal | events + seat counters (Redis) | Redis |

## Public API (gateway)
| Method | Path | Auth | Forwards to |
|---|---|---|---|
| GET | `/api/me` | `X-API-Key` | (gateway: returns the user) |
| GET | `/api/events` | – | inventory `GET /events` |
| GET | `/api/events/{id}` | – | inventory `GET /events/{id}` |
| POST | `/api/bookings` `{"event_id","quantity"}` | `X-API-Key` | booking `POST /bookings` |
| GET | `/api/bookings` | `X-API-Key` | booking `GET /bookings` |
| GET | `/api/bookings/{id}` | `X-API-Key` | booking `GET /bookings/{id}` |
| POST | `/api/bookings/{id}/cancel` | `X-API-Key` | booking `POST /bookings/{id}/cancel` |

Internal only: inventory `POST /events/{id}/reserve` and `/release` (called by booking-service).
Every service: `GET /healthz` (liveness), `GET /readyz` (readiness), `GET /metrics` (Prometheus).

## Design decisions (be ready to explain them)
- **Saga for bookings**: save `PENDING` → reserve seats → `CONFIRMED`. Sold out → `FAILED`.
  Inventory error/timeout → `release` (compensation) → `FAILED`; if the release fails too, the booking
  stays `PENDING` (visible, can be reconciled later). Cancel: `CANCELLING` → release → `CANCELLED`.
- **Idempotency**: reserve/release are keyed by `booking_id` and run as atomic Redis Lua scripts:
  retries never double-reserve, two users can't both take the last seat.
- **Readiness checks only a service's own dependencies**: otherwise one outage cascades through the chain.
- **Observability**: JSON logs on stdout with `request_id` propagated across services (`X-Request-ID`),
  RED metrics (`http_requests_total`, `http_request_duration_seconds` labelled by route *template*),
  business metric `bookings_total{outcome}`, `app_info{version}`.
- **Identity**: the gateway maps the API key to a user and sends `X-User-ID`. booking-service trusts it,
  which is only safe because booking-service is not reachable from outside (→ NetworkPolicy, your job).

## Configuration (environment variables)
| Variable | Service | Default |
|---|---|---|
| `APP_VERSION` | all | `dev` |
| `API_KEYS` (`key1:alice,key2:bob`) | gateway | empty = all auth refused |
| `INVENTORY_URL`, `BOOKING_URL`, `UPSTREAM_TIMEOUT_S` | gateway | localhost, 3 s |
| `DATABASE_URL` | booking | `postgresql+psycopg://tickethub:tickethub@localhost:5432/bookings` |
| `INVENTORY_URL`, `INVENTORY_TIMEOUT_S` | booking | `http://localhost:8001`, 2 s |
| `REDIS_URL`, `SEED_EVENTS` | inventory | `redis://localhost:6379/0`, `true` |

### Fault injection (all services, read at startup → a fault ships with a *version*)
| Variable | Effect |
|---|---|
| `FAULT_ERROR_RATE=0.05` | 5 % of requests → HTTP 500 |
| `FAULT_LATENCY_MS=150` | +150 ms (±20 %) per request |
| `FAULT_CPU_MS=50` | 50 ms busy CPU per request |
| `FAULT_LEAK_KB=100` | 100 KB never freed per request |
| `FAULT_STARTUP_DELAY_S=60` | not ready for 60 s (liveness stays OK) |

Probes and `/metrics` are never affected.

## Run locally (without containers for the app)
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r services/booking/requirements.txt -r services/gateway/requirements.txt \
            -r services/inventory/requirements.txt -r requirements-dev.txt
docker run -d --name redis -p 6379:6379 redis:7
docker run -d --name pg -p 5432:5432 -e POSTGRES_USER=tickethub -e POSTGRES_PASSWORD=tickethub -e POSTGRES_DB=bookings postgres:16
(cd services/inventory && uvicorn app.main:app --port 8001) &
(cd services/booking && uvicorn app.main:app --port 8002) &
(cd services/gateway && API_KEYS=k1:alice uvicorn app.main:app --port 8080) &

curl localhost:8080/api/events
curl -X POST localhost:8080/api/bookings -H 'x-api-key: k1' -H 'content-type: application/json' \
     -d '{"event_id":"evt-3","quantity":2}'
```

## Tests
```bash
for s in gateway inventory booking; do (cd services/$s && pytest -q); done
```
22 tests (plus a browser test of the UI during development): saga paths (confirm, sold out, compensation, compensation failure), idempotency,
ownership, auth, timeouts, probes, metrics cardinality, fault injection.

## Known limitations (left on purpose, good discussion points)
- No DB migrations (`create_all` at startup) → Alembic would be the next step.
- No reconciliation job for `PENDING` bookings.
- Shared modules (`observability.py`, `faults.py`, `lifecycle.py`) are copied in each service.
