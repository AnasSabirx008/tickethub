"""Readiness state shared by the probes and the slow-start fault."""
import asyncio


class Lifecycle:
    def __init__(self):
        self.started = False

    async def finish_startup(self, delay_s):
        if delay_s:
            await asyncio.sleep(delay_s)   # liveness stays OK, readiness stays KO
        self.started = True
