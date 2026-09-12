# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The bridge as a platform the host can connect, disconnect and send through.

``hermes msteams-bridge serve`` runs the listener in the foreground, which is
right for a container and wrong for a host that already has a process, a
lifecycle and a place in its own UI for "is this connected?".

So this is the same listener, owned by the host instead. It binds when the host
connects, drops when the host disconnects, and gives the host one way to send:
say it on the call this person is already on, or ring them and say it when they
answer. :class:`standin.VoiceDelivery` decides which, so the rules about who may
be rung and how often are the same ones the rest of the SDK enforces.

Everything host-shaped is in this file. The listener, the wire and the delivery
policy are all the SDK's, and every host import goes through :mod:`.api`, which
is the one enforced boundary in this package.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from typing import Any

from standin import CallServer, InboundMessage, LiveCalls, PendingMessages, VoiceDelivery
from standin.outbound import OutboundCaller, OutboundPolicy

from .config import PluginConfig, resolve_config
from .handler import LIVE_CALLS
from .log import logger
from .service import handler_factory

__all__ = ["MicrosoftTeamsPlatform", "register_platform"]

#: The environment names the host's own authorization layer reads.
#:
#: It cannot see the plugin's config block, so a caller admitted to the CALL by
#: the config-block allowlist was then refused at the agent-turn layer: the call
#: connects and the agent declines to do anything, for a reason nothing logs.
_HOST_ALLOWLIST_ENV = "MSTEAMS_BRIDGE_ALLOWLIST"
_HOST_ALLOW_ALL_ENV = "MSTEAMS_BRIDGE_ALLOW_ALL"


