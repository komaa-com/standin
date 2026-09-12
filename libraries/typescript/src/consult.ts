// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The slow half of a two-speed agent.
 *
 * A voice model has to answer in under a second or the call sounds broken. Real
 * work does not fit in a second. Looking something up, reading a file, driving a
 * browser, running a tool: those take ten seconds, or five minutes, and a caller
 * listening to silence has no way to tell the difference between thinking and
 * crashed.
 *
 * So an agent that does real work is two agents. The fast one talks. The slow
 * one works. This module is the seam between them, and it is in the core because
 * the seam is the same whichever provider is doing the talking and whichever
 * agent framework is doing the working.
 *
 * Two paths, and the difference between them is a promise:
 *
 * {@link Consultant} is "hold on, let me check". Time-boxed, answered in the
 * same breath. The caller waits, so the box has to be small.
 *
 * {@link BackgroundTasks} is "I'll send you the result". The caller hangs up.
 * That promise outlives the call, and therefore has to outlive the process: a
 * restart between making it and keeping it is ordinary, and an in-memory task
 * list breaks the promise silently. Every task is on disk before the work
 * starts.
 *
 * Neither knows what your agent is. You supply a function; what it does inside
 * is yours.
 *
 * Identical in shape to the Python SDK's `standin.consult`.
 */

import { randomUUID } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";

import type { ToolSpec } from "./callTools.js";
import { nowMs } from "./hmac.js";
import { logger } from "./log.js";
import { stateDir } from "./outbound.js";

/**
 * How long a caller will wait on the line before "let me check" stops sounding
 * like thinking and starts sounding like a dropped call.
 */
export const DEFAULT_CONSULT_TIMEOUT_MS = 45_000;

/** A background task gets far longer, because nobody is listening to it. */
export const DEFAULT_TASK_TIMEOUT_MS = 300_000;

/**
 * Promises go stale. Delivering a two-hour-old answer to a question somebody has
 * long since answered themselves is worse than delivering nothing.
 */
export const DEFAULT_TASK_TTL_MS = 2 * 60 * 60 * 1000;

/**
 * How many interrupted tasks one startup will take on. A restart storm must not
 * fan out every promise the fleet ever made at once. What is left stays queued.
 */
export const DEFAULT_RESUME_LIMIT = 5;

/** A claim older than this belonged to a process that died holding it. */
const STALE_CLAIM_MS = 600_000;

/** What a consultation is: a question in, an answer out. */
export type Asker = (query: string) => string | Promise<string>;

/**
 * Called to build an asker, on first use and again after a timeout. It is a
 * factory rather than a value because a timed-out consultation cannot be
 * stopped: it runs on, and whatever state it holds is no longer yours.
 */
export type AskerFactory = () => Asker;

/** Deliver a finished task's result. Returns whether it actually went out. */
export type Deliverer = (threadId: string, text: string) => Promise<boolean>;

export const CONSULT_TOOL: ToolSpec = {
  name: "consult",
  description:
    "Hand a question or a task to your fuller agent, which can look things up, read " +
    "files, browse, and run tools. Use it for anything beyond conversation. The caller " +
    "waits on the line, so use it only when the answer will come back quickly.",
  parameters: {
    query: {
      type: "string",
      description: "What to look into or do, phrased as a task.",
    },
  },
  required: ["query"],
};

export const BACKGROUND_TASK_TOOL: ToolSpec = {
  name: "background_task",
  description:
    "Start a long job and have the result sent to the caller in chat when it is done. " +
    "Use this instead of consult when the work will not finish while the caller waits. " +
    "Tell the caller you are on it; the result is delivered afterwards.",
  parameters: {
    query: {
      type: "string",
      description: "The task to run in the background.",
    },
  },
  required: ["query"],
};

