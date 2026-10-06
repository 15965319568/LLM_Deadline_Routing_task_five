"""Deadline policy with atomic placement and local ownership accounting."""
import asyncio
from fastapi import HTTPException
from .capacity import Capacity, Reservation
from .feedback import Feedback
from .metrics import Monitoring
from .observations import Observations, CacheCatalog
from .profiles import Surface, Tokenizers, rounded_us
from .stream import FirstToken


class DeadlineRouter:
    deadline_enabled = True
    session_key = None
    max_instance_failover_reroute_attempts = 0

    def __init__(self, configuration, clock, tokenizers=None):
        self.config, self.clock = configuration, clock
        self.profiles = {p["endpoint_id"]: p for p in configuration["endpoints"]}
        self.surfaces = {eid: Surface(p["surface"]) for eid, p in self.profiles.items()}
        self.tokenizers = tokenizers or Tokenizers()
        self.observations = Observations(clock, configuration["max_age_us"], configuration["owner"])
        self.cache = CacheCatalog(clock)
        self.capacity = Capacity()
        self.feedback = Feedback(configuration["calibration"], self.profiles)
        self.monitoring = Monitoring()
        self.lock = asyncio.Lock()
        self.parsers = {}
        self.backend_spans = {}
        self.epochs = {eid: 1 for eid in self.profiles}

    def extract_session_id(self, request, request_json):
        return None

    async def route_request(self, endpoints, engine_stats, request_stats, request, request_json):
        rid = request.state.deadline_request_id
        model = request_json["model"]
        arrival = self.clock.now_us()
        deadline = request_json.get("deadline_us")
        if not isinstance(deadline, int) or isinstance(deadline, bool):
            raise HTTPException(400, "deadline_us must be an absolute integer microsecond timestamp")
        if request_json.get("stream") is not True:
            raise HTTPException(400, "deadline-aware completions require stream=true")
        tokenized = {}
        for endpoint in endpoints:
            profile = self.profiles[endpoint.Id]
            version = profile["tokenizer"]
            if version not in tokenized:
                tokenized[version] = await self.tokenizers.encode(version, request_json.get("prompt", ""))
        async with self.lock:
            now = self.clock.now_us()
            choices = []
            for endpoint in endpoints:
                if not endpoint.healthy or endpoint.sleep:
                    continue
                profile = self.profiles[endpoint.Id]
                if not profile.get('usable', True):
                    continue
                observed = self.observations.load(endpoint.Id)
                if observed is None:
                    continue
                external_work, external_decode, _ = observed
                reserved, decoding = self.capacity.load(endpoint.Id)
                tokens = tokenized[profile["tokenizer"]]
                match = await self.cache.matched(endpoint.Id, profile["revision"], profile["tokenizer"], tokens)
                surface = self.surfaces[endpoint.Id]
                if self.config.get('require_support') and (len(tokens)-match > surface.tokens[-1] or external_decode+decoding > surface.decodes[-1]):
                    continue
                baseline = surface.estimate(len(tokens) - match, external_decode + decoding)
                work = self.feedback.factors[endpoint.Id] * baseline
                predicted = rounded_us(now + external_work + reserved + work + profile["transport_us"])
                choices.append((predicted, endpoint.Id, work, endpoint.url, baseline))
            feasible = [choice for choice in choices if choice[0] <= deadline]
            if not feasible:
                status = 429 if choices else 503
                self.monitoring.decide(rid, model, arrival, status)
                raise HTTPException(status, "No deadline-feasible endpoint" if choices else "No current load observation")
            predicted, eid, work, url, baseline = min(feasible)
            row = Reservation(rid, eid, arrival, deadline, predicted, work, model,
                              epoch=self.epochs[eid], baseline_us=baseline)
            self.capacity.reserve(row)
            self.parsers[rid] = FirstToken()
            self.monitoring.decide(rid, model, arrival, 200, eid, predicted)
            return url

    def on_headers(self, request_id, headers, status):
        self.backend_spans[request_id] = (dict(headers), status)

    def on_chunk(self, request_id, chunk):
        parser = self.parsers.get(request_id)
        headers, status = self.backend_spans.get(request_id, ({}, 500))
        if parser is None or status >= 400 or not parser.feed(chunk):
            return
        row = self.capacity.first_token(request_id, self.clock.now_us())
        if row is None:
            return
        self.monitoring.first(row)
        if row.epoch != self.epochs[row.endpoint]:
            return
        try:
            self.feedback.observe(row.endpoint, headers["x-service-sample"],
                                  row.baseline_us if self.config.get('require_support') else float(headers["x-baseline-us"]), float(headers["x-service-us"]))
        except (KeyError, ValueError, TypeError):
            pass

    def finish(self, request_id, success):
        self.capacity.finish(request_id, success)
        self.parsers.pop(request_id, None)
        self.backend_spans.pop(request_id, None)

    def diagnostics(self):
        nodes = {}
        for eid in sorted(self.profiles):
            reserved, decoding = self.capacity.load(eid)
            sample = self.observations.load(eid)
            nodes[eid] = dict(reserved_us=round(reserved, 6), decoding=decoding,
                              correction=round(self.feedback.factors[eid], 6), drift=self.feedback.status(eid),
                              age_us=sample[2] if sample else -1)
            if self.config.get('require_support'):
                nodes[eid].update(epoch=self.epochs[eid], profile_status='ready' if self.profiles[eid].get('usable', True) else 'insufficient_data')
        return dict(nodes=nodes, decisions=list(self.monitoring.decisions))

    def summary(self, start_us, end_us):
        return self.monitoring.summary(self.capacity.requests.values(), start_us, end_us)
