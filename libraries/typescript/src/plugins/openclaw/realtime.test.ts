import { describe, expect, it } from "vitest";

import { Transcript } from "../../index.js";
import { turnSpeaker } from "./realtime.js";
describe("speaker attribution", () => {
  const start = { caller: { displayName: "Alaa Elhenawy" } };

  it("files each unmixed speaker under their own name, as separate blocks", () => {
    const transcript = new Transcript();
    const session = { speaker: "Dana Reyes", start };
    transcript.add(turnSpeaker(session), "we agreed to ship on Friday");
    session.speaker = "Omar Haddad";
    transcript.add(turnSpeaker(session), "and the budget stays as it is");
    expect(transcript.turns.map((t) => t.speaker)).toEqual(["Dana", "Omar"]);
    expect(transcript.turns).toHaveLength(2);
  });

  it("falls back to the caller's first name on mixed audio, then to Caller", () => {
    expect(turnSpeaker({ speaker: undefined, start })).toBe("Alaa");
    expect(turnSpeaker({ speaker: "  ", start })).toBe("Alaa");
    expect(turnSpeaker({ start: { caller: {} } })).toBe("Caller");
  });
});
