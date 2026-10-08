import asyncio
import hashlib
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


def _lease(lease_id, resource="p1", layout="q16", page_index=0, tokens="甲甲甲甲", session_id="", **kwargs):
    digest = hashlib.sha256(f"{layout}|{session_id}|{page_index}|{tokens}".encode()).hexdigest()
    row = dict(lease_id=lease_id, resource=resource, layout=layout, page_index=page_index,
               tokens=tokens, session_id=session_id, producer="kv-cache-a", generation=1,
               page_hash=digest, valid_from_us=100000, expires_us=101000,
               ingested_us=100000)
    row.update(kwargs)
    return row


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
        request_id = {k.lower(): v for k, v in kwargs["headers"].items()}["x-request-id"]
        ack = dict(request_id=request_id, resource=resource,
                   layout="q16", kv_handle=f"{request_id}:{resource}:test")
        if role == "prefill":
            ack["cached_tokens"] = kwargs["json"]["cached_tokens"]
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
        _lease("p0"),
        _lease("p2", page_index=2),
        _lease("wrong", resource="p1", layout="q8", page_index=1),
        _lease("p2-0", resource="p2"),
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
        assert len(transport.calls) == 1 and transport.calls[0][0].startswith("p")
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
        assert len(transport.calls) == 2 and transport.calls[0][0].startswith("p") and transport.calls[1][0].startswith("l")
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


def test_session_namespaces_and_priority_are_admission_inputs(tmp_path):
    gateway = _profile(tmp_path)
    gateway.cache([
        _lease("alpha-page", session_id="alpha"),
        _lease("plain-page"),
    ])

    async def run():
        alpha = dict(_body(prompt="甲" * 8), session_id="alpha", priority=2)
        beta = dict(_body(prompt="甲" * 8), session_id="beta", priority=0)
        assert (await gateway.admit("alpha", alpha))[0] == 200
        assert gateway.decisions["alpha"]["cached_tokens"] == 4
        assert (await gateway.admit("beta", beta))[0] == 200
        assert gateway.decisions["beta"]["cached_tokens"] == 0
        assert (await gateway.admit("bad-priority", dict(_body(), tenant="batch", priority=2)))[0] == 400

    asyncio.run(run())


def test_draining_snapshot_blocks_new_path_but_keeps_capacity(tmp_path):
    gateway = _profile(tmp_path)
    gateway.observe([dict(resource="p1", owner="foreign", boot=1, seq=1, event_us=100000,
                          ingested_us=100000, slots=0, pages=0, work_us=0,
                          healthy=True, draining=True, capacity_epoch=2)])

    async def run():
        status, row = await gateway.admit("draining", _body(prompt="甲" * 8))
        assert status == 200 and row["path"][0] != "p1"
        assert gateway.inspect()["nodes"]["p1"]["slots"] == 0

    asyncio.run(run())


def test_stream_requires_done_marker(tmp_path):
    profile = tmp_path / "profile"
    compile_fabric(SOURCE, profile, 100000)
    transport = AckTransport()
    original = transport._stream

    async def no_done():
        async for chunk in original():
            if b"[DONE]" not in chunk:
                yield chunk

    transport._stream = no_done
    gateway = FabricGateway(SOURCE, profile, clock=ManualClock(100000), transport=transport)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway") as client:
            reply = await client.post("/v1/completions", json=_body(), headers={"X-Request-Id": "no-done"})
        assert reply.status_code == 200
        assert gateway.inspect()["requests"]["no-done"]["outcome"] == "error"

    asyncio.run(run())


def test_tenant_start_window_and_priority_charge(tmp_path):
    gateway = _profile(tmp_path)
    body = dict(_body(prompt="甲" * 8), tenant="gold", priority=2)

    async def run():
        for index in range(1, 5):
            assert (await gateway.admit(f"window-{index}", body))[0] == 200
            gateway.finish(f"window-{index}", "cancelled")
        assert (await gateway.admit("window-5", body))[0] == 429
        gateway.clock.advance_to(100501)
        assert (await gateway.admit("window-6", body))[0] == 200

    asyncio.run(run())


def test_transfer_fault_quarantines_sticky_route(tmp_path):
    profile = tmp_path / "profile"
    compile_fabric(SOURCE, profile, 100000)
    transport = AckTransport(mode="transfer")
    gateway = FabricGateway(SOURCE, profile, clock=ManualClock(100000), transport=transport)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway") as client:
            reply = await client.post("/v1/completions", json=_body(), headers={"X-Request-Id": "fault"})
        assert reply.status_code == 502
        path = tuple(gateway.decisions["fault"]["path"])
        assert "/".join(path) in gateway.inspect()["quarantined_paths"]
        status, row = await gateway.admit("retry", dict(_body(), session_id="sticky"))
        assert status == 200
        sticky_path = tuple(row["path"])
        gateway.sessions["sticky"] = (sticky_path, gateway.clock.now_us() + 1000)
        gateway.quarantine[sticky_path] = gateway.clock.now_us() + 100
        assert (await gateway.admit("retry-2", dict(_body(), session_id="sticky")))[0] == 429

    asyncio.run(run())


def test_revoked_lease_is_visible_only_after_tombstone_time(tmp_path):
    gateway = _profile(tmp_path)
    lease = _lease("fresh", session_id="gamma", expires_us=103000, valid_from_us=100000)
    tombstone = dict(lease, revoked_us=100200, ingested_us=100210)
    gateway.cache([lease, tombstone])

    async def run():
        before, row = await gateway.admit("before-revoke", dict(_body(prompt="甲" * 8), session_id="gamma"))
        assert before == 200 and row["cached_tokens"] == 4
        gateway.finish("before-revoke", "cancelled")
        gateway.clock.advance_to(100210)
        after, row = await gateway.admit("after-revoke", dict(_body(prompt="甲" * 8), session_id="gamma"))
        assert after == 200 and row["cached_tokens"] == 0

    asyncio.run(run())


def test_stream_tail_after_done_is_error_and_releases_route(tmp_path):
    profile = tmp_path / "profile"
    compile_fabric(SOURCE, profile, 100000)
    transport = AckTransport()
    original = transport._stream

    async def tail():
        async for chunk in original():
            yield chunk
        yield b"data: {\"choices\":[{\"text\":\"late\"}]}\n\n"

    transport._stream = tail
    gateway = FabricGateway(SOURCE, profile, clock=ManualClock(100000), transport=transport)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway") as client:
            reply = await client.post("/v1/completions", json=_body(), headers={"X-Request-Id": "tail"})
        assert reply.status_code == 200
        assert gateway.inspect()["requests"]["tail"]["outcome"] == "error"
        assert gateway.inspect()["quarantined_paths"]

    asyncio.run(run())


def test_ack_handle_must_be_request_and_resource_scoped(tmp_path):
    profile = tmp_path / "profile"
    compile_fabric(SOURCE, profile, 100000)
    transport = AckTransport()
    gateway = FabricGateway(SOURCE, profile, clock=ManualClock(100000), transport=transport)

    async def run():
        original = transport.request

        @asynccontextmanager
        async def forged(**kwargs):
            async with original(**kwargs) as response:
                if kwargs["url"].endswith("/v1/prefill"):
                    payload = await response.json()
                    payload["kv_handle"] = "other:p1:forged"
                    async def body():
                        return payload
                    response.json = body
                yield response

        transport.request = forged
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway") as client:
            reply = await client.post("/v1/completions", json=_body(), headers={"X-Request-Id": "scoped"})
        assert reply.status_code == 502
        assert len(transport.calls) == 1

    asyncio.run(run())
