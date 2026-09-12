# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The LiveKit plugin's own behaviour, with no LiveKit server.

Everything here is reachable without a room: config validation, the room-name
contract, job-metadata parsing, and the context queue the plugin owns because
the SDK deliberately does not. Anything that needs a live room belongs in a
live-service test, kept out of this suite, so a fork PR still passes CI
with no credentials.
"""

from __future__ import annotations

import json
import re

import pytest

from standin import CallHandler, StandInError

pytestmark = pytest.mark.unit

# livekit-agents is the [livekit] extra, not a base dependency, so this module
# is genuinely absent on a base install. PluginNotInstalled subclasses
# ImportError precisely so importorskip reads it as one.
livekit_plugin = pytest.importorskip(
    "standin.plugins.livekit", reason='needs pip install "standin-sdk[livekit]"'
)

LK_ENV = {
    "LIVEKIT_URL": "ws://localhost:7880",
    "LIVEKIT_API_KEY": "devkey",
    "LIVEKIT_API_SECRET": "secret-not-a-real-one",
}


@pytest.fixture(autouse=True)
def _livekit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in LK_ENV.items():
        monkeypatch.setenv(k, v)


def _handler(**kwargs):
    return livekit_plugin.TeamsCallHandler(agent_name="standin-msteams", **kwargs)


# ---------------------------------------------------------------- the seam


def test_handler_satisfies_the_sdk_seam() -> None:
    """The handler is a CallHandler and nothing more - no base class, no ABC."""
    h = _handler()
    assert isinstance(h, CallHandler)
    for method in ("on_start", "on_caller_audio", "on_context", "on_goodbye", "aclose"):
        assert callable(getattr(h, method)), method
    # It inherits from nothing but object: the seam is structural.
    assert livekit_plugin.TeamsCallHandler.__mro__[1:] == (object,)


# ---------------------------------------------------------- config guardrails


@pytest.mark.parametrize("missing", sorted(LK_ENV))
def test_missing_livekit_credentials_fail_at_construction(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """A misconfigured project must fail at worker startup with a clear message,
    not on the first real call with a caller already on the line."""
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(StandInError) as err:
        _handler()
    assert missing in str(err.value)


def test_explicit_arguments_beat_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in LK_ENV:
        monkeypatch.delenv(k, raising=False)
    h = _handler(
        livekit_url="wss://explicit.example",
        livekit_api_key="k",
        livekit_api_secret="s",
    )
    assert h is not None


# ------------------------------------------------------- the room-name contract


def test_room_name_is_the_shipped_contract() -> None:
    """Both SDKs derive the same name for the same call, so a room
    created by either is the same room."""
    assert livekit_plugin.handler.room_name_for("msteams-", "call-123") == "msteams-call-123"


def test_room_name_sanitises_a_hostile_call_id() -> None:
    """callId arrives as a DECODED url segment, so it can carry anything a
    %-escape can smuggle."""
    got = livekit_plugin.handler.room_name_for("msteams-", "a/b?c=d&e#f 🙂")
    assert got == "msteams-a-b-c-d-e-f--"
    assert "/" not in got and "?" not in got and " " not in got


def test_room_name_is_bounded_at_100_chars() -> None:
    got = livekit_plugin.handler.room_name_for("msteams-", "x" * 500)
    assert len(got) == 100
    assert got.startswith("msteams-xxx")


def test_room_prefix_is_configurable() -> None:
    assert livekit_plugin.handler.room_name_for("teams-", "c1") == "teams-c1"


# --------------------------------------------------------------- CallInfo


class _Job:
    def __init__(self, metadata: str) -> None:
        self.metadata = metadata


class _Ctx:
    def __init__(self, metadata: str) -> None:
        self.job = _Job(metadata)


def test_call_info_reads_dispatch_metadata() -> None:
    ctx = _Ctx(
        json.dumps(
            {
                "source": "msteams",
                "caller_name": "Alaa",
                "tenant_id": "tenant-1",
                "call_id": "call-1",
                "thread_id": "19:meeting@thread.v2",
                "user_id": "aad-1",
                "call_direction": "outbound",
            }
        )
    )
    info = livekit_plugin.CallInfo.from_job(ctx)
    assert info.is_teams_call
    assert info.caller_name == "Alaa"
    assert info.tenant_id == "tenant-1"
    assert info.call_id == "call-1"
    assert info.thread_id == "19:meeting@thread.v2"
    assert info.user_id == "aad-1"
    assert info.direction == "outbound"


def test_a_number_an_operator_typed_wrong_fails_loud(monkeypatch) -> None:
    """Substituting the default means the setting they are looking at is not
    the one in force, and silence is what makes that take an afternoon."""
    from standin import StandInError

    for bad in ("twelve", "-5", "0", "12.5"):
        monkeypatch.setenv("LIVEKIT_TILE_VIDEO_FPS", bad)
        with pytest.raises(StandInError, match="whole number above zero"):
            livekit_plugin.TeamsCallHandler(
                livekit_url="wss://x", livekit_api_key="k", livekit_api_secret="s"
            )


def test_an_unset_number_still_takes_the_default(monkeypatch) -> None:
    monkeypatch.delenv("LIVEKIT_TILE_VIDEO_FPS", raising=False)
    handler = livekit_plugin.TeamsCallHandler(
        livekit_url="wss://x", livekit_api_key="k", livekit_api_secret="s"
    )
    assert handler.tile_video_fps == 12


@pytest.mark.parametrize(
    ("value", "on", "identity"),
    [
        ("", True, ""),
        ("auto", True, ""),
        ("off", False, ""),
        ("avatar-worker-1", True, "avatar-worker-1"),
    ],
)
def test_the_tile_relay_can_be_pinned_to_one_participant(
    monkeypatch, value: str, on: bool, identity: str
) -> None:
    """Without a name the relay takes whichever participant published first,
    which on a busy room is the wrong one."""
    monkeypatch.setenv("LIVEKIT_TILE_VIDEO", value)
    handler = livekit_plugin.TeamsCallHandler(
        livekit_url="wss://x", livekit_api_key="k", livekit_api_secret="s"
    )
    assert handler.tile_video is on
    assert handler.tile_video_identity == identity


def test_the_typescript_plugin_dispatches_the_keys_this_one_reads() -> None:
    """The two SDKs dispatch into the same LiveKit rooms, and a job carrying
    metadata in the other shape reads as "not a Microsoft Teams call" here: the
    agent sees no caller at all, with nothing logged. Read from the TypeScript
    source so the two cannot drift apart again without this failing."""
    from pathlib import Path

    handler_ts = (
        Path(__file__).resolve().parents[2]
        / "typescript"
        / "src"
        / "plugins"
        / "livekit"
        / "handler.ts"
    ).read_text()
    emitted = set(re.findall(r"metadata\.([a-z_]+)\s*=", handler_ts))
    emitted |= set(re.findall(r"^\s+([a-z_]+):\s", handler_ts, re.M))

    read_here = {"caller_name", "tenant_id", "call_id", "thread_id", "user_id", "call_direction"}
    missing = read_here - emitted
    assert not missing, f"the TypeScript plugin never sets {sorted(missing)}"


def test_a_bare_string_on_a_topic_is_dropped() -> None:
    """Both topics carry a JSON object with a "text" string. A bare string is
    what the TypeScript plugin used to publish, and this is the silence it
    produced: every context sentence and the goodbye reaching nothing."""
    seen: list[str] = []
    call = livekit_plugin.TeamsCall(on_context=seen.append)

    class _Packet:
        def __init__(self, topic: str, data: bytes) -> None:
            self.topic = topic
            self.data = data

    call._on_data(_Packet(livekit_plugin.TOPIC_CONTEXT, b"a bare string"))
    assert seen == []

    call._on_data(
        _Packet(livekit_plugin.TOPIC_CONTEXT, json.dumps({"text": "three people"}).encode())
    )
    assert seen == ["three people"]


@pytest.mark.parametrize(
    "metadata",
    [
        "",
        "not json",
        "[1,2,3]",
        '"a string"',
        "{}",
        json.dumps({"source": "sip"}),
        json.dumps({"source": "msteams-lookalike"}),
    ],
)
def test_call_info_never_raises_on_a_foreign_job(metadata: str) -> None:
    """A worker that also serves web or SIP rooms must read those as 'not a
    Microsoft Teams call' rather than taking the worker down."""
    info = livekit_plugin.CallInfo.from_job(_Ctx(metadata))
    assert info.is_teams_call is False
    assert info.caller_name == ""


