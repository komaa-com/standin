// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Writing the meeting up after it ends.
 *
 * The recap runs during teardown, so nothing here may throw. And it runs on a
 * transcript kept for the whole call, so nothing here may grow without limit.
 *
 * The Python twin is `tests/test_minutes.py`.
 */

import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { inflateRawSync } from "node:zlib";

import { describe, expect, it } from "vitest";

import type { PersonalChat } from "./chat.js";
import {
  DOCUMENT_NOT_ATTACHED,
  MAX_TRANSCRIPT_ENTRIES,
  MAX_TRANSCRIPT_ENTRY_CHARS,
  MAX_TRANSCRIPT_TURNS,
  MINUTES_TOOL,
  Transcript,
  type DeliveryTarget,
  type Turn,
  hasSpeakerPrefix,
  isSummaryRequest,
  minutesPrompt,
  parseMinutesSections,
  postMinutes,
  resolveMinutesTarget,
  writeMinutesDocx,
} from "./minutes.js";

/** The meeting this call is attached to. */
const THREAD: DeliveryTarget = {
  kind: "thread",
  conversationId: "19:meeting_x@thread.v2",
  tenantId: "tenant-a",
};

/** The caller's own chat with the bot. */
const DM: DeliveryTarget = {
  kind: "caller-dm",
  conversationId: "a:1dana",
  tenantId: "tenant-a",
};

/** As `PersonalChats.forCaller()` would hand it over. */
const CALLER_CHAT: PersonalChat = {
  conversationId: "a:1dana",
  tenantId: "tenant-a",
  aadId: "dana-aad",
  displayName: "Dana",
  atMs: 1_000,
};

function scratch(): string {
  return mkdtempSync(join(tmpdir(), "standin-minutes-"));
}

function transcript(): Transcript {
  const t = new Transcript();
  t.add("Dana", "we should push the launch to March");
  t.add("Ali", "agreed, I will tell the field team");
  return t;
}

async function summarise(): Promise<string> {
  return "**Decisions**\nThe launch moves to March.";
}

const posted = async (): Promise<boolean> => true;

/** Read a zip back the way a reader would: local headers, then inflate. */
function readZip(path: string): Map<string, string> {
  const data = readFileSync(path);
  const parts = new Map<string, string>();
  let at = 0;
  while (data.readUInt32LE(at) === 0x04034b50) {
    const crc = data.readUInt32LE(at + 14);
    const compressed = data.readUInt32LE(at + 18);
    const uncompressed = data.readUInt32LE(at + 22);
    const nameLength = data.readUInt16LE(at + 26);
    const extraLength = data.readUInt16LE(at + 28);
    const name = data.subarray(at + 30, at + 30 + nameLength).toString("utf8");
    const start = at + 30 + nameLength + extraLength;
    const body = inflateRawSync(data.subarray(start, start + compressed));
    expect(body.length, `${name} declared the wrong size`).toBe(uncompressed);
    expect(crc32(body), `${name} declared the wrong checksum`).toBe(crc);
    parts.set(name, body.toString("utf8"));
    at = start + compressed;
  }
  // An end-of-central-directory record has to be in there, or no reader will
  // even look at the entries above.
  expect(data.includes(Buffer.from([0x50, 0x4b, 0x05, 0x06]))).toBe(true);
  return parts;
}

function crc32(data: Buffer): number {
  let crc = 0xffffffff;
  for (const byte of data) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1)
      crc = crc & 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1;
  }
  return (crc ^ 0xffffffff) >>> 0;
}

