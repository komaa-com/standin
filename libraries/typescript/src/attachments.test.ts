// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * What somebody attached to a chat message.
 *
 * The fetch posture is the point. An attachment URL arrives inside a message
 * somebody else wrote, so the origin pin is the only thing bounding where this
 * worker can be told to go, and every cap has to hold while reading rather than
 * after.
 *
 * The Python twin is `tests/test_attachments.py`.
 */

import { existsSync, readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  MAX_IMAGES,
  attachmentsNote,
  buildChatTurn,
  cardActionNote,
  chatAttachmentOrigin,
  chatImageData,
  chatImageDataUrl,
  fetchChatAudio,
  fetchChatImages,
  spoolClip,
  transcribeVoiceMessages,
} from "./attachments.js";
import {
  OUTBOUND_IMAGE_MAX_BYTES,
  buildReply,
  outboundImage,
  parseInbound,
  sanitizeImageName,
  sniffImageType,
  type InboundMessage,
} from "./chat.js";

const ORIGIN = "https://teams.standin.komaa.com";
// The real eight-byte PNG signature. The first four alone are not a PNG, which
// is exactly what the magic-number check is for.
const PNG = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  Buffer.alloc(40),
]);

/** An injected fetch, so every edge is reachable without a socket. */
function fetcher(
  bodies: Record<
    string,
    { body?: Buffer; status?: number; headers?: Record<string, string> }
  >,
  seen: string[] = [],
): typeof fetch {
  return (async (input: string | URL) => {
    const url = String(input);
    seen.push(url);
    const spec = bodies[url];
    if (spec === undefined) throw new Error("nothing there");
    const body = spec.body ?? PNG;
    return new Response(body, {
      status: spec.status ?? 200,
      headers: spec.headers ?? { "content-type": "image/png" },
    });
  }) as unknown as typeof fetch;
}

function imageAttachment(
  name = "shot.png",
  extra: Record<string, unknown> = {},
) {
  return {
    kind: "image",
    name,
    contentType: "image/png",
    url: `${ORIGIN}/relay/${name}`,
    ...extra,
  };
}

function message(over: Partial<InboundMessage> = {}): InboundMessage {
  return {
    tenantId: "t",
    conversationId: "c",
    activityId: "a",
    scope: "personal",
    text: "",
    senderIsGuest: false,
    senderIsLinkedOwner: false,
    attachments: [],
    mentions: [],
    ...over,
  } as InboundMessage;
}

describe("the origin pin", () => {
  it("comes from the channel the messages arrived on", () => {
    delete process.env.STANDIN_CHAT_URL;
    expect(chatAttachmentOrigin()).toBe(ORIGIN);
    expect(chatAttachmentOrigin("wss://gateway.example/api/chat/channel")).toBe(
      "https://gateway.example",
    );
    expect(chatAttachmentOrigin("ws://127.0.0.1:9444/x")).toBe(
      "http://127.0.0.1:9444",
    );
    // A default port is not spelled out, so the two forms compare equal.
    expect(chatAttachmentOrigin("wss://host:443/x")).toBe("https://host");
  });

  it.each(["", "not a url", "file:///etc/passwd", "ftp://host/x"])(
    "is undefined when it cannot be resolved (%s)",
    (url) => {
      // undefined means fetch nothing, which is the safe direction.
      expect(chatAttachmentOrigin(url)).toBeUndefined();
    },
  );

  it("fetches nothing when there is no origin", async () => {
    // An unset origin read as "anywhere" turns a configuration typo into a
    // fetcher a message can point at any address it likes.
    const seen: string[] = [];
    expect(
      await fetchChatImages([imageAttachment()], {
        origin: undefined,
        fetchFn: fetcher({}, seen),
      }),
    ).toEqual([]);
    expect(seen).toEqual([]);
  });

  it("refuses an attachment from another origin", async () => {
    const seen: string[] = [];
    const other = imageAttachment("x.png", {
      url: "http://169.254.169.254/latest/meta-data/",
    });
    expect(
      await fetchChatImages([other], {
        origin: ORIGIN,
        fetchFn: fetcher({}, seen),
      }),
    ).toEqual([]);
    expect(seen).toEqual([]);
  });
});

