// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/** Exercise the real listener and WebSocket transport on loopback. */

import { request } from "node:http";

import { afterEach, describe, expect, it } from "vitest";
import { WebSocket } from "ws";

import {
  CallServer,
  MAX_AUDIO_BUFFER_BYTES,
  isUnanswered,
} from "./callServer.js";
import type { CallHandler, CallSession } from "./handler.js";
import { SIGNATURE_HEADER, TIMESTAMP_HEADER, signHandshake } from "./hmac.js";
import { TileStream } from "./tile.js";
import type { VideoFrame } from "./vision.js";

const SECRET = "local-transport-test-secret";
const servers: CallServer[] = [];
const sockets: WebSocket[] = [];

afterEach(async () => {
  for (const ws of sockets.splice(0)) ws.terminate();
  await Promise.all(servers.splice(0).map((server) => server.aclose()));
});

async function listen(
  handlerFactory: () => CallHandler = () => ({}),
): Promise<CallServer> {
  const server = new CallServer({
    handlerFactory,
    secret: SECRET,
    host: "127.0.0.1",
    port: 0,
  });
  servers.push(server);
  await server.start();
  return server;
}

function headers(callId: string, secret = SECRET): Record<string, string> {
  const timestamp = String(Date.now());
  return {
    [TIMESTAMP_HEADER]: timestamp,
    [SIGNATURE_HEADER]: signHandshake(secret, timestamp, callId),
  };
}

