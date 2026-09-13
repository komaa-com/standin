// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * End-of-call meeting recap for the OpenClaw plugin.
 *
 * Transcript, resolveMinutesTarget and postMinutes already live in the SDK.
 * This file is the missing wire: collect turns during the call (the realtime
 * bridge), and at hang-up post minutes into the Teams chat through the chat
 * lane the runtime started when meetingRecap is on.
 *
 * The consult that writes the minutes can take tens of seconds, so hang-up
 * must not wait for it. Held only in memory, that promise dies with the
 * process. Held here it is a restart-recoverable local spool: a small file,
 * written BEFORE the task is scheduled and removed only once the minutes have
 * actually been delivered, expired (24 hours), or hit the retry limit. The
 * file is customer meeting data. A restart of this process (or a plugin reload
 * that opens the chat lane again) drains what the last one left.
 *
 * A restart still loses the spool if the directory dies with the process. Set
 * `STANDIN_RECAP_DIR` (or `STANDIN_STATE_DIR`) to persistent storage when recap
 * reliability matters. That is the same bound as the SDK's other on-disk
 * promises.
 *
 * Recording-banner wait already lives in this plugin. Session scope keys the
 * consult that writes the minutes.
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

import {
  postMinutes,
  resolveMinutesTarget,
  Transcript,
  type CallSession,
  type ChatChannel,
  type DeliveryTarget,
  type PersonalChats,
  type Summariser,
} from "../../index.js";
import { stateDir } from "../../outbound.js";

import type { CallLogger } from "./realtime.js";

/** How long a recap may sit before delivering it is worse than dropping it. */
const RECAP_TTL_MS = 24 * 60 * 60 * 1000;

/** How many recaps one drain will take on. What is left stays queued. */
const RESUME_LIMIT = 32;

/** Bound on files in the spool, claimed and waiting together. */
const MAX_RECAP_JOBS = 32;

/** A recap that fails the consult or the send this many times is dropped. */
const MAX_ATTEMPTS = 8;

/** A claim older than this belonged to a process that died holding it. */
const STALE_CLAIM_MS = 600_000;

/** Posted, finished with nothing to retry, or keep the spool for another try. */
export type RecapStatus = "posted" | "done" | "retry";

type TurnRecord = { speaker: string; text: string; role: "assistant" | "caller" };
type DestinationRecord = {
  kind: "thread" | "caller-dm";
  conversationId: string;
  tenantId: string;
};

export interface RecapJob {
  jobId: string;
  callId: string;
  threadId: string;
  tenantId: string;
  callerAadId: string;
  participantCount: number;
  sessionKey: string;
  turns: TurnRecord[];
  visuals: string[];
  destinations: DestinationRecord[];
  createdMs: number;
  attempts: number;
  claimPath?: string;
}

/** Recaps this process is still running. Drain uses the empty set as "steal claims". */
const inflight = new Set<Promise<void>>();

/**
 * Strip the minutes-prompt wrapper and return the raw transcript.
 *
 * A helper for a host that explicitly wants the transcript posted as-is. It is
 * NOT used as a fallback here: when no summariser is wired, or the one that is
 * fails, nothing is posted. A verbatim dump of a whole call in a meeting thread
 * is the wrong thing to do by accident.
 */
export function transcriptOnly(prompt: string): string {
  const marker = "\n\nTranscript:\n";
  const at = prompt.indexOf(marker);
  const body = at >= 0 ? prompt.slice(at + marker.length) : prompt;
  return body.trim().slice(0, 4000);
}

/** Where unfinished recaps sit. */
export function recapDir(): string {
  const configured = (process.env.STANDIN_RECAP_DIR ?? "").trim();
  const path = configured || join(stateDir(), "recap");
  mkdirSync(path, { recursive: true, mode: 0o700 });
  return path;
}

function nowMs(): number {
  return Date.now();
}

class RecapJobs {
  readonly #dir: string;