describe("what is worth trying", () => {
  it("fetches a pasted screenshot", async () => {
    const item = imageAttachment();
    const got = await fetchChatImages([item], {
      origin: ORIGIN,
      fetchFn: fetcher({ [item.url]: { body: PNG } }),
    });
    expect(got).toHaveLength(1);
    expect(chatImageData(got[0]!)).toEqual(PNG);
    expect(chatImageDataUrl(got[0]!).startsWith("data:image/png;base64,")).toBe(
      true,
    );
  });

  it("fetches an image dragged in as a file", async () => {
    // The same png dragged from disk arrives as a file whose declared type is a
    // bare extension, so gating on the kind alone makes it invisible while the
    // note says one was sent.
    const item = {
      kind: "file",
      name: "diagram.png",
      contentType: "png",
      url: `${ORIGIN}/d`,
    };
    const got = await fetchChatImages([item], {
      origin: ORIGIN,
      fetchFn: fetcher({
        [item.url]: { body: PNG, headers: { "content-type": "image/png" } },
      }),
    });
    expect(got).toHaveLength(1);
    // The real type comes off the response, not the bare extension.
    expect(got[0]!.mime).toBe("image/png");
  });

  it("fetches a voice note that arrives as a file", async () => {
    // The audio kind is documented but a voice note relays as a file, so a
    // plugin that gates on the kind transcribes nothing, ever.
    const item = {
      kind: "file",
      name: "audio.wav",
      contentType: "wav",
      url: `${ORIGIN}/v`,
    };
    const got = await fetchChatAudio([item], {
      origin: ORIGIN,
      fetchFn: fetcher({
        [item.url]: {
          body: Buffer.from("RIFF"),
          headers: { "content-type": "audio/wav" },
        },
      }),
    });
    expect(got).toHaveLength(1);
    expect(got[0]!.mime).toBe("audio/wav");
  });

  it("accepts a video container as audio", async () => {
    // Some clients label a voice note with a container type that speech-to-text
    // reads perfectly well.
    const item = { kind: "audio", name: "note.mp4", url: `${ORIGIN}/v` };
    const got = await fetchChatAudio([item], {
      origin: ORIGIN,
      fetchFn: fetcher({
        [item.url]: {
          body: Buffer.from("ftyp"),
          headers: { "content-type": "video/mp4" },
        },
      }),
    });
    expect(got).toHaveLength(1);
  });

  it("does not request an attachment marked unrelayable", async () => {
    const seen: string[] = [];
    const item = imageAttachment("x.png", { relayable: false });
    expect(
      await fetchChatImages([item], {
        origin: ORIGIN,
        fetchFn: fetcher({}, seen),
      }),
    ).toEqual([]);
    expect(seen).toEqual([]);
  });
});

describe("the fetch posture", () => {
  it("refuses something that is not an image before reading it", async () => {
    // An error page would otherwise be base64'd in front of a model as though it
    // were a picture, and the message's own declared type must not decide that.
    const item = imageAttachment();
    const got = await fetchChatImages([item], {
      origin: ORIGIN,
      fetchFn: fetcher({
        [item.url]: {
          body: Buffer.from("<html>gone</html>"),
          headers: { "content-type": "text/html" },
        },
      }),
    });
    expect(got).toEqual([]);
  });

  it("refuses a body over the cap while it is read", async () => {
    // A content-length that lies, or is simply absent, otherwise allocates
    // whatever it likes before a later check objects.
    const item = imageAttachment();
    const big = { body: Buffer.concat([PNG, Buffer.alloc(200, 1)]) };
    expect(
      await fetchChatImages([item], {
        origin: ORIGIN,
        maxBytes: 32,
        fetchFn: fetcher({ [item.url]: big }),
      }),
    ).toEqual([]);
    // And the same body passes under a cap that fits it.
    expect(
      await fetchChatImages([item], {
        origin: ORIGIN,
        maxBytes: 4096,
        fetchFn: fetcher({ [item.url]: big }),
      }),
    ).toHaveLength(1);
  });

  it.each([301, 302, 401, 404, 500])(
    "drops an attachment on status %s",
    async (status) => {
      const item = imageAttachment();
      expect(
        await fetchChatImages([item], {
          origin: ORIGIN,
          fetchFn: fetcher({ [item.url]: { body: PNG, status } }),
        }),
      ).toEqual([]);
    },
  );

  it("lets one unreachable attachment cost only itself", async () => {
    const good = imageAttachment("good.png");
    const bad = imageAttachment("bad.png");
    const got = await fetchChatImages([bad, good], {
      origin: ORIGIN,
      fetchFn: fetcher({ [good.url]: { body: PNG } }),
    });
    expect(got.map((i) => i.name)).toEqual(["good.png"]);
  });

  it("keeps only so many images", async () => {
    const items = Array.from({ length: MAX_IMAGES + 3 }, (_, i) =>
      imageAttachment(`${i}.png`),
    );
    const bodies = Object.fromEntries(items.map((i) => [i.url, { body: PNG }]));
    expect(
      await fetchChatImages(items, {
        origin: ORIGIN,
        fetchFn: fetcher(bodies),
      }),
    ).toHaveLength(MAX_IMAGES);
  });

  it("caps attempts, not just successes", async () => {
    // The accept cap counts only successes, so a message naming fifty
    // attachments that all fail still costs fifty timeouts and blows the turn.
    const items = Array.from({ length: 50 }, (_, i) =>
      imageAttachment(`${i}.png`),
    );
    const seen: string[] = [];
    expect(
      await fetchChatImages(items, {
        origin: ORIGIN,
        fetchFn: fetcher({}, seen),
      }),
    ).toEqual([]);
    expect(seen).toHaveLength(8);
  });
});

