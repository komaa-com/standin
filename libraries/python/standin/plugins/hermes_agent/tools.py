# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""What the realtime model can ask this plugin to do.

The tool set is small, and small is the point. In realtime mode the model is the
conversation; this plugin's job is to give it one door into Hermes and one lever
over the call itself. Everything else the caller wants happens on the far side of
that door, inside the agent, with the caller's own tools and skills - which is
the reason for running Hermes at all rather than a bare realtime model.

Realtime tools use the flat shape ``{type, name, description, parameters}``, not
the chat-completions nesting. The model calls one by name and
:class:`ToolRunner` dispatches it.

The tool set tracks the call seam. This plugin speaks the audio seam: audio
in, audio out, plus call context and the closing line. Tools that drive other
surfaces arrive with those surfaces.
"""

from __future__ import annotations

import json
from typing import Any

from .log import logger

__all__ = ["HERMES_AGENT_CONSULT", "SET_CALL_LANGUAGE", "ToolRunner", "default_tools"]

HERMES_AGENT_CONSULT: dict[str, Any] = {
    "type": "function",
    "name": "hermes_agent_consult",
    "description": (
        "Delegate to the Hermes agent to answer a question or perform an action - "
        "lookups, calculations, files, web, running tools, or using any of the "
        "installed Hermes skills. Use this for anything beyond small talk. "
        "Returns a short result to speak to the caller."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look into or do, phrased as a task.",
            }
        },
        "required": ["query"],
    },
}

SET_CALL_LANGUAGE: dict[str, Any] = {
    "type": "function",
    "name": "set_call_language",
    "description": (
        "Pin the call to a specific language for the rest of the conversation "
        "(applies immediately). Use when the caller asks to continue in another "
        "language, for example 'let's speak French from now on'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "description": "ISO 639-1 code, for example 'fr', 'de', 'ar', 'en'.",
            }
        },
        "required": ["language"],
    },
}


def default_tools() -> list[dict[str, Any]]:
    """The tool set offered to the model on every call."""
    return [HERMES_AGENT_CONSULT, SET_CALL_LANGUAGE]


class ToolRunner:
    """Dispatches one tool call and returns a string for the model to speak.

    Never raises. A tool result is fed straight back into a live conversation,
    so every failure has to arrive as words: an exception here would leave the
    model waiting on a result that never comes, with the caller listening to
    nothing.

    Args:
        consult: the call's :class:`~.consult.AgentConsult`.
        set_language: coroutine taking an ISO 639-1 code, wired by the handler
            to rebuild and push the session instructions.
        consult_timeout_s: how long a consult may take.
    """

    def __init__(self, *, consult: Any, set_language: Any, consult_timeout_s: float = 45.0) -> None:
        self._consult = consult
        self._set_language = set_language
        self._timeout_s = consult_timeout_s

    @staticmethod
    def parse_args(args_json: str) -> dict[str, Any]:
        """Decode a tool's arguments. Anything unparseable becomes ``{}``.

        Model-generated JSON, so malformed input is a normal event rather than an
        exceptional one, and a tool with no arguments is better than no tool.
        """
        try:
            args = json.loads(args_json or "{}")
        except (TypeError, ValueError):
            return {}
        return args if isinstance(args, dict) else {}

    async def run(self, name: str, args: dict[str, Any]) -> str:
        """Run the named tool and return its spoken result."""
        if name == "hermes_agent_consult":
            query = str(args.get("query") or "")
            return await self._consult.ask(query, timeout_s=self._timeout_s)

        if name == "set_call_language":
            code = str(args.get("language") or "").strip().lower()[:8]
            if not code:
                return "I didn't catch which language you'd like."
            await self._set_language(code)
            return f"Switched to {code}."

        # An unknown name is the model's mistake, not a fault: tell it so, in a
        # sentence it can recover from, rather than failing the turn.
        logger.warning("standin: model called an unknown tool %r", name[:64])
        return "That isn't something I can do on this call."