  constructor(directory?: string) {
    this.#dir = directory ?? recapDir();
    mkdirSync(this.#dir, { recursive: true, mode: 0o700 });
  }

  remember(job: RecapJob): RecapJob | undefined {
    this.#trim();
    const target = join(this.#dir, `${job.jobId}.json`);
    const temp = join(this.#dir, `.${job.jobId}.${randomUUID()}.tmp`);
    try {
      writeFileSync(temp, JSON.stringify(asJson(job)), {
        encoding: "utf8",
        mode: 0o600,
      });
      renameSync(temp, target);
    } catch (err) {
      try {
        rmSync(temp, { force: true });
      } catch {
        // Nothing left to clean up.
      }
      return undefined;
    }
    return job;
  }

  begin(job: RecapJob | undefined): RecapJob | undefined {
    if (job === undefined || !job.jobId) return job;
    try {
      const claim = join(this.#dir, `${job.jobId}.claimed`);
      renameSync(join(this.#dir, `${job.jobId}.json`), claim);
      return { ...job, claimPath: claim };
    } catch {
      return job;
    }
  }

  finish(job: RecapJob | undefined, complete: boolean): void {
    if (job === undefined || !job.jobId) return;
    const claim = job.claimPath ?? join(this.#dir, `${job.jobId}.claimed`);
    const waiting = join(this.#dir, `${job.jobId}.json`);
    if (complete) {
      for (const path of [claim, waiting]) {
        try {
          rmSync(path, { force: true });
        } catch {
          // Nothing to retire.
        }
      }
      return;
    }
    const updated: RecapJob = { ...job, attempts: job.attempts + 1 };
    delete updated.claimPath;
    const temp = join(this.#dir, `.${job.jobId}.${randomUUID()}.tmp`);
    try {
      writeFileSync(temp, JSON.stringify(asJson(updated)), {
        encoding: "utf8",
        mode: 0o600,
      });
      renameSync(temp, waiting);
    } catch {
      try {
        rmSync(temp, { force: true });
      } catch {
        // The next cycle's orphan recovery is the backstop.
      }
      return;
    }
    if (existsSync(claim) && claim !== waiting) {
      try {
        rmSync(claim, { force: true });
      } catch {
        // Already gone.
      }
    }
  }

  claimPending(stealClaims: boolean): RecapJob[] {
    this.#recoverOrphans(stealClaims ? 0 : STALE_CLAIM_MS);

    const claimed: RecapJob[] = [];
    for (const name of readdirSync(this.#dir).sort()) {
      if (!name.endsWith(".json")) continue;
      if (claimed.length >= RESUME_LIMIT) break;
      const path = join(this.#dir, name);
      let record: RecapJob & { v?: number };
      try {
        record = JSON.parse(readFileSync(path, "utf8")) as RecapJob & { v?: number };
      } catch {
        rmSync(path, { force: true });
        continue;
      }
      if (record.v !== 1) {
        rmSync(path, { force: true });
        continue;
      }
      const job = fromJson(record);
      const ageMs = nowMs() - (job.createdMs ?? 0);
      if (job.createdMs && ageMs > RECAP_TTL_MS) {
        rmSync(path, { force: true });
        continue;
      }
      if (job.attempts >= MAX_ATTEMPTS) {
        rmSync(path, { force: true });
        continue;
      }
      if (job.turns.length === 0 && job.visuals.length === 0) {
        rmSync(path, { force: true });
        continue;
      }
      const claim = path.replace(/\.json$/, ".claimed");
      try {
        renameSync(path, claim);
      } catch {
        continue;
      }
      claimed.push({ ...job, claimPath: claim });
    }
    return claimed;
  }

  #recoverOrphans(staleMs: number): void {
    const cutoff = Date.now() - staleMs;
    for (const name of readdirSync(this.#dir)) {
      if (!name.endsWith(".claimed")) continue;
      const path = join(this.#dir, name);
      try {
        if (statSync(path).mtimeMs <= cutoff) {
          renameSync(path, path.replace(/\.claimed$/, ".json"));
        }
      } catch {
        // Gone, or taken by somebody else between the read and the rename.
      }
    }
  }

  #trim(): void {
    const names = readdirSync(this.#dir);
    const waiting = names
      .filter((name) => name.endsWith(".json"))
      .map((name) => join(this.#dir, name))
      .sort((a, b) => statSync(a).mtimeMs - statSync(b).mtimeMs);
    const claimed = names.filter((name) => name.endsWith(".claimed")).length;
    let overflow = waiting.length + claimed - MAX_RECAP_JOBS + 1;
    for (const path of waiting) {
      if (overflow <= 0) return;
      try {
        rmSync(path, { force: true });
      } catch {
        // Already gone.
      }
      overflow -= 1;
    }
  }
}

function asJson(job: RecapJob): Record<string, unknown> {
  return {
    v: 1,
    jobId: job.jobId,
    callId: job.callId,
    threadId: job.threadId,
    tenantId: job.tenantId,
    callerAadId: job.callerAadId,
    participantCount: job.participantCount,
    sessionKey: job.sessionKey,
    turns: job.turns,
    visuals: job.visuals,
    destinations: job.destinations,
    createdMs: job.createdMs,
    attempts: job.attempts,
  };
}

function fromJson(raw: RecapJob & { v?: number }): RecapJob {
  const turns: TurnRecord[] = [];
  for (const item of raw.turns ?? []) {
    const text = (item.text ?? "").trim();
    if (!text) continue;
    turns.push({
      speaker: item.speaker ?? "",
      text: item.text,
      role: item.role === "assistant" ? "assistant" : "caller",
    });
  }
  const destinations: DestinationRecord[] = [];
  for (const item of raw.destinations ?? []) {
    const conversationId = (item.conversationId ?? "").trim();
    if (!conversationId) continue;
    destinations.push({
      kind: item.kind === "caller-dm" ? "caller-dm" : "thread",
      conversationId,
      tenantId: (item.tenantId ?? "").trim(),
    });
  }
  return {
    jobId: raw.jobId ?? "",
    callId: raw.callId ?? "",
    threadId: raw.threadId ?? "",
    tenantId: raw.tenantId ?? "",
    callerAadId: raw.callerAadId ?? "",
    participantCount: raw.participantCount || 1,
    sessionKey: raw.sessionKey ?? "",
    turns,
    visuals: (raw.visuals ?? []).map((v) => String(v)).filter((v) => v.trim() !== ""),
    destinations,
    createdMs: raw.createdMs ?? 0,
    attempts: raw.attempts ?? 0,
  };
}

function destinationsFor(
  session: CallSession,
  chats: PersonalChats | undefined,
): DeliveryTarget[] {
  const target = resolveTarget(session, chats);
  return target === undefined ? [] : [target];
}

function resolveTarget(
  session: CallSession,
  chats: PersonalChats | undefined,
): DeliveryTarget | undefined {
  const start = session.start;
  const tenant = (start.tenantId ?? "").trim();
  const callerAad = start.caller.aadId ?? "";
  const callerChat =
    chats !== undefined && callerAad && tenant
      ? chats.forCaller({ callerAadId: callerAad, tenantId: tenant })
      : undefined;
  return resolveMinutesTarget({
    threadId: start.threadId,
    humanCount: session.participantCount || 1,
    callerAadId: callerAad,
    callerChat,
    sessionTenantId: tenant || undefined,
  });
}

function jobFromSession(options: {
  session: CallSession;
  transcript: Transcript;
  sessionKey?: string;
  chats?: PersonalChats;
}): RecapJob | undefined {
  const { session, transcript } = options;
  if (transcript.empty) return undefined;
  const threadId = (session.start.threadId ?? "").trim();
  const callerAadId = (session.start.caller.aadId ?? "").trim();
  if (!threadId && !callerAadId) return undefined;
  return {
    jobId: randomUUID().replace(/-/g, ""),
    callId: session.callId,
    threadId,
    tenantId: (session.start.tenantId ?? "").trim(),
    callerAadId: callerAadId,
    participantCount: session.participantCount || 1,
    sessionKey: options.sessionKey ?? "",
    turns: transcript.turns.map((turn) => ({
      speaker: turn.speaker,
      text: turn.text,
      role: turn.role === "assistant" ? "assistant" : "caller",
    })),
    visuals: [...transcript.visuals],
    destinations: destinationsFor(session, options.chats).map((target) => ({
      kind: target.kind,
      conversationId: target.conversationId,
      tenantId: target.tenantId,
    })),
    createdMs: nowMs(),
    attempts: 0,
  };
}

function sessionFromJob(job: RecapJob): CallSession {
  return {
    callId: job.callId,
    participantCount: job.participantCount || 1,
    start: {
      callId: job.callId,
      threadId: job.threadId,
      tenantId: job.tenantId,
      caller: { aadId: job.callerAadId },
      direction: "inbound",
    },
  } as CallSession;
}

function transcriptFromJob(job: RecapJob): Transcript {
  const transcript = new Transcript();
  for (const turn of job.turns) {
    transcript.add(turn.speaker, turn.text, turn.role);
  }
  for (const shown of job.visuals) transcript.addVisual(shown);
  return transcript;
}

export async function runMeetingRecap(options: {
  enabled: boolean;
  session: CallSession;
  transcript: Transcript;
  summarise?: Summariser;
  chat?: Pick<ChatChannel, "send">;
  chats?: PersonalChats;
  logger?: CallLogger;
  destinations?: DeliveryTarget[];
}): Promise<RecapStatus> {
  if (!options.enabled) return "done";
  if (options.transcript.empty) return "done";
  const { session, transcript, chat, chats, logger } = options;
  // Before anything is spent: with no lane there is nowhere to post, and the
  // summarising consult is the expensive part.
  if (!chat) {
    logger?.warn?.(
      "standin-msteams: meetingRecap is on but no chat lane is open; no minutes posted",
    );
    return "retry";
  }
  try {
    const target =
      options.destinations && options.destinations.length > 0
        ? options.destinations
        : resolveTarget(session, chats);
    if (target === undefined || (Array.isArray(target) && target.length === 0)) {
      logger?.info?.(
        "standin-msteams: no minutes posted; this call has no Microsoft Teams chat",
      );
      return "done";
    }

    const summarise: Summariser =
      options.summarise ?? (() => Promise.resolve(""));

    const result = await postMinutes(
      summarise,
      transcript,
      target,
      async (destination, text) => {
        return chat.send({
          tenantId: destination.tenantId,
          conversationId: destination.conversationId,
          text,
        });
      },
    );
    logger?.info?.(
      `standin-msteams: meeting recap ${result.delivered ? "posted" : "not posted"} for ${session.callId}`,
    );
    if (result.delivered) return "posted";
    return "retry";
  } catch (err) {
    logger?.warn?.(
      `standin-msteams: meeting recap failed - ${err instanceof Error ? err.message : String(err)}`,
    );
    return "retry";
  }
}

async function runAndFinish(
  job: RecapJob | undefined,
  options: Parameters<typeof runMeetingRecap>[0],
): Promise<void> {
  const store = new RecapJobs();
  if (job === undefined) {
    // Disk refused the write. Still try once in memory: a recap that cannot
    // survive a restart is better than no recap at all.
    await runMeetingRecap(options);
    return;
  }
  const held = store.begin(job);
  if (held === undefined || !held.claimPath) {
    // Another drain already claimed this file. Running it here would post
    // the same minutes twice.
    return;
  }
  let status: RecapStatus = "retry";
  try {
    status = await runMeetingRecap(options);
  } finally {
    store.finish(held, status !== "retry");
  }
}

/**
 * Spool the recap, then run it without holding the call's teardown.
 *
 * The transcript is on disk before the returned promise is scheduled. Hang-up
 * must not await the consult; callers `void` this. Tests may await it.
 */
export function enqueueMeetingRecap(options: {
  enabled: boolean;
  session: CallSession;
  transcript: Transcript;
  summarise?: Summariser;
  chat?: Pick<ChatChannel, "send">;
  chats?: PersonalChats;
  logger?: CallLogger;
  sessionKey?: string;
}): Promise<void> {
  if (!options.enabled) return Promise.resolve();
  const job = jobFromSession(options);
  if (job === undefined) return Promise.resolve();
  const stored = new RecapJobs().remember(job);
  const destinations = job.destinations.map((item) => ({
    kind: item.kind,
    conversationId: item.conversationId,
    tenantId: item.tenantId,
  }));
  const work = runAndFinish(stored, {
    ...options,
    enabled: true,
    destinations: destinations.length > 0 ? destinations : undefined,
  });
  inflight.add(work);
  void work.finally(() => inflight.delete(work));
  return work;
}

/**
 * Finish recaps a previous process left behind. Returns how many posted.
 *
 * Call it once the chat lane is open. Sequential on purpose. Uses the chat
 * lane handed in, so a plugin reload cannot post through a lane that was
 * already closed.
 */
export async function drainMeetingRecap(options: {
  chat?: Pick<ChatChannel, "send">;
  chats?: PersonalChats;
  summarise?: (sessionKey: string, prompt: string) => Promise<string>;
  logger?: CallLogger;
}): Promise<number> {
  if (!options.chat) return 0;
  const store = new RecapJobs();
  let posted = 0;
  for (const job of store.claimPending(inflight.size === 0)) {
    options.logger?.info?.(
      "standin-msteams: finishing a meeting recap left by a restart",
    );
    const sessionKey = job.sessionKey;
    const status = await runMeetingRecap({
      enabled: true,
      session: sessionFromJob(job),
      transcript: transcriptFromJob(job),
      chat: options.chat,
      chats: options.chats,
      summarise: options.summarise
        ? (prompt) => options.summarise!(sessionKey, prompt)
        : undefined,
      logger: options.logger,
      destinations:
        job.destinations.length > 0
          ? job.destinations.map((item) => ({
              kind: item.kind,
              conversationId: item.conversationId,
              tenantId: item.tenantId,
            }))
          : undefined,
    });
    store.finish(job, status !== "retry");
    if (status === "posted") posted += 1;
    else if (status === "retry") {
      options.logger?.warn?.(
        "standin-msteams: a meeting recap is kept for the next try",
      );
    }
  }
  return posted;
}
