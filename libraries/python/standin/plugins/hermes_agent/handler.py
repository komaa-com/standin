# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""One Microsoft Teams call, answered by a realtime model with Hermes behind it.

This is the whole Hermes-specific half of the plugin in realtime mode.
Everything that is the same for every framework - the socket StandIn dials, the
HMAC handshake and its replay guard, capacity and draining, the wire protocol,
outbound sequence numbers and the audio timeline, the watchdogs, teardown
ordering - belongs to :class:`standin.CallServer` and is not repeated here.
The resampler and the frame aligner come from the SDK too, because they are
properties of the wire rather than of this plugin.

What is genuinely this plugin's:

* connect one realtime provider session per call, with the caller's own Hermes
  persona and skills index in its instructions
* the echo guard, so the model does not answer its own playback
* the group gate, so the assistant stays out of a meeting it was not addressed in
* barge-in: cut StandIn's buffered audio BEFORE cancelling the model upstream
* the consult door into Hermes, and the tool loop around it

**Who talks to whom.** In realtime mode Hermes is not the dialogue brain. The
speech-to-speech model is: it hears the caller and answers them. Hermes is
reached only when the model calls ``hermes_agent_consult``, and it never sees
audio. That is a deliberate division - the model is good at conversation and
Hermes is good at work - and it is why this file talks to a provider socket and
:mod:`~standin.plugins.hermes_agent.consult` talks to the agent.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import replace
from typing import Any

from standin import (
    REALTIME_SAMPLE_RATE_HZ,
    SAMPLE_RATE_HZ,
    CallSession,
    FrameAligner,
    StandInError,
    frame_duration_ms,
    resample_pcm16,
)
from standin.audio import pcm16_rms
from standin.avatar import ExpressionCue
from standin.delivery import LiveCalls
from standin.echo_guard import EchoGuard
from standin.gate import GroupGate, is_verbal_interrupt
from standin.lipsync import TurnLipSync

from .api import skills_index_text, soul_text
from .config import PluginConfig, caller_allowed, resolve_config, session_key
from .consult import AgentConsult
from .log import logger
from .realtime import RealtimeConfig, RealtimeSession, realtime_config
from .tools import ToolRunner, default_tools

__all__ = ["LIVE_CALLS", "RealtimeHandler"]

#: The calls this worker is on right now.
#:
#: One registry for the whole process, because the thing that wants to reach a
#: live call arrives from outside any of them: a scheduled job, a chat message,
#: the host handing over a task. A per-handler registry could never be found by
#: any of those.
LIVE_CALLS = LiveCalls()

#: Human names for the language clause. An unknown code passes through as-is;
#: the model reads ISO codes perfectly well and a missing entry must not drop
#: the language the operator configured.
_LANGUAGE_NAMES = {
    "en": "English",
    "ar": "Arabic",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "pt": "Portuguese",
    "tr": "Turkish",
    "ru": "Russian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "hi": "Hindi",
    "ur": "Urdu",
    "pl": "Polish",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "fi": "Finnish",
}

#: The SDK renders call context as finished English sentences rather than as
#: structured messages, and both SDKs render the same text, so these patterns
#: hold in either. They read the two sentences that carry a DECISION for this
#: plugin; everything else is passed to the model verbatim.
_PARTICIPANTS = re.compile(r"There are (\d+) human participants")
_ONE_TO_ONE = "1:1 call"
_RECORDING_ACTIVE = "recording is now ACTIVE"
_RECORDING_INACTIVE = "recording is not active"
_DTMF = re.compile(r'pressed the "(.+?)" key')


