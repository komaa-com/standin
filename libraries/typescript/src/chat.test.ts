// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The messages lane, and the memory that lets a 1:1 call be answered in chat.
 *
 * Posting call content into the wrong conversation is the failure this memory
 * exists to prevent, so most of what is asserted here is what it REFUSES.
 *
 * The Python twin is `tests/test_chat.py`.
 */

import type { AddressInfo } from "node:net";

import { afterEach, describe, expect, it } from "vitest";
import { WebSocket, WebSocketServer } from "ws";

import {
  CHAT_FALLBACK_WINDOW_MS,
  ChatChannel,
  PersonalChats,
  type InboundMessage,
  parseInbound,
} from "./chat.js";

const TENANT = "tenant-a";

/** One inbound message, as the gateway would have relayed it. */
function inbound(over: Partial<InboundMessage> = {}): InboundMessage {
  return {
    tenantId: TENANT,
    conversationId: "a:1dana",
    activityId: "activity-1",
    scope: "personal",
    text: "hello",
    senderName: "Dana",
    senderAadId: "dana-aad",
    senderIsGuest: false,
    senderIsLinkedOwner: false,
    attachments: [],
    mentions: [],
    ...over,
  };
}

describe("remembering who has a 1:1 chat", () => {
  it("remembers the chat a personal message came from", () => {
    const chats = new PersonalChats();
    chats.remember(inbound(), 1_000);

    const mine = chats.forCaller({
      callerAadId: "dana-aad",
      tenantId: TENANT,
      nowMs: 2_000,
    });
    expect(mine).toEqual({
      conversationId: "a:1dana",
      tenantId: TENANT,
      aadId: "dana-aad",
      displayName: "Dana",
      atMs: 1_000,
    });
  });

  it("decides a chat is personal by its scope, never by its conversation id", () => {
    // A bot's personal chat is addressed "a:1...", so an id-prefix test admits
    // nothing at all, and "19:..." is the group shape the rule excludes.
    const chats = new PersonalChats();
    chats.remember(
      inbound({ conversationId: "19:team_thread@thread.v2" }),
      1_000,
    );
    expect(
      chats.forCaller({
        callerAadId: "dana-aad",
        tenantId: TENANT,
        nowMs: 2_000,
      })?.conversationId,
    ).toBe("19:team_thread@thread.v2");
  });

  it("remembers nothing from a group or a channel", () => {
    // An @mention in a team channel would otherwise make that channel the
    // caller's chat and put a private escalation in front of their team.
    const chats = new PersonalChats();
    chats.remember(
      inbound({ scope: "channel", conversationId: "a:1looks-personal" }),
      1_000,
    );
    chats.remember(
      inbound({ scope: "group", conversationId: "a:1also-personal" }),
      1_000,
    );
    expect(
      chats.forCaller({
        callerAadId: "dana-aad",
        tenantId: TENANT,
        nowMs: 2_000,
      }),
    ).toBeUndefined();
  });

  it("answers only for the tenant the chat is in", () => {
    const chats = new PersonalChats();
    chats.remember(inbound(), 1_000);
    expect(
      chats.forCaller({
        callerAadId: "dana-aad",
        tenantId: "tenant-b",
        nowMs: 2_000,
      }),
    ).toBeUndefined();
  });

  it("forgets a chat older than the window", () => {
    // A conversation from last week is not evidence of who is on the phone
    // today.
    const chats = new PersonalChats();
    chats.remember(inbound(), 0);

    const caller = { callerAadId: "dana-aad", tenantId: TENANT };
    expect(
      chats.forCaller({ ...caller, nowMs: CHAT_FALLBACK_WINDOW_MS }),
    ).toBeDefined();
    expect(
      chats.forCaller({ ...caller, nowMs: CHAT_FALLBACK_WINDOW_MS + 1 }),
    ).toBeUndefined();
  });

  it("gives a call that names nobody no chat at all", () => {
    // Without the identity rule every anonymous caller collapses onto whoever
    // chatted last.
    const chats = new PersonalChats();
    chats.remember(inbound(), 1_000);
    expect(chats.forCaller({ tenantId: TENANT, nowMs: 2_000 })).toBeUndefined();
  });

  it("gives a caller nobody has chatted with no chat", () => {
    const chats = new PersonalChats();
    chats.remember(inbound(), 1_000);
    expect(
      chats.forCaller({
        callerAadId: "ali-aad",
        tenantId: TENANT,
        nowMs: 2_000,
      }),
    ).toBeUndefined();
  });

  it("matches whoever chatted last only when explicitly told it may", () => {
    // One explicit, default-off setting for a single-operator install.
    const chats = new PersonalChats();
    chats.remember(inbound(), 1_000);
    chats.remember(
      inbound({ senderAadId: "ali-aad", conversationId: "a:1ali" }),
      5_000,
    );

    expect(chats.forCaller({ tenantId: TENANT, nowMs: 6_000 })).toBeUndefined();
    expect(
      chats.forCaller({
        tenantId: TENANT,
        nowMs: 6_000,
        allowUnidentified: true,
      })?.conversationId,
    ).toBe("a:1ali");
  });

  it("does not reach into another tenant even when told to take whoever chatted last", () => {
    const chats = new PersonalChats();
    chats.remember(
      inbound({ tenantId: "tenant-b", conversationId: "a:1elsewhere" }),
      5_000,
    );
    expect(
      chats.forCaller({
        tenantId: TENANT,
        nowMs: 6_000,
        allowUnidentified: true,
      }),
    ).toBeUndefined();
  });

  it("keeps the most recent chat a person wrote from", () => {
    const chats = new PersonalChats();
    chats.remember(inbound(), 1_000);
    chats.remember(
      inbound({ conversationId: "a:1dana-new", activityId: "activity-2" }),
      4_000,
    );
    expect(
      chats.forCaller({
        callerAadId: "dana-aad",
        tenantId: TENANT,
        nowMs: 5_000,
      })?.conversationId,
    ).toBe("a:1dana-new");
  });

  it("ignores a message that names no conversation or no tenant", () => {
    const chats = new PersonalChats();
    chats.remember(inbound({ conversationId: "   " }), 1_000);
    chats.remember(inbound({ tenantId: "  ", senderAadId: "ali-aad" }), 1_000);
    expect(
      chats.forCaller({
        callerAadId: "dana-aad",
        tenantId: TENANT,
        nowMs: 2_000,
      }),
    ).toBeUndefined();
    expect(
      chats.forCaller({
        callerAadId: "ali-aad",
        tenantId: TENANT,
        nowMs: 2_000,
      }),
    ).toBeUndefined();
  });

  it("is bounded, and forgets the least recently seen person first", () => {
    // It lives in a process that is also carrying live audio.
    const chats = new PersonalChats();
    for (let index = 0; index < 600; index += 1) {
      chats.remember(
        inbound({
          senderAadId: `person-${index}`,
          conversationId: `a:1${index}`,
        }),
        1_000,
      );
    }
    expect(
      chats.forCaller({
        callerAadId: "person-0",
        tenantId: TENANT,
        nowMs: 2_000,
      }),
    ).toBeUndefined();
    expect(
      chats.forCaller({
        callerAadId: "person-599",
        tenantId: TENANT,
        nowMs: 2_000,
      }),
    ).toBeDefined();
  });

  it("takes the message the gateway actually sends", () => {
    // The wire shape, not a hand-built object: a scope the parser defaults is
    // still a scope this memory has to agree with.
    const chats = new PersonalChats();
    chats.remember(
      parseInbound(
        JSON.stringify({
          tenantId: TENANT,
          conversationId: "a:1dana",
          activityId: "activity-1",
          scope: "personal",
          text: "hello",
          sender: { displayName: "Dana", aadObjectId: "dana-aad" },
        }),
      ),
      1_000,
    );
    expect(
      chats.forCaller({
        callerAadId: "dana-aad",
        tenantId: TENANT,
        nowMs: 2_000,
      })?.displayName,
    ).toBe("Dana");
  });
});

