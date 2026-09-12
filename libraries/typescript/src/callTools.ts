// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The things an agent can do about the call it is on.
 *
 * A voice model can talk. What it cannot do, unless you tell it, is hang up, put
 * a picture on its own video tile, react with an expression, or look at what the
 * caller is showing. Those are properties of being on a Microsoft Teams call,
 * not of any provider, so they live here and every plugin gets the same set.
 *
 * Two halves, and the split matters:
 *
 * {@link CallTools.schemas} declares the tools, in the shape your provider
 * wants. Every provider invented its own JSON for "here is a function you may
 * call", so the same five capabilities were being written out four times with
 * four sets of wording. One description, rendered per dialect, means a caller
 * gets the same behaviour whichever provider answers.
 *
 * {@link CallTools.dispatch} runs one, and **never throws**. It returns a
 * sentence. The result goes back to a model that will read it out, so "I could
 * not show that because the image was too large" is worth something and a stack
 * trace is not.
 *
 * Adding a fifth capability is one edit here rather than one per plugin, which
 * is the whole reason this is not in a plugin.
 *
 * Identical in shape to the Python SDK's `standin.calltools`.
 */

import type { CallSession } from "./handler.js";
import { logger } from "./log.js";
import { VisionTools } from "./visionTools.js";

/**
 * What a tool did, for the providers that want more than a sentence.
 *
 * `text` is the sentence, and for most callers it is the whole answer. `ok` is
 * for the providers whose tool-result frame carries an error flag (ElevenLabs,
 * OpenAI): it is false only when the SDK KNOWS the tool did not do what it was
 * asked, which is a missing or malformed argument, a rejected value, a handler
 * that threw, or a name no tool answers.
 *
 * It is not a verdict on the vision tools. Those answer in sentences by design,
 * so "there is nothing to look at" comes back as ok, with the reason in the text
 * where the model will actually read it.
 */
export interface ToolResult {
  readonly text: string;
  readonly ok: boolean;
}

/** One capability, described once, rendered per provider. */
export interface ToolSpec {
  readonly name: string;
  /** Written FOR A MODEL: when to reach for it, not what the code does. */
  readonly description: string;
  /**
   * JSON Schema for one parameter. `enum` is here because a mode or a choice
   * spelled out in the schema is obeyed far more reliably by a model than the
   * same list written into the description.
   */
  readonly parameters?: Record<
    string,
    { type: string; description: string; enum?: readonly string[] }
  >;
  readonly required?: readonly string[];
}

/** Which provider's JSON shape {@link toolSchemas} should emit. */
export type ToolDialect = "flat" | "openai" | "anthropic";

/**
 * The capabilities every call has. The descriptions are written FOR A MODEL:
 * they say when to reach for the tool, not what the code does, because that is
 * the only thing the model reads before deciding.
 */
/**
 * Deliberately NOT one of the built-ins.
 *
 * The built-in list is declared unconditionally, so a deployment with no
 * renderer would still be telling every model it can show a web page. It would
 * promise the caller and then apologise, on every call, everywhere. An absent
 * tool is honest; a broken one is not. A plugin that actually has a renderer
 * registers this one through {@link CallTools.register}.
 */
export const SHOW_PAGE_TOOL: ToolSpec = {
  name: "show_page",
  description:
    "Open a web page and show the caller a picture of it on your video tile. Use it " +
    "when the caller asks about a page, a dashboard or a document that lives at a URL.",
  parameters: {
    url: { type: "string", description: "Public https URL of the page." },
    caption: { type: "string", description: "Optional short caption." },
  },
  required: ["url"],
};

