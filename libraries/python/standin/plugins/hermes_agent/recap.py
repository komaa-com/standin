# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""End-of-call meeting recap for the Hermes plugin.

The SDK already owns Transcript, resolve_minutes_target and post_minutes. This
file is the missing wire: collect turns during the call, and at hang-up post
minutes into the Teams chat through the chat lane the platform started.

The consult that writes the minutes can take tens of seconds, so hang-up must
not wait for it. Held only in memory, that promise dies with the process. Held
here it is a restart-recoverable local spool: a small file, written BEFORE the
task is scheduled and removed only once the minutes have actually been
delivered, expired (24 hours), or hit the retry limit. The file is customer
meeting data. A restart of this process (or a plugin reload that opens the chat
lane again) drains what the last one left.

A restart still loses the spool if the directory dies with the process. Set
``STANDIN_RECAP_DIR`` (or ``STANDIN_STATE_DIR``) to persistent storage when recap
reliability matters. That is the same bound as the SDK's other on-disk promises.

Recording-banner wait and session scope already live in this plugin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from standin.minutes import (
    DeliveryTarget,
    Transcript,
    post_minutes,
    resolve_minutes_target,
)
from standin.outbound import state_dir

from .log import logger

__all__ = [
    "CHAT",
    "PERSONAL_CHATS",
    "drain_recap_spool",
    "recap_dir",
    "run_meeting_recap",
    "schedule_meeting_recap",
    "set_chat_lane",
]

#: How long a recap may sit before delivering it is worse than dropping it.
_RECAP_TTL_S = 24 * 60 * 60

#: How many recaps one drain will take on. What is left stays queued.
_RESUME_LIMIT = 32

#: Bound on files in the spool, claimed and waiting together.
_MAX_RECAP_JOBS = 32

#: A recap that fails the consult or the send this many times is dropped.
_MAX_ATTEMPTS = 8

#: A claim older than this belonged to a process that died holding it.
_STALE_CLAIM_S = 600.0

#: Posted, finished with nothing to retry, or keep the spool for another try.
RecapStatus = Literal["posted", "done", "retry"]

#: The chat lane the platform started, if any. Recap posts through it. None is
#: the normal state when the host did not give a respond callable.
CHAT: Any = None
PERSONAL_CHATS: Any = None


def set_chat_lane(chat: Any, chats: Any = None) -> None:
    """Called from platform connect/disconnect. Safe to call with None."""
    global CHAT, PERSONAL_CHATS
    CHAT = chat
    PERSONAL_CHATS = chats


def recap_dir() -> Path:
    """Where unfinished recaps sit.

    ``STANDIN_RECAP_DIR`` when set, otherwise ``~/.standin/state/recap`` (or
    ``STANDIN_STATE_DIR/recap``). Deliberately not a temp directory: a temp
    directory passes every test and loses every recap on the next reboot.
    """
    configured = os.environ.get("STANDIN_RECAP_DIR", "").strip()
    path = Path(configured) if configured else state_dir() / "recap"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class RecapJob:
    """One recap, as it sits on disk."""

    job_id: str
    call_id: str
    thread_id: str
    tenant_id: str
    caller_aad_id: str
    participant_count: int
    session_key: str
    turns: tuple[dict[str, str], ...]
    visuals: tuple[str, ...]
    destinations: tuple[dict[str, str], ...]
    created_ms: int
    attempts: int = 0
    claim_path: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "v": 1,
            "jobId": self.job_id,
            "callId": self.call_id,
            "threadId": self.thread_id,
            "tenantId": self.tenant_id,
            "callerAadId": self.caller_aad_id,
            "participantCount": self.participant_count,
            "sessionKey": self.session_key,
            "turns": list(self.turns),
            "visuals": list(self.visuals),
            "destinations": list(self.destinations),
            "createdMs": self.created_ms,
            "attempts": self.attempts,
        }

    @staticmethod
    def from_json(raw: dict[str, Any], claim_path: str = "") -> RecapJob:
        turns: list[dict[str, str]] = []
        for item in raw.get("turns") or []:
            if not isinstance(item, dict):
                continue
            speaker = str(item.get("speaker") or "")
            text = str(item.get("text") or "")
            role = str(item.get("role") or "caller")
            if role not in ("assistant", "caller"):
                role = "caller"
            if text:
                turns.append({"speaker": speaker, "text": text, "role": role})
        visuals = tuple(str(v) for v in (raw.get("visuals") or []) if str(v).strip())
        destinations: list[dict[str, str]] = []
        for item in raw.get("destinations") or []:
            if not isinstance(item, dict):
                continue
            conversation_id = str(item.get("conversationId") or "").strip()
            tenant_id = str(item.get("tenantId") or "").strip()
            kind = str(item.get("kind") or "thread")
            if kind not in ("thread", "caller-dm"):
                kind = "thread"
            if conversation_id:
                destinations.append(
                    {
                        "kind": kind,
                        "conversationId": conversation_id,
                        "tenantId": tenant_id,
                    }
                )
        return RecapJob(
            job_id=str(raw.get("jobId") or ""),
            call_id=str(raw.get("callId") or ""),
            thread_id=str(raw.get("threadId") or ""),
            tenant_id=str(raw.get("tenantId") or ""),
            caller_aad_id=str(raw.get("callerAadId") or ""),
            participant_count=int(raw.get("participantCount") or 1),
            session_key=str(raw.get("sessionKey") or ""),
            turns=tuple(turns),
            visuals=visuals,
            destinations=tuple(destinations),
            created_ms=int(raw.get("createdMs") or 0),
            attempts=int(raw.get("attempts") or 0),
            claim_path=claim_path,
        )


