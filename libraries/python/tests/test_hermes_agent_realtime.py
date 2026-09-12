# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Exercise the actual provider event loop without connecting to a provider."""

from __future__ import annotations

import asyncio
import json

import pytest

from standin.plugins.hermes_agent.realtime import RealtimeConfig, RealtimeSession

pytestmark = pytest.mark.unit


class _Socket:
    def __init__(self):
        self.closed = False
        self.messages = []

    async def send_str(self, message):
        self.messages.append(json.loads(message))

    async def close(self):
        self.closed = True


def _tool_response(*ids):
    return {
        "type": "response.done",
        "response": {
            "output": [
                {"type": "function_call", "name": "lookup", "call_id": call_id, "arguments": "{}"}
                for call_id in ids
            ]
        },
    }


async def test_all_tool_results_arrive_before_the_next_model_response():
    rt = RealtimeSession(RealtimeConfig(api_key="test-only"))
    socket = _Socket()
    rt._ws = socket
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    async def tool(name, call_id, arguments):
        if call_id == "second":
            second_started.set()
            await release_second.wait()
        await rt.send_function_result(call_id, f"result for {call_id}")

    rt.on_function_call = tool
    try:
        await rt._dispatch(_tool_response("first", "second"))
        await asyncio.wait_for(second_started.wait(), timeout=1)
        assert [message["type"] for message in socket.messages] == ["conversation.item.create"]
        assert socket.messages[0]["item"]["call_id"] == "first"
        release_second.set()
        await asyncio.gather(*rt._tool_tasks)
        assert [message["type"] for message in socket.messages] == [
            "conversation.item.create",
            "conversation.item.create",
            "response.create",
        ]
        assert socket.messages[1]["item"]["call_id"] == "second"
    finally:
        release_second.set()
        await rt.close()


async def test_one_tool_still_resumes_the_model_after_its_result():
    rt = RealtimeSession(RealtimeConfig(api_key="test-only"))
    socket = _Socket()
    rt._ws = socket

    async def tool(name, call_id, arguments):
        await rt.send_function_result(call_id, "answer")

    rt.on_function_call = tool
    try:
        await rt._dispatch(_tool_response("one"))
        await asyncio.gather(*rt._tool_tasks)
        assert [message["type"] for message in socket.messages] == [
            "conversation.item.create",
            "response.create",
        ]
    finally:
        await rt.close()


async def test_closing_during_a_tool_batch_does_not_start_another_response():
    rt = RealtimeSession(RealtimeConfig(api_key="test-only"))
    socket = _Socket()
    rt._ws = socket
    started = asyncio.Event()

    async def tool(name, call_id, arguments):
        started.set()
        await asyncio.Event().wait()

    rt.on_function_call = tool
    await rt._dispatch(_tool_response("one", "two"))
    await asyncio.wait_for(started.wait(), timeout=1)
    await rt.close()
    assert socket.closed
    assert not socket.messages
    assert not rt._tool_tasks
