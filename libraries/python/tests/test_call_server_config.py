# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The Python listener reads the same environment configuration as TypeScript."""

from __future__ import annotations

import pytest

from standin import CallServer, StandInError

pytestmark = pytest.mark.unit


def test_environment_configures_the_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STANDIN_HOST", "127.0.0.1")
    monkeypatch.setenv("STANDIN_PORT", "9555")
    monkeypatch.setenv("STANDIN_WS_PATH", " /custom/calling/ ")
    server = CallServer(handler_factory=object, secret="local-test-secret")
    assert server._host == "127.0.0.1"
    assert server._port == 9555
    assert server.ws_path == "/custom/calling"


def test_explicit_arguments_override_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STANDIN_HOST", "wrong-host")
    monkeypatch.setenv("STANDIN_PORT", "not-a-port")
    monkeypatch.setenv("STANDIN_WS_PATH", "/wrong-path")
    server = CallServer(
        handler_factory=object,
        secret="local-test-secret",
        host="127.0.0.1",
        port=0,
        ws_path="/explicit",
    )
    assert server._host == "127.0.0.1"
    assert server._port == 0
    assert server.ws_path == "/explicit"


def test_missing_environment_keeps_existing_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("STANDIN_HOST", "STANDIN_PORT", "STANDIN_WS_PATH"):
        monkeypatch.delenv(name, raising=False)
    server = CallServer(handler_factory=object, secret="local-test-secret")
    assert server._host == "0.0.0.0"
    assert server._port == 9442
    assert server.ws_path == "/msteams/calling"


@pytest.mark.parametrize("path", ["", "/", "  /  "])
def test_environment_cannot_select_an_empty_websocket_path(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setenv("STANDIN_WS_PATH", path)
    with pytest.raises(StandInError, match="ws_path must be a real path"):
        CallServer(handler_factory=object, secret="local-test-secret")