export const BUILT_IN_TOOLS: readonly ToolSpec[] = [
  {
    name: "end_call",
    description:
      "Hang up. Use this when the conversation is finished, the caller says goodbye, " +
      "or the caller asks you to hang up.",
  },
  {
    name: "express",
    description:
      "Show an emotion on your avatar's face. Use it to react naturally, for example " +
      "happy when greeting someone or surprised at unexpected news.",
    parameters: {
      emotion: {
        type: "string",
        description: "happy, sad, surprised, thinking or neutral.",
      },
    },
    required: ["emotion"],
  },
  {
    name: "show_image",
    description:
      "Show the caller an image on your video tile. Give a public https URL of a jpeg " +
      "or png. Use it when seeing something would help more than hearing it.",
    parameters: {
      url: {
        type: "string",
        description: "Public https URL of a jpeg or png.",
      },
      caption: { type: "string", description: "Optional short caption." },
      display: {
        type: "string",
        enum: ["fullscreen", "overlay"],
        description:
          'How to show it: "fullscreen" for something being read, "overlay" to keep ' +
          "your face beside it. Leave it out to use the default.",
      },
    },
    required: ["url"],
  },
  {
    name: "look",
    description:
      "Look at the caller's camera or shared screen and find out what is visible. Use " +
      "it when the caller refers to something they are showing you.",
    parameters: {
      source: {
        type: "string",
        description: 'Which video to look at: "camera" or "screenshare".',
      },
      question: {
        type: "string",
        description: "What you want to know about what they are showing.",
      },
    },
  },
  {
    name: "look_back",
    description:
      "Look again at something the caller showed earlier and has already moved past. " +
      "Use it when they ask about a slide or screen that is no longer up. Only works " +
      "while the call is being recorded.",
    parameters: {
      question: {
        type: "string",
        description: "What you want to know about it.",
      },
    },
  },
];

function jsonSchema(spec: ToolSpec): Record<string, unknown> {
  return {
    type: "object",
    properties: spec.parameters ?? {},
    required: [...(spec.required ?? [])],
  };
}

/**
 * Render the tool declarations in one provider's shape.
 *
 * - `flat`: `{name, description, parameters}`. What Deepgram's Settings message
 *   and ElevenLabs' client tools both take.
 * - `openai`: the same, tagged `type: "function"`, which the Realtime API wants.
 * - `anthropic`: `{name, description, input_schema}`.
 *
 * An unknown dialect falls back to `flat` rather than throwing, because the
 * consequence of guessing wrong here is a tool a model never sees, and a plugin
 * author is better served by a working default than by a crash at connect time.
 */
export function toolSchemas(
  dialect: ToolDialect | string = "flat",
  extra: readonly ToolSpec[] = [],
): Record<string, unknown>[] {
  const specs = [...BUILT_IN_TOOLS, ...extra];
  if (dialect === "openai") {
    return specs.map((spec) => ({
      type: "function",
      name: spec.name,
      description: spec.description,
      parameters: jsonSchema(spec),
    }));
  }
  if (dialect === "anthropic") {
    return specs.map((spec) => ({
      name: spec.name,
      description: spec.description,
      input_schema: jsonSchema(spec),
    }));
  }
  if (dialect !== "flat" && dialect !== "") {
    logger.debug(
      `standin: unknown tool dialect "${dialect}"; using the flat shape`,
    );
  }
  return specs.map((spec) => ({
    name: spec.name,
    description: spec.description,
    parameters: jsonSchema(spec),
  }));
}

/**
 * A tool of your own. Returns what the model is told, which it will read out or
 * reason from, so keep it short and keep it fast: the caller is listening to
 * silence while it runs.
 */
export type ToolHandler = (
  params: Record<string, unknown>,
) => string | Promise<string>;

/** Options for {@link CallTools}. */
export interface CallToolsOptions {
  /** The vision tools these dispatch into. One is built if you pass none. */
  vision?: VisionTools;
}

/**
 * The built-in call capabilities, bound to one call.
 *
 * Built once per call by a plugin, which then only has to translate its
 * provider's tool-call frame into a name and an object:
 *
 * ```ts
 * const tools = new CallTools(session, { vision: new VisionTools(session, { describer }) });
 * agent.declare(tools.schemas("flat"));
 * // ...
 * const result = await tools.dispatch(name, params);   // never throws
 * ```
 *
 * Your own tools go in the same place, so a model sees one list:
 *
 * ```ts
 * tools.register({ name: "open_ticket", description: "..." }, handler);
 * ```
 */
