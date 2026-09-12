# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The things an agent can do about the call it is on.

A voice model can talk. What it cannot do, unless you tell it, is hang up, put a
picture on its own video tile, react with an expression, or look at what the
caller is showing. Those are properties of being on a Microsoft Teams call, not
of any provider, so they live here and every plugin gets the same set.

Two halves, and the split matters:

:meth:`CallTools.schemas` declares the tools, in the shape your provider wants.
    Every provider invented its own JSON for "here is a function you may call",
    so the same five capabilities were being written out four times with four
    sets of wording. One description, rendered per dialect, means a caller gets
    the same behaviour whichever provider answers.

:meth:`CallTools.dispatch` runs one, and **never raises**.
    It returns a sentence. The result goes back to a model that will read it
    out, so "I could not show that because the image was too large" is worth
    something and a traceback is not.

Adding a fifth capability is one edit here rather than one per plugin, which is
the whole reason this is not in a plugin.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .handler import CallSession
from .log import logger
from .vision_tools import VisionTools

__all__ = [
    "BUILT_IN_TOOLS",
    "SHOW_PAGE_TOOL",
    "CallTools",
    "ToolResult",
    "ToolSpec",
    "tool_schemas",
]


@dataclass(frozen=True)
class ToolResult:
    """What a tool did, for the providers that want more than a sentence.

    ``text`` is the sentence, and for most callers it is the whole answer.
    ``ok`` is for the providers whose tool-result frame carries an error flag
    (ElevenLabs, OpenAI): it is False only when the SDK KNOWS the tool did not
    do what it was asked, which is a missing or malformed argument, a rejected
    value, a handler that raised, or a name no tool answers.

    It is not a verdict on the vision tools. Those answer in sentences by
    design, so "there is nothing to look at" comes back as ok, with the reason
    in the text where the model will actually read it.
    """

    text: str
    ok: bool = True

    def __str__(self) -> str:
        return self.text


class ToolSpec:
    """One capability, described once, rendered per provider."""

    __slots__ = ("description", "name", "parameters", "required")

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
        required: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters or {}
        self.required = required

    def json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self.parameters,
            "required": list(self.required),
        }


#: The capabilities every call has. The descriptions are written FOR A MODEL:
#: they say when to reach for the tool, not what the code does, because that is
#: the only thing the model reads before deciding.
SHOW_PAGE_TOOL = ToolSpec(
    name="show_page",
    description=(
        "Open a web page and show the caller a picture of it on your video tile. Use it "
        "when the caller asks about a page, a dashboard or a document that lives at a URL."
    ),
    parameters={
        "url": {"type": "string", "description": "Public https URL of the page."},
        "caption": {"type": "string", "description": "Optional short caption."},
    },
    required=("url",),
)
"""Deliberately NOT one of the built-ins.

The built-in list is declared unconditionally, so a deployment with no renderer
would still be telling every model it can show a web page. It would promise the
caller and then apologise, on every call, everywhere. An absent tool is honest;
a broken one is not. A plugin that actually has a renderer registers this one
through :meth:`CallTools.register`.
"""

BUILT_IN_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="end_call",
        description=(
            "Hang up. Use this when the conversation is finished, the caller says goodbye, "
            "or the caller asks you to hang up."
        ),
    ),
    ToolSpec(
        name="express",
        description=(
            "Show an emotion on your avatar's face. Use it to react naturally, for example "
            "happy when greeting someone or surprised at unexpected news."
        ),
        parameters={
            "emotion": {
                "type": "string",
                "description": "happy, sad, surprised, thinking or neutral.",
            }
        },
        required=("emotion",),
    ),
    ToolSpec(
        name="show_image",
        description=(
            "Show the caller an image on your video tile. Give a public https URL of a jpeg "
            "or png. Use it when seeing something would help more than hearing it."
        ),
        parameters={
            "url": {"type": "string", "description": "Public https URL of a jpeg or png."},
            "caption": {"type": "string", "description": "Optional short caption."},
            "display": {
                "type": "string",
                "enum": ["fullscreen", "overlay"],
                "description": (
                    'How to show it: "fullscreen" for something being read, "overlay" to '
                    "keep your face beside it. Leave it out to use the default."
                ),
            },
        },
        required=("url",),
    ),
    ToolSpec(
        name="look",
        description=(
            "Look at the caller's camera or shared screen and find out what is visible. Use "
            "it when the caller refers to something they are showing you."
        ),
        parameters={
            "source": {
                "type": "string",
                "description": 'Which video to look at: "camera" or "screenshare".',
            },
            "question": {
                "type": "string",
                "description": "What you want to know about what they are showing.",
            },
        },
    ),
    ToolSpec(
        name="look_back",
        description=(
            "Look again at something the caller showed earlier and has already moved past. "
            "Use it when they ask about a slide or screen that is no longer up. Only works "
            "while the call is being recorded."
        ),
        parameters={
            "question": {"type": "string", "description": "What you want to know about it."}
        },
    ),
)


