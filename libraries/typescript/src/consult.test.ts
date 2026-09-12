// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The slow half of a two-speed agent.
 *
 * Everything here is about a caller listening to silence. A consultation that
 * queues answers far too late; one that throws leaves the agent with nothing to
 * say. And a promise to send a result later has to survive the deploy that
 * happens between making it and keeping it.
 *
 * The Python twin is `tests/test_consult.py`.
 */

import {
  mkdtempSync,
  readFileSync,
  readdirSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  BACKGROUND_TASK_TOOL,
  BackgroundTasks,
  CONSULT_TOOL,
  Consultant,
  type BackgroundTask,
} from "./consult.js";

const dirs: string[] = [];

function scratch(): string {
  const dir = mkdtempSync(join(tmpdir(), "standin-tasks-"));
  dirs.push(dir);
  return dir;
}

function tasks(
  directory: string,
  options: { ttlMs?: number; resumeLimit?: number } = {},
) {
  return new BackgroundTasks({ directory, ...options });
}

afterEach(() => {
  dirs.length = 0;
});

describe("consulting a slower agent", () => {
  it("brings an answer back as a sentence", async () => {
    const consultant = new Consultant(() => (q) => `the answer to ${q}`);
    expect(await consultant.ask("the billing question")).toBe(
      "the answer to the billing question",
    );
  });

  it("works with an async agent", async () => {
    const consultant = new Consultant(() => async () => "looked it up");
    expect(await consultant.ask("anything")).toBe("looked it up");
  });

  it("answers an empty question without running anything", async () => {
    let ran = false;
    const consultant = new Consultant(() => () => {
      ran = true;
      return "x";
    });
    expect(await consultant.ask("   ")).toContain("did not catch");
    expect(ran).toBe(false);
  });

  it("refuses a second consultation rather than queueing it", async () => {
    // Queued, it would wait out both timeouts and answer long after the caller
    // stopped caring. Refused, the model has something to say now.
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const consultant = new Consultant(() => async () => {
      await held;
      return "first";
    });

    const first = consultant.ask("one");
    await Promise.resolve();
    expect(await consultant.ask("two")).toContain("still finishing");

    release();
    expect(await first).toBe("first");
    expect(consultant.busy).toBe(false);
  });

  it("admits a timeout rather than papering over it", async () => {
    // The work cannot be cancelled, so promising a follow-up would promise
    // something nothing will send.
    const consultant = new Consultant(
      () => () =>
        new Promise<string>((resolve) =>
          setTimeout(() => resolve("too late"), 5_000),
        ),
      5,
    );
    const answer = await consultant.ask("x");
    expect(answer).toContain("took too long");
    // No background tool was registered, so it must not point at one.
    expect(answer).not.toContain("background");
  });

  it("points at the background path on a timeout when there is one", async () => {
    const consultant = new Consultant(
      () => () =>
        new Promise<string>((resolve) =>
          setTimeout(() => resolve("too late"), 5_000),
        ),
      5,
      true,
    );
    expect(await consultant.ask("x")).toContain("background");
  });

  it("builds a fresh agent after a timeout", async () => {
    // The abandoned run keeps going and still holds whatever state it had.
    // Sharing that with the next consultation is how one slow question corrupts
    // the one after it.
    let built = 0;
    const consultant = new Consultant(() => {
      built += 1;
      const slow = built === 1;
      return () =>
        slow
          ? new Promise<string>((resolve) =>
              setTimeout(() => resolve("late"), 5_000),
            )
          : "fresh";
    }, 5);

    await consultant.ask("one");
    expect(await consultant.ask("two")).toBe("fresh");
    expect(built).toBe(2);
  });

  it("never lets a failing agent reach the model as an exception", async () => {
    const consultant = new Consultant(() => () => {
      throw new Error("the agent is down");
    });
    expect(await consultant.ask("x")).toContain("ran into a problem");
  });

  it("turns an empty answer into something to say", async () => {
    expect(await new Consultant(() => () => "  ").ask("x")).toContain(
      "did not find anything",
    );
  });

  it("describes both tools for a model, distinguishably", () => {
    for (const spec of [CONSULT_TOOL, BACKGROUND_TASK_TOOL]) {
      expect(spec.description.toLowerCase()).toContain("use");
      expect(spec.required).toEqual(["query"]);
    }
    // The two have to be distinguishable, or a model picks whichever comes
    // first and every long job is run while somebody waits on the line.
    expect(CONSULT_TOOL.description).toContain("waits on the line");
    expect(BACKGROUND_TASK_TOOL.description).toContain(
      "will not finish while the caller waits",
    );
  });
});