describe("transcription", () => {
  const clip = { kind: "audio", name: "note.wav", url: `${ORIGIN}/v` };
  const served = {
    [clip.url]: {
      body: Buffer.from("RIFF"),
      headers: { "content-type": "audio/wav" },
    },
  };

  it("turns a voice note into words", async () => {
    const said = await transcribeVoiceMessages([clip], {
      origin: ORIGIN,
      fetchFn: fetcher(served),
      transcribe: async (data, mime) => `heard ${data.length} bytes of ${mime}`,
    });
    expect(said).toBe("heard 4 bytes of audio/wav");
  });

  it("fetches nothing without a transcriber", async () => {
    const seen: string[] = [];
    expect(
      await transcribeVoiceMessages([clip], {
        origin: ORIGIN,
        fetchFn: fetcher({}, seen),
      }),
    ).toBe("");
    expect(seen).toEqual([]);
  });

  it("lets a failing transcriber cost that clip only", async () => {
    const said = await transcribeVoiceMessages([clip], {
      origin: ORIGIN,
      fetchFn: fetcher(served),
      transcribe: async () => {
        throw new Error("the speech endpoint is down");
      },
    });
    expect(said).toBe("");
  });
});

describe("the notes", () => {
  it("names what came with the message", () => {
    const note = attachmentsNote([
      imageAttachment("plan.png"),
      { kind: "file", name: "q3.xlsx" },
    ]);
    expect(note).toContain("plan.png");
    expect(note).toContain("q3.xlsx");
  });

  it("says which ones could not be read", () => {
    // A model told a picture was attached and could not be opened says something
    // useful. A model told nothing answers as if the message were empty.
    expect(
      attachmentsNote([imageAttachment()], new Map([[0, "unreadable"]])),
    ).toContain("unreadable");
  });

  it("is bounded", () => {
    const note = attachmentsNote(
      Array.from({ length: 40 }, (_, i) => imageAttachment(`${i}.png`)),
    );
    expect(note).toContain("more");
    expect(note.split("\n")).toHaveLength(12);
  });

  it("is empty when nothing was attached", () => {
    expect(attachmentsNote([])).toBe("");
  });

  it("carries a button press to the model", () => {
    // A card message arrives with empty text, so without this the agent is asked
    // nothing at all.
    const note = cardActionNote({ action: "approve", id: "42" });
    expect(note).toContain("approve");
    expect(note).toContain("42");
  });

  it("is empty with no card action", () => {
    expect(cardActionNote(undefined)).toBe("");
  });

  it("bounds a card payload", () => {
    expect(cardActionNote({ blob: "x".repeat(90_000) }).length).toBeLessThan(
      5_000,
    );
  });
});

describe("the turn", () => {
  it("reads in the order a person would say it", async () => {
    const image = imageAttachment("plan.png");
    const audio = { kind: "audio", name: "note.wav", url: `${ORIGIN}/v` };
    const turn = await buildChatTurn(
      message({
        text: "what do you make of this?",
        attachments: [image, audio],
        cardAction: { action: "approve" },
      }),
      {
        origin: ORIGIN,
        transcribe: async () => "and here is what I said",
        fetchFn: fetcher({
          [image.url]: { body: PNG },
          [audio.url]: {
            body: Buffer.from("RIFF"),
            headers: { "content-type": "audio/wav" },
          },
        }),
      },
    );
    expect(turn.images).toHaveLength(1);
    expect(turn.voiceNote).toBe("and here is what I said");
    const order = [
      turn.query.indexOf("what do you make of this?"),
      turn.query.indexOf("approve"),
      turn.query.indexOf("and here is what I said"),
      turn.query.indexOf("plan.png"),
    ];
    expect(order).toEqual([...order].sort((a, b) => a - b));
  });

  it("is just the text when nothing was attached", async () => {
    const turn = await buildChatTurn(message({ text: "hello" }), {
      origin: ORIGIN,
    });
    expect(turn.query).toBe("hello");
    expect(turn.images).toEqual([]);
  });

  it("never throws when everything fails", async () => {
    const turn = await buildChatTurn(
      message({ text: "look at this", attachments: [imageAttachment()] }),
      { origin: ORIGIN, fetchFn: fetcher({}) },
    );
    expect(turn.query).toContain("look at this");
    expect(turn.query).toContain("unreadable");
  });
});