async function until(predicate: () => boolean, label: string): Promise<void> {
  const deadline = Date.now() + 2_000;
  while (!predicate()) {
    if (Date.now() > deadline)
      throw new Error(`timed out waiting for ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function upgradeStatus(
  server: CallServer,
  path: string,
  auth: Record<string, string> = {},
): Promise<number> {
  return new Promise((resolve, reject) => {
    const req = request({
      hostname: "127.0.0.1",
      port: server.port,
      path,
      headers: {
        connection: "Upgrade",
        upgrade: "websocket",
        "sec-websocket-key": "AAAAAAAAAAAAAAAAAAAAAA==",
        "sec-websocket-version": "13",
        ...auth,
      },
    });
    req.once("response", (res) => {
      res.resume();
      resolve(res.statusCode ?? 0);
    });
    req.once("upgrade", (res, socket) => {
      socket.destroy();
      resolve(res.statusCode ?? 0);
    });
    req.once("error", reject);
    req.setTimeout(2_000, () =>
      req.destroy(new Error("upgrade response timed out")),
    );
    req.end();
  });
}

async function connect(
  server: CallServer,
  callId: string,
  auth = headers(callId),
) {
  const frames: Record<string, unknown>[] = [];
  const ws = new WebSocket(
    `ws://127.0.0.1:${server.port}${server.wsPath}/${callId}`,
    { headers: auth },
  );
  sockets.push(ws);
  ws.on("message", (data) => frames.push(JSON.parse(data.toString())));
  // Terminating a CONNECTING socket during test cleanup can emit an error.
  ws.on("error", () => undefined);
  await new Promise<void>((resolve, reject) => {
    ws.once("open", resolve);
    ws.once("error", reject);
  });
  return { ws, frames };
}

describe("what the session knows about the call", () => {
  async function started(callId: string, start: Record<string, unknown> = {}) {
    const state = {
      session: undefined as CallSession | undefined,
      speakers: [] as string[],
    };
    const server = await listen(() => ({
      async onStart(call) {
        state.session = call;
      },
      async onSpeakerChange(name) {
        state.speakers.push(name);
      },
    }));
    const { ws } = await connect(server, callId);
    return { server, ws, state };
  }

  function audio(speakerName?: string): string {
    const frame: Record<string, unknown> = {
      type: "audio.frame",
      seq: 1,
      timestampMs: 0,
      payloadBase64: Buffer.alloc(320).toString("base64"),
    };
    if (speakerName !== undefined) frame.speakerName = speakerName;
    return JSON.stringify(frame);
  }

  it("does not let session.start undo a reported recording status", async () => {
    // recording.status can land BEFORE session.start, and session.start omits
    // the field when the state was unknown at answer time. Seeding from the
    // snapshot unconditionally turns a live ACTIVE into false, and every
    // recording-gated capability stays shut for the whole call with nothing said.
    const { ws, state } = await started("recording-race");
    ws.send(JSON.stringify({ type: "recording.status", status: "active" }));
    await new Promise((resolve) => setTimeout(resolve, 30));
    ws.send(
      JSON.stringify({ type: "session.start", callId: "recording-race" }),
    );
    await until(() => state.session !== undefined, "the call to start");

    expect(state.session!.recordingActive).toBe(true);
  });

  it("still lets a later report turn recording off", async () => {
    // The latch stops the snapshot downgrading a report. It must not stop a
    // real later report.
    const { ws, state } = await started("recording-latch");
    ws.send(JSON.stringify({ type: "recording.status", status: "active" }));
    await new Promise((resolve) => setTimeout(resolve, 30));
    ws.send(
      JSON.stringify({ type: "session.start", callId: "recording-latch" }),
    );
    await until(
      () => state.session?.recordingActive === true,
      "recording to be on",
    );

    ws.send(JSON.stringify({ type: "recording.status", status: "inactive" }));
    await until(
      () => state.session?.recordingActive === false,
      "recording to turn off",
    );
  });

  it("carries the active speaker to the handler, on change only", async () => {
    // The wire carries speakerName on every frame of unmixed audio. The server
    // used to read it and throw it away, so nothing could attribute a transcript.
    const { ws, state } = await started("speaker");
    ws.send(JSON.stringify({ type: "session.start", callId: "speaker" }));
    await until(() => state.session !== undefined, "the call to start");

    for (const name of ["Dana", "Dana", "Dana", "Ali", "Ali", "Dana"])
      ws.send(audio(name));
    await until(() => state.speakers.length === 3, "three speaker changes");

    // The name rides every frame, and a model told forty times a second who is
    // speaking would hear nothing else.
    expect(state.speakers).toEqual(["Dana", "Ali", "Dana"]);
    expect(state.session!.speaker).toBe("Dana");
  });

  it("reports no speaker on a mixed call", async () => {
    // speakerName is absent on the mixed path, which is most calls.
    const { ws, state } = await started("speaker-mixed");
    ws.send(JSON.stringify({ type: "session.start", callId: "speaker-mixed" }));
    await until(() => state.session !== undefined, "the call to start");

    ws.send(audio());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(state.speakers).toEqual([]);
    expect(state.session!.speaker).toBeUndefined();
  });

  it("sheds agent audio when the peer stops reading", async () => {
    // A peer that stops reading turns every send into a queue, and that queue
    // is what stalls the provider loop feeding it. The caller hears a gap; the
    // call does not wedge.
    const { ws, state } = await started("shedding");
    ws.send(JSON.stringify({ type: "session.start", callId: "shedding" }));
    await until(() => state.session !== undefined, "the call to start");
    const session = state.session!;

    const wedged = { get: () => MAX_AUDIO_BUFFER_BYTES + 1 };
    const original = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(session),
      "bufferedBytes",
    )!;
    Object.defineProperty(
      Object.getPrototypeOf(session),
      "bufferedBytes",
      wedged,
    );
    try {
      const before = session.mediaTimeMs;
      for (let i = 0; i < 5; i += 1)
        await session.sendAudio(Buffer.alloc(320, 1));
      // The timeline is the CALLER's clock: a dropped frame is a gap in what
      // they hear, not a rewind.
      expect(session.mediaTimeMs).toBeGreaterThan(before);
    } finally {
      // Put the real property back. Deleting it leaves the prototype without it
      // for every test that follows.
      Object.defineProperty(
        Object.getPrototypeOf(session),
        "bufferedBytes",
        original,
      );
    }
  });

  it("carries the participant count, not just the sentence", async () => {
    // The sentence is for a model. The number is for a plugin that has to branch
    // on it, and re-parsing the sentence to get it back is how five copies of
    // the same regex appeared.
    const { ws, state } = await started("participants");
    ws.send(JSON.stringify({ type: "session.start", callId: "participants" }));
    await until(() => state.session !== undefined, "the call to start");
    expect(state.session!.participantCount).toBe(0);

    ws.send(JSON.stringify({ type: "participants", count: 4 }));
    await until(
      () => state.session?.participantCount === 4,
      "the count to arrive",
    );
  });
});