def test_call_info_tolerates_no_context_at_all() -> None:
    assert livekit_plugin.CallInfo.from_job(None).is_teams_call is False


def test_call_info_ignores_wrongly_typed_fields() -> None:
    info = livekit_plugin.CallInfo.from_job(
        _Ctx(json.dumps({"source": "msteams", "caller_name": 7, "call_id": None}))
    )
    assert info.is_teams_call
    assert info.caller_name == ""
    assert info.call_id == ""


def test_call_info_direction_defaults_to_inbound() -> None:
    info = livekit_plugin.CallInfo.from_job(_Ctx(json.dumps({"source": "msteams"})))
    assert info.direction == "inbound"


# ------------------------------------------------------------- context queue


async def test_context_is_queued_until_the_agent_binds() -> None:
    """A LiveKit data packet reaches only participants connected at that
    instant, and the first `participants` message arrives seconds before the
    dispatched agent joins. The SDK does not queue, so the plugin must."""
    h = _handler()
    await h.on_context("first")
    await h.on_context("second")
    assert [text for _, text in h._pending_context] == ["first", "second"]


async def test_pending_context_is_bounded() -> None:
    """A caller can press DTMF faster than an agent joins; an unbounded queue
    would grow for the life of the call and then flush a wall of stale text."""
    h = _handler()
    for i in range(50):
        await h.on_context(f"ctx-{i}")
    assert len(h._pending_context) == 16
    # The NEWEST are kept: stale participant counts are worse than none.
    assert [t for _, t in h._pending_context] == [f"ctx-{i}" for i in range(34, 50)]