describe("the channel feeding that memory", () => {
  const channels: ChatChannel[] = [];
  const servers: WebSocketServer[] = [];

  afterEach(async () => {
    await Promise.all(channels.splice(0).map((channel) => channel.aclose()));
    for (const server of servers.splice(0)) {
      for (const client of server.clients) client.terminate();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });

  async function until(predicate: () => boolean, label: string): Promise<void> {
    const deadline = Date.now() + 2_000;
    while (!predicate()) {
      if (Date.now() > deadline)
        throw new Error(`timed out waiting for ${label}`);
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
  }

  /** A channel wired to a memory, and the socket the gateway would speak on. */
  async function lane(
    respond: (message: InboundMessage) => Promise<string> = async () => "ok",
  ): Promise<{ socket: WebSocket; chats: PersonalChats }> {
    const server = new WebSocketServer({ host: "127.0.0.1", port: 0 });
    servers.push(server);
    await new Promise<void>((resolve) =>
      server.once("listening", () => resolve()),
    );

    const connected: WebSocket[] = [];
    server.on("connection", (socket) =>
      connected.push(socket as unknown as WebSocket),
    );

    const chats = new PersonalChats();
    const channel = new ChatChannel({
      respond,
      secret: "chat-memory-test-secret",
      url: `ws://127.0.0.1:${(server.address() as AddressInfo).port}`,
      chats,
    });
    channels.push(channel);
    await channel.start();
    await until(() => connected.length === 1, "the channel to dial in");
    return { socket: connected[0]!, chats };
  }

  /** One inbound message on the wire, as the gateway spells it. */
  function body(over: Record<string, unknown> = {}): string {
    return JSON.stringify({
      tenantId: TENANT,
      conversationId: "a:1dana",
      activityId: "activity-1",
      scope: "personal",
      text: "hello",
      sender: { displayName: "Dana", aadObjectId: "dana-aad" },
      ...over,
    });
  }

  it("takes a redelivery as more evidence of where this person chats, and answers once", async () => {
    // StandIn is at-least-once. The second copy must not start a second turn,
    // but it is the same person in the same conversation, so remembering it
    // again changes nothing. The third message is what makes the wait here
    // deterministic: by the time it is answered, the copy before it is settled.
    const answered: string[] = [];
    const { socket, chats } = await lane(async (message) => {
      answered.push(message.activityId);
      return "ok";
    });

    socket.send(body());
    socket.send(body());
    socket.send(body({ activityId: "activity-2" }));

    await until(
      () => answered.length === 2,
      "both messages that are not repeats",
    );
    expect(answered).toEqual(["activity-1", "activity-2"]);
    expect(
      chats.forCaller({ callerAadId: "dana-aad", tenantId: TENANT })
        ?.conversationId,
    ).toBe("a:1dana");
  });

  it("remembers every message it takes, so a call can be answered in chat", async () => {
    const server = new WebSocketServer({ host: "127.0.0.1", port: 0 });
    servers.push(server);
    await new Promise<void>((resolve) =>
      server.once("listening", () => resolve()),
    );

    const connected: WebSocket[] = [];
    server.on("connection", (socket) =>
      connected.push(socket as unknown as WebSocket),
    );

    const chats = new PersonalChats();
    const channel = new ChatChannel({
      respond: async () => "ok",
      secret: "chat-memory-test-secret",
      url: `ws://127.0.0.1:${(server.address() as AddressInfo).port}`,
      chats,
    });
    channels.push(channel);
    await channel.start();
    await until(() => connected.length === 1, "the channel to dial in");

    connected[0]!.send(
      JSON.stringify({
        tenantId: TENANT,
        conversationId: "a:1dana",
        activityId: "activity-1",
        scope: "personal",
        text: "hello",
        sender: { displayName: "Dana", aadObjectId: "dana-aad" },
      }),
    );

    await until(
      () =>
        chats.forCaller({ callerAadId: "dana-aad", tenantId: TENANT }) !==
        undefined,
      "the chat to be remembered",
    );
    expect(
      chats.forCaller({ callerAadId: "dana-aad", tenantId: TENANT })
        ?.conversationId,
    ).toBe("a:1dana");
  });
});