class MicrosoftTeamsPlatform:
    """One Microsoft Teams voice lane, owned by the host.

    Built once and kept. :meth:`connect` and :meth:`disconnect` are both safe to
    call twice, because a host reload calls them in whatever order it likes.
    """

    def __init__(
        self,
        plugin: PluginConfig | None = None,
        respond: Callable[[InboundMessage], Awaitable[str]] | None = None,
    ) -> None:
        self._plugin = plugin
        #: Answers a chat message. Supplied by the host, because only the host
        #: has the agent that should answer one. Without it there is no chat
        #: lane, which is the correct behaviour rather than a degraded one.
        self._respond = respond
        self._server: CallServer | None = None
        self._chat: Any = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._connected = False
        self._live: LiveCalls = LIVE_CALLS
        self._delivery: VoiceDelivery | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        """Bind the listener. Returns False rather than raising, ever.

        A host that gets an exception marks the whole plugin broken, when what
        actually happened is that this one platform is not configured. False is
        the honest answer to "is this connected?" and leaves the rest of the
        host alone.
        """
        if self._connected:
            return True

        plugin = self._plugin or resolve_config()
        secret = os.environ.get("STANDIN_SECRET", "").strip()
        if not secret:
            logger.info(
                "standin: STANDIN_SECRET is not set, so the Microsoft Teams voice lane "
                "stays off. Paste the connection secret from the StandIn portal."
            )
            return False

        _mirror_admission(plugin)

        server = CallServer(handler_factory=handler_factory(plugin=plugin))
        try:
            await server.start()
        except OSError as err:
            # Two owners of the same port silently split inbound calls, and the
            # split is invisible until half of them go unanswered. The likeliest
            # second owner is a standalone serve, so it is named here rather
            # than left to be guessed from an errno.
            logger.error(
                "standin: could not bind the Microsoft Teams listener on %s:%s (%s). "
                "A standalone `hermes msteams-bridge serve` may already hold that port.",
                server.host,
                server.port,
                err,
            )
            return False
        except Exception as err:
            logger.error("standin: the Microsoft Teams listener did not start: %s", err)
            return False

        self._server = server
        self._connected = True
        self._delivery = _build_delivery(self._live)

        # After the call path is up, and in its own guard. A chat credential or
        # URL problem is a configuration problem, not a reason to drop calls,
        # and a chat port conflict has taken voice down before.
        try:
            self._chat = await self._start_chat_lane()
        except Exception as err:
            logger.warning("standin: the chat lane did not start, calls are unaffected: %s", err)
            self._chat = None
        return True

    async def disconnect(self) -> None:
        """Give the port back, and everything holding it. Safe to call twice.

        Everything this owns is cancelled AND awaited before this returns. A
        teardown that returns early leaves a listener bound and live calls half
        drained, and the next connect then fails the bind for a reason that
        looks like somebody else's process.
        """
        chat, self._chat = self._chat, None
        if chat is not None:
            with contextlib.suppress(Exception):
                await chat.aclose()

        tasks, self._tasks = self._tasks, set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        server, self._server = self._server, None
        if server is not None:
            with contextlib.suppress(Exception):
                await server.aclose()
        self._connected = False

    async def send(
        self, chat_id: str, content: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Say this to that person, on their live call or on a new one.

        A thin map onto :class:`standin.VoiceDelivery`, which is also what the
        standalone sender uses. Two delivery paths that each re-implemented the
        allowlist and the rate limit would drift, and the drift shows up as one
        of them ringing somebody the other would have refused.
        """
        if self._delivery is None:
            return {"ok": False, "error": "the Microsoft Teams voice lane is not connected"}
        thread_id = str((metadata or {}).get("thread_id", "") or "")
        result = await self._delivery.deliver(content, target=chat_id, thread_id=thread_id)
        return {
            "ok": result.ok,
            "mode": result.mode,
            "call_id": result.call_id,
            "error": result.error,
        }

    async def _start_chat_lane(self) -> Any:
        """The chat lane, if the host gave something to answer with.

        Deliberately not a second inbound port. A listener of its own would mean
        a second admission policy on a second surface, which is the exposure the
        SDK's outward-dialling chat lane exists to avoid: it connects to StandIn
        and listens on nothing.
        """
        if self._respond is None:
            return None
        from standin import ChatChannel

        chat = ChatChannel(respond=self._respond)
        await chat.start()
        return chat

    def get_chat_info(self, chat_id: str) -> dict[str, str]:
        """What the host renders beside a conversation.

        Always a direct one. A voice leg is 1:1 by construction even when the
        meeting it belongs to has other people in it, and reporting a group
        would make the host render something it cannot address.
        """
        return {"name": "Teams call", "type": "dm"}


def _build_delivery(live: LiveCalls) -> VoiceDelivery:
    caller: OutboundCaller | None
    try:
        caller = OutboundCaller.from_env()
    except Exception as err:
        # Outbound is optional. Without it, a message for somebody who is not
        # on a call is refused in a sentence rather than crashing the send.
        logger.info("standin: outbound calling is not configured: %s", err)
        caller = None
    return VoiceDelivery(
        live,
        caller=caller,
        policy=OutboundPolicy.from_env(),
        pending=PendingMessages(),
    )


def _mirror_admission(plugin: PluginConfig) -> None:
    """Put the resolved allowlist where the host's authorization can see it.

    Only where it is currently empty, so an operator who set these by hand keeps
    what they set. Once, at connect, never per call.
    """
    if plugin.allowlist and not os.environ.get(_HOST_ALLOWLIST_ENV, "").strip():
        os.environ[_HOST_ALLOWLIST_ENV] = ",".join(plugin.allowlist)
    if plugin.allow_all and not os.environ.get(_HOST_ALLOW_ALL_ENV, "").strip():
        os.environ[_HOST_ALLOW_ALL_ENV] = "1"


def register_platform(ctx: Any, platform: MicrosoftTeamsPlatform) -> bool:
    """Offer the platform to the host, if the host takes platforms at all.

    Older hosts have no such API, and a plugin that raises at registration takes
    the whole host down at startup over a capability the operator may never use.
    """
    try:
        ctx.register_platform("msteams", platform)
    except (AttributeError, TypeError):
        logger.info(
            "standin: this host does not register platforms, so the Microsoft Teams "
            "lane is available through `hermes msteams-bridge serve` instead."
        )
        return False
    return True