describe("the transcript", () => {
  it("records who said it", () => {
    expect(transcript().render()).toContain(
      "Dana: we should push the launch to March",
    );
  });

  it("does not record an empty turn blank", () => {
    const t = new Transcript();
    t.add("Dana", "   ");
    expect(t.empty).toBe(true);
  });

  it("always gives a speaker a name", () => {
    const t = new Transcript();
    t.add("", "something");
    expect(t.render().startsWith("Caller:")).toBe(true);
  });

  it("is bounded", () => {
    // A two-hour meeting sits in the memory of a process that is also carrying
    // live audio. A speaker each, so nothing here is merged away.
    const t = new Transcript();
    for (let index = 0; index < MAX_TRANSCRIPT_TURNS + 50; index += 1) {
      t.add(`Speaker ${index}`, `turn ${index}`);
    }
    expect(t.turns).toHaveLength(MAX_TRANSCRIPT_TURNS);
    // The tail is what survives: the end of a meeting is what minutes are about.
    expect(t.render()).toContain(`turn ${MAX_TRANSCRIPT_TURNS + 49}`);
  });

  it("keeps the tail when rendering something too long", () => {
    const t = new Transcript();
    t.add("Dana", "x".repeat(500));
    t.add("Dana", "the last thing said");
    const rendered = t.render(100);
    expect(rendered).toHaveLength(100);
    expect(rendered).toContain("the last thing said");
  });

  it("renders the recap window, not every turn it holds", () => {
    // A recap is mostly about how the meeting ENDED, and the model is given
    // that window rather than the whole hard bound the transcript keeps.
    const t = new Transcript();
    for (let index = 0; index < MAX_TRANSCRIPT_ENTRIES + 5; index += 1) {
      t.add(`Speaker ${index}`, `turn ${index}`);
    }
    const rendered = t.render();
    expect(rendered).not.toContain("Speaker 0:");
    expect(rendered.split("\n")).toHaveLength(MAX_TRANSCRIPT_ENTRIES);
    expect(rendered).toContain(`turn ${MAX_TRANSCRIPT_ENTRIES + 4}`);
  });

  it("records what was shown beside what was said", () => {
    // The half a transcript-first recap structurally cannot have.
    const t = transcript();
    t.addVisual("Sara's shared screen: the Q3 revenue dashboard");
    expect(t.render()).toContain("[Shared on screen during the call]");
    expect(t.render()).toContain("Q3 revenue dashboard");
  });

  it("records an unchanged screen once", () => {
    // The vision lane answers about whatever is on screen each time it is
    // asked, so a static slide would otherwise fill the record.
    const t = new Transcript();
    for (let index = 0; index < 5; index += 1) t.addVisual("the same slide");
    t.addVisual("a different slide");
    t.addVisual("the same slide");
    expect([...t.visuals]).toEqual([
      "the same slide",
      "a different slide",
      "the same slide",
    ]);
  });

  it("is not empty when only something was shown", () => {
    const t = new Transcript();
    t.addVisual("a diagram");
    expect(t.empty).toBe(false);
  });
});

describe("recognising the request", () => {
  it.each([
    "can you summarise the meeting",
    "send me the minutes of this call",
    "give me a recap of the discussion",
    "write up notes from the conversation",
  ])("recognises %s", (text) => {
    expect(isSummaryRequest(text)).toBe(true);
  });

  it.each([
    "summarise this document",
    "what are the minutes in an hour",
    "recap the article",
    "",
  ])("does not mistake %s for one", (text) => {
    // Summarise alone is asked about a document, an email, or a page the
    // agent is looking at.
    expect(isSummaryRequest(text)).toBe(false);
  });

  it("forbids inventing what was on screen", () => {
    // A model handed "Sara shared a dashboard" will invent the numbers on it,
    // and minutes that invent numbers are worse than minutes with a gap.
    const prompt = minutesPrompt("Dana: hello");
    expect(prompt).toContain("never infer what was on screen");
    expect(prompt).toContain("Dana: hello");
  });

  it("describes the tool for a model", () => {
    expect(MINUTES_TOOL.description.toLowerCase()).toContain("use it when");
    expect(MINUTES_TOOL.required).toBeUndefined();
  });
});