describe("the unanswered call reaper", () => {
  it("has an exact boundary", () => {
    // Pure, with the clock passed in, so the boundary is testable without
    // waiting. Strictly greater, so a tick landing exactly on the grace does
    // not reap a call one instant early.
    const call = {
      startedAtMs: 100,
      answeredAtMs: undefined as number | undefined,
    };
    expect(isUnanswered(call, 10, 109)).toBe(false);
    expect(isUnanswered(call, 10, 110)).toBe(false); // exactly the grace: not yet
    expect(isUnanswered(call, 10, 111)).toBe(true);
    expect(isUnanswered(call, 0, 1_000)).toBe(false); // disabled means never
    call.answeredAtMs = 101;
    expect(isUnanswered(call, 10, 1_000)).toBe(false);
  });

  it("ends a call nothing answers", async () => {
    // An agent dispatch that never lands is invisible to every other watchdog:
    // session.start arrived, onStart succeeded, and the caller keeps talking.
    const reasons: string[] = [];
    const server = new CallServer({
      handlerFactory: () => ({
        async aclose(reason) {
          reasons.push(reason);
        },
      }),
      secret: SECRET,
      host: "127.0.0.1",
      port: 0,
      audioIdleTimeoutMs: 0,
      staleCallReaperMs: 120,
    });
    servers.push(server);
    await server.start();

    const { ws } = await connect(server, "unanswered");
    ws.send(JSON.stringify({ type: "session.start", callId: "unanswered" }));
    await until(
      () => reasons.includes("no-agent-answered"),
      "the call to be reaped",
    );
  });

  it("counts sending audio as answering", async () => {
    // Every plugin is covered without doing anything: a provider that connects
    // and then produces no audio at all is reaped exactly like an agent that
    // never joined.
    let session: CallSession | undefined;
    const server = await listen(() => ({
      async onStart(call) {
        session = call;
      },
    }));
    const { ws } = await connect(server, "answered-by-audio");
    ws.send(
      JSON.stringify({ type: "session.start", callId: "answered-by-audio" }),
    );
    await until(() => session !== undefined, "the call to start");
    expect(session!.answered).toBe(false);
    await session!.sendAudio(Buffer.alloc(320));
    expect(session!.answered).toBe(true);
  });

  it("lets an agent that listens first say so", async () => {
    // A listen-first agent joins and stays quiet, and must not be reaped for it.
    let session: CallSession | undefined;
    const server = await listen(() => ({
      async onStart(call) {
        session = call;
      },
    }));
    const { ws } = await connect(server, "listen-first");
    ws.send(JSON.stringify({ type: "session.start", callId: "listen-first" }));
    await until(() => session !== undefined, "the call to start");
    session!.markAnswered();
    expect(session!.answered).toBe(true);
  });
});

describe("the call duration ceiling", () => {
  it("ends a call that will not end on its own", async () => {
    // The idle watchdog ends a call that went QUIET. This ends one that has
    // not: a caller who will not hang up, a model looping at itself, an
    // automated system that dialled and never stopped talking. None of those
    // trips a silence check, and every one bills a provider by the minute.
    const goodbyes: string[] = [];
    const reasons: string[] = [];
    const server = new CallServer({
      handlerFactory: () => ({
        async onGoodbye(text) {
          goodbyes.push(text);
        },
        async aclose(reason) {
          reasons.push(reason);
        },
      }),
      secret: SECRET,
      host: "127.0.0.1",
      port: 0,
      audioIdleTimeoutMs: 0,
      maxCallMs: 150,
      goodbyeText: "Out of time, goodbye.",
      goodbyeGraceMs: 30,
    });
    servers.push(server);
    await server.start();

    const { ws } = await connect(server, "ceiling");
    ws.send(JSON.stringify({ type: "session.start", callId: "ceiling" }));

    // The closing line reaches the handler through onGoodbye, the same callback
    // StandIn's own closing line uses, so a plugin needs no new code for this.
    await until(() => goodbyes.length === 1, "the goodbye to be delivered");
    expect(goodbyes).toEqual(["Out of time, goodbye."]);
    await until(
      () => reasons.includes("call-duration-limit"),
      "the call to end on its limit",
    );
  });

  it("flushes playback before it speaks", async () => {
    // Otherwise the line queues behind however many seconds of agent audio the
    // service still holds, and the call ends before anyone hears it.
    const server = new CallServer({
      handlerFactory: () => ({}),
      secret: SECRET,
      host: "127.0.0.1",
      port: 0,
      audioIdleTimeoutMs: 0,
      maxCallMs: 150,
      goodbyeGraceMs: 30,
    });
    servers.push(server);
    await server.start();

    const { ws, frames } = await connect(server, "ceiling-flush");
    ws.send(JSON.stringify({ type: "session.start", callId: "ceiling-flush" }));
    await until(
      () => frames.some((frame) => frame.type === "assistant.cancel"),
      "playback to be flushed",
    );
  });

  it("has no ceiling by default", async () => {
    // A hard cap on a live call is an operator's decision, not a default.
    const server = await listen();
    expect(server.maxCallMs).toBe(0);
  });
});

