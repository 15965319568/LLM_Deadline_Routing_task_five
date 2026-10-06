"""CPU-capable gateway bootstrapping using production-stack's actual router."""
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
import aiohttp
from fastapi import FastAPI
from fastapi.responses import Response
from vllm_router.routers.main_router import main_router
from vllm_router.service_discovery import EndpointInfo
from vllm_router.stats.request_stats import RequestStatsMonitor
from .clock import MonotonicClock
from .router import DeadlineRouter


class Discovery:
    aliases = {}

    def __init__(self, endpoints):
        self.endpoints = endpoints

    def get_endpoint_info(self):
        return list(self.endpoints)

    def has_ever_seen_model(self, model):
        return any(model in endpoint.model_names for endpoint in self.endpoints)


class DeadlineGateway:
    def __init__(self, configuration, clock=None, transport=None, tokenizers=None):
        self.config = copy.deepcopy(configuration)
        self.clock = clock or MonotonicClock()
        self.router = DeadlineRouter(self.config, self.clock, tokenizers)
        endpoints = [EndpointInfo(url=p["url"], model_names=[p["model"]], Id=p["endpoint_id"],
                                  added_timestamp=0, model_label="deadline", sleep=p.get("draining", False),
                                  healthy=p.get("healthy", True)) for p in self.config["endpoints"]]
        self.discovery = Discovery(endpoints)
        self.session = transport

        @asynccontextmanager
        async def lifespan(app):
            async with self:
                yield

        self.app = FastAPI(lifespan=lifespan)
        self.app.include_router(main_router)
        self.app.state.router = self.router
        self.app.state.deadline_router = self.router
        self.app.state.service_discovery = self.discovery
        self.app.state.otel_enabled = False
        self.app.state.semantic_cache_available = False
        self.app.state.callbacks = None
        self.app.state.engine_stats_scraper = SimpleNamespace(get_engine_stats=lambda: {})
        monitor = object.__new__(RequestStatsMonitor)
        monitor.__init__(60)
        self.app.state.request_stats_monitor = monitor
        self.app.state.aiohttp_client_wrapper = lambda: self.session

        @self.app.get("/metrics")
        async def metrics():
            return Response(self.router.monitoring.render(self.inspect()["nodes"]), media_type="text/plain")

        @self.app.get("/deadline/diagnostics")
        async def diagnostics():
            return self.inspect()

    async def __aenter__(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
            self.owns_session = True
        else:
            self.owns_session = False
        return self

    async def __aexit__(self, *args):
        if self.owns_session:
            await self.session.close()
            self.session = None

    def observe(self, snapshots):
        return [self.router.observations.ingest(sample) for sample in snapshots]

    def cache(self, claims, available=True):
        self.router.cache.replace(claims, available)

    def state(self, endpoint_id, healthy=True, draining=False):
        endpoint = next(e for e in self.discovery.endpoints if e.Id == endpoint_id)
        endpoint.healthy, endpoint.sleep = healthy, draining

    def feedback(self, endpoint, sample_id, baseline_us, service_us):
        return self.router.feedback.observe(endpoint, sample_id, baseline_us, service_us)

    def terminal(self, request_id, success=False):
        self.router.finish(request_id, success)

    def inspect(self):
        return self.router.diagnostics()

    def summary(self, start_us, end_us):
        return self.router.summary(start_us, end_us)
