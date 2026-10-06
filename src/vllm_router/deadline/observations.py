"""Snapshot applicability and prefix-cache lookup.

Snapshots carry per-owner work, not an already combined router load. A router
must exclude its own owner before adding its local capacity reservations.
"""
import copy
import math
import asyncio


def finite_nonnegative(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


class Observations:
    def __init__(self, clock, max_age_us, owner):
        self.clock, self.max_age_us, self.owner = clock, max_age_us, owner
        self.samples = {}

    def ingest(self, sample):
        now = self.clock.now_us()
        try:
            if sample["observed_at_us"] > now or sample["observed_at_us"] < 0:
                return False
            if not isinstance(sample["sample_seq"], int) or sample["sample_seq"] < 0:
                return False
            if not isinstance(sample["by_owner"], dict):
                return False
            for row in sample["by_owner"].values():
                if not finite_nonnegative(row["backlog_us"]) or not finite_nonnegative(row["decode_count"]):
                    return False
                if not isinstance(row["decode_count"], int):
                    return False
            previous = self.samples.get(sample["endpoint_id"])
            if previous is not None and sample["sample_seq"] <= previous["sample_seq"]:
                return False
            self.samples[sample["endpoint_id"]] = copy.deepcopy(sample)
            return True
        except (KeyError, TypeError):
            return False

    def load(self, endpoint):
        sample = self.samples.get(endpoint)
        if sample is None:
            return None
        age = self.clock.now_us() - sample["observed_at_us"]
        if not 0 <= age <= self.max_age_us:
            return None
        other = [row for owner, row in sample["by_owner"].items() if owner != self.owner]
        return sum(row["backlog_us"] for row in other), sum(row["decode_count"] for row in other), age


class CacheCatalog:
    def __init__(self, clock):
        self.clock = clock
        self.claims = []
        self.available = True

    def replace(self, claims, available=True):
        self.claims = copy.deepcopy(claims)
        self.available = available

    async def matched(self, endpoint, model_revision, tokenizer, tokens):
        await asyncio.sleep(0)
        if not self.available:
            return 0
        result = 0
        now = self.clock.now_us()
        for row in self.claims:
            if (row["endpoint_id"], row["model_revision"], row["tokenizer"]) != (endpoint, model_revision, tokenizer):
                continue
            if not row["created_at_us"] <= now <= row["expires_at_us"]:
                continue
            prefix = row["prefix"]
            if tokens[:len(prefix)] == prefix:
                result = max(result, min(len(tokens), max(0, row["matched_tokens"])))
        return result