/**
 * One slow agent, reachable from a live call without breaking it.
 *
 * Built once per call and asked whenever the fast model delegates:
 *
 * ```ts
 * const consultant = new Consultant(() => myAgent.run);
 * const answer = await consultant.ask("what did we bill Contoso last quarter?");
 * ```
 *
 * Everything here exists because a caller is listening while it runs.
 *
 * **One at a time, and the second asker is told so.** A model that can delegate
 * can delegate twice before the first answer lands. Queueing the second behind
 * the first means it waits out both timeouts and answers far too late, so it is
 * refused immediately with something the model can say out loud.
 *
 * **A timeout is admitted, not papered over.** The work cannot be cancelled: a
 * promise runs to its end whatever this returns. So the answer says it stopped
 * rather than promising a follow-up nothing will send. Pass
 * `backgroundAvailable` when you have registered {@link BACKGROUND_TASK_TOOL}
 * and the answer will point the caller at it.
 *
 * Your asker must not block. Node has one thread, and a synchronous agent holds
 * it for its whole duration, stopping the call's audio along with everything
 * else. Do the work in a promise, a worker, or another process.
 */
export class Consultant {
  readonly #build: AskerFactory;
  readonly #timeoutMs: number;
  readonly #backgroundAvailable: boolean;
  #asker: Asker | undefined;
  #busy = false;

  constructor(
    build: AskerFactory,
    timeoutMs: number = DEFAULT_CONSULT_TIMEOUT_MS,
    backgroundAvailable = false,
  ) {
    this.#build = build;
    this.#timeoutMs = timeoutMs;
    // What the caller is told on a timeout depends on whether there is anywhere
    // to hand the work off to. Pointing at a background task this agent cannot
    // start is a promise nothing keeps, so it is opt in.
    this.#backgroundAvailable = backgroundAvailable;
  }

  /** Whether a consultation is running right now. */
  get busy(): boolean {
    return this.#busy;
  }