describe("the recap", () => {
  it("posts the minutes to the chat", async () => {
    const sent: string[] = [];
    const result = await postMinutes(
      summarise,
      transcript(),
      THREAD,
      async (_target, text) => {
        sent.push(text);
        return true;
      },
    );
    expect(result.delivered).toBe(true);
    expect(result.spoken).toContain("posted the minutes");
    expect(sent[0]).toContain("The launch moves to March.");
  });

  it("tells a call with nothing said apart from one with nowhere to post", async () => {
    // Conflating them tells people their conversation did not count when it did.
    const nothing = await postMinutes(
      summarise,
      new Transcript(),
      THREAD,
      posted,
    );
    expect(nothing.spoken).toContain("not enough of a conversation");

    const nowhere = await postMinutes(
      summarise,
      transcript(),
      undefined,
      posted,
    );
    expect(nowhere.spoken).toContain("no Microsoft Teams chat");
  });

  it("posts a one-to-one call to the caller's own chat instead of refusing", async () => {
    // A resolved caller chat is a real target, so the refusal no longer applies.
    const sent: string[] = [];
    const result = await postMinutes(
      summarise,
      transcript(),
      DM,
      async (_target, text) => {
        sent.push(text);
        return true;
      },
    );
    expect(result.delivered).toBe(true);
    expect(sent[0]).toContain("The launch moves to March.");
  });

  it("hands the delivery the target that was resolved for it", async () => {
    // The recipient is settled before the summarising run and nothing
    // downstream may pick another one.
    let seen: DeliveryTarget | undefined;
    await postMinutes(summarise, transcript(), DM, async (target) => {
      seen = target;
      return true;
    });
    expect(seen).toEqual(DM);
  });

  it("says so in the message when the document cannot ride along", async () => {
    // Somebody told minutes were coming with a document, who gets text and no
    // explanation, assumes the attachment was lost and goes looking for it.
    const sent: string[] = [];
    const result = await postMinutes(
      summarise,
      transcript(),
      THREAD,
      async (_target, text) => {
        sent.push(text);
        return true;
      },
      { documentDir: join(scratch(), "minutes") },
    );
    expect(result.document).toBeTruthy();
    expect(sent[0]).toContain(DOCUMENT_NOT_ATTACHED);

    sent.length = 0;
    await postMinutes(
      summarise,
      transcript(),
      THREAD,
      async (_target, text) => {
        sent.push(text);
        return true;
      },
    );
    // Nothing was written, so there is nothing whose absence needs explaining.
    expect(sent[0]).not.toContain(DOCUMENT_NOT_ATTACHED);
  });

  it("never throws a failing summariser into teardown", async () => {
    const result = await postMinutes(
      async () => {
        throw new Error("the agent is down");
      },
      transcript(),
      THREAD,
      posted,
    );
    expect(result.spoken).toBe("I could not summarize the meeting.");
    expect(result.delivered).toBe(false);
  });

  it("reports an empty summary rather than posting it", async () => {
    const sent: string[] = [];
    const result = await postMinutes(
      async () => "   ",
      transcript(),
      THREAD,
      async (_t, text) => {
        sent.push(text);
        return true;
      },
    );
    expect(result.spoken).toContain("could not summarize");
    expect(sent).toEqual([]);
  });

  it("admits a failing delivery and still returns the minutes", async () => {
    const result = await postMinutes(
      summarise,
      transcript(),
      THREAD,
      async () => {
        throw new Error("the chat channel is closed");
      },
    );
    expect(result.delivered).toBe(false);
    expect(result.spoken).toContain("could not post it");
    // The minutes still came back, so a caller can do something else with them.
    expect(result.minutes).toBeTruthy();
  });

  it("writes the document when somewhere was named", async () => {
    const result = await postMinutes(summarise, transcript(), THREAD, posted, {
      documentDir: join(scratch(), "minutes"),
    });
    expect(result.document).toBeTruthy();
    expect(readZip(result.document!).size).toBe(4);
  });

  it("writes no document when none was asked for", async () => {
    const result = await postMinutes(summarise, transcript(), THREAD, posted);
    expect(result.document).toBeUndefined();
  });

  it("tries the next target when the first answers 404", async () => {
    // A meeting joined over the calling path never produced a conversation
    // reference, so the thread answers 404 while the caller's own chat is
    // perfectly reachable.
    const tried: string[] = [];
    const result = await postMinutes(
      summarise,
      transcript(),
      [THREAD, DM],
      async (target) => {
        tried.push(target.conversationId);
        return target === THREAD ? { ok: false, status: 404 } : { ok: true };
      },
    );
    expect(tried).toEqual([THREAD.conversationId, DM.conversationId]);
    expect(result.delivered).toBe(true);
    expect(result.target).toEqual(DM);
  });

  it.each([401, 500, 503])("tries nothing else on a %i", async (status) => {
    // 401 is our signing and 5xx is the gateway: both would fail identically at
    // the next target, and only a 404 proves nothing was delivered.
    const tried: string[] = [];
    const result = await postMinutes(
      summarise,
      transcript(),
      [THREAD, DM],
      async (target) => {
        tried.push(target.conversationId);
        return { ok: false, status };
      },
    );
    expect(tried).toEqual([THREAD.conversationId]);
    expect(result.delivered).toBe(false);
    expect(result.target).toBeUndefined();
  });

  it("skips a hole in the target list rather than throwing into teardown", async () => {
    // A caller builds the list from two lookups and one of them found nothing.
    const tried: DeliveryTarget[] = [];
    const result = await postMinutes(
      summarise,
      transcript(),
      [undefined, DM] as unknown as DeliveryTarget[],
      async (target) => {
        tried.push(target);
        return true;
      },
    );
    expect(tried).toEqual([DM]);
    expect(result.delivered).toBe(true);
  });

  it("never reads a rejected post as a delivered one", async () => {
    // An outcome object is always truthy, so a recap the gateway rejected would
    // otherwise be spoken and logged as delivered.
    const result = await postMinutes(
      summarise,
      transcript(),
      THREAD,
      async () => ({
        ok: false,
        status: 404,
      }),
    );
    expect(result.delivered).toBe(false);
    expect(result.spoken).toContain("could not post it");

    // Nor is anything else that merely happens to be truthy: only a real
    // boolean, or an outcome that says ok, is a post that landed.
    const stray = await postMinutes(
      summarise,
      transcript(),
      THREAD,
      async () => "posted" as unknown as boolean,
    );
    expect(stray.delivered).toBe(false);
  });

  it("does not lose the minutes when the document cannot be written", async () => {
    // The document is a convenience. The minutes are the point.
    const blocker = join(scratch(), "blocked");
    writeFileSync(blocker, "not a directory");

    const result = await postMinutes(summarise, transcript(), THREAD, posted, {
      documentDir: blocker,
    });
    expect(result.document).toBeUndefined();
    expect(result.delivered).toBe(true);
  });
});

