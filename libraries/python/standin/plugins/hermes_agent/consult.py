# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Running the caller's real Hermes agent, from inside a live call.

In realtime mode the speech-to-speech model is the conversation and Hermes is
not: the model handles the talking and delegates anything that is actual work -
a lookup, a file, the web, a skill - to the agent through the
``hermes_agent_consult`` tool. This class is what that tool runs.

Almost none of that is Hermes-specific, and none of the hard parts are. Running
a blocking agent off the event loop so the audio keeps flowing, refusing a
second consult rather than queueing it, admitting a timeout instead of promising
a follow-up nothing will deliver: all of that is :class:`standin.Consultant`,
shared with every other plugin.

What is left here, and the only reason this file exists, is knowing how to build
a Hermes agent: which model block the host resolved, which key it uses, and how
to pin a task id so the agent's own tool sessions stay findable across turns.
"""

from __future__ import annotations

import inspect
import os
import uuid
from typing import Any

from standin.consult import Consultant

from .api import HermesUnavailable, build_consult_agent, model_config_block
from .log import logger

__all__ = ["AgentConsult"]

#: What a caller hears when the worker is running outside a Hermes host.
_NO_HOST = "I can't reach my tools right now, so I can't look that up."


class AgentConsult:
    """One reusable Hermes agent for one call.

    Args:
        session_id: the agent session this call's consults belong to, which is
            what gives a caller memory across turns and - depending on the
            configured scope - across calls. See :func:`~.config.session_key`.
        model: override the model from the host's ``model:`` block.
        timeout_s: how long one consult may take before the model is told, in
            words, that it did not finish.
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        model: str | None = None,
        timeout_s: float = 45.0,
    ) -> None:
        self._session_id = session_id
        self._model = model
        # A stable task id for the agent's own tool sessions. Without one the
        # host mints a fresh id per turn, so anything that wants to follow THIS
        # consult's work across turns cannot find it.
        self.task_id = f"standin:consult:{session_id or uuid.uuid4().hex[:8]}"
        # The SDK owns the timing, the refusals and the rebuild-after-timeout.
        # This plugin only says how to build the thing being asked.
        self._consultant = Consultant(self._build, timeout_s=timeout_s)

    async def ask(self, query: str, *, timeout_s: float | None = None) -> str:
        """Run ``query`` through the agent and return something speakable.

        Never raises. Every failure comes back as a sentence the model can say
        out loud, because the alternative on a live call is silence while the
        caller waits for an answer that is not coming.
        """
        return await self._consultant.ask(query, timeout_s=timeout_s)

    # ---- how to build a Hermes agent -------------------------------------

    def _build(self) -> Any:
        """Return the callable one consult runs. Built on first use.

        Building an agent is expensive and most calls never consult at all, so
        it is deferred until something actually asks.
        """
        try:
            agent = build_consult_agent(**self._agent_kwargs())
        except HermesUnavailable as err:
            # The named boundary error, spoken. This is what a caller hears when
            # the worker runs outside a Hermes host, and it should sound like a
            # configuration problem rather than a broken assistant. Returned as
            # an asker rather than raised, so the SDK's generic failure line
            # does not replace the specific one.
            logger.error("standin: %s", err)
            return lambda query: _NO_HOST

        run = getattr(agent, "run_conversation", None)
        pins_task = run is not None and _accepts_task_id(run)

        def ask(query: str) -> str:
            try:
                if pins_task:
                    result = run(query, task_id=self.task_id)
                    if isinstance(result, dict):
                        return str(result.get("final_response") or "")
                    return str(result)
                return str(agent.chat(query))  # an older host without the kwarg
            except HermesUnavailable as err:
                logger.error("standin: %s", err)
                return _NO_HOST

        return ask

    def _agent_kwargs(self) -> dict[str, Any]:
        """Build ``AIAgent`` kwargs from the host's own ``model:`` block.

        A bare ``AIAgent()`` leaves the model unset and every consult comes back
        as a deployment error, so this passes the same provider, model, base url
        and api mode the Hermes CLI resolves for a chat turn. The caller gets the
        agent they configured, not a default one.
        """
        kwargs: dict[str, Any] = {"quiet_mode": True}
        block = model_config_block()
        for src, dst in (
            ("default", "model"),
            ("provider", "provider"),
            ("base_url", "base_url"),
            ("api_mode", "api_mode"),
        ):
            if block.get(src):
                kwargs[dst] = block[src]
        if self._model:
            kwargs["model"] = self._model
        key = (
            os.getenv("AZURE_FOUNDRY_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
        )
        if key:
            kwargs.setdefault("api_key", key)
        if self._session_id:
            kwargs["session_id"] = self._session_id
        return kwargs


def _accepts_task_id(fn: Any) -> bool:
    try:
        return "task_id" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