async def test_context_is_not_queued_once_the_agent_is_bound() -> None:
    h = _handler()
    h._agent_identity = "agent-1"  # what track_subscribed sets
    await h.on_context("live")
    assert h._pending_context == []


async def test_goodbye_is_never_queued() -> None:
    """Teardown follows a goodbye within seconds. A goodbye with no agent to
    hear it was never going to be spoken."""
    h = _handler()
    await h.on_goodbye("Goodbye now.")
    assert h._pending_context == []


# ------------------------------------------------------------ safe by default


async def test_callbacks_are_inert_before_on_start() -> None:
    """The SDK can deliver context before session.start on a fast dial, and
    cancels a call that never starts. Nothing here may raise."""
    h = _handler()
    await h.on_caller_audio(b"\x01\x02" * 160)
    await h.on_context("early")
    await h.on_goodbye("early goodbye")
    await h.aclose("pre-start-timeout")


async def test_aclose_is_safe_twice() -> None:
    h = _handler()
    await h.aclose("first")
    await h.aclose("second")


async def test_audio_after_close_is_dropped() -> None:
    h = _handler()
    await h.aclose("call-ended")
    await h.on_caller_audio(b"\x01\x02" * 160)  # must not raise


async def test_the_callers_first_words_survive_the_room_join():
    """Joining a room takes a moment, and what the caller says in that moment is
    usually why they rang. This plugin used to drop it."""
    from standin.plugins.livekit.handler import TeamsCallHandler

    handler = TeamsCallHandler(
        agent_name="a",
        livekit_url="wss://x",
        livekit_api_key="k",
        livekit_api_secret="s",
    )
    await handler.on_caller_audio(b"\x01\x02" * 160)
    assert handler._pending_audio.holding is True

    published: list[bytes] = []
    released = await handler._pending_audio.release(send_audio=published.append)
    assert released == (1, 0)
    assert published == [b"\x01\x02" * 160]
