"""Bounded-label production metrics and replayable decision diagnostics."""
import math
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class Monitoring:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.routes = Counter("deadline_routes", "Routing outcomes", ["model", "result"], registry=self.registry)
        self.ttft = Histogram("deadline_ttft_seconds", "Generated-token latency", ["endpoint"], registry=self.registry)
        self.error = Gauge("deadline_prediction_error_seconds", "Latest signed TTFT prediction error", ["endpoint"], registry=self.registry)
        self.reserved = Gauge("deadline_reserved_prefill_us", "Outstanding local prefill work", ["endpoint"], registry=self.registry)
        self.decoding = Gauge("deadline_local_decodes", "Outstanding local decode requests", ["endpoint"], registry=self.registry)
        self.factor = Gauge("deadline_correction", "Service cost multiplier", ["endpoint"], registry=self.registry)
        self.age = Gauge("deadline_snapshot_age_us", "Observation age; -1 means unavailable", ["endpoint"], registry=self.registry)
        self.drift = Gauge("deadline_drift", "Window state", ["endpoint", "state"], registry=self.registry)
        self.decisions = []
        self.latencies = []

    def decide(self, request_id, model, at, status, endpoint=None, predicted=None):
        self.routes.labels(model=model, result=str(status)).inc()
        self.decisions.append(dict(request_id=request_id, arrival_us=at, status=status,
                                   endpoint_id=endpoint, predicted_first_us=predicted))

    def first(self, row):
        latency = (row.first_us - row.arrival_us) / 1_000_000
        self.latencies.append(latency)
        self.ttft.labels(endpoint=row.endpoint).observe(latency)
        self.error.labels(endpoint=row.endpoint).set((row.first_us - row.predicted_first_us) / 1_000_000)

    def render(self, nodes):
        for eid, node in nodes.items():
            self.reserved.labels(endpoint=eid).set(node["reserved_us"])
            self.decoding.labels(endpoint=eid).set(node["decoding"])
            self.factor.labels(endpoint=eid).set(node["correction"])
            self.age.labels(endpoint=eid).set(node["age_us"])
            for state in ("insufficient_data", "normal", "slow", "fast"):
                self.drift.labels(endpoint=eid, state=state).set(int(node["drift"] == state))
        return generate_latest(self.registry)

    def summary(self, requests, start_us, end_us):
        if end_us <= start_us:
            raise ValueError("Evaluation window must have positive width")
        decisions = [r for r in self.decisions if start_us <= r["arrival_us"] < end_us]
        rows = [r for r in requests if start_us <= r.arrival_us < end_us]
        latencies = sorted(r.first_us - r.arrival_us for r in rows if r.first_us is not None)
        completed = sum(r.outcome == "completed" for r in rows)
        good = sum(r.outcome == "completed" and r.first_us <= r.deadline_us for r in rows)
        missed = sum(r.outcome is not None and (r.first_us is None or r.first_us > r.deadline_us) for r in rows)
        return dict(offered=len(decisions), accepted=len(rows), rejected=sum(r["status"] != 200 for r in decisions),
                    completed=completed, deadline_missed=missed,
                    goodput_per_s=round(good * 1_000_000 / (end_us - start_us), 6),
                    ttft_p95_us=latencies[math.ceil(.95 * len(latencies)) - 1] if latencies else None)