def tool_schemas(dialect: str = "flat", extra: tuple[ToolSpec, ...] = ()) -> list[dict[str, Any]]:
    """Render the tool declarations in one provider's shape.

    ``flat``
        ``{name, description, parameters}``. What Deepgram's Settings message
        and ElevenLabs' client tools both take.
    ``openai``
        The same, tagged ``type: "function"``, which the Realtime API wants.
    ``anthropic``
        ``{name, description, input_schema}``.

    An unknown dialect falls back to ``flat`` rather than raising, because the
    consequence of guessing wrong here is a tool a model never sees, and a
    plugin author is better served by a working default than by a crash at
    connect time.
    """
    specs = (*BUILT_IN_TOOLS, *extra)
    if dialect == "openai":
        return [
            {
                "type": "function",
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.json_schema(),
            }
            for spec in specs
        ]
    if dialect == "anthropic":
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.json_schema(),
            }
            for spec in specs
        ]
    if dialect not in ("flat", ""):
        logger.debug("standin: unknown tool dialect %r; using the flat shape", dialect)
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.json_schema(),
        }
        for spec in specs
    ]


#: A tool of your own. Returns what the model is told, which it will read out or
#: reason from, so keep it short and keep it fast: the caller is listening to
#: silence while it runs.
ToolHandler = Callable[[dict[str, Any]], "str | Awaitable[str]"]


class CallTools:
    """The built-in call capabilities, bound to one call.

    Built once per call by a plugin, which then only has to translate its
    provider's tool-call frame into a name and a dict::

        tools = CallTools(session, vision=VisionTools(session, describer=...))
        agent.declare(tools.schemas("flat"))
        ...
        result = await tools.dispatch(name, params)   # never raises

    Your own tools go in the same place, so a model sees one list::

        tools.register("open_ticket", spec, handler)
    """

    def __init__(
        self,
        session: CallSession,
        vision: VisionTools | None = None,
    ) -> None:
        self._session = session
        self._vision = vision or VisionTools(session)
        self._extra: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    @property
    def vision(self) -> VisionTools:
        """The vision tools these dispatch into."""
        return self._vision

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        """Add a tool of your own.

        Refuses a name that shadows a built-in, and refuses it HERE rather than
        at the first call: a shadowed ``end_call`` is an agent that has quietly
        lost the ability to hang up, and that is not something to discover
        mid-conversation.
        """
        if any(spec.name == built_in.name for built_in in BUILT_IN_TOOLS):
            raise ValueError(f"{spec.name!r} is a built-in call capability and cannot be replaced")
        self._extra[spec.name] = (spec, handler)

    def schemas(self, dialect: str = "flat") -> list[dict[str, Any]]:
        """Every tool, built-in and yours, in one provider's shape."""
        return tool_schemas(dialect, tuple(spec for spec, _ in self._extra.values()))

    async def dispatch(self, name: str, params: dict[str, Any] | None = None) -> str:
        """Run one tool and return the sentence the model should be told.

        Never raises. The result is read out loud, so a failure has to arrive as
        a sentence or the agent simply goes quiet and the caller waits. Use
        :meth:`run` instead when your provider's tool-result frame also carries
        an error flag.
        """
        return (await self.run(name, params)).text

    async def run(self, name: str, params: dict[str, Any] | None = None) -> ToolResult:
        """Run one tool and return the sentence plus whether it worked.

        Never raises. See :class:`ToolResult` for what ``ok`` does and does not
        claim.
        """
        args = params or {}
        try:
            return await self._run(name, args)
        except Exception as err:
            logger.warning("standin: the %s tool failed: %s", name, err)
            return ToolResult(f"{name} failed: {err}", ok=False)

    async def _run(self, name: str, params: dict[str, Any]) -> ToolResult:
        if name == "end_call":
            await self._session.end("agent-ended-call")
            return ToolResult("the call is ending")

        if name == "express":
            emotion = params.get("emotion")
            emotion = emotion.strip() if isinstance(emotion, str) else ""
            if not emotion:
                return ToolResult("express needs an 'emotion'", ok=False)
            try:
                await self._session.express(emotion)
            except ValueError as err:
                return ToolResult(str(err), ok=False)
            return ToolResult(f"expressing {emotion}")

        if name == "show_image":
            url = params.get("url")
            caption = params.get("caption")
            display = params.get("display")
            return ToolResult(
                await self._vision.show_url(
                    url if isinstance(url, str) else "",
                    caption if isinstance(caption, str) else None,
                    display=display if isinstance(display, str) else None,
                )
            )

        if name == "look":
            source = params.get("source")
            question = params.get("question")
            return ToolResult(
                await self._vision.look(
                    question if isinstance(question, str) else "",
                    source if isinstance(source, str) else None,
                )
            )

        if name == "look_back":
            question = params.get("question")
            return ToolResult(
                await self._vision.look_back(question if isinstance(question, str) else "")
            )

        entry = self._extra.get(name)
        if entry is None:
            return ToolResult(f'"{name}" is not a tool this agent has', ok=False)
        _, handler = entry
        result = handler(params)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[misc]
        return ToolResult(str(result))