describe("the document", () => {
  it("is a real docx", () => {
    // Four parts, or Word will not open it, and every entry has to declare a
    // size and a checksum a reader will actually check.
    const path = join(scratch(), "minutes.docx");
    writeMinutesDocx(
      "Meeting minutes",
      "**Decisions**\nThe launch moves to March.",
      path,
    );

    const parts = readZip(path);
    expect([...parts.keys()].sort()).toEqual([
      "[Content_Types].xml",
      "_rels/.rels",
      "word/_rels/document.xml.rels",
      "word/document.xml",
    ]);
    const document = parts.get("word/document.xml")!;
    expect(document).toContain("The launch moves to March.");
    expect(document).toContain("<w:b/>"); // the heading kept its emphasis
  });

  it("escapes what would break the XML", () => {
    // Minutes come from a model summarising whatever people said.
    const path = join(scratch(), "minutes.docx");
    writeMinutesDocx("Minutes", "Ali said <b>go</b> & Dana agreed", path);
    expect(readZip(path).get("word/document.xml")).toContain(
      "&lt;b&gt;go&lt;/b&gt; &amp; Dana",
    );
  });

  it("does not turn blank lines into empty paragraphs", () => {
    const path = join(scratch(), "minutes.docx");
    writeMinutesDocx("Minutes", "one\n\n\ntwo", path);
    const document = readZip(path).get("word/document.xml")!;
    expect(document.split("<w:p>").length - 1).toBe(3); // the title plus two lines
  });
});