class RealtimeHandler:
    """The :class:`standin.CallHandler` for Hermes. One instance per call.

    Built by :func:`~.service.handler_factory`; construct it directly only when
    embedding the plugin yourself.

    Args:
        config: the realtime provider. Resolved from the Hermes config block and
            the environment when omitted.
        plugin: this plugin's policy - allowlist, session scope, wake phrases.
            Resolved the same way when omitted.

    Raises:
        StandInError: no realtime API key. Raised at CONSTRUCTION, so a
            misconfigured worker says so at startup in one line rather than on
            the first real call with a caller already on the line.
    """

    def __init__(
        self,
        *,
        config: RealtimeConfig | None = None,
        plugin: PluginConfig | None = None,
    ) -> None:
        self._cfg = config or realtime_config()
        if not self._cfg.configured:
            raise StandInError(
                "no realtime API key: set OPENAI_API_KEY, or "
                "MSTEAMS_BRIDGE_REALTIME_API_KEY / AZURE_OPENAI_API_KEY for Azure"
            )
        self._plugin = plugin or resolve_config()

        self._call: CallSession | None = None
        self._rt: RealtimeSession | None = None
        self._consult: AgentConsult | None = None
        self._tools: ToolRunner | None = None
        self._gate: GroupGate | None = None
        self._echo = EchoGuard()
        self._aligner = FrameAligner()
        #: The mouth and the face. Both cosmetic, both cheap, and both built
        #: here so a call that never starts still has something to reset.
        self._lip = TurnLipSync()
        self._cue = ExpressionCue()
        self._reply_text: list[str] = []

        self._closed = False
        self._greeted = False
        self._recording_active = False
        #: Egress backstop for a turn the group gate refused. The provider may
        #: already have started generating audio by the time the transcript that
        #: decides arrives, so refusing to SEND is the only guarantee.
        self._drop_response = False
        #: Whether server VAD creates responses on its own. Off in a meeting, so
        #: the gate decides before a single sample is generated.
        self._auto_on = True

    # ---- the CallHandler surface -----------------------------------------

    async def on_start(self, session: CallSession) -> None:
        """Admit the caller, then open the realtime session."""
        self._call = session
        start = session.start
        inbound = start.direction != "outbound"

        # Outbound is a call WE placed, so there is nobody to admit. Inbound is
        # checked, and deny-by-default: see caller_allowed.
        if inbound and not caller_allowed(
            self._plugin, start.caller.aad_id, start.caller.display_name
        ):
            logger.info(
                "standin: caller is not on the allowlist; refusing call %s", session.call_id
            )
            # end() returns immediately when called from on_start rather than
            # deadlocking against teardown, so this is a legitimate refusal path.
            await session.end("caller-not-allowlisted")
            return

        LIVE_CALLS.register(self, call_id=session.call_id, thread_id=start.thread_id)
        self._lip = TurnLipSync()
        self._cue = ExpressionCue()
        self._reply_text = []
        self._gate = GroupGate(
            wake_phrases=self._plugin.wake_phrases,
            require_address=self._plugin.require_address,
            follow_up_window_ms=self._plugin.follow_up_window_ms,
            thread_id=start.thread_id,
        )
        self._consult = AgentConsult(
            session_id=session_key(self._plugin, start),
            model=self._plugin.consult_model or None,
        )
        self._tools = ToolRunner(
            consult=self._consult,
            set_language=self.set_call_language,
            consult_timeout_s=self._plugin.consult_timeout_s,
        )
        self._recording_active = (start.recording_status or "").lower() == "active"

        rt = RealtimeSession(replace(self._cfg, instructions=self._build_instructions()))
        rt.tools = default_tools()
        rt.on_audio_delta = self._on_model_audio
        rt.on_input_transcript = self._on_input_transcript
        rt.on_transcript_delta = self._on_reply_text
        rt.on_speech_started = self._on_barge_in
        rt.on_response_done = self._on_response_done
        rt.on_function_call = self._on_function_call
        rt.on_error = self._on_provider_error
        rt.on_close = self._on_provider_closed
        self._rt = rt
        try:
            await rt.connect()
        except Exception:
            # Without a brain the caller sits in silent dead air until they give
            # up. End the call instead, with a reason that names the cause.
            logger.exception("standin: realtime connect failed for call %s", session.call_id)
            await session.end("realtime-connect-failed")
            return

        if self._can_gate():
            # Start in MANUAL response mode. Until we know how many people are
            # in the room, an auto-reply could speak over a meeting - and a
            # response cancelled after it started is one the caller has already
            # heard the beginning of.
            await rt.set_auto_response(False)
            self._auto_on = False
        else:
            # No caller transcription means no transcript, so no gate and no
            # verbal interrupts. Leave auto-response ON: a silent assistant is
            # worse than an ungated one, and the operator turned this off.
            logger.warning(
                "standin: caller transcription is disabled, so the group gate and "
                "verbal interrupts are inactive on call %s",
                session.call_id,
            )

        await self._maybe_greet()

    async def on_caller_audio(self, pcm: bytes) -> None:
        """The caller's voice: guard it, resample it, push it to the model."""
        rt = self._rt
        if rt is None or self._closed:
            return
        if self._plugin.require_recording and not self._recording_active:
            return
        if not self._echo.allow_input(pcm16_rms(pcm)):
            return
        await rt.push_audio(resample_pcm16(pcm, SAMPLE_RATE_HZ, REALTIME_SAMPLE_RATE_HZ))

    async def on_context(self, text: str) -> None:
        """Read call context: participants, recording status, DTMF.

        Two of these carry a decision for this plugin and the rest are simply
        things the model should know, so they go into the conversation without
        provoking a reply.
        """
        rt = self._rt
        if rt is None or self._closed:
            return

        match = _PARTICIPANTS.search(text)
        if match or _ONE_TO_ONE in text:
            count = int(match.group(1)) if match else 1
            if self._gate is not None:
                self._gate.note_participants(count)
            await self._sync_auto_response()
            return

        if _RECORDING_ACTIVE in text:
            self._recording_active = True
            await self._maybe_greet()
            return
        if _RECORDING_INACTIVE in text:
            self._recording_active = False
            return

        if _DTMF.search(text):
            # A keypad press IS a turn: "press 1 for support" needs an answer.
            await rt.send_user_text(text, respond=True)
            return

        await rt.send_user_text(text, respond=False)

    async def say(self, text: str) -> None:
        """Speak a line that arrived from outside the call.

        Not an interruption: a notification is not more important than the
        sentence the caller is halfway through. It goes in as context with a
        reply asked for, so the model says it in its own voice and in whatever
        language the call is being held in.

        This is what makes the handler a :class:`standin.LiveSpeaker`, so a
        delivery addressed at somebody already on the phone reaches them here
        instead of ringing them a second time.
        """
        rt = self._rt
        if rt is None or self._closed or not text.strip():
            raise RuntimeError("this call cannot be spoken into")
        await rt.send_user_text(f"Tell the caller this now: {text.strip()}", respond=True)

    async def on_goodbye(self, text: str) -> None:
        """StandIn is ending the call and wants this line spoken first.

        **Why it cancels first.** Realtime response creation is guarded on "is
        a response already active", and a goodbye arrives precisely when one
        usually is, because StandIn sends it to end a call that is still in
        progress. A plain say would be swallowed by that guard and the caller
        would hear nothing before the line went dead. Cancel, then speak.

        The SDK has already told StandIn to drop the buffered agent audio, so
        cancelling playback again is belt and braces; what the call below really
        does here is reset the state on OUR side of the seam - the aligner's
        residual belongs to the interrupted turn, and the playout clock must
        stop pretending we are still speaking.
        """
        call, rt = self._call, self._rt
        if call is None or rt is None or self._closed or not text.strip():
            return
        await call.cancel_playback()
        self._aligner.reset()
        self._echo.collapse()
        # An unaddressed meeting turn may have latched the egress drop, and a
        # goodbye dropped on the way out is exactly the failure this method
        # exists to fix - once at the provider, once at the wire.
        self._drop_response = False
        await rt.interrupt_and_say(f"Say this to the caller, then stop: {text}")

    async def aclose(self, reason: str) -> None:
        """Close the provider session. Called exactly once, on every path."""
        self._closed = True
        call = self._call
        if call is not None:
            LIVE_CALLS.unregister(self, call_id=call.call_id, thread_id=call.start.thread_id)
        rt, self._rt = self._rt, None
        if rt is not None:
            with contextlib.suppress(Exception):
                await rt.close()
        self._call = None

    # ---- instructions ----------------------------------------------------

    def _build_instructions(self) -> str:
        """Assemble the session instructions.

        Identity comes from Hermes, not from here. The operator's SOUL.md - the
        same slot Hermes injects into every chat prompt - leads, so "what is your
        name?" gets the same answer on a call as in chat. The provider's
        ``instructions`` setting stays what it should be: the voice-behaviour
        layer on top, about brevity and delegation, not a second persona.
        """
        parts: list[str] = []

        soul = soul_text()
        if soul:
            parts.append(
                "Your identity and persona (the same assistant the user knows from "
                f"chat):\n{soul}\n\nYou are currently speaking on a live Microsoft "
                "Microsoft Teams voice call."
            )
        parts.append(self._cfg.instructions)

        skills = skills_index_text()
        if skills:
            parts.append(
                "You also have the user's installed Hermes skills, the same ones "
                "available in chat. You cannot run them inside this voice loop: "
                "delegate that work with hermes_agent_consult and tell the caller "
                f"what you are doing. Installed skills:\n{skills}"
            )

        name = self._caller_first_name()
        if name:
            parts.append(
                f"CALLER IDENTITY: You are speaking with {name}. Greet them by their "
                "first name once, warmly and briefly, then continue naturally - do "
                "not repeat their name every turn."
            )

        phrases = ", ".join(f'"{p}"' for p in self._plugin.wake_phrases)
        parts.append(
            "If more than one person is on the call, stay silent unless someone "
            f"addresses you by name ({phrases}); in a one-on-one call respond normally."
        )

        languages = self._cfg.languages
        if languages:
            names = ", ".join(_LANGUAGE_NAMES.get(code, code) for code in languages)
            first = _LANGUAGE_NAMES.get(languages[0], languages[0])
            parts.append(
                f"You speak these languages: {names}. Reply in the caller's language "
                f"when it is one of them; otherwise politely continue in {first}. "
                "Switch when the caller switches, and translate on request."
            )
        else:
            parts.append("Detect the caller's language and reply in it; switch when they switch.")
        return " ".join(parts)

    async def set_call_language(self, code: str) -> None:
        """Pin the call to one language and push the rebuilt instructions live.

        Rebuilt, not patched: the persona, etiquette and skills clauses go with
        it, so a language change cannot quietly drop the rest of the prompt.
        """
        self._cfg = replace(self._cfg, languages=(code,))
        if self._rt is not None:
            await self._rt.update_instructions(self._build_instructions())

    def _caller_first_name(self) -> str:
        call = self._call
        name = (call.start.caller.display_name or "") if call is not None else ""
        return name.strip().split(" ")[0] if name.strip() else ""

    # ---- provider callbacks ----------------------------------------------

    async def _on_model_audio(self, pcm24: bytes) -> None:
        """The model's voice, on its way to the caller."""
        call = self._call
        if call is None or self._closed:
            return
        if self._drop_response:
            # A turn the gate refused. Drop the residual too: it belongs to this
            # response, and carrying it into the next one would splice half a
            # word onto the front of an answer somebody else asked for.
            self._aligner.reset()
            return
        pcm16 = resample_pcm16(pcm24, REALTIME_SAMPLE_RATE_HZ, SAMPLE_RATE_HZ)
        for frame in self._aligner.push(pcm16):
            await call.send_audio(frame)
            # The only duration this worker genuinely knows. A realtime model
            # hands back no timings, and a guess from text length drifts further
            # out of step with the voice the longer the turn runs.
            self._lip.audio_sent(frame)
            # Advance the playout clock by DURATION, not by frame count: it is
            # what the echo guard compares against wall time.
            self._echo.note_output(frame_duration_ms(frame))

    async def _on_response_done(self) -> None:
        """End of turn: flush the aligner so the last word is not clipped."""
        call = self._call
        tail = self._aligner.flush()
        if call is not None and tail is not None and not self._drop_response and not self._closed:
            with contextlib.suppress(Exception):
                await call.send_audio(tail)
                self._lip.audio_sent(tail)
                self._echo.note_output(frame_duration_ms(tail))
        await self._send_visemes()
        self._drop_response = False

    async def _send_visemes(self) -> None:
        """One timeline per turn, spread over the audio that turn actually sent.

        Cosmetic, so everything here is swallowed: the worst acceptable outcome
        is a still mouth over correct audio, and the unacceptable one is a
        dropped turn because a lip shape raised.
        """
        call = self._call
        text, self._reply_text = "".join(self._reply_text), []
        marks = []
        with contextlib.suppress(Exception):
            marks = self._lip.finish(text)
        if call is None or self._closed or not marks:
            return
        with contextlib.suppress(Exception):
            await call.send_speech_marks(marks)

    async def _on_reply_text(self, text: str) -> None:
        """A piece of what the model is saying, as it is being said.

        Held for the viseme timeline, and read for the face. Re-read on every
        piece rather than at the end, because waiting for the final transcript
        leaves the face wrong for the whole time the reply is being spoken.
        """
        if self._closed or self._drop_response:
            return
        self._reply_text.append(text)
        call = self._call
        if call is None:
            return
        with contextlib.suppress(Exception):
            emotion = self._cue.cue("".join(self._reply_text))
            if emotion:
                await call.express(emotion)

    async def _on_barge_in(self) -> None:
        """The caller started speaking over us."""
        await self._cut_playback()

    async def _cut_playback(self) -> None:
        """Stop being heard, in the only order that works.

        ``cancel_playback`` first: it is the one lever that un-sends audio
        StandIn already has. Cancelling the model first stops it generating, but
        the caller still hears every buffered sample already handed over - which
        is the whole length of the answer they interrupted.
        """
        call, rt = self._call, self._rt
        if call is None or self._closed:
            return
        await call.cancel_playback()
        self._aligner.reset()
        # The service flushes audio the caller never heard. Keeping the count
        # would spread the NEXT turn's words over its own audio plus the audio
        # that was thrown away, and the mouth would run long for the rest of it.
        self._lip.cancel()
        self._reply_text = []
        self._echo.collapse()
        self._echo.mark_caller_turn()
        if rt is not None:
            await rt.cancel_response()

    async def _on_input_transcript(self, text: str) -> None:
        """The caller's finished turn: verbal interrupts, then the group gate."""
        rt = self._rt
        if rt is None or self._closed:
            return
        self._echo.mark_caller_turn()

        if is_verbal_interrupt(text, self._plugin.wake_phrases):
            # Suppress any reply to the interruption itself: "stop" does not
            # want an answer, it wants silence.
            self._drop_response = True
            await self._cut_playback()
            return

        gate = self._gate
        if gate is None:
            return
        decision = gate.decide(text, time.monotonic() * 1000.0)
        if decision.respond:
            # Clear a drop latched by an earlier unaddressed turn. That turn
            # created no response, so no response.done ever fired to reset it,
            # and left latched it would eat THIS answer's audio instead.
            self._drop_response = False
            if not self._auto_on:
                await rt.create_response()
        else:
            self._drop_response = True
            await rt.cancel_response()

    async def _on_function_call(self, name: str, call_id: str, args_json: str) -> None:
        """Run a tool the model called, and hand the result back."""
        runner, rt = self._tools, self._rt
        if runner is None or rt is None or self._closed:
            return
        call = self._call
        thinking = self._cue.thinking(True)
        if call is not None and thinking:
            with contextlib.suppress(Exception):
                await call.express(thinking)
        try:
            result = await runner.run(name, ToolRunner.parse_args(args_json))
            await rt.send_function_result(call_id, result or "Done.")
        finally:
            # In a finally because the model may stay SILENT after a tool
            # result, success or failure: with no reply text to re-read, the
            # face would stay mid-think for the rest of the call.
            done = self._cue.thinking(False)
            if call is not None and done:
                with contextlib.suppress(Exception):
                    await call.express(done)

    async def _on_provider_error(self, error: Any) -> None:
        """A provider error event. The session already cleared its response latch."""
        logger.warning("standin: realtime provider error on call %s: %s", self._call_id(), error)

    async def _on_provider_closed(self, reason: str) -> None:
        """The provider dropped the socket. End the call rather than sit mute."""
        call = self._call
        if call is not None and not self._closed:
            await call.end(f"realtime-{reason}")

    # ---- helpers ---------------------------------------------------------

    def _can_gate(self) -> bool:
        """Is there a caller transcript to gate on?"""
        return bool(self._cfg.input_transcribe_model)

    def _call_id(self) -> str:
        return self._call.call_id if self._call is not None else "?"

    async def _sync_auto_response(self) -> None:
        """Let server VAD answer on its own only in a confirmed 1:1 call."""
        rt, gate = self._rt, self._gate
        if rt is None or gate is None or not self._can_gate():
            return
        enable = not gate.is_group
        await rt.set_auto_response(enable)
        self._auto_on = enable

    async def _maybe_greet(self) -> None:
        """Speak first, once, at the right moment.

        On an outbound call the recording going active is the proxy for "they
        picked up", so greeting earlier would talk into a ringing phone. Inbound
        has already been answered, so when the operator has turned the recording
        requirement off there is nothing left to wait for - and waiting anyway is
        what once left the assistant mute for entire calls, because on that
        configuration the transition never arrives.
        """
        rt, call = self._rt, self._call
        if rt is None or call is None or self._greeted or self._closed:
            return
        outbound = call.start.direction == "outbound"
        if not self._recording_active and (self._plugin.require_recording or outbound):
            return
        self._greeted = True
        name = self._caller_first_name()
        who = f" the caller, {name}," if name else " the caller"
        await rt.request_say(f"Greet{who} warmly and briefly, then ask how you can help.")
