# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Outbound calling: the signed request, the durable store, and the guards.

The client is driven against a real local HTTP server that verifies the
signature the way StandIn does, so these assert the bytes on the wire rather
than that the code runs.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest
from aiohttp import web

from standin._hmac import SIGNATURE_V2_HEADER, TIMESTAMP_HEADER, canonical_request, sign_handshake
from standin.outbound import (
    OutboundCaller,
    OutboundError,
    OutboundPolicy,
    PendingMessage,
    PendingMessages,
)

pytestmark = pytest.mark.unit

SECRET = "outbound-secret-never-a-real-one"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeStandIn:
    """Verifies v2 the way the service does, and records what arrived."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        self.payload: dict = {"callId": "call-out-1", "scenarioId": "scenario-9"}

    async def handle(self, request: web.Request) -> web.Response:
        raw = await request.read()
        timestamp = request.headers.get(TIMESTAMP_HEADER, "")
        signature = request.headers.get(SIGNATURE_V2_HEADER, "")
        expected = sign_handshake(
            SECRET, timestamp, canonical_request(request.method, request.path, raw)
        )
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "body": raw.decode() or "",
                "signed": signature == expected,
                "v1": request.headers.get("X-StandIn-Signature", ""),
            }
        )
        if signature != expected:
            return web.Response(status=401, text="bad signature")
        if self.status >= 400:
            return web.Response(status=self.status, text="nope")
        return web.json_response(self.payload)


@pytest.fixture
async def standin():
    fake = FakeStandIn()
    app = web.Application()
    app.router.add_route("*", "/api/calls", fake.handle)
    app.router.add_route("*", "/api/calls/{call_id}", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    fake.url = f"http://127.0.0.1:{port}"
    try:
        yield fake
    finally:
        await runner.cleanup()


def _caller(standin) -> OutboundCaller:
    return OutboundCaller(secret=SECRET, worker_url=standin.url)


# ------------------------------------------------------------------ the wire


async def test_place_call_signs_the_exact_request(standin):
    """v2 binds method, path and a hash of the body, which is what puts the
    tenant under the signature."""
    placed = await _caller(standin).place_call(user_object_id="aad-1", tenant_id="tenant-A")
    assert placed.call_id == "call-out-1"
    assert placed.scenario_id == "scenario-9"

    sent = standin.requests[0]
    assert sent["signed"] is True
    assert sent["method"] == "POST"
    assert sent["path"] == "/api/calls"
    assert json.loads(sent["body"]) == {"userObjectId": "aad-1", "tenantId": "tenant-A"}


async def test_no_v1_signature_is_sent(standin):
    """Sending v1 alongside v2 would let a downgrade pick the weaker one, which
    leaves the tenant, and so the organisation being rung, unsigned."""
    await _caller(standin).place_call(user_object_id="aad-1", tenant_id="tenant-A")
    assert standin.requests[0]["v1"] == ""


async def test_cancel_signs_the_call_id_into_the_path(standin):
    assert await _caller(standin).cancel_call("call-out-1") is True
    sent = standin.requests[0]
    assert sent["method"] == "DELETE"
    assert sent["path"] == "/api/calls/call-out-1"
    assert sent["signed"] is True


async def test_a_rejected_signature_says_what_to_check(standin):
    """A wrong path reads exactly like a wrong secret, so the message names both."""
    caller = OutboundCaller(secret="the-wrong-secret", worker_url=standin.url)
    with pytest.raises(OutboundError, match="rejected the signature"):
        await caller.place_call(user_object_id="aad-1", tenant_id="tenant-A")


async def test_an_error_status_is_reported_not_swallowed(standin):
    standin.status = 503
    with pytest.raises(OutboundError, match="503"):
        await _caller(standin).place_call(user_object_id="aad-1", tenant_id="tenant-A")


async def test_a_response_with_no_call_id_is_an_error(standin):
    standin.payload = {"scenarioId": "s"}
    with pytest.raises(OutboundError, match="no callId"):
        await _caller(standin).place_call(user_object_id="aad-1", tenant_id="tenant-A")


async def test_cancel_never_raises(standin):
    """It runs on the no-answer path, where an exception turns a tidy-up into a
    failure and the caller has already stopped waiting."""
    standin.status = 500
    assert await _caller(standin).cancel_call("call-out-1") is False
    assert await _caller(standin).cancel_call("") is False


async def test_an_unreachable_worker_names_the_url():
    caller = OutboundCaller(
        secret=SECRET, worker_url=f"http://127.0.0.1:{_free_port()}", timeout_s=2
    )
    with pytest.raises(OutboundError, match="could not reach"):
        await caller.place_call(user_object_id="aad-1", tenant_id="tenant-A")


@pytest.mark.parametrize("url", ["ftp://host/x", "http://user:pw@host", "not a url", "https://"])
def test_a_bad_worker_url_is_refused_at_construction(url):
    with pytest.raises(OutboundError):
        OutboundCaller(secret=SECRET, worker_url=url)


def test_a_missing_secret_is_refused_at_construction(monkeypatch):
    monkeypatch.delenv("STANDIN_SECRET", raising=False)
    with pytest.raises(OutboundError, match="STANDIN_SECRET"):
        OutboundCaller(worker_url="http://127.0.0.1:9440")


# --------------------------------------------------------------- the store


def _store(tmp_path) -> PendingMessages:
    return PendingMessages(tmp_path / "outbound")


def test_a_parked_message_survives_a_new_process(tmp_path):
    """The answering leg is a different call and may be a different process. In
    memory, a restart between the two loses it and the callee hears silence."""
    _store(tmp_path).park(PendingMessage(call_id="c1", text="Your build finished."))
    recovered = _store(tmp_path).pop("c1")
    assert recovered is not None
    assert recovered.text == "Your build finished."


def test_a_message_can_only_be_popped_once(tmp_path):
    """Two workers answering the same leg is normal. Only one may speak."""
    store = _store(tmp_path)
    store.park(PendingMessage(call_id="c1", text="once"))
    assert store.pop("c1") is not None
    assert store.pop("c1") is None


def test_popping_an_unknown_call_is_not_an_error(tmp_path):
    assert _store(tmp_path).pop("never-parked") is None


def test_the_thread_it_came_from_is_remembered(tmp_path):
    """Without it, an unanswered call has nowhere to put the answer."""
    store = _store(tmp_path)
    store.park(
        PendingMessage(
            call_id="c1", text="hi", thread_id="19:meeting@thread.v2", requested_by="aad-9"
        )
    )
    got = store.pop("c1")
    assert got.thread_id == "19:meeting@thread.v2"
    assert got.requested_by == "aad-9"


def test_a_call_id_cannot_escape_the_store_directory(tmp_path):
    store = _store(tmp_path)
    store.park(PendingMessage(call_id="../../etc/passwd", text="nope"))
    written = list((tmp_path / "outbound").glob("*.json"))
    assert len(written) == 1
    assert written[0].parent == tmp_path / "outbound"


def test_stale_messages_are_claimed_for_the_no_answer_path(tmp_path):
    store = _store(tmp_path)
    store.park(PendingMessage(call_id="old", text="nobody answered", created_ms=1))
    store.park(PendingMessage(call_id="new", text="still ringing"))

    taken = store.claim_stale(older_than_s=1)
    assert [m.call_id for m in taken] == ["old"]
    # Claimed exactly once, so a second sweep cannot post it twice.
    assert store.claim_stale(older_than_s=1) == []
    assert store.pop("new") is not None


def test_an_orphaned_claim_is_judged_by_the_claim_not_the_record(tmp_path):
    """A record is claimed BECAUSE it is already old. Judging a half-finished
    claim by the record's age would let a second sweep take a message the first
    is still delivering, and post it twice."""
    store = _store(tmp_path)
    store.park(PendingMessage(call_id="old", text="in flight", created_ms=1))
    store.claim_stale(older_than_s=1)

    # The claim was made just now, so a fresh sweep must leave it alone even
    # though the record it holds is ancient.
    assert store.recover_orphans(older_than_s=60) == []


# --------------------------------------------------------------- the policy


def test_outbound_is_off_until_somebody_is_allowed():
    """Inbound, the caller chose to dial. Outbound, a model was talked into it."""
    with pytest.raises(OutboundError, match="outbound calling is off"):
        OutboundPolicy().check("aad-1")


def test_an_inbound_allow_all_does_not_allow_an_outbound_target():
    policy = OutboundPolicy(allowed=frozenset({"aad-1"}))
    policy.check("aad-1")
    with pytest.raises(OutboundError, match="not on this agent's outbound allowlist"):
        policy.check("aad-2")


def test_the_hourly_cap_counts_placed_calls():
    policy = OutboundPolicy(allowed=frozenset({"aad-1"}), max_per_hour=2)
    for _ in range(2):
        policy.check("aad-1")
        policy.record()
    with pytest.raises(OutboundError, match="already placed 2 calls"):
        policy.check("aad-1")


def test_a_zero_cap_is_no_cap():
    policy = OutboundPolicy(allowed=frozenset({"aad-1"}), max_per_hour=0)
    for _ in range(50):
        policy.check("aad-1")
        policy.record()


def test_policy_from_env_is_off_unless_set(monkeypatch):
    monkeypatch.delenv("STANDIN_OUTBOUND_ALLOW", raising=False)
    assert OutboundPolicy.from_env().allowed == frozenset()

    monkeypatch.setenv("STANDIN_OUTBOUND_ALLOW", " aad-1 , aad-2 ,, ")
    monkeypatch.setenv("STANDIN_OUTBOUND_MAX_PER_HOUR", "3")
    policy = OutboundPolicy.from_env()
    assert policy.allowed == frozenset({"aad-1", "aad-2"})
    assert policy.max_per_hour == 3


async def test_the_whole_path_end_to_end(standin, tmp_path):
    """Ask, park, answer, speak: the shape every plugin wires."""
    policy = OutboundPolicy(allowed=frozenset({"aad-1"}))
    store = _store(tmp_path)

    policy.check("aad-1")
    placed = await _caller(standin).place_call(user_object_id="aad-1", tenant_id="tenant-A")
    policy.record()
    store.park(
        PendingMessage(call_id=placed.call_id, text="Your build finished.", thread_id="19:chat")
    )

    # Minutes later, a different call arrives with direction="outbound".
    spoken = store.pop(placed.call_id)
    assert spoken is not None
    assert spoken.text == "Your build finished."
    await asyncio.sleep(0)


# ------------------------------------------------------- the outcome route


async def _outcome_server(seen, *, secret=SECRET):
    from standin import CallServer

    server = CallServer(
        handler_factory=lambda: object(),
        secret=secret,
        host="127.0.0.1",
        port=_free_port(),
        on_call_outcome=lambda call_id, outcome: seen.append((call_id, outcome)),
    )
    await server.start()
    return server


async def _post_outcome(server, call_id, body, *, secret=SECRET, timestamp=None):
    import aiohttp as _aiohttp

    from standin._hmac import now_ms as _now

    path = f"{server.ws_path}/outcome/{call_id}"
    stamp = str(timestamp if timestamp is not None else _now())
    raw = body.encode()
    headers = {
        TIMESTAMP_HEADER: stamp,
        SIGNATURE_V2_HEADER: sign_handshake(secret, stamp, canonical_request("POST", path, raw)),
    }
    url = f"http://127.0.0.1:{server._port}{path}"
    async with _aiohttp.ClientSession() as http:
        async with http.post(url, data=raw, headers=headers) as response:
            return response.status


async def test_a_signed_outcome_reaches_the_plugin():
    """The only signal that nobody answered. Without it an unanswered call waits
    out the ring timeout before anything can be said about it."""
    seen: list[tuple[str, str]] = []
    server = await _outcome_server(seen)
    try:
        status = await _post_outcome(server, "call-out-1", '{"outcome":"no-answer"}')
        assert status == 204
        assert seen == [("call-out-1", "no-answer")]
    finally:
        await server.aclose()


async def test_an_unsigned_outcome_is_refused():
    seen: list[tuple[str, str]] = []
    server = await _outcome_server(seen)
    try:
        assert await _post_outcome(server, "c", "{}", secret="the-wrong-secret") == 401
        assert seen == []
    finally:
        await server.aclose()


async def test_a_stale_outcome_is_refused():
    """The signature is only worth anything inside the replay window."""
    seen: list[tuple[str, str]] = []
    server = await _outcome_server(seen)
    try:
        from standin._hmac import now_ms as _now

        assert await _post_outcome(server, "c", "{}", timestamp=_now() - 10_000_000) == 401
        assert seen == []
    finally:
        await server.aclose()


async def test_an_oversized_outcome_is_refused_before_it_is_read():
    """The body must be read before a signature over its hash can be checked,
    so the cap is what bounds an unauthenticated peer."""
    seen: list[tuple[str, str]] = []
    server = await _outcome_server(seen)
    try:
        assert await _post_outcome(server, "c", "x" * (9 * 1024)) == 413
        assert seen == []
    finally:
        await server.aclose()


async def test_no_outcome_route_exists_unless_a_plugin_asks_for_one():
    """A worker that never places a call opens no extra surface."""
    import aiohttp as _aiohttp

    from standin import CallServer

    server = CallServer(
        handler_factory=lambda: object(),
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
    )
    await server.start()
    try:
        url = f"http://127.0.0.1:{server._port}{server.ws_path}/outcome/c"
        async with _aiohttp.ClientSession() as http:
            async with http.post(url, data=b"{}") as response:
                assert response.status == 404
    finally:
        await server.aclose()


async def test_a_plugin_that_raises_on_an_outcome_still_acknowledges():
    """Otherwise StandIn retries a report the worker will never accept."""
    from standin import CallServer

    def explode(call_id: str, outcome: str) -> None:
        raise RuntimeError("the plugin is broken")

    server = CallServer(
        handler_factory=lambda: object(),
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        on_call_outcome=explode,
    )
    await server.start()
    try:
        assert await _post_outcome(server, "c", '{"outcome":"no-answer"}') == 204
    finally:
        await server.aclose()
