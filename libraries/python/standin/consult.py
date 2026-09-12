# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The slow half of a two-speed agent.

A voice model has to answer in under a second or the call sounds broken. Real
work does not fit in a second. Looking something up, reading a file, driving a
browser, running a skill: those take ten seconds, or five minutes, and a caller
listening to silence has no way to tell the difference between thinking and
crashed.

So an agent that does real work is two agents. The fast one talks. The slow one
works. This module is the seam between them, and it is in the core because the
seam is the same whichever provider is doing the talking and whichever agent
framework is doing the working.

Two paths, and the difference between them is a promise:

:class:`Consultant`
    "Hold on, let me check." Time-boxed, answered in the same breath. The caller
    waits, so the box has to be small.

:class:`BackgroundTasks`
    "I'll send you the result." The caller hangs up. That promise outlives the
    call, and therefore has to outlive the process: a restart between making it
    and keeping it is ordinary, and an in-memory task list breaks the promise
    silently. Every task is on disk before the work starts.

Neither knows what your agent is. You supply a callable; what it does inside is
yours.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._hmac import now_ms
from .calltools import ToolSpec
from .log import logger
from .outbound import state_dir

__all__ = [
    "BACKGROUND_TASK_TOOL",
    "CONSULT_TOOL",
    "BackgroundTask",
    "BackgroundTasks",
    "Consultant",
]

#: How long a caller will wait on the line before "let me check" stops sounding
#: like thinking and starts sounding like a dropped call.
DEFAULT_CONSULT_TIMEOUT_S = 45.0

#: A background task gets far longer, because nobody is listening to it.
DEFAULT_TASK_TIMEOUT_S = 300.0

#: Promises go stale. Delivering a two-hour-old answer to a question somebody
#: has long since answered themselves is worse than delivering nothing.
DEFAULT_TASK_TTL_S = 2 * 60 * 60

#: How many interrupted tasks one startup will take on. A restart storm must not
#: fan out every promise the fleet ever made at once. What is left stays queued.
DEFAULT_RESUME_LIMIT = 5

#: A claim older than this belonged to a process that died holding it.
_STALE_CLAIM_S = 600.0

#: What a consultation is: a question in, an answer out. Sync or async; a sync
#: one is run off the event loop, because blocking the loop stops the audio.
Asker = Callable[[str], "str | Awaitable[str]"]

#: Called to build an asker, on first use and again after a timeout. It is a
#: factory rather than a value because a timed-out consultation cannot be
#: killed: the thread runs on, and whatever state it holds is no longer yours.
AskerFactory = Callable[[], Asker]

#: Deliver a finished task's result. Returns whether it actually went out.
Deliverer = Callable[[str, str], Awaitable[bool]]


CONSULT_TOOL = ToolSpec(
    name="consult",
    description=(
        "Hand a question or a task to your fuller agent, which can look things up, read "
        "files, browse, and run tools. Use it for anything beyond conversation. The caller "
        "waits on the line, so use it only when the answer will come back quickly."
    ),
    parameters={
        "query": {"type": "string", "description": "What to look into or do, phrased as a task."}
    },
    required=("query",),
)

BACKGROUND_TASK_TOOL = ToolSpec(
    name="background_task",
    description=(
        "Start a long job and have the result sent to the caller in chat when it is done. "
        "Use this instead of consult when the work will not finish while the caller waits. "
        "Tell the caller you are on it; the result is delivered afterwards."
    ),
    parameters={"query": {"type": "string", "description": "The task to run in the background."}},
    required=("query",),
)


def _is_async(asker: Asker) -> bool:
    """Whether calling this returns a coroutine, decided WITHOUT calling it.

    It has to be decided first. By the time a blocking function has returned,
    it has already blocked, and the audio has already stopped.
    """
    # The second check catches a callable object with an async __call__, which
    # the first one does not see.
    return asyncio.iscoroutinefunction(asker) or asyncio.iscoroutinefunction(type(asker).__call__)