class RecapJobs:
    """Unfinished recaps that survive the process that promised them.

    The two-phase claim is what makes a restart safe. Waiting is a ``.json``;
    being run is a ``.claimed``. A process that dies mid-run leaves a claim
    behind, and the next startup takes it back once it is old enough to be
    certain nobody is still working on it, or immediately when this process
    holds no in-flight recap of its own. A delivery that fails puts the job
    back rather than dropping it.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or recap_dir()
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def remember(self, job: RecapJob) -> RecapJob | None:
        """Write the recap down, before starting the work.

        Returns ``None`` when it could not be written, which is a recap that
        still runs but will not survive a restart.
        """
        self._trim()
        target = self._dir / f"{job.job_id}.json"
        temp = self._dir / f".{job.job_id}.{uuid.uuid4().hex}.tmp"
        try:
            temp.write_text(json.dumps(job.as_json(), ensure_ascii=False), encoding="utf-8")
            temp.chmod(0o600)
            temp.replace(target)
        except OSError as err:
            logger.warning("standin: a meeting recap could not be made durable: %s", err)
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)
            return None
        return job

    def begin(self, job: RecapJob | None) -> RecapJob | None:
        """Mark a recap as being worked on, so a crash mid-run is recoverable."""
        if job is None or not job.job_id:
            return job
        source = self._dir / f"{job.job_id}.json"
        claim = self._dir / f"{job.job_id}.claimed"
        try:
            source.rename(claim)
        except OSError:
            return job
        return replace(job, claim_path=str(claim))

    def finish(self, job: RecapJob | None, *, complete: bool) -> None:
        """Retire a claimed recap, or put it back so the next cycle retries it."""
        if job is None or not job.job_id:
            return
        claim = Path(job.claim_path) if job.claim_path else self._dir / f"{job.job_id}.claimed"
        waiting = self._dir / f"{job.job_id}.json"
        if complete:
            for path in (claim, waiting):
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
            return
        updated = replace(job, attempts=job.attempts + 1, claim_path="")
        temp = self._dir / f".{job.job_id}.{uuid.uuid4().hex}.tmp"
        try:
            temp.write_text(json.dumps(updated.as_json(), ensure_ascii=False), encoding="utf-8")
            temp.chmod(0o600)
            temp.replace(waiting)
        except OSError as err:
            logger.warning("standin: a meeting recap could not be put back: %s", err)
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)
            return
        if claim.exists() and claim.resolve() != waiting.resolve():
            with contextlib.suppress(OSError):
                claim.unlink(missing_ok=True)

    def claim_pending(self, *, steal_claims: bool) -> list[RecapJob]:
        """Take the recaps a previous process left behind.

        Claiming is a rename, so two drains cannot take the same job.
        """
        now = time.time()
        self._recover_orphans(now, stale_s=0.0 if steal_claims else _STALE_CLAIM_S)

        claimed: list[RecapJob] = []
        for path in sorted(self._dir.glob("*.json")):
            if len(claimed) >= _RESUME_LIMIT:
                logger.info("standin: recap drain limit reached; the remaining recaps stay queued")
                break
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                continue
            if not isinstance(record, dict) or int(record.get("v") or 0) != 1:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                continue

            job = RecapJob.from_json(record)
            age_s = (_now_ms() - job.created_ms) / 1000 if job.created_ms else 0
            if job.created_ms and age_s > _RECAP_TTL_S:
                logger.info(
                    "standin: dropping a meeting recap promised %.0f hours ago", age_s / 3600
                )
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                continue
            if job.attempts >= _MAX_ATTEMPTS:
                logger.info("standin: dropping a meeting recap that failed too many times")
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                continue
            if not job.turns and not job.visuals:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                continue

            claim = path.with_suffix(".claimed")
            try:
                path.rename(claim)
            except OSError:
                continue
            claimed.append(RecapJob.from_json(record, claim_path=str(claim)))
        return claimed

    def _recover_orphans(self, now: float, *, stale_s: float) -> None:
        for path in self._dir.glob("*.claimed"):
            with contextlib.suppress(OSError):
                if now - path.stat().st_mtime >= stale_s:
                    path.rename(path.with_suffix(".json"))

    def _trim(self) -> None:
        waiting = sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        claimed = list(self._dir.glob("*.claimed"))
        overflow = len(waiting) + len(claimed) - _MAX_RECAP_JOBS + 1
        for path in waiting:
            if overflow <= 0:
                return
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            overflow -= 1


def _job_from_session(
    *,
    session: Any,
    transcript: Transcript,
    session_id: str,
) -> RecapJob | None:
    if transcript.empty:
        return None
    start = session.start
    caller = start.caller
    thread_id = (start.thread_id or "").strip()
    caller_aad = (caller.aad_id or "").strip()
    if not thread_id and not caller_aad:
        return None
    tenant = (start.tenant_id or "").strip()
    turns = tuple(
        {"speaker": turn.speaker, "text": turn.text, "role": turn.role} for turn in transcript.turns
    )
    visuals = tuple(transcript.visuals)
    return RecapJob(
        job_id=uuid.uuid4().hex,
        call_id=str(getattr(session, "call_id", "") or ""),
        thread_id=thread_id,
        tenant_id=tenant,
        caller_aad_id=caller_aad,
        participant_count=int(getattr(session, "participant_count", 1) or 1),
        session_key=session_id,
        turns=turns,
        visuals=visuals,
        destinations=tuple(_destinations_for(session)),
        created_ms=_now_ms(),
    )


def _destinations_for(session: Any) -> list[dict[str, str]]:
    target = _resolve_target(session)
    if target is None:
        return []
    return [
        {
            "kind": target.kind,
            "conversationId": target.conversation_id,
            "tenantId": target.tenant_id,
        }
    ]


def _resolve_target(session: Any) -> DeliveryTarget | None:
    start = session.start
    caller = start.caller
    tenant = (start.tenant_id or "").strip()
    chats = PERSONAL_CHATS
    caller_chat = None
    if chats is not None and caller.aad_id and tenant:
        caller_chat = chats.for_caller(
            caller_aad_id=caller.aad_id,
            tenant_id=tenant,
        )
    return resolve_minutes_target(
        thread_id=start.thread_id,
        human_count=getattr(session, "participant_count", 1) or 1,
        caller_aad_id=caller.aad_id,
        caller_chat=caller_chat,
        session_tenant_id=tenant or None,
    )


def _targets_from_job(job: RecapJob) -> list[DeliveryTarget]:
    targets: list[DeliveryTarget] = []
    for item in job.destinations:
        conversation_id = item.get("conversationId") or ""
        if not conversation_id:
            continue
        kind: Literal["thread", "caller-dm"] = (
            "caller-dm" if item.get("kind") == "caller-dm" else "thread"
        )
        targets.append(
            DeliveryTarget(
                kind=kind,
                conversation_id=conversation_id,
                tenant_id=item.get("tenantId") or "",
            )
        )
    return targets


def _session_from_job(job: RecapJob) -> Any:
    caller = SimpleNamespace(aad_id=job.caller_aad_id or None)
    start = SimpleNamespace(
        thread_id=job.thread_id,
        tenant_id=job.tenant_id or None,
        caller=caller,
    )
    return SimpleNamespace(
        start=start,
        call_id=job.call_id,
        participant_count=job.participant_count or 1,
    )


def _transcript_from_job(job: RecapJob) -> Transcript:
    transcript = Transcript()
    for turn in job.turns:
        role: Literal["assistant", "caller"] = (
            "assistant" if turn.get("role") == "assistant" else "caller"
        )
        transcript.add(turn.get("speaker") or "", turn.get("text") or "", role=role)
    for shown in job.visuals:
        transcript.add_visual(shown)
    return transcript


async def run_meeting_recap(
    *,
    session: Any,
    transcript: Transcript,
    consult: Any,
    enabled: bool,
    destinations: list[DeliveryTarget] | None = None,
) -> RecapStatus:
    """Write the meeting up. Never raises: this runs during teardown.

    Returns ``posted`` when the minutes reached chat, ``done`` when there is
    nothing left to try (disabled, empty transcript, nowhere to post), and
    ``retry`` when a later drain could still succeed (no chat lane, the
    summariser failed, or the send failed).
    """
    if not enabled:
        return "done"
    if transcript.empty:
        return "done"
    # Before anything is spent: with no lane there is nowhere to post, and the
    # summarising consult is the expensive part.
    if CHAT is None:
        logger.warning("standin: meeting_recap is on but no chat lane is open; no minutes posted")
        return "retry"
    try:
        target: DeliveryTarget | list[DeliveryTarget] | None = destinations
        if not target:
            target = _resolve_target(session)
        if not target:
            logger.info("standin: no minutes posted; this call has no Microsoft Teams chat")
            return "done"

        async def summarise(prompt: str) -> str:
            if consult is None:
                return ""
            return await consult.ask(prompt)

        async def deliver(destination: Any, text: str) -> bool:
            chat = CHAT
            if chat is None:
                logger.info("standin: meeting recap has nowhere to post; chat lane is off")
                return False
            return await chat.send(
                tenant_id=destination.tenant_id,
                conversation_id=destination.conversation_id,
                text=text,
            )

        result = await post_minutes(summarise, transcript, target, deliver)
        logger.info(
            "standin: meeting recap %s for %s",
            "posted" if result.delivered else "not posted",
            getattr(session, "call_id", ""),
        )
        if result.delivered:
            return "posted"
        if result.minutes:
            return "retry"
        return "retry"
    except Exception:
        logger.exception("standin: meeting recap failed")
        return "retry"


#: Recaps in flight. A task nobody references can be collected mid-run; this set
#: holds each one until it finishes. Drain uses it to decide whether a leftover
#: ``.claimed`` file is still owned by this process.
_TASKS: set[asyncio.Task[None]] = set()


async def _run_and_finish(
    job: RecapJob | None,
    *,
    session: Any,
    transcript: Transcript,
    consult: Any,
    destinations: list[DeliveryTarget] | None,
) -> None:
    store = RecapJobs()
    if job is None:
        # Disk refused the write. Still try once in memory: a recap that
        # cannot survive a restart is better than no recap at all.
        await run_meeting_recap(
            session=session,
            transcript=transcript,
            consult=consult,
            enabled=True,
            destinations=destinations,
        )
        return
    held = store.begin(job)
    if held is None or not held.claim_path:
        # Another drain already claimed this file. Running it here would post
        # the same minutes twice.
        return
    status: RecapStatus = "retry"
    try:
        status = await run_meeting_recap(
            session=session,
            transcript=transcript,
            consult=consult,
            enabled=True,
            destinations=destinations,
        )
    finally:
        store.finish(held, complete=status != "retry")


def schedule_meeting_recap(
    *,
    session: Any,
    transcript: Transcript,
    consult: Any,
    enabled: bool,
    session_id: str = "",
) -> asyncio.Task[None] | None:
    """Run the recap without holding the call's teardown.

    The SDK awaits the handler's ``aclose`` before it ends the session, closes
    the socket and frees the connection slot, and a recap consult can take tens
    of seconds. The transcript is on disk before this returns. Never raises.
    """
    if not enabled:
        return None
    job = _job_from_session(session=session, transcript=transcript, session_id=session_id)
    if job is None:
        return None
    stored = RecapJobs().remember(job)
    destinations = _targets_from_job(job)
    try:
        task = asyncio.get_running_loop().create_task(
            _run_and_finish(
                stored,
                session=session,
                transcript=transcript,
                consult=consult,
                destinations=destinations or None,
            ),
            name=f"standin-recap-{getattr(session, 'call_id', '')}",
        )
    except Exception:
        logger.exception("standin: meeting recap could not be scheduled")
        return None
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def drain_recap_spool(*, consult: Any | None = None) -> int:
    """Finish recaps a previous process left behind. Returns how many posted.

    Call it once the chat lane is open. Sequential on purpose: a restart storm
    must not fan out one consult per interrupted recap. Uses the current chat
    lane, so a plugin reload cannot post through a lane that was already closed.

    When ``consult`` is omitted, each job builds a Hermes :class:`AgentConsult`
    from the stored session key.
    """
    if CHAT is None:
        return 0
    store = RecapJobs()
    posted = 0
    for job in store.claim_pending(steal_claims=not _TASKS):
        logger.info("standin: finishing a meeting recap left by a restart")
        agent = consult
        if agent is None:
            from .consult import AgentConsult

            agent = AgentConsult(session_id=job.session_key or None)
        status = await run_meeting_recap(
            session=_session_from_job(job),
            transcript=_transcript_from_job(job),
            consult=agent,
            enabled=True,
            destinations=_targets_from_job(job) or None,
        )
        store.finish(job, complete=status != "retry")
        if status == "posted":
            posted += 1
        elif status == "retry":
            logger.warning("standin: a meeting recap is kept for the next try")
    return posted
