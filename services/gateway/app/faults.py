"""Fault injection, driven ONLY by environment variables read at startup.

Why env vars: a fault must arrive with a new *version* being deployed, exactly like a real bug,
so that the canary analysis has something to catch. A runtime toggle would bypass the deployment.

  FAULT_ERROR_RATE        0.0-1.0  share of requests answered with HTTP 500
  FAULT_LATENCY_MS        int      extra latency per request (+/-20% jitter)
  FAULT_CPU_MS            int      busy CPU loop per request (blocks the event loop, like real CPU-bound code)
  FAULT_LEAK_KB           int      memory kept forever per request (memory leak)
  FAULT_STARTUP_DELAY_S   int      seconds before the service reports ready (slow start)
"""
import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .observability import SKIP_PATHS, log

_leak = []  # never freed, on purpose


@dataclass(frozen=True)
class Faults:
    error_rate: float = 0.0
    latency_ms: int = 0
    cpu_ms: int = 0
    leak_kb: int = 0
    startup_delay_s: int = 0

    @classmethod
    def from_env(cls):
        f = cls(
            error_rate=float(os.getenv("FAULT_ERROR_RATE", "0")),
            latency_ms=int(os.getenv("FAULT_LATENCY_MS", "0")),
            cpu_ms=int(os.getenv("FAULT_CPU_MS", "0")),
            leak_kb=int(os.getenv("FAULT_LEAK_KB", "0")),
            startup_delay_s=int(os.getenv("FAULT_STARTUP_DELAY_S", "0")),
        )
        if f != cls():
            log(logging.WARNING, "fault injection ACTIVE", **f.__dict__)
        return f


class FaultMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, faults):
        super().__init__(app)
        self.f = faults

    async def dispatch(self, request, call_next):
        if request.url.path in SKIP_PATHS:
            return await call_next(request)
        f = self.f
        if f.leak_kb:
            _leak.append(bytearray(f.leak_kb * 1024))
        if f.cpu_ms:
            end = time.perf_counter() + f.cpu_ms / 1000
            while time.perf_counter() < end:
                pass
        if f.latency_ms:
            await asyncio.sleep(f.latency_ms * random.uniform(0.8, 1.2) / 1000)
        if f.error_rate and random.random() < f.error_rate:
            return JSONResponse({"detail": "injected fault"}, status_code=500)
        return await call_next(request)