describe("spooling a clip", () => {
  it("removes it afterwards", async () => {
    // A transcription engine is handed somebody's voice, and leaving it in a
    // temporary directory is a copy nobody decided to keep.
    let kept = "";
    await spoolClip(
      { data: Buffer.from("RIFF"), mime: "audio/wav", name: "n.wav" },
      async (path) => {
        kept = path;
        expect(readFileSync(path)).toEqual(Buffer.from("RIFF"));
        expect(path.endsWith(".wav")).toBe(true);
      },
    );
    expect(existsSync(kept)).toBe(false);
  });

  it("removes it even when the body throws", async () => {
    let kept = "";
    await expect(
      spoolClip(
        { data: Buffer.from("RIFF"), mime: "audio/wav", name: "" },
        async (path) => {
          kept = path;
          throw new Error("the engine failed");
        },
      ),
    ).rejects.toThrow("the engine failed");
    expect(existsSync(kept)).toBe(false);
  });
});

describe("what goes back out", () => {
  it("echoes which connection the reply is from", () => {
    // One tenant can have several connections, so the tenant alone no longer
    // says who a reply is from.
    const reply = buildReply(
      message({ text: "hi", bindingId: "binding-2" }),
      "hello",
    );
    expect(reply.bindingId).toBe("binding-2");
  });

  it("sends no binding when the inbound carried none", () => {
    expect(buildReply(message({ text: "hi" }), "hello")).not.toHaveProperty(
      "bindingId",
    );
  });

  it("reads the binding off the wire", () => {
    const body = JSON.stringify({
      tenantId: "t",
      conversationId: "c",
      activityId: "a",
      text: "hi",
      bindingId: "binding-7",
    });
    expect(parseInbound(body).bindingId).toBe("binding-7");
  });

  it("checks an image against its own bytes", () => {
    // A declared type is a claim. Without the signature check an HTML document
    // labelled image/png is posted into somebody's chat under this bot's name.
    expect(outboundImage(PNG, "image/png").contentType).toBe("image/png");
    expect(() =>
      outboundImage(Buffer.from("<html>hello</html>"), "image/png"),
    ).toThrow(/not image\/png/);
    expect(() =>
      outboundImage(Buffer.from("GIF89a1234567890"), "image/png"),
    ).toThrow(/not image\/png/);
  });

  it("refuses a scriptable image by type", () => {
    // SVG is scriptable XML, which is precisely what an image must not be.
    expect(() => outboundImage(Buffer.from("<svg/>"), "image/svg+xml")).toThrow(
      /must be one of/,
    );
  });

  it("accepts a common misspelling of jpeg", () => {
    const jpeg = Buffer.concat([
      Buffer.from([0xff, 0xd8, 0xff]),
      Buffer.alloc(10),
    ]);
    expect(outboundImage(jpeg, "image/jpg").contentType).toBe("image/jpeg");
  });

  it("refuses an oversized image", () => {
    const big = Buffer.concat([PNG, Buffer.alloc(OUTBOUND_IMAGE_MAX_BYTES)]);
    expect(() => outboundImage(big, "image/png")).toThrow(/over the/);
  });

  it("treats a RIFF container as webp only when it says so", () => {
    expect(sniffImageType(Buffer.from("RIFF____WEBPVP8 "))).toBe("image/webp");
    expect(sniffImageType(Buffer.from("RIFF____WAVEfmt "))).toBeUndefined();
  });

  it.each([
    ["../../etc/passwd.png", "passwd.png"],
    ["C:\\Users\\x\\plan.png", "plan.png"],
    ["  ", undefined],
    ["..", undefined],
  ])("cannot let a filename escape or be empty (%s)", (given, expected) => {
    // It reaches a chat as a download, chosen by a model somebody is steering.
    expect(sanitizeImageName(given)).toBe(expected);
  });

  it("keeps the extension on a long filename", () => {
    const got = sanitizeImageName("x".repeat(400) + ".png")!;
    expect(got.endsWith(".png")).toBe(true);
    expect(got.length).toBeLessThanOrEqual(200);
  });

  it("carries neither text nor image on a typing indicator", () => {
    // It is a state, not a message.
    const reply = buildReply(
      message({ text: "hi" }),
      "hello",
      "typing",
      outboundImage(PNG, "image/png"),
    );
    expect(reply).not.toHaveProperty("text");
    expect(reply).not.toHaveProperty("image");
  });
});