describe("merging the fragments a transcript arrives in", () => {
  it("joins one speaker's fragments into a single entry", () => {
    // A model fed half-sentences as separate turns writes minutes that read
    // like a stutter.
    const t = new Transcript();
    t.add("Dana", "we should push");
    t.add("Dana", "the launch to March");
    expect([...t.turns]).toEqual([
      {
        speaker: "Dana",
        text: "we should push the launch to March",
        role: "caller",
      },
    ]);
  });

  it("never merges two speakers into one entry", () => {
    // Every later person's words would be filed under the first speaker's
    // name, which is worse than no attribution because it is confidently wrong.
    const t = new Transcript();
    t.add("Dana", "we should push the launch");
    t.add("Ali", "I disagree");
    expect(t.turns).toHaveLength(2);
    expect(t.turns[1]).toEqual({
      speaker: "Ali",
      text: "I disagree",
      role: "caller",
    });
  });

  it("never merges the agent's own words into a caller's entry", () => {
    // Both sides can be recorded under one name, and the side is what keeps
    // them apart when the name does not.
    const t = new Transcript();
    t.add("Assistant", "shall I write that up", "assistant");
    t.add("Assistant", "yes please");
    expect([...t.turns].map((turn) => turn.role)).toEqual([
      "assistant",
      "caller",
    ]);
  });

  it("starts a new entry rather than growing one past the cap", () => {
    // One long same-speaker run would otherwise become a single ever-growing
    // entry that the entry count can never trim.
    const t = new Transcript();
    t.add("Dana", "x".repeat(MAX_TRANSCRIPT_ENTRY_CHARS - 10));
    t.add("Dana", "y".repeat(50));
    expect(t.turns).toHaveLength(2);
    for (const turn of t.turns)
      expect(turn.text.length).toBeLessThan(MAX_TRANSCRIPT_ENTRY_CHARS);
  });

  it("discards from the front once a recap window is full", () => {
    const t = new Transcript({ maxEntries: MAX_TRANSCRIPT_ENTRIES });
    for (let index = 0; index < MAX_TRANSCRIPT_ENTRIES + 5; index += 1) {
      t.add(`Speaker ${index}`, `turn ${index}`);
    }
    expect(t.turns).toHaveLength(MAX_TRANSCRIPT_ENTRIES);
    expect(t.turns[0]).toEqual({
      speaker: "Speaker 5",
      text: "turn 5",
      role: "caller",
    });
  });
});

