"""Local request ownership through prefill, decode and terminal states."""
from dataclasses import dataclass


@dataclass
class Reservation:
    request_id: str
    endpoint: str
    arrival_us: int
    deadline_us: int
    predicted_first_us: int
    work_us: float
    model: str
    epoch: int = 1
    baseline_us: float = 0
    stage: str = "prefill"
    first_us: int | None = None
    outcome: str | None = None


class Capacity:
    def __init__(self):
        self.requests = {}

    def load(self, endpoint):
        rows = [row for row in self.requests.values() if row.endpoint == endpoint]
        return (sum(row.work_us for row in rows if row.stage == "prefill"),
                sum(row.stage == "decode" for row in rows))

    def reserve(self, reservation):
        if reservation.request_id in self.requests:
            raise ValueError("Request IDs must be unique within this router session")
        self.requests[reservation.request_id] = reservation

    def first_token(self, request_id, now):
        row = self.requests.get(request_id)
        if row is None or row.stage != "prefill":
            return None
        row.first_us, row.stage = now, "decode"
        return row

    def finish(self, request_id, success):
        row = self.requests.get(request_id)
        if row is None or row.stage == "terminal":
            return None
        row.outcome = "completed" if success and row.first_us is not None else "failed"
        row.stage = "terminal"
        return row
