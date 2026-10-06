"""Online calibration based on backend service spans, excluding queue time."""
from collections import deque
from .observations import finite_nonnegative


class Feedback:
    def __init__(self, policy, endpoint_ids):
        self.policy = policy
        self.factors = {eid: 1.0 for eid in endpoint_ids}
        self.samples = {eid: deque(maxlen=policy["window_samples"]) for eid in endpoint_ids}
        self.seen = set()

    def observe(self, endpoint, sample_id, baseline_us, service_us):
        identity = (endpoint, sample_id)
        if endpoint not in self.factors or identity in self.seen:
            return False
        if not finite_nonnegative(baseline_us) or baseline_us == 0 or not finite_nonnegative(service_us):
            return False
        self.seen.add(identity)
        ratio = service_us / baseline_us
        alpha = self.policy["alpha"]
        updated = (1 - alpha) * self.factors[endpoint] + alpha * ratio
        self.factors[endpoint] = min(self.policy["upper"], max(self.policy["lower"], updated))
        self.samples[endpoint].append(ratio)
        return True

    def status(self, endpoint):
        samples = self.samples[endpoint]
        if len(samples) < self.policy["window_samples"]:
            return "insufficient_data"
        ratio = sum(samples) / len(samples)
        if ratio > self.policy["drift_upper"]:
            return "slow"
        if ratio < self.policy["drift_lower"]:
            return "fast"
        return "normal"