describe("reading the sections a model wrote", () => {
  it.each(["#", "##", "###", "####", "#####", "######"])(
    "takes %s followed by a space as a heading",
    (hashes) => {
      // Asked for "### Key points" a model returns whichever depth it feels
      // like that day, and accepting one form produced one unheaded blob.
      expect(
        parseMinutesSections(`${hashes} Key points\n- we ship in March`),
      ).toEqual([{ heading: "Key points", items: ["we ship in March"] }]);
    },
  );

  it("does not take a hash with no space after it as a heading", () => {
    expect(parseMinutesSections("#Key points")).toEqual([
      { heading: "Summary", items: ["#Key points"] },
    ]);
  });

  it("does not take seven hashes as a heading", () => {
    expect(parseMinutesSections("####### deep")).toEqual([
      { heading: "Summary", items: ["####### deep"] },
    ]);
  });

  it("takes a line that is bold end to end as a heading", () => {
    expect(parseMinutesSections("**Key points**\n- we ship")).toEqual([
      { heading: "Key points", items: ["we ship"] },
    ]);
  });

  it("strips the one trailing colon a bold heading often carries", () => {
    expect(parseMinutesSections("**Key points:**\n- we ship")[0]?.heading).toBe(
      "Key points",
    );
    expect(parseMinutesSections("### Key points:\n- we ship")[0]?.heading).toBe(
      "Key points",
    );
  });

  it("does not take a bold run that does not end the line as a heading", () => {
    expect(parseMinutesSections("**Dana** owns the launch")).toEqual([
      { heading: "Summary", items: ["**Dana** owns the launch"] },
    ]);
  });

  it("treats a line of hashes with nothing after them as an item", () => {
    expect(parseMinutesSections("### \n### Decisions")).toEqual([
      { heading: "Summary", items: ["###"] },
      { heading: "Decisions", items: [] },
    ]);
  });

  it.each([
    "- we ship in March",
    "* we ship in March",
    "• we ship in March",
    "1. we ship in March",
    "2) we ship in March",
  ])("strips the marker from %s", (line) => {
    // Models mix every marker inside one answer, and leaving the marker on
    // glues "- " to every line of the document.
    expect(parseMinutesSections(`### Decisions\n${line}`)[0]?.items).toEqual([
      "we ship in March",
    ]);
  });

  it("keeps an unbulleted line exactly as written", () => {
    // A section written as one prose paragraph is common, and dropping it took
    // whole sections out of the document with no trace.
    expect(
      parseMinutesSections("### Decisions\nWe decided to ship in March.")[0]
        ?.items,
    ).toEqual(["We decided to ship in March."]);
  });

  it("files anything before the first heading under Summary", () => {
    // A model that answers in one paragraph would otherwise parse to nothing
    // and produce a document with a title and no body.
    expect(
      parseMinutesSections(
        "we talked about the launch\n\n### Decisions\n- ship",
      ),
    ).toEqual([
      { heading: "Summary", items: ["we talked about the launch"] },
      { heading: "Decisions", items: ["ship"] },
    ]);
  });

  it("opens a second section when a heading repeats", () => {
    expect(
      parseMinutesSections("### Decisions\n- one\n### Decisions\n- two"),
    ).toEqual([
      { heading: "Decisions", items: ["one"] },
      { heading: "Decisions", items: ["two"] },
    ]);
  });

  it("drops a line that is only a bullet marker", () => {
    expect(
      parseMinutesSections("### Decisions\n-\n- we ship")[0]?.items,
    ).toEqual(["we ship"]);
  });

  it("keeps a section with no items, and leaves omitting it to the document", () => {
    // Splitting it this way keeps the parser round-trippable and leaves one
    // place that decides what is worth printing.
    expect(
      parseMinutesSections(
        "### Decisions\n### Action items\n- tell the field team",
      ),
    ).toEqual([
      { heading: "Decisions", items: [] },
      { heading: "Action items", items: ["tell the field team"] },
    ]);
  });

  it("skips blank lines everywhere", () => {
    expect(parseMinutesSections("\n\n### Decisions\n\n- we ship\n\n")).toEqual([
      { heading: "Decisions", items: ["we ship"] },
    ]);
  });

  it("returns nothing at all for nothing at all", () => {
    expect(parseMinutesSections("")).toEqual([]);
    expect(parseMinutesSections("   \n  ")).toEqual([]);
  });
});

describe("attribution the text already carries", () => {
  it("sees a name, a colon and a space", () => {
    expect(hasSpeakerPrefix("Sara: we should ship on Friday")).toBe(true);
    expect(hasSpeakerPrefix("Sara Khan: we should ship")).toBe(true);
  });

  it("does not see one in a leading colon or a leading space", () => {
    // A turn beginning ": ok" or " Sara: ok" is not attribution.
    expect(hasSpeakerPrefix(": ok")).toBe(false);
    expect(hasSpeakerPrefix(" Sara: ok")).toBe(false);
  });

  it("does not see one when the colon has nothing after it", () => {
    expect(hasSpeakerPrefix("Sara:ok")).toBe(false);
    expect(hasSpeakerPrefix("Sara:")).toBe(false);
  });

  it("only reads as far as the first colon", () => {
    expect(hasSpeakerPrefix("a:b: c")).toBe(false);
  });

  it("sees nothing in nothing", () => {
    expect(hasSpeakerPrefix("")).toBe(false);
  });
});