describe("background tasks", () => {
  it("writes the promise down before the work starts", () => {
    const dir = scratch();
    const task = tasks(dir).remember("find the Contoso numbers", "19:thread");
    expect(task).toBeDefined();
    const record = JSON.parse(
      readFileSync(join(dir, `${task!.taskId}.json`), "utf8"),
    );
    expect(record.query).toBe("find the Contoso numbers");
    expect(record.threadId).toBe("19:thread");
  });

  it("retires a delivered task", () => {
    const dir = scratch();
    const store = tasks(dir);
    const task = store.remember("x", "19:thread");
    store.begin(task);
    expect(readdirSync(dir).some((f) => f.endsWith(".claimed"))).toBe(true);
    store.done(task);
    expect(readdirSync(dir)).toEqual([]);
  });

  it("lets the next process take back an interrupted task", () => {
    // The whole point: a deploy between promising and delivering must not lose
    // the promise.
    const dir = scratch();
    tasks(dir).remember("the interrupted question", "19:thread");

    const claimed = tasks(dir).claimPending();
    expect(claimed.map((t) => t.query)).toEqual(["the interrupted question"]);
    expect(claimed[0]?.claimPath).toBeTruthy();
  });

  it("gives one task to exactly one worker", () => {
    // Two workers starting together is what running more than one means.
    const dir = scratch();
    tasks(dir).remember("x", "19:t");
    expect(tasks(dir).claimPending()).toHaveLength(1);
    expect(tasks(dir).claimPending()).toEqual([]);
  });

  it("puts a task back when the delivery failed", () => {
    const dir = scratch();
    const store = tasks(dir);
    store.remember("x", "19:t");
    store.finish(store.claimPending()[0]!, false);
    expect(tasks(dir).claimPending()).toHaveLength(1);
  });

  it("recovers a claim from a crashed process", () => {
    const dir = scratch();
    const store = tasks(dir);
    const task = store.remember("x", "19:t");
    store.begin(task);
    // Old enough that nobody can still be working on it.
    const old = Date.now() / 1000 - 3600;
    utimesSync(join(dir, `${task!.taskId}.claimed`), old, old);

    expect(tasks(dir).claimPending()).toHaveLength(1);
  });

  it("leaves a fresh claim alone", () => {
    // Taking a claim somebody is still working on delivers the same answer
    // twice.
    const dir = scratch();
    const store = tasks(dir);
    store.begin(store.remember("x", "19:t"));
    expect(tasks(dir).claimPending()).toEqual([]);
  });

  it("drops a stale promise rather than keeping it", () => {
    // An answer two hours after the question is worse than no answer.
    const dir = scratch();
    const store = tasks(dir, { ttlMs: -1 });
    store.remember("x", "19:t");
    expect(store.claimPending()).toEqual([]);
    expect(readdirSync(dir)).toEqual([]);
  });

  it("drops a task with nowhere to deliver", () => {
    const dir = scratch();
    const store = tasks(dir);
    store.remember("x", "");
    expect(store.claimPending()).toEqual([]);
  });

  it("leaves tasks beyond the resume limit queued rather than vanishing", () => {
    // A restart storm must not become a quiet way of losing promises.
    const dir = scratch();
    const store = tasks(dir, { resumeLimit: 2 });
    for (let index = 0; index < 5; index += 1)
      store.remember(`task ${index}`, "19:t");

    expect(store.claimPending()).toHaveLength(2);
    expect(readdirSync(dir).filter((f) => f.endsWith(".json"))).toHaveLength(3);
  });

  it("removes an unreadable record rather than reading it for ever", () => {
    const dir = scratch();
    writeFileSync(join(dir, "broken.json"), "{not json");
    expect(tasks(dir).claimPending()).toEqual([]);
    expect(readdirSync(dir)).toEqual([]);
  });

  it("runs the work and delivers it on resume", async () => {
    const dir = scratch();
    tasks(dir).remember("the question", "19:thread");

    const sent: Array<[string, string]> = [];
    const delivered = await tasks(dir).resume(
      () => (q) => `answer to ${q}`,
      async (threadId, text) => {
        sent.push([threadId, text]);
        return true;
      },
    );

    expect(delivered).toBe(1);
    expect(sent).toEqual([["19:thread", "answer to the question"]]);
    expect(readdirSync(dir)).toEqual([]);
  });

  it("keeps the promise for the next try when the delivery fails", async () => {
    const dir = scratch();
    tasks(dir).remember("the question", "19:thread");

    const delivered = await tasks(dir).resume(
      () => () => "answer",
      async () => false,
    );
    expect(delivered).toBe(0);
    expect(tasks(dir).claimPending()).toHaveLength(1);
  });

  it("treats a delivery that throws as undelivered", async () => {
    const dir = scratch();
    tasks(dir).remember("the question", "19:thread");

    const delivered = await tasks(dir).resume(
      () => () => "answer",
      async () => {
        throw new Error("the chat endpoint is down");
      },
    );
    expect(delivered).toBe(0);
    expect(tasks(dir).claimPending()).toHaveLength(1);
  });

  it("round-trips a task through its record", () => {
    const dir = scratch();
    const task = tasks(dir).remember("q", "19:t", "s")!;
    const record = JSON.parse(
      readFileSync(join(dir, `${task.taskId}.json`), "utf8"),
    ) as BackgroundTask;
    expect(record.query).toBe("q");
    expect(record.sessionKey).toBe("s");
    expect(record.createdMs).toBeGreaterThan(0);
  });
});