class Consultant:
    """One slow agent, reachable from a live call without breaking it.

    Built once per call and asked whenever the fast model delegates::

        consultant = Consultant(lambda: my_agent.run)
        answer = await consultant.ask("what did we bill Contoso last quarter?")

    Everything here exists because a caller is listening while it runs.

    **A blocking agent runs off the loop.** A sync callable goes to a thread.
    Called inline it would stop the audio for as long as it took, and the caller
    would hear the call itself stall.

    **One at a time, and the second asker is told so.** A model that can delegate
    can delegate twice before the first answer lands. Queueing the second behind
    the first means it waits out both timeouts and answers far too late, so it is
    refused immediately with something the model can say out loud.

    **A timeout is admitted, not papered over.** The work cannot be cancelled: a
    thread runs to its end whatever this returns. So the answer says it stopped
    rather than promising a follow-up nothing will send. Pass
    ``background_available=True`` when you have registered
    :data:`BACKGROUND_TASK_TOOL` and the answer will point the caller at it.
    """

    def __init__(
        self,
        build: AskerFactory,
        timeout_s: float = DEFAULT_CONSULT_TIMEOUT_S,
        background_available: bool = False,
    ) -> None:
        self._build = build
        self._timeout_s = timeout_s
        # What the caller is told on a timeout depends on whether there is
        # anywhere to hand the work off to. Pointing at a background task this
        # agent cannot start is a promise nothing keeps, so it is opt in.
        self._background_available = background_available
        self._asker: Asker | None = None
        self._busy = False

    @property
    def busy(self) -> bool:
        """Whether a consultation is running right now."""
        return self._busy

    async def ask(self, query: str, timeout_s: float | None = None) -> str:
        """Put a question to the slow agent and return what to say.

        Never raises, and always returns something speakable. A tool result goes
        straight back to a model that will read it out, so an exception here is
        an agent that goes quiet mid-sentence.
        """
        question = (query or "").strip()
        if not question:
            return "I did not catch what you wanted me to look into."
        if self._busy:
            return "I am still finishing the last one. Give me a moment and ask again."

        self._busy = True
        try:
            answer = await asyncio.wait_for(
                self._run(question), timeout=timeout_s or self._timeout_s
            )
        except asyncio.TimeoutError:
            # The work is still running and cannot be stopped, so the asker is
            # dropped: the next consultation builds a fresh one rather than
            # sharing state with something nobody is waiting for any more.
            self._asker = None
            logger.warning("standin: a consultation ran past its time and was abandoned")
            if self._background_available:
                return (
                    "Sorry, that took too long and I had to stop. Ask me to work on it in the "
                    "background and I will send you the result when it is done."
                )
            return "Sorry, that took too long and I had to stop. Ask me again and I will narrow it down."
        except Exception as err:
            logger.warning("standin: a consultation failed: %s", err)
            return "Sorry, I ran into a problem working on that."
        finally:
            self._busy = False

        return answer.strip() or "I did not find anything to report."

    async def _run(self, question: str) -> str:
        if self._asker is None:
            self._asker = self._build()
        asker = self._asker
        if _is_async(asker):
            return str(await asker(question))  # type: ignore[misc]
        # A blocking agent goes to a thread. Called inline it would hold the
        # event loop for its whole duration, and the audio with it. Deciding
        # before the call rather than after is the point: by the time a sync
        # function has returned, it has already blocked.
        result = await asyncio.to_thread(asker, question)
        if hasattr(result, "__await__"):
            return str(await result)  # type: ignore[misc]
        return str(result)


@dataclass(frozen=True)
class BackgroundTask:
    """One promise, as it sits on disk."""

    task_id: str
    query: str
    thread_id: str
    """Where the result goes. A task with nowhere to deliver cannot be kept."""
    session_key: str = ""
    created_ms: int = 0
    claim_path: str = ""
    """Set only on a claimed task, so :meth:`BackgroundTasks.finish` can put it
    back if the delivery fails."""

    def as_json(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "query": self.query,
            "threadId": self.thread_id,
            "sessionKey": self.session_key,
            "createdMs": self.created_ms or now_ms(),
        }

    @staticmethod
    def from_json(raw: dict[str, Any], claim_path: str = "") -> BackgroundTask:
        return BackgroundTask(
            task_id=str(raw.get("taskId") or ""),
            query=str(raw.get("query") or ""),
            thread_id=str(raw.get("threadId") or ""),
            session_key=str(raw.get("sessionKey") or ""),
            created_ms=int(raw.get("createdMs") or 0),
            claim_path=claim_path,
        )