  /**
   * Put a question to the slow agent and return what to say.
   *
   * Never throws, and always returns something speakable. A tool result goes
   * straight back to a model that will read it out, so an exception here is an
   * agent that goes quiet mid-sentence.
   */
  async ask(query: string, timeoutMs?: number): Promise<string> {
    const question = (query ?? "").trim();
    if (question === "")
      return "I did not catch what you wanted me to look into.";
    if (this.#busy)
      return "I am still finishing the last one. Give me a moment and ask again.";

    this.#busy = true;
    let timer: NodeJS.Timeout | undefined;
    try {
      const timedOut = Symbol("timed out");
      const deadline = new Promise<typeof timedOut>((resolve) => {
        timer = setTimeout(
          () => resolve(timedOut),
          timeoutMs ?? this.#timeoutMs,
        );
        timer.unref?.();
      });
      const answer = await Promise.race([this.#run(question), deadline]);
      if (answer === timedOut) {
        // The work is still running and cannot be stopped, so the asker is
        // dropped: the next consultation builds a fresh one rather than sharing
        // state with something nobody is waiting for any more.
        this.#asker = undefined;
        logger.warn(
          "standin: a consultation ran past its time and was abandoned",
        );
        return this.#backgroundAvailable
          ? "Sorry, that took too long and I had to stop. Ask me to work on it in the " +
              "background and I will send you the result when it is done."
          : "Sorry, that took too long and I had to stop. Ask me again and I will narrow it down.";
      }
      return answer.trim() || "I did not find anything to report.";
    } catch (err) {
      logger.warn(`standin: a consultation failed: ${String(err)}`);
      return "Sorry, I ran into a problem working on that.";
    } finally {
      if (timer !== undefined) clearTimeout(timer);
      this.#busy = false;
    }
  }

  async #run(question: string): Promise<string> {
    if (this.#asker === undefined) this.#asker = this.#build();
    return String(await this.#asker(question));
  }
}

/** One promise, as it sits on disk. */
export interface BackgroundTask {
  readonly taskId: string;
  readonly query: string;
  /** Where the result goes. A task with nowhere to deliver cannot be kept. */
  readonly threadId: string;
  readonly sessionKey?: string;
  readonly createdMs?: number;
  /**
   * Set only on a claimed task, so {@link BackgroundTasks.finish} can put it
   * back if the delivery fails.
   */
  readonly claimPath?: string;
}

/** Options for {@link BackgroundTasks}. */
export interface BackgroundTasksOptions {
  directory?: string;
  ttlMs?: number;
  resumeLimit?: number;
}

/**
 * Promised work that survives the process that promised it.
 *
 * "I will send you the result" is a promise made on a call that is about to end.
 * Held in memory it lasts until the next deploy, and then it is gone with
 * nothing said to the person waiting. Held here it is a small file, written
 * BEFORE the work starts and removed only once the result has actually been
 * delivered.
 *
 * The two-phase claim is what makes a restart safe. A task waiting to run is a
 * `.json`; a task being run is a `.claimed`. A process that dies mid-run leaves
 * a claim behind, and the next startup takes it back once it is old enough to be
 * certain nobody is still working on it. A delivery that fails puts the task
 * back rather than dropping it.
 */
export class BackgroundTasks {
  readonly #dir: string;
  readonly #ttlMs: number;
  readonly #resumeLimit: number;

  constructor(options: BackgroundTasksOptions = {}) {
    this.#dir = options.directory ?? join(stateDir(), "tasks");
    mkdirSync(this.#dir, { recursive: true, mode: 0o700 });
    this.#ttlMs = options.ttlMs ?? DEFAULT_TASK_TTL_MS;
    this.#resumeLimit = Math.max(
      1,
      options.resumeLimit ?? DEFAULT_RESUME_LIMIT,
    );
  }

  /**
   * Write the promise down, before starting the work.
   *
   * Returns undefined when it could not be written, which is a task that runs
   * but will not survive a restart. That is worth knowing about and worth
   * continuing with: a non-durable answer still beats no answer.
   */
  remember(
    query: string,
    threadId: string,
    sessionKey = "",
  ): BackgroundTask | undefined {
    const task: BackgroundTask = {
      taskId: randomUUID().replace(/-/g, ""),
      query,
      threadId,
      sessionKey,
      createdMs: nowMs(),
    };
    const target = join(this.#dir, `${task.taskId}.json`);
    const temp = `${target}.${randomUUID()}.tmp`;
    try {
      writeFileSync(temp, JSON.stringify(task), {
        encoding: "utf8",
        mode: 0o600,
      });
      renameSync(temp, target);
    } catch (err) {
      logger.warn(
        `standin: a background task could not be made durable: ${String(err)}`,
      );
      try {
        rmSync(temp, { force: true });
      } catch {
        // Nothing left to clean up.
      }
      return undefined;
    }
    return task;
  }

  /**
   * Mark a task as being worked on, so a crash mid-run is recoverable.
   *
   * Without this, a process that dies halfway leaves a task that looks
   * untouched, and the next startup runs it again while the delivery from the
   * first run may still be in flight.
   */
  begin(task: BackgroundTask | undefined): void {
    if (task === undefined || !task.taskId) return;
    try {
      renameSync(
        join(this.#dir, `${task.taskId}.json`),
        join(this.#dir, `${task.taskId}.claimed`),
      );
    } catch {
      // Already claimed, already gone, or never durable.
    }
  }

  /** The result was delivered. Retire the record, in either phase. */
  done(task: BackgroundTask | undefined): void {
    if (task === undefined || !task.taskId) return;
    for (const suffix of [".json", ".claimed"]) {
      try {
        rmSync(join(this.#dir, `${task.taskId}${suffix}`), { force: true });
      } catch {
        // Nothing to retire.
      }
    }
  }

  /**
   * Take the tasks a previous process left behind.
   *
   * Claiming is a rename, so two workers starting together cannot take the same
   * task. Everything beyond the resume limit is LEFT WHERE IT IS for the next
   * cycle rather than discarded: a busy restart must not become a quiet way of
   * losing promises.
   */
  claimPending(): BackgroundTask[] {
    this.#recoverOrphans();

    const claimed: BackgroundTask[] = [];
    for (const name of readdirSync(this.#dir).sort()) {
      if (!name.endsWith(".json")) continue;
      if (claimed.length >= this.#resumeLimit) {
        logger.info(
          "standin: resume limit reached; the remaining background tasks stay queued",
        );
        break;
      }
      const path = join(this.#dir, name);
      let record: BackgroundTask;
      try {
        record = JSON.parse(readFileSync(path, "utf8")) as BackgroundTask;
      } catch {
        // Unreadable is unrecoverable, and leaving it means reading it again on
        // every startup for ever.
        rmSync(path, { force: true });
        continue;
      }

      const ageMs = nowMs() - (record.createdMs ?? 0);
      if (record.createdMs !== undefined && ageMs > this.#ttlMs) {
        logger.info(
          `standin: dropping a background task promised ${Math.round(ageMs / 60_000)} minutes ago`,
        );
        rmSync(path, { force: true });
        continue;
      }
      if (!record.threadId) {
        logger.info(
          "standin: dropping a background task with nowhere to deliver",
        );
        rmSync(path, { force: true });
        continue;
      }

      const claim = path.replace(/\.json$/, ".claimed");
      try {
        renameSync(path, claim);
      } catch {
        continue; // somebody else took it first
      }
      claimed.push({ ...record, claimPath: claim });
    }
    return claimed;
  }

  /** Retire a claimed task, or put it back so the next cycle retries it. */
  finish(task: BackgroundTask, delivered: boolean): void {
    if (!task.claimPath) return;
    try {
      if (delivered) {
        rmSync(task.claimPath, { force: true });
      } else if (existsSync(task.claimPath)) {
        renameSync(
          task.claimPath,
          task.claimPath.replace(/\.claimed$/, ".json"),
        );
      }
    } catch {
      // The next cycle's orphan recovery is the backstop.
    }
  }

  /**
   * Re-run what a previous process left unfinished. Returns how many landed.
   *
   * Call it once at startup. Sequential on purpose: a restart storm must not fan
   * out one agent per interrupted task across the whole fleet.
   */
  async resume(
    build: AskerFactory,
    deliver: Deliverer,
    timeoutMs: number = DEFAULT_TASK_TIMEOUT_MS,
  ): Promise<number> {
    let delivered = 0;
    for (const task of this.claimPending()) {
      logger.info("standin: finishing a background task left by a restart");
      const answer = await new Consultant(build, timeoutMs).ask(
        task.query,
        timeoutMs,
      );
      let sent = false;
      try {
        sent = await deliver(task.threadId, answer);
      } catch (err) {
        logger.warn(
          `standin: delivering a resumed background task failed: ${String(err)}`,
        );
      }
      this.finish(task, sent);
      if (sent) delivered += 1;
      else
        logger.warn(
          "standin: a resumed background task is kept for the next try",
        );
    }
    return delivered;
  }

  /**
   * Put back claims whose owner died.
   *
   * Judged by the claim's own age. A claim younger than the window may still
   * have somebody working on it, and taking it would deliver the same answer
   * twice.
   */
  #recoverOrphans(): void {
    const cutoff = Date.now() - STALE_CLAIM_MS;
    for (const name of readdirSync(this.#dir)) {
      if (!name.endsWith(".claimed")) continue;
      const path = join(this.#dir, name);
      try {
        if (statSync(path).mtimeMs < cutoff) {
          renameSync(path, path.replace(/\.claimed$/, ".json"));
        }
      } catch {
        // Gone, or taken by somebody else between the read and the rename.
      }
    }
  }
}
