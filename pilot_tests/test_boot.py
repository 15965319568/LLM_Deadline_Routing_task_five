"""Public boot/compatibility checks; not a substitute for the product contract."""
import json
from pathlib import Path
from types import SimpleNamespace
from vllm_router.deadline import DeadlineGateway, ManualClock
from vllm_router.routers.routing_logic import RoundRobinRouter


def test_can_boot_cpu_gateway():
    scenario = json.loads(Path('workloads/mixed-load.json').read_text())
    gateway = DeadlineGateway(scenario['config'], ManualClock(), transport=object())
    assert len(gateway.discovery.get_endpoint_info()) == 3
    assert any(route.path == '/v1/completions' for route in gateway.app.routes)


def test_round_robin_is_available():
    router = RoundRobinRouter()
    endpoints = [SimpleNamespace(url='http://second'), SimpleNamespace(url='http://first')]
    assert router.route_request(endpoints, {}, {}, None) in [e.url for e in endpoints]