class BackgroundTasks:
    """Promised work that survives the process that promised it.

    "I will send you the result" is a promise made on a call that is about to
    end. Held in memory it lasts until the next deploy, and then it is gone with
    nothing said to the person waiting. Held here it is a small file, written
    BEFORE the work starts and removed only once the result has actually been
    delivered.

    The two-phase claim is what makes a restart safe. A task waiting to run is a
    ``.json``; a task being run is a ``.claimed``. A process that dies mid-run
    leaves a claim behind, and the next startup takes it back once it is old
    enough to be certain nobody is still working on it. A delivery that fails
    puts the task back rather than dropping it.
    """

    def __init__(
        self,
        directory: Path | None = None,
        ttl_s: float = DEFAULT_TASK_TTL_S,
        resume_limit: int = DEFAULT_RESUME_LIMIT,
    ) -> None:
        self._dir = directory or (state_dir() / "tasks")
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._ttl_s = ttl_s
        self._resume_limit = max(1, resume_limit)

    def remember(self, query: str, thread_id: str, session_key: str = "") -> BackgroundTask | None:
        """Write the promise down, before starting the work.

        Returns ``None`` when it could not be written, which is a task that runs
        but will not survive a restart. That is worth knowing about and worth
        continuing with: a non-durable answer still beats no answer.
        """
        task = BackgroundTask(
            task_id=uuid.uuid4().hex,
            query=query,
            thread_id=thread_id,
            session_key=session_key,
            created_ms=now_ms(),
        )
        target = self._dir / f"{task.task_id}.json"
        temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(task.as_json(), ensure_ascii=False), encoding="utf-8")
            temp.chmod(0o600)
            temp.replace(target)
        except OSError as err:
            logger.warning("standin: a background task could not be made durable: %s", err)
            with contextlib.suppress(OSError):
                temp.unlink()
            return None
        return task

    def begin(self, task: BackgroundTask | None) -> None:
        """Mark a task as being worked on, so a crash mid-run is recoverable.

        Without this, a process that dies halfway leaves a task that looks
        untouched, and the next startup runs it again while the delivery from
        the first run may still be in flight.
        """
        if task is None or not task.task_id:
            return
        source = self._dir / f"{task.task_id}.json"
        with contextlib.suppress(OSError):
            source.rename(self._dir / f"{task.task_id}.claimed")

    def done(self, task: BackgroundTask | None) -> None:
        """The result was delivered. Retire the record, in either phase."""
        if task is None or not task.task_id:
            return
        for suffix in (".json", ".claimed"):
            with contextlib.suppress(OSError):
                (self._dir / f"{task.task_id}{suffix}").unlink(missing_ok=True)

    def claim_pending(self) -> list[BackgroundTask]:
        """Take the tasks a previous process left behind.

        Claiming is a rename, so two workers starting together cannot take the
        same task. Everything beyond the resume limit is LEFT WHERE IT IS for
        the next cycle rather than discarded: a busy restart must not become a
        quiet way of losing promises.
        """
        now = time.time()
        self._recover_orphans(now)

        claimed: list[BackgroundTask] = []
        for path in sorted(self._dir.glob("*.json")):
            if len(claimed) >= self._resume_limit:
                logger.info(
                    "standin: resume limit reached; the remaining background tasks stay queued"
                )
                break
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # Unreadable is unrecoverable, and leaving it means reading it
                # again on every startup for ever.
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                continue

            task = BackgroundTask.from_json(record)
            age_s = (now_ms() - task.created_ms) / 1000
            if task.created_ms and age_s > self._ttl_s:
                logger.info(
                    "standin: dropping a background task promised %.0f minutes ago", age_s / 60
                )
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                continue
            if not task.thread_id:
                logger.info("standin: dropping a background task with nowhere to deliver")
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                continue

            claim = path.with_suffix(".claimed")
            try:
                path.rename(claim)
            except OSError:
                continue  # somebody else took it first
            claimed.append(BackgroundTask.from_json(record, claim_path=str(claim)))
        return claimed

    def finish(self, task: BackgroundTask, delivered: bool) -> None:
        """Retire a claimed task, or put it back so the next cycle retries it."""
        if not task.claim_path:
            return
        claim = Path(task.claim_path)
        with contextlib.suppress(OSError):
            if delivered:
                claim.unlink(missing_ok=True)
            elif claim.exists():
                claim.rename(claim.with_suffix(".json"))

    async def resume(
        self,
        build: AskerFactory,
        deliver: Deliverer,
        timeout_s: float = DEFAULT_TASK_TIMEOUT_S,
    ) -> int:
        """Re-run what a previous process left unfinished. Returns how many landed.

        Call it once at startup. Sequential on purpose: a restart storm must not
        fan out one agent per interrupted task across the whole fleet.
        """
        delivered = 0
        for task in self.claim_pending():
            logger.info("standin: finishing a background task left by a restart")
            consultant = Consultant(build, timeout_s=timeout_s)
            answer = await consultant.ask(task.query, timeout_s=timeout_s)
            try:
                sent = await deliver(task.thread_id, answer)
            except Exception as err:
                logger.warning("standin: delivering a resumed background task failed: %s", err)
                sent = False
            self.finish(task, delivered=sent)
            if sent:
                delivered += 1
            else:
                logger.warning("standin: a resumed background task is kept for the next try")
        return delivered

    def _recover_orphans(self, now: float) -> None:
        """Put back claims whose owner died.

        Judged by the claim's own age. A claim younger than the window may still
        have somebody working on it, and taking it would deliver the same answer
        twice.
        """
        for path in self._dir.glob("*.claimed"):
            with contextlib.suppress(OSError):
                if now - path.stat().st_mtime > _STALE_CLAIM_S:
                    path.rename(path.with_suffix(".json"))
