# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The slow half of a two-speed agent.

Everything here is about a caller listening to silence. A consultation that
blocks the loop stops the audio; one that queues answers far too late; one that
raises leaves the agent with nothing to say. And a promise to send a result
later has to survive the deploy that happens between making it and keeping it.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from standin.consult import (
    BACKGROUND_TASK_TOOL,
    CONSULT_TOOL,
    BackgroundTask,
    BackgroundTasks,
    Consultant,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------- consultation


async def test_an_answer_comes_back_as_a_sentence():
    consultant = Consultant(lambda: lambda q: f"the answer to {q}")
    assert await consultant.ask("the billing question") == "the answer to the billing question"


async def test_an_async_agent_works_too():
    async def ask(query: str) -> str:
        return "looked it up"

    assert await Consultant(lambda: ask).ask("anything") == "looked it up"


async def test_a_blocking_agent_does_not_stop_the_audio():
    """A sync agent called inline would hold the event loop for its whole
    duration, and the call would stall with it."""
    ticks = 0

    async def keep_ticking() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.001)

    ticker = asyncio.create_task(keep_ticking())
    try:
        answer = await Consultant(lambda: lambda q: (time.sleep(0.15), "done")[1]).ask("x")
    finally:
        ticker.cancel()
    assert answer == "done"
    assert ticks > 1, "the loop was blocked for the whole consultation"


async def test_an_empty_question_is_answered_not_run():
    ran = False

    def ask(query: str) -> str:
        nonlocal ran
        ran = True
        return "x"

    assert "did not catch" in await Consultant(lambda: ask).ask("   ")
    assert ran is False