describe("the document a person keeps", () => {
  function document(
    minutes: string,
    options?: Parameters<typeof writeMinutesDocx>[3],
  ): string {
    const path = join(scratch(), "minutes.docx");
    writeMinutesDocx("Meeting minutes", minutes, path, options);
    return readZip(path).get("word/document.xml")!;
  }

  it("opens at A4 with margins", () => {
    // Without this Word opens it at Letter with no margins, which is the first
    // thing anyone notices about a document they were asked to keep.
    const body = document("one line");
    expect(body).toContain('<w:pgSz w:w="11906" w:h="16838"/>');
    expect(body).toContain(
      '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" ' +
        'w:header="708" w:footer="708" w:gutter="0"/>',
    );
  });

  it("carries a relationships part for the document itself", () => {
    // Validators refuse a part with no rels part, and a document Word repairs
    // on open is a document nobody trusts again.
    const path = join(scratch(), "minutes.docx");
    writeMinutesDocx("Meeting minutes", "one line", path);
    const rels = readZip(path).get("word/_rels/document.xml.rels")!;
    expect(rels).toContain("<Relationships");
    expect(rels.trimEnd().endsWith("/>")).toBe(true);
  });

  it("sizes the title above the headings", () => {
    const body = document("", {
      sections: [
        { heading: "Decisions", items: ["The launch moves to March."] },
      ],
    });
    expect(body).toContain('<w:b/><w:sz w:val="40"/>');
    expect(body).toContain('<w:spacing w:after="120"/>');
    expect(body).toContain('<w:b/><w:sz w:val="28"/>');
    expect(body).toContain('<w:spacing w:before="200" w:after="80"/>');
  });

  it("sizes a heading the same way whether the sections were parsed or not", () => {
    // A document whose headings are sized on one path and not on the other is
    // two documents.
    const body = document("**Decisions**\nThe launch moves to March.");
    expect(body).toContain('<w:b/><w:sz w:val="28"/>');
    expect(body).toContain('<w:t xml:space="preserve">Decisions</w:t>');
  });

  it("keeps every run's spacing", () => {
    // Without it the bullet's own space and every indent are collapsed away.
    expect(document("one line")).not.toContain("<w:t>");
  });

  it("writes the sections it is given instead of reading the minutes", () => {
    const body = document("IGNORED PROSE", {
      sections: parseMinutesSections("### Decisions\n- we ship in March"),
    });
    expect(body).toContain("Decisions");
    expect(body).toContain(
      '<w:t xml:space="preserve">• we ship in March</w:t>',
    );
    expect(body).not.toContain("IGNORED PROSE");
  });

  it("leaves out a section whose items are all blank", () => {
    // A bare "Decisions" over white space reads as a section the agent failed
    // to fill, rather than one that had nothing in it.
    const body = document("", {
      sections: [{ heading: "Decisions", items: ["  ", ""] }],
    });
    expect(body).not.toContain("Decisions");
  });

  it("puts the subtitle under the title", () => {
    const body = document("one line", {
      subtitle: "Call with Dana - ~12 min, 3 human participants.",
    });
    expect(body).toContain("Call with Dana - ~12 min, 3 human participants.");
  });

  it("writes who said what after the summary", () => {
    // The half a transcript-only recap tool cannot produce: unmixed audio gave
    // a real speaker per utterance.
    const turns: Turn[] = [
      { speaker: "Assistant", text: "I will write this up", role: "assistant" },
      { speaker: "Dana", text: "we should push the launch" },
    ];
    const body = document("", { sections: [], transcript: turns });
    expect(body).toContain("Attributed transcript");
    expect(body).toContain("Assistant: I will write this up");
    expect(body).toContain("Dana: we should push the launch");
  });

  it("does not prefix a turn that already names its speaker", () => {
    // Re-labelling it destroys the attribution, and prefixing it again reads
    // as a transcription error.
    const body = document("", {
      sections: [],
      transcript: [
        { speaker: "Caller", text: "Sara: we should ship on Friday" },
      ],
    });
    expect(body).toContain("Sara: we should ship on Friday");
    expect(body).not.toContain("Caller: Sara:");
  });

  it("calls an unnamed caller by the label it was given", () => {
    const body = document("", {
      sections: [],
      transcript: [{ speaker: "", text: "hello" }],
      callerLabel: "Caller side",
    });
    expect(body).toContain("Caller side: hello");
  });

  it("calls the agent by the label it was given", () => {
    const body = document("", {
      sections: [],
      transcript: [{ speaker: "Assistant", text: "hello", role: "assistant" }],
      assistantLabel: "Nadia",
    });
    expect(body).toContain("Nadia: hello");
  });

  it("skips an empty turn and writes no transcript section for nothing", () => {
    const body = document("", {
      sections: [],
      transcript: [{ speaker: "Dana", text: "   " }],
    });
    expect(body).not.toContain("Attributed transcript");
  });

  it("escapes the ampersand before anything else", () => {
    // The other four replacements get double-escaped otherwise.
    const body = document("Ali said <b>go</b> & Dana agreed");
    expect(body).toContain("&lt;b&gt;go&lt;/b&gt; &amp; Dana");
    expect(body).not.toContain("&amp;amp;");
  });
});