describe("CallServer transport", () => {
  it("says whether it is bound, and refuses to bind twice", async () => {
    // A host that calls connect twice binds a second listener and leaks the
    // first, then reports a dead platform as connected.
    const server = await listen();
    expect(server.running).toBe(true);
    expect(server.port).toBeGreaterThan(0);
    await expect(server.start()).rejects.toThrow("already running");
    await server.aclose();
    expect(server.running).toBe(false);
    // Closing twice is not an error: teardown runs from more than one place.
    await server.aclose();
  });

  it.each(["%", "%ZZ", "%E0%A4%A"])(
    "rejects malformed percent escapes %s without crashing",
    async (suffix) => {
      const server = await listen();
      expect(await upgradeStatus(server, `${server.wsPath}/${suffix}`)).toBe(
        400,
      );
      // A separate valid request reaches authentication after the malformed one.
      expect(await upgradeStatus(server, `${server.wsPath}/healthy`)).toBe(401);
      expect(server.activeCalls).toBe(0);
    },
  );

  it("rejects a malformed URL before authentication without crashing", async () => {
    const server = await listen();
    expect(await upgradeStatus(server, "//[")).toBe(400);
    expect(await upgradeStatus(server, `${server.wsPath}/healthy`)).toBe(401);
  });

  it("authenticates upgrades and prevents a captured handshake from being reused", async () => {
    const server = await listen();
    const path = `${server.wsPath}/authenticated`;
    expect(await upgradeStatus(server, path)).toBe(401);
    expect(
      await upgradeStatus(
        server,
        path,
        headers("authenticated", "wrong-secret"),
      ),
    ).toBe(401);
    const auth = headers("authenticated");
    const { ws } = await connect(server, "authenticated", auth);
    expect(server.activeCalls).toBe(1);
    ws.close();
    await until(
      () => server.activeCalls === 0,
      "the first call slot to be freed",
    );
    expect(await upgradeStatus(server, path, auth)).toBe(401);
  });

  it("orders startup, echoes PCM, cancels playback and tears down one real call", async () => {
    const order: string[] = [];
    const reasons: string[] = [];
    const received: Buffer[] = [];
    let session: CallSession | undefined;
    const server = await listen(() => ({
      async onStart(call) {
        order.push("start-begin");
        await new Promise((resolve) => setTimeout(resolve, 20));
        session = call;
        order.push("start-end");
      },
      async onCallerAudio(pcm) {
        order.push("audio");
        received.push(pcm);
        await session!.sendAudio(pcm);
      },
      aclose(reason) {
        reasons.push(reason);
      },
    }));
    const { ws, frames } = await connect(server, "roundtrip");
    const pcm = Buffer.alloc(640, 3);
    ws.send(
      JSON.stringify({
        type: "session.start",
        callId: "roundtrip",
        caller: { displayName: "Local caller" },
      }),
    );
    ws.send(
      JSON.stringify({
        type: "audio.frame",
        payloadBase64: pcm.toString("base64"),
      }),
    );
    ws.send(JSON.stringify({ type: "ping", ts: 42 }));
    await until(
      () => frames.some((frame) => frame.type === "pong"),
      "PCM echo and pong",
    );
    expect(order).toEqual(["start-begin", "start-end", "audio"]);
    expect(session!.callId).toBe("roundtrip");
    expect(session!.start.caller.displayName).toBe("Local caller");
    expect(received).toEqual([pcm]);
    expect(frames.find((frame) => frame.type === "audio.frame")).toMatchObject({
      seq: 1,
      timestampMs: 0,
      payloadBase64: pcm.toString("base64"),
    });
    expect(frames.find((frame) => frame.type === "pong")).toMatchObject({
      ts: 42,
    });

    await session!.cancelPlayback();
    await until(
      () => frames.some((frame) => frame.type === "assistant.cancel"),
      "playback cancellation",
    );
    expect(
      frames.find((frame) => frame.type === "assistant.cancel"),
    ).toMatchObject({ turnId: 1 });

    ws.send(JSON.stringify({ type: "session.end", reason: "caller-hung-up" }));
    await until(() => server.activeCalls === 0, "the call slot to be freed");
    expect(reasons).toEqual(["caller-hung-up"]);
    expect(frames.find((frame) => frame.type === "session.end")).toMatchObject({
      reason: "caller-hung-up",
    });
  });

  it("carries the vision lane: frames reach the handler, the latest is kept, images go back", async () => {
    const seen: VideoFrame[] = [];
    let session: CallSession | undefined;
    const server = await listen(() => ({
      onStart(call) {
        session = call;
      },
      onVideoFrame(frame) {
        seen.push(frame);
      },
    }));
    const { ws, frames } = await connect(server, "vision");
    const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0]).toString("base64");
    const videoFrame = (source: string, extra: Record<string, unknown> = {}) =>
      JSON.stringify({
        type: "video.frame",
        source,
        ts: 1_738_000_000_000,
        width: 1280,
        height: 720,
        mime: "image/jpeg",
        dataBase64: jpeg,
        participantId: "aad-1",
        participantName: "Alaa",
        ...extra,
      });

    ws.send(JSON.stringify({ type: "session.start", callId: "vision" }));
    ws.send(videoFrame("camera", { width: 320, height: 240 }));
    ws.send(videoFrame("screenshare"));
    await until(() => seen.length === 2, "two video frames");

    expect(seen[0].source).toBe("camera");
    expect([seen[0].width, seen[0].height]).toEqual([320, 240]);
    expect(seen[1].participantName).toBe("Alaa");
    expect(seen[1].data).toEqual(Buffer.from(jpeg, "base64"));

    // No source: the screen share wins, because an agent asked to look is
    // nearly always being asked about what is being SHOWN.
    expect(session!.latestVideoFrame()!.source).toBe("screenshare");
    expect(session!.latestVideoFrame("camera")!.width).toBe(320);

    // A newer frame REPLACES the older one rather than accumulating: only the
    // latest matters, and a history would be an unbounded buffer of the
    // caller's screen.
    ws.send(videoFrame("screenshare", { width: 1920, height: 1080 }));
    await until(
      () => session!.latestVideoFrame("screenshare")!.width === 1920,
      "the newer share frame",
    );

    // Unusable frames are dropped and the call stays up.
    ws.send(videoFrame("screenshare", { dataBase64: "not base64!" }));
    ws.send(videoFrame("whiteboard"));
    ws.send(videoFrame("camera", { width: 0 }));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(seen.length).toBe(3);

    await session!.displayImage(Buffer.from([0xff, 0xd8, 0xff, 0xe0]), {
      caption: "Q3 revenue",
      durationMs: 4000,
    });
    await until(
      () => frames.some((frame) => frame.type === "display.image"),
      "the image on the tile",
    );
    expect(
      frames.find((frame) => frame.type === "display.image"),
    ).toMatchObject({
      mime: "image/jpeg",
      caption: "Q3 revenue",
      durationMs: 4000,
      dataBase64: jpeg,
      ts: 0,
    });
  });

  it("carries the tile lane: its own sequence, the audio clock, paced and dropped", async () => {
    let session: CallSession | undefined;
    const server = await listen(() => ({
      onStart(call) {
        session = call;
      },
    }));
    const { ws, frames } = await connect(server, "tile");
    ws.send(JSON.stringify({ type: "session.start", callId: "tile" }));
    await until(() => session !== undefined, "the call to start");

    // The tile is a separate stream from the audio: its own sequence, but the
    // SAME clock, so the two cannot disagree about what time it is.
    await session!.sendTileFrame(Buffer.from("one"), 640, 360);
    await until(
      () => frames.some((f) => f.type === "display.frame"),
      "the first tile frame",
    );
    expect(frames.find((f) => f.type === "display.frame")).toMatchObject({
      seq: 1,
      ts: 0,
      width: 640,
      height: 360,
    });

    await session!.sendAudio(Buffer.alloc(320, 1));
    await until(() => frames.some((f) => f.type === "audio.frame"), "audio");
    await session!.sendTileFrame(Buffer.from("two"), 640, 360);
    await until(
      () => frames.filter((f) => f.type === "display.frame").length === 2,
      "the second tile frame",
    );
    expect(frames.filter((f) => f.type === "display.frame")[1]).toMatchObject({
      seq: 2,
      ts: 10,
    });

    // A ready-made JPEG costs no optional dependency.
    const tile = new TileStream(session!, { fps: 20 });
    await tile.start();
    try {
      for (let i = 0; i < 30; i += 1) {
        tile.offerJpeg(Buffer.from(`frame-${i}`));
        await new Promise((r) => setTimeout(r, 5));
      }
      await until(() => tile.framesSent >= 2, "paced frames");
      // Paced, so far fewer than the 30 offered went out.
      expect(tile.framesSent).toBeLessThan(30);

      // Each offered frame is sent at most once: a stalled source is silence,
      // not one stale frame repeated. Let anything already offered drain first,
      // or this samples a frame that is still in flight.
      await new Promise((r) => setTimeout(r, 150));
      const settled = tile.framesSent;
      await new Promise((r) => setTimeout(r, 200));
      expect(tile.framesSent).toBe(settled);
    } finally {
      await tile.aclose();
    }

    // Both streams share a socket, and a caller forgives a dropped frame far
    // more readily than a break in the voice.
    const starved = new TileStream(session!, { fps: 20, maxBufferedBytes: -1 });
    await starved.start();
    try {
      for (let i = 0; i < 5; i += 1) {
        starved.offerJpeg(Buffer.from("dropped"));
        await new Promise((r) => setTimeout(r, 20));
      }
      await until(() => starved.framesDropped >= 1, "a dropped frame");
      expect(starved.framesSent).toBe(0);
    } finally {
      await starved.aclose();
    }
  });

  it("uses the encoder it is given", async () => {
    let session: CallSession | undefined;
    const server = await listen(() => ({
      onStart(call) {
        session = call;
      },
    }));
    const { ws, frames } = await connect(server, "tile-encode");
    ws.send(JSON.stringify({ type: "session.start", callId: "tile-encode" }));
    await until(() => session !== undefined, "the call to start");

    const seen: Array<[number, number]> = [];
    const tile = new TileStream(session!, {
      fps: 20,
      encoder: async (rgb, width, height) => {
        seen.push([width, height]);
        return Buffer.concat([Buffer.from("encoded:"), rgb.subarray(0, 4)]);
      },
    });
    await tile.start();
    try {
      tile.offerRgb(Buffer.alloc(24, 7), 4, 2);
      await until(
        () => frames.some((f) => f.type === "display.frame"),
        "an encoded frame",
      );
      const frame = frames.find((f) => f.type === "display.frame")!;
      expect(
        Buffer.from(String(frame.dataBase64), "base64").toString(),
      ).toContain("encoded:");
      expect(seen).toEqual([[4, 2]]);
      // Encoded frames are reported at the tile size they were resized to.
      expect([frame.width, frame.height]).toEqual([640, 360]);
    } finally {
      await tile.aclose();
    }
  });

  it("refuses a call from onStart before running cleanup", async () => {
    const order: string[] = [];
    const server = await listen(() => ({
      async onStart(call) {
        order.push("start-begin");
        await call.end("not-allowed");
        order.push("start-end");
      },
      aclose(reason) {
        order.push(`close:${reason}`);
      },
    }));
    const { ws } = await connect(server, "refused");
    ws.send(JSON.stringify({ type: "session.start", callId: "refused" }));
    await until(() => server.activeCalls === 0, "refused call teardown");
    expect(order).toEqual(["start-begin", "start-end", "close:not-allowed"]);
  });
});