async def test_a_second_consultation_is_refused_rather_than_queued():
    """Queued, it would wait out both timeouts and answer long after the caller
    stopped caring. Refused, the model has something to say now."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def ask(query: str) -> str:
        started.set()
        await release.wait()
        return "first"

    consultant = Consultant(lambda: ask)
    first = asyncio.create_task(consultant.ask("one"))
    await started.wait()

    assert "still finishing" in await consultant.ask("two")
    release.set()
    assert await first == "first"
    # And the door is open again once it finishes.
    assert consultant.busy is False


async def test_a_timeout_is_admitted_rather_than_papered_over():
    """The work cannot be cancelled, so promising a follow-up would promise
    something nothing will send."""

    async def ask(query: str) -> str:
        await asyncio.sleep(10)
        return "too late"

    answer = await Consultant(lambda: ask, timeout_s=0.01).ask("x")
    assert "took too long" in answer
    # No background tool was registered, so it must not point at one.
    assert "background" not in answer


async def test_a_timeout_points_at_the_background_path_when_there_is_one():
    async def ask(query: str) -> str:
        await asyncio.sleep(10)
        return "too late"

    answer = await Consultant(lambda: ask, timeout_s=0.01, background_available=True).ask("x")
    assert "background" in answer


async def test_a_timeout_builds_a_fresh_agent_next_time():
    """The abandoned run keeps going and still holds whatever state it had.
    Sharing that with the next consultation is how one slow question corrupts
    the one after it."""
    built = 0

    def build():
        nonlocal built
        built += 1
        slow = built == 1

        async def ask(query: str) -> str:
            if slow:
                await asyncio.sleep(10)
            return "fresh"

        return ask

    consultant = Consultant(build, timeout_s=0.01)
    await consultant.ask("one")
    assert await consultant.ask("two") == "fresh"
    assert built == 2


async def test_a_failing_agent_never_reaches_the_model_as_an_exception():
    def ask(query: str) -> str:
        raise RuntimeError("the agent is down")

    assert "ran into a problem" in await Consultant(lambda: ask).ask("x")


async def test_an_empty_answer_is_still_something_to_say():
    assert "did not find anything" in await Consultant(lambda: lambda q: "  ").ask("x")


def test_both_tools_are_described_for_a_model():
    for spec in (CONSULT_TOOL, BACKGROUND_TASK_TOOL):
        assert "use" in spec.description.lower()
        assert spec.required == ("query",)
    # The two have to be distinguishable, or a model picks whichever comes first
    # and every long job is run while somebody waits on the line.
    assert "waits on the line" in CONSULT_TOOL.description
    assert "will not finish while the caller waits" in BACKGROUND_TASK_TOOL.description


# --------------------------------------------------------- background tasks


def test_a_promise_is_on_disk_before_the_work_starts(tmp_path):
    tasks = BackgroundTasks(directory=tmp_path)
    task = tasks.remember("find the Contoso numbers", thread_id="19:thread")
    assert task is not None
    record = json.loads((tmp_path / f"{task.task_id}.json").read_text())
    assert record["query"] == "find the Contoso numbers"
    assert record["threadId"] == "19:thread"


def test_a_delivered_task_is_retired(tmp_path):
    tasks = BackgroundTasks(directory=tmp_path)
    task = tasks.remember("x", thread_id="19:thread")
    tasks.begin(task)
    assert list(tmp_path.glob("*.claimed"))
    tasks.done(task)
    assert list(tmp_path.iterdir()) == []


def test_an_interrupted_task_is_taken_back_by_the_next_process(tmp_path):
    """The whole point: a deploy between promising and delivering must not lose
    the promise."""
    first = BackgroundTasks(directory=tmp_path)
    first.remember("the interrupted question", thread_id="19:thread")

    second = BackgroundTasks(directory=tmp_path)
    claimed = second.claim_pending()
    assert [task.query for task in claimed] == ["the interrupted question"]
    assert claimed[0].claim_path


def test_a_task_is_claimed_once_even_by_two_workers(tmp_path):
    """Two workers starting together is what running more than one means."""
    BackgroundTasks(directory=tmp_path).remember("x", thread_id="19:t")
    first = BackgroundTasks(directory=tmp_path).claim_pending()
    second = BackgroundTasks(directory=tmp_path).claim_pending()
    assert len(first) == 1
    assert second == []


def test_a_failed_delivery_puts_the_task_back(tmp_path):
    tasks = BackgroundTasks(directory=tmp_path)
    tasks.remember("x", thread_id="19:t")
    claimed = tasks.claim_pending()[0]

    tasks.finish(claimed, delivered=False)
    assert len(BackgroundTasks(directory=tmp_path).claim_pending()) == 1


def test_a_claim_from_a_crashed_process_is_recovered(tmp_path):
    tasks = BackgroundTasks(directory=tmp_path)
    task = tasks.remember("x", thread_id="19:t")
    tasks.begin(task)
    claim = tmp_path / f"{task.task_id}.claimed"
    # Old enough that nobody can still be working on it.
    old = time.time() - 3600
    import os

    os.utime(claim, (old, old))

    assert len(BackgroundTasks(directory=tmp_path).claim_pending()) == 1


def test_a_fresh_claim_is_left_alone(tmp_path):
    """Taking a claim somebody is still working on delivers the same answer
    twice."""
    tasks = BackgroundTasks(directory=tmp_path)
    tasks.begin(tasks.remember("x", thread_id="19:t"))
    assert BackgroundTasks(directory=tmp_path).claim_pending() == []


def test_a_stale_promise_is_dropped_rather_than_kept(tmp_path):
    """An answer two hours after the question is worse than no answer."""
    tasks = BackgroundTasks(directory=tmp_path, ttl_s=0.001)
    tasks.remember("x", thread_id="19:t")
    time.sleep(0.01)
    assert tasks.claim_pending() == []
    assert list(tmp_path.iterdir()) == []


def test_a_task_with_nowhere_to_deliver_is_dropped(tmp_path):
    tasks = BackgroundTasks(directory=tmp_path)
    tasks.remember("x", thread_id="")
    assert tasks.claim_pending() == []


def test_beyond_the_resume_limit_tasks_stay_queued_rather_than_vanish(tmp_path):
    """A restart storm must not become a quiet way of losing promises."""
    tasks = BackgroundTasks(directory=tmp_path, resume_limit=2)
    for index in range(5):
        tasks.remember(f"task {index}", thread_id="19:t")

    assert len(tasks.claim_pending()) == 2
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_an_unreadable_record_is_removed_rather_than_read_for_ever(tmp_path):
    (tmp_path / "broken.json").write_text("{not json")
    tasks = BackgroundTasks(directory=tmp_path)
    assert tasks.claim_pending() == []
    assert list(tmp_path.iterdir()) == []


async def test_resuming_runs_the_work_and_delivers_it(tmp_path):
    BackgroundTasks(directory=tmp_path).remember("the question", thread_id="19:thread")

    sent: list[tuple[str, str]] = []

    async def deliver(thread_id: str, text: str) -> bool:
        sent.append((thread_id, text))
        return True

    resumed = BackgroundTasks(directory=tmp_path)
    assert await resumed.resume(lambda: lambda q: f"answer to {q}", deliver) == 1
    assert sent == [("19:thread", "answer to the question")]
    assert list(tmp_path.iterdir()) == []


async def test_a_delivery_that_fails_leaves_the_promise_for_the_next_try(tmp_path):
    BackgroundTasks(directory=tmp_path).remember("the question", thread_id="19:thread")

    async def deliver(thread_id: str, text: str) -> bool:
        return False

    resumed = BackgroundTasks(directory=tmp_path)
    assert await resumed.resume(lambda: lambda q: "answer", deliver) == 0
    assert len(BackgroundTasks(directory=tmp_path).claim_pending()) == 1


async def test_a_delivery_that_raises_is_treated_as_undelivered(tmp_path):
    BackgroundTasks(directory=tmp_path).remember("the question", thread_id="19:thread")

    async def deliver(thread_id: str, text: str) -> bool:
        raise RuntimeError("the chat endpoint is down")

    resumed = BackgroundTasks(directory=tmp_path)
    assert await resumed.resume(lambda: lambda q: "answer", deliver) == 0
    assert len(BackgroundTasks(directory=tmp_path).claim_pending()) == 1


def test_a_task_round_trips_through_its_record():
    task = BackgroundTask(task_id="t1", query="q", thread_id="19:t", session_key="s", created_ms=5)
    assert BackgroundTask.from_json(task.as_json()) == task