describe("where the minutes go", () => {
  it("sends a meeting recap to the meeting even when the count says one", () => {
    // The count only arrives on topologies that send a participants frame; on
    // a hosted worker it stays pinned at 1, and a count-only test put the
    // minutes of a group call in one attendee's DM.
    expect(
      resolveMinutesTarget({
        threadId: "19:meeting_x@thread.v2",
        humanCount: 1,
        callerAadId: "dana-aad",
        callerChat: CALLER_CHAT,
        sessionTenantId: "tenant-a",
      }),
    ).toEqual({
      kind: "thread",
      conversationId: "19:meeting_x@thread.v2",
      tenantId: "tenant-a",
    });
  });

  it("sends a group recap to the thread when a real count does arrive", () => {
    expect(
      resolveMinutesTarget({
        threadId: "a:1group",
        humanCount: 3,
        sessionTenantId: "tenant-a",
      }),
    ).toEqual({
      kind: "thread",
      conversationId: "a:1group",
      tenantId: "tenant-a",
    });
  });

  it("sends a one-to-one recap to the caller's own chat", () => {
    expect(
      resolveMinutesTarget({
        threadId: "",
        humanCount: 1,
        callerAadId: "dana-aad",
        callerChat: CALLER_CHAT,
        sessionTenantId: "tenant-a",
      }),
    ).toEqual({
      kind: "caller-dm",
      conversationId: "a:1dana",
      tenantId: "tenant-a",
    });
  });

  it("gives a call that identifies nobody nowhere to post", () => {
    // Rather than minutes in a stranger's chat.
    expect(
      resolveMinutesTarget({
        threadId: "  ",
        humanCount: 1,
        sessionTenantId: "tenant-a",
      }),
    ).toBeUndefined();
  });

  it("does not use a chat that belongs to somebody else", () => {
    expect(
      resolveMinutesTarget({
        threadId: "",
        callerAadId: "ali-aad",
        callerChat: CALLER_CHAT,
        sessionTenantId: "tenant-a",
      }),
    ).toBeUndefined();
  });

  it("takes the tenant from the session, then configuration, then the chat", () => {
    // All three describe the tenant this worker is bound to. The caller's own
    // tenant id describes whoever is on the phone and is not accepted at all.
    const call = {
      threadId: "19:meeting_x@thread.v2",
      callerChat: CALLER_CHAT,
    };
    expect(
      resolveMinutesTarget({
        ...call,
        sessionTenantId: "from-session",
        configTenantId: "from-config",
      })?.tenantId,
    ).toBe("from-session");
    expect(
      resolveMinutesTarget({ ...call, configTenantId: "from-config" })
        ?.tenantId,
    ).toBe("from-config");
    expect(
      resolveMinutesTarget({
        threadId: "",
        callerAadId: "dana-aad",
        callerChat: { ...CALLER_CHAT, tenantId: "from-the-remembered-chat" },
      })?.tenantId,
    ).toBe("from-the-remembered-chat");
  });
});
