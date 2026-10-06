import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx

from serving_lab.fabric import FabricGateway, compile_fabric
from vllm_router.deadline import ManualClock


SOURCE = Path(__file__).resolve().parents[1] / "captures" / "fabric-7"


def _profile(tmp_path):
    compile_fabric(SOURCE, tmp_path, 100000)
    return FabricGateway(SOURCE, tmp_path, clock=ManualClock(100000))


def _body(deadline=101000, prompt="甲甲甲甲甲甲甲甲"):
    return dict(model="lab-model", prompt=prompt, max_tokens=4, deadline_us=deadline, stream=True)


class AckTransport:
    def __init__(self, mode=None, empty=False):
        self.mode = mode
        self.empty = empty
        self.calls = []

    async def _stream(self):
        if self.empty:
            yield b": heartbeat\n\ndata: {\"choices\":[{\"text\":\"\"}]}\n\n"
            yield b"data: [DONE]\n\n"
        else:
            yield ": heartbeat\n\ndata: {\"choices\":[{\"text\":\"甲\"}]}\n\n".encode()
            yield b"data: [DONE]\n\n"

    @asynccontextmanager
    async def request(self, **kwargs):
        resource = kwargs["url"].split("/")[2]
        self.calls.append((resource, kwargs["json"]))
        role = "transfer" if resource.startswith("l") else ("decode" if resource.startswith("d") else "prefill")
        headers = {"x-service-sample": resource + "-sample", "x-service-us": "10"}
        if role == "decode":
            yield SimpleNamespace(status=200, headers=headers, content=SimpleNamespace(iter_any=lambda: self._stream()))
            return
        await asyncio.sleep(0)
        ack = dict(request_id=kwargs["headers"]["X-Request-Id"], resource=resource,
                   layout="q16", kv_handle="handle:" + resource)
        if role == "transfer":
            ack["target"] = kwargs["json"]["target"]
        if self.mode == role:
            ack["request_id"] = "wrong"

        async def payload():
            return ack

        yield SimpleNamespace(status=200, headers=headers, json=payload)


def test_invalid_body_and_duplicate_request_id(tmp_path):
    gateway = _profile(tmp_path)

    async def run():
        assert (await gateway.admit("bad", {"model": "lab-model"}))[0] == 400
        assert (await gateway.admit("same", _body()))[0] == 200
        assert (await gateway.admit("same", _body()))[0] == 409

    asyncio.run(run())


def test_snapshot_conflict_and_ttl_are_not_counted(tmp_path):
    gateway = _profile(tmp_path)
    gateway.observe([dict(resource="d1", owner="foreign", boot=1, seq=1, event_us=100000,
                          ingested_us=100000, slots=1, pages=2, work_us=11)])
    assert gateway.inspect()["nodes"]["d1"]["work_us"] == 11
    gateway.observe([dict(resource="d1", owner="foreign", boot=1, seq=1, event_us=100000,
                          ingested_us=100000, slots=1, pages=3, work_us=99)])
    assert gateway.inspect()["nodes"]["d1"]["work_us"] == 0
    gateway.clock.advance_to(100171)
    assert gateway.inspect()["nodes"]["d1"]["work_us"] == 0


def test_cache_prefix_stops_at_gap_and_wrong_layout(tmp_path):
    gateway = _profile(tmp_path)
    gateway.cache([
        dict(lease_id="p0", resource="p1", layout="q16", page_index=0, tokens="甲甲甲甲",
             valid_from_us=100000, expires_us=101000, ingested_us=100000),
        dict(lease_id="p2", resource="p1", layout="q16", page_index=2, tokens="甲甲甲甲",
             valid_from_us=100000, expires_us=101000, ingested_us=100000),
        dict(lease_id="wrong", resource="p1", layout="q8", page_index=1, tokens="甲甲甲甲",
             valid_from_us=100000, expires_us=101000, ingested_us=100000),
    ])

    async def run():
        status, row = await gateway.admit("cache", _body(prompt="甲" * 12))
        assert status == 200
        assert row["cached_tokens"] == 4

    asyncio.run(run())


def test_tenant_pages_are_reserved_independently_of_gpu_slots(tmp_path):
    gateway = _profile(tmp_path)
    first = dict(_body(prompt="甲" * 12), tenant="gold", max_tokens=4)
    second = dict(_body(prompt="甲" * 12), tenant="gold", max_tokens=4)

    async def run():
        assert (await gateway.admit("gold-1", first))[0] == 200
        # The global decode has room, but gold.max_slots is two and the page budget is eight.
        assert (await gateway.admit("gold-2", second))[0] == 200
        assert (await gateway.admit("gold-3", second))[0] == 429
        assert (await gateway.admit("unknown", dict(_body(), tenant="missing")))[0] == 400

    asyncio.run(run())


def test_bad_prefill_ack_does_not_start_transfer(tmp_path):
    profile = tmp_path / "profile"
    compile_fabric(SOURCE, profile, 100000)
    transport = AckTransport(mode="prefill")
    gateway = FabricGateway(SOURCE, profile, clock=ManualClock(100000), transport=transport)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway") as client:
            reply = await client.post("/v1/completions", json=_body(), headers={"X-Request-Id": "bad-prefill"})
        assert reply.status_code == 502
        assert [name for name, _ in transport.calls] == ["p1"]
        assert gateway.inspect()["requests"]["bad-prefill"]["held"] == []

    asyncio.run(run())


def test_bad_transfer_ack_does_not_start_decode(tmp_path):
    profile = tmp_path / "profile"
    compile_fabric(SOURCE, profile, 100000)
    transport = AckTransport(mode="transfer")
    gateway = FabricGateway(SOURCE, profile, clock=ManualClock(100000), transport=transport)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway") as client:
            reply = await client.post("/v1/completions", json=_body(), headers={"X-Request-Id": "bad-transfer"})
        assert reply.status_code == 502
        assert [name for name, _ in transport.calls] == ["p1", "l11"]
        assert gateway.inspect()["requests"]["bad-transfer"]["outcome"] == "error"

    asyncio.run(run())


def test_empty_stream_is_error_and_does_not_record_ttft(tmp_path):
    profile = tmp_path / "profile"
    compile_fabric(SOURCE, profile, 100000)
    transport = AckTransport(empty=True)
    gateway = FabricGateway(SOURCE, profile, clock=ManualClock(100000), transport=transport)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway") as client:
            reply = await client.post("/v1/completions", json=_body(), headers={"X-Request-Id": "empty"})
        assert reply.status_code == 200
        assert gateway.inspect()["requests"]["empty"]["outcome"] == "error"
        assert gateway.inspect()["ttft_count"] == 0

    asyncio.run(run())


def test_reload_rejects_invalid_document_without_mutation(tmp_path):
    gateway = _profile(tmp_path)
    before = gateway.inspect()
    document = json.loads((tmp_path / "fabric-profiles.json").read_text())
    document["resources"]["p1"]["service_us"][0] = -1
    (tmp_path / "fabric-profiles.json").write_text(json.dumps(document))

    async def run():
        try:
            await gateway.reload(tmp_path)
        except ValueError:
            return
        raise AssertionError("invalid reload was accepted")

    asyncio.run(run())
    assert gateway.inspect() == before
