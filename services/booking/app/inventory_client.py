"""HTTP client to inventory-service. Propagates the correlation ID."""
import httpx

from .observability import request_id_var


class InventoryClient:
    def __init__(self, base_url, timeout_s):
        self.http = httpx.Client(base_url=base_url, timeout=timeout_s)

    def _headers(self):
        return {"x-request-id": request_id_var.get()}

    def reserve(self, event_id, booking_id, qty):
        return self.http.post(f"/events/{event_id}/reserve", headers=self._headers(),
                              json={"booking_id": booking_id, "quantity": qty})

    def release(self, event_id, booking_id):
        return self.http.post(f"/events/{event_id}/release", headers=self._headers(),
                              json={"booking_id": booking_id})

    def close(self):
        self.http.close()
