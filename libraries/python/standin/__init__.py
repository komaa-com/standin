# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""StandIn SDK - put your AI agent into a Microsoft Teams call.

StandIn (https://standin.komaa.com) is the hosted bridge that joins the Microsoft Teams
call: it owns the Microsoft side entirely - the bot registration, Graph, media
negotiation, the avatar tile - and speaks to your worker over one authenticated
socket per call. This SDK is that socket's other end, and it is all a
plugin needs to answer a real Microsoft Teams call:

* :class:`CallServer` - answers the dial, authenticates it, speaks the wire
  protocol, and drives one :class:`CallHandler` per call.
* :class:`CallHandler` - the five-method seam a plugin implements.
* :class:`ChatChannel` - the messages lane, dialed OUT from the worker, so
  Microsoft Teams chat needs no listener, no port, and no bot credential of your own.

ONE package holds all of it, core and plugins both::

    from standin import CallServer, CallSession, ChatChannel, FrameAligner, VideoFrame
    from standin.plugins.livekit import TeamsCall

That is a deliberate trade. A call surface - screen share, call-back, camera,
chat, adaptive cards - is built once and has to reach every framework StandIn
supports. Split across one wheel per framework, each surface costs N
hand-threaded releases; in one package it costs one.

The price of collapsing is that ``import standin`` now sits above code that
references LiveKit and Hermes. It must not cost a reader who installed neither
anything, so:

* plugins are reached through a module-level ``__getattr__`` (PEP 562) and
  are imported the first time they are touched, never at ``import standin``;
* no module under :mod:`standin.plugins` imports its framework at module
  load time, and a missing one raises
  :class:`standin.PluginNotInstalled` naming the extra to install, not a
  bare traceback.

So the base install is aiohttp and nothing else, and each framework is one
extra: ``pip install "standin-sdk[livekit]"``.

You do not normally use this module directly. Import the plugin for your
framework and it wires this up for you. Writing one is the other direction, and
it is deliberately small - see :mod:`standin.handler`, or copy
:mod:`standin.plugins.echo`, which answers a real call in under 100 lines.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._exceptions import PluginNotInstalled, StandInError
from ._hmac import (
    CHAT_REPLAY_WINDOW_MS,
    REPLAY_WINDOW_MS,
    SIGNATURE_HEADER,
    SIGNATURE_V2_HEADER,
    TIMESTAMP_HEADER,
    canonical_request,
    now_ms,
    sign_body,
    sign_handshake,
    sign_request,
    verify_body,
    verify_handshake,
)
from .ambient import (
    AMBIENT_BACKSTOP_MS,
    AMBIENT_SOURCE_ORDER,
    DEFAULT_AMBIENT_MAX_PER_MINUTE,
    MAX_QUEUED_AMBIENT_IMAGES,
    AmbientImage,
    AmbientSink,
    AmbientVision,
)
from .attachments import (
    ChatAudio,
    ChatImage,
    ChatTurn,
    Transcriber,
    attachment_origin,
    attachments_note,
    build_chat_turn,
    card_action_note,
    fetch_chat_audio,
    fetch_chat_images,
    spool_clip,
    transcribe_voice_messages,
)
from .audio import (
    BYTES_PER_SAMPLE,
    FRAME_BYTES,
    FRAME_MS,
    REALTIME_SAMPLE_RATE_HZ,
    FrameAligner,
    frame_duration_ms,
    pcm16_rms,
    resample_pcm16,
)
from .avatar import (
    EMOTIONS,
    MAX_EMOTION_CHARS,
    MAX_VISEME_ID,
    ExpressionCue,
    SpeechMark,
    expression,
    infer_emotion,
    speech_marks,
)
from .call_server import MAX_AUDIO_BUFFER_BYTES, CallServer
from .calltools import BUILT_IN_TOOLS, SHOW_PAGE_TOOL, CallTools, ToolSpec, tool_schemas
from .chat import (
    CHAT_FALLBACK_WINDOW_MS,
    OUTBOUND_IMAGE_CONTENT_TYPES,
    OUTBOUND_IMAGE_MAX_BYTES,
    SCHEMA_VERSION,
    ChatChannel,
    InboundMessage,
    OutboundImage,
    PersonalChat,
    PersonalChats,
    build_reply,
    outbound_image,
    parse_inbound,
    sanitize_image_name,
    sniff_image_type,
)
from .consult import (
    BACKGROUND_TASK_TOOL,
    CONSULT_TOOL,
    BackgroundTask,
    BackgroundTasks,
    Consultant,
)
from .delivery import TENANT_ENV, Delivery, LiveCalls, LiveSpeaker, VoiceDelivery
from .echo_guard import ECHO_BARGE_IN_RMS, ECHO_SUPPRESSION_WINDOW_MS, EchoGuard
from .gate import (
    DEFAULT_FOLLOW_UP_WINDOW_MS,
    GateDecision,
    GroupGate,
    is_addressed,
    is_meeting_thread,
    is_verbal_interrupt,
)
from .handler import (
    CallHandler,
    CallSession,
    HandlerFactory,
    SpeakerHandler,
    VideoHandler,
)
from .lane import (
    TROUBLE_ANSWERING,
    TROUBLE_HEARING,
    TROUBLE_SPEAKING,
    VoiceLane,
    VoiceTurn,
)
from .lipsync import (
    CHAR_VISEMES,
    SILENCE_VISEME,
    TurnLipSync,
    estimate_visemes,
    viseme_for_char,
    visemes_from_alignment,
)
from .media import (
    MEDIA_ROOTS_ENV,
    AgentMedia,
    load_media,
    media_roots,
    parse_media,
)
from .minutes import (
    DOCUMENT_NOT_ATTACHED,
    MAX_TRANSCRIPT_ENTRIES,
    MAX_TRANSCRIPT_ENTRY_CHARS,
    MINUTES_TOOL,
    RECAP_MIN_TURNS,
    DeliveryTarget,
    MinutesSection,
    RecapResult,
    Transcript,
    has_speaker_prefix,
    is_summary_request,
    minutes_prompt,
    parse_minutes_sections,
    post_minutes,
    resolve_minutes_target,
    write_minutes_docx,
)
from .outbound import (
    CALL_BACK_TOOL,
    CHAT_CALLBACK_TOOL,
    ChatCallbackTarget,
    ChatSender,
    OutboundCaller,
    OutboundError,
    OutboundLane,
    OutboundLeg,
    OutboundPolicy,
    PendingMessage,
    PendingMessages,
    PlacedCall,
    call_thread_is_postable,
)
from .protocol import (
    NUM_CHANNELS,
    SAMPLE_RATE_HZ,
    Caller,
    SessionStart,
    assistant_cancel,
)
from .smoke import SmokeCheck, SmokeResult, SyntheticCall, run_smoke
from .smoke import report as smoke_report
from .startup import MAX_PENDING_AUDIO, MAX_PENDING_CONTEXT, StartupBuffer
from .tile import MAX_TILE_FPS, TILE_HEIGHT, TILE_WIDTH, TileStream, jpeg_encoder
from .version import __version__
from .vision import (
    VIDEO_SOURCES,
    FrameDescriber,
    VideoFrame,
    display_frame,
    display_image,
    fallback_owner,
    frame_caption,
    frame_digest,
    frame_owner,
    parse_video_frame,
)
from .vision_tools import (
    DISPLAY_MODES,
    MAX_SLIDESHOW_IMAGES,
    PAGE_DISPLAY_MS,
    PAGE_RENDER_TIMEOUT_S,
    SLIDESHOW_HOLD_MS,
    KeyframeStore,
    PageRenderer,
    ShowItem,
    ShownImage,
    VisionBudget,
    VisionTools,
    WalkthroughStep,
    display_image_name,
    normalize_display_mode,
)
from .voice import (
    DEFAULT_MAX_UTTERANCE_MS,
    DEFAULT_MIN_UTTERANCE_MS,
    DEFAULT_PREROLL_MS,
    DEFAULT_SILENCE_MS,
    DEFAULT_SPEECH_RMS,
    PacedPlayback,
    Playback,
    UtteranceSegmenter,
    decode_wav,
    encode_wav,
)

__all__ = [
    "resolve_minutes_target",
    "parse_minutes_sections",
    "has_speaker_prefix",
    "MinutesSection",
    "DeliveryTarget",
    "RECAP_MIN_TURNS",
    "MAX_TRANSCRIPT_ENTRY_CHARS",
    "MAX_TRANSCRIPT_ENTRIES",
    "DOCUMENT_NOT_ATTACHED",
    "PersonalChats",
    "PersonalChat",
    "CHAT_FALLBACK_WINDOW_MS",
    "infer_emotion",
    "ExpressionCue",
    "visemes_from_alignment",
    "viseme_for_char",
    "estimate_visemes",
    "TurnLipSync",
    "SILENCE_VISEME",
    "CHAR_VISEMES",
    "VoiceTurn",
    "VoiceLane",
    "TROUBLE_SPEAKING",
    "TROUBLE_HEARING",
    "TROUBLE_ANSWERING",
    "smoke_report",
    "run_smoke",
    "SyntheticCall",
    "SmokeResult",
    "SmokeCheck",
    "VoiceDelivery",
    "LiveSpeaker",
    "LiveCalls",
    "Delivery",
    "TENANT_ENV",
    "PageRenderer",
    "PAGE_RENDER_TIMEOUT_S",
    "PAGE_DISPLAY_MS",
    "SHOW_PAGE_TOOL",
    "normalize_display_mode",
    "display_image_name",
    "ShownImage",
    "ShowItem",
    "SLIDESHOW_HOLD_MS",
    "MAX_SLIDESHOW_IMAGES",
    "DISPLAY_MODES",
    "BACKGROUND_TASK_TOOL",
    "AMBIENT_BACKSTOP_MS",
    "AMBIENT_SOURCE_ORDER",
    "DEFAULT_AMBIENT_MAX_PER_MINUTE",
    "MAX_QUEUED_AMBIENT_IMAGES",
    "BUILT_IN_TOOLS",
    "BYTES_PER_SAMPLE",
    "CALL_BACK_TOOL",
    "CHAT_CALLBACK_TOOL",
    "CHAT_REPLAY_WINDOW_MS",
    "CONSULT_TOOL",
    "DEFAULT_FOLLOW_UP_WINDOW_MS",
    "DEFAULT_MAX_UTTERANCE_MS",
    "DEFAULT_MIN_UTTERANCE_MS",
    "DEFAULT_PREROLL_MS",
    "DEFAULT_SILENCE_MS",
    "DEFAULT_SPEECH_RMS",
    "ECHO_BARGE_IN_RMS",
    "ECHO_SUPPRESSION_WINDOW_MS",
    "EMOTIONS",
    "FRAME_BYTES",
    "FRAME_MS",
    "MAX_AUDIO_BUFFER_BYTES",
    "MAX_EMOTION_CHARS",
    "MEDIA_ROOTS_ENV",
    "MINUTES_TOOL",
    "MAX_PENDING_AUDIO",
    "MAX_PENDING_CONTEXT",
    "MAX_TILE_FPS",
    "MAX_VISEME_ID",
    "NUM_CHANNELS",
    "OUTBOUND_IMAGE_CONTENT_TYPES",
    "OUTBOUND_IMAGE_MAX_BYTES",
    "REALTIME_SAMPLE_RATE_HZ",
    "REPLAY_WINDOW_MS",
    "SAMPLE_RATE_HZ",
    "SCHEMA_VERSION",
    "SIGNATURE_HEADER",
    "SIGNATURE_V2_HEADER",
    "TILE_HEIGHT",
    "TILE_WIDTH",
    "TIMESTAMP_HEADER",
    "VIDEO_SOURCES",
    "AgentMedia",
    "AmbientImage",
    "AmbientSink",
    "AmbientVision",
    "BackgroundTask",
    "BackgroundTasks",
    "CallHandler",
    "CallServer",
    "CallSession",
    "CallTools",
    "Caller",
    "Consultant",
    "ChatAudio",
    "ChatImage",
    "ChatTurn",
    "Transcriber",
    "ChatCallbackTarget",
    "ChatChannel",
    "ChatSender",
    "EchoGuard",
    "FrameAligner",
    "FrameDescriber",
    "GateDecision",
    "GroupGate",
    "HandlerFactory",
    "InboundMessage",
    "KeyframeStore",
    "OutboundCaller",
    "OutboundImage",
    "OutboundError",
    "OutboundLane",
    "OutboundLeg",
    "OutboundPolicy",
    "PendingMessage",
    "PacedPlayback",
    "PendingMessages",
    "PlacedCall",
    "Playback",
    "RecapResult",
    "PluginNotInstalled",
    "SessionStart",
    "SpeakerHandler",
    "SpeechMark",
    "StandInError",
    "StartupBuffer",
    "TileStream",
    "Transcript",
    "ToolSpec",
    "UtteranceSegmenter",
    "VideoFrame",
    "VideoHandler",
    "VisionBudget",
    "VisionTools",
    "WalkthroughStep",
    "__version__",
    "assistant_cancel",
    "attachment_origin",
    "attachments_note",
    "build_chat_turn",
    "build_reply",
    "call_thread_is_postable",
    "canonical_request",
    "card_action_note",
    "decode_wav",
    "fetch_chat_audio",
    "fetch_chat_images",
    "display_frame",
    "display_image",
    "encode_wav",
    "expression",
    "fallback_owner",
    "frame_caption",
    "frame_digest",
    "frame_owner",
    "frame_duration_ms",
    "is_addressed",
    "load_media",
    "media_roots",
    "is_meeting_thread",
    "is_summary_request",
    "is_verbal_interrupt",
    "jpeg_encoder",
    "minutes_prompt",
    "now_ms",
    "outbound_image",
    "parse_inbound",
    "parse_media",
    "parse_video_frame",
    "pcm16_rms",
    "post_minutes",
    "plugins",
    "resample_pcm16",
    "sanitize_image_name",
    "sign_body",
    "spool_clip",
    "sign_handshake",
    "sign_request",
    "sniff_image_type",
    "speech_marks",
    "transcribe_voice_messages",
    "tool_schemas",
    "verify_body",
    "verify_handshake",
    "write_minutes_docx",
]


# Every plugin this SDK ships. The name is the module under
# standin/plugins/, and adding one here is the ONLY edit outside its own
# directory - that is the whole point of collapsing to one package.
_PLUGINS = ("cartesia", "deepgram", "echo", "elevenlabs", "hermes_agent", "livekit")


if TYPE_CHECKING:  # pragma: no cover - for type checkers and IDEs only
    from . import plugins


def __getattr__(name: str) -> Any:
    """Resolve :mod:`standin.plugins` and its members on first touch.

    PEP 562. Nothing here may run at ``import standin``: a plugin module
    is allowed to reference a framework the reader never installed, and touching
    one is the reader saying they did install it. Importing them eagerly would
    make the base install depend on every framework at once, which is exactly
    what one package must not cost.

    The shorthands (``standin.livekit``) exist because one package can afford
    them; the documented, stable spelling stays ``standin.plugins.livekit``
    so the path says which layer a name comes from.
    """
    import importlib

    if name == "plugins":
        return importlib.import_module("standin.plugins")
    if name in _PLUGINS:
        return importlib.import_module(f"standin.plugins.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Keep tab-completion honest: the exports plus the plugins."""
    return sorted([*__all__, *_PLUGINS])