export class CallTools {
  readonly #session: CallSession;
  readonly vision: VisionTools;
  readonly #extra = new Map<string, { spec: ToolSpec; handler: ToolHandler }>();

  constructor(session: CallSession, options: CallToolsOptions = {}) {
    this.#session = session;
    this.vision = options.vision ?? new VisionTools(session);
  }

  /**
   * Add a tool of your own.
   *
   * Refuses a name that shadows a built-in, and refuses it HERE rather than at
   * the first call: a shadowed `end_call` is an agent that has quietly lost the
   * ability to hang up, and that is not something to discover mid-conversation.
   */
  register(spec: ToolSpec, handler: ToolHandler): void {
    if (BUILT_IN_TOOLS.some((builtIn) => builtIn.name === spec.name)) {
      throw new Error(
        `"${spec.name}" is a built-in call capability and cannot be replaced`,
      );
    }
    this.#extra.set(spec.name, { spec, handler });
  }

  /** Every tool, built-in and yours, in one provider's shape. */
  schemas(dialect: ToolDialect | string = "flat"): Record<string, unknown>[] {
    return toolSchemas(
      dialect,
      [...this.#extra.values()].map((entry) => entry.spec),
    );
  }

  /**
   * Run one tool and return the sentence the model should be told.
   *
   * Never throws. The result is read out loud, so a failure has to arrive as a
   * sentence or the agent simply goes quiet and the caller waits. Use
   * {@link run} instead when your provider's tool-result frame also carries an
   * error flag.
   */
  async dispatch(
    name: string,
    params: Record<string, unknown> = {},
  ): Promise<string> {
    return (await this.run(name, params)).text;
  }

  /**
   * Run one tool and return the sentence plus whether it worked.
   *
   * Never throws. See {@link ToolResult} for what `ok` does and does not claim.
   */
  async run(
    name: string,
    params: Record<string, unknown> = {},
  ): Promise<ToolResult> {
    try {
      return await this.#run(name, params ?? {});
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      logger.warn(`standin: the ${name} tool failed: ${reason}`);
      return { text: `${name} failed: ${reason}`, ok: false };
    }
  }

  async #run(
    name: string,
    params: Record<string, unknown>,
  ): Promise<ToolResult> {
    if (name === "end_call") {
      await this.#session.end("agent-ended-call");
      return { text: "the call is ending", ok: true };
    }

    if (name === "express") {
      const raw = params.emotion;
      const emotion = typeof raw === "string" ? raw.trim() : "";
      if (emotion === "")
        return { text: "express needs an 'emotion'", ok: false };
      try {
        await this.#session.express(emotion);
      } catch (err) {
        return {
          text: err instanceof Error ? err.message : String(err),
          ok: false,
        };
      }
      return { text: `expressing ${emotion}`, ok: true };
    }

    if (name === "show_image") {
      const url = typeof params.url === "string" ? params.url : "";
      const caption =
        typeof params.caption === "string" ? params.caption : undefined;
      const display =
        typeof params.display === "string" ? params.display : undefined;
      return {
        text: await this.vision.showUrl(url, caption, display),
        ok: true,
      };
    }

    if (name === "look") {
      const question =
        typeof params.question === "string" ? params.question : "";
      const source =
        typeof params.source === "string" ? params.source : undefined;
      return { text: await this.vision.look(question, source), ok: true };
    }

    if (name === "look_back") {
      const question =
        typeof params.question === "string" ? params.question : "";
      return { text: await this.vision.lookBack(question), ok: true };
    }

    const entry = this.#extra.get(name);
    if (entry === undefined) {
      return { text: `"${name}" is not a tool this agent has`, ok: false };
    }
    return { text: String(await entry.handler(params)), ok: true };
  }
}
