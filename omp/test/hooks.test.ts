/** HOOK-01 (D-09/D-10) and the OPEN-3 R1 runtime part: before_agent_start behaviour; the hook never spawns. */
import { afterAll, beforeEach, describe, expect, spyOn, test } from "bun:test";
import { mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import type { Bridge } from "../src/bridge.ts";
import { SWARM_SLUGS } from "../src/guard.ts";
import { classifiedText, classifyPrompt, GSD_HANDOFF, RUNTIME_HEADING, runtimeContext, runtimePart, shellQuote, SWARM_CONTEXT, swarmContext } from "../src/hooks.ts";
import { createSwarmExtension } from "../src/index.ts";
import type { ExtensionContext } from "../src/omp-api.ts";
import { agentCtx, fakeCtx, fakePi, isolateEnv, REPO_ROOT, tmpDir } from "./helpers.ts";

isolateEnv("SWARM_CHILD", "SWARM_AGENT", "SWARM_ROOT");

const FIXTURE = JSON.parse(readFileSync(join(REPO_ROOT, "tests", "fixtures", "classifier_prompts.json"), "utf8")) as {
  positive: string[];
  negative: string[];
};
const SDLC = FIXTURE.positive[0];
const PRIOR = ["base prompt", "project rules"];

const spawn = spyOn(Bun, "spawn");
const spawnSync = spyOn(Bun, "spawnSync");
afterAll(() => {
  spawn.mockRestore();
  spawnSync.mockRestore();
});

beforeEach(() => {
  delete process.env.SWARM_CHILD;
  delete process.env.SWARM_AGENT;
  delete process.env.SWARM_ROOT;
});

/** The before_agent_start handler the real factory registers; the bridge throws if ever reached. */
function hook(): (event: unknown, ctx: ExtensionContext) => unknown {
  const bridge: Bridge = () => {
    throw new Error("the context hook must never reach the bridge");
  };
  const pi = fakePi();
  pi.load(createSwarmExtension({ bridge }));
  return pi.handler("before_agent_start");
}

const top = () => fakeCtx("/nonexistent-hook-cwd");

/** The injected parts of a handler result (none when it left the prompt untouched). */
const parts = (out: unknown): string[] => (out as { systemPrompt?: string[] } | undefined)?.systemPrompt ?? [];

describe("fixture positives inject once, after the prior parts", () => {
  for (const prompt of FIXTURE.positive) {
    test(`positive: ${prompt.slice(0, 60)}`, () => {
      const out = hook()({ prompt, systemPrompt: PRIOR }, top()) as { systemPrompt: string[] };
      expect(out.systemPrompt.slice(0, -1)).toEqual(PRIOR);
      const added = out.systemPrompt.at(-1) ?? "";
      expect(added.startsWith("## AgentSwarm")).toBe(true);
      expect(added).toContain("/swarm <brief>");
      expect(added).toContain("If A01 cannot dispatch");
      expect(added).toContain("Read-only discovery stays in this session");
      expect(added).toContain("signed task.assign");
      expect(added).toContain("Do not ask for credentials");
      expect(added).toContain("not a swarm BLOCKED");
      expect(added).not.toContain("yields BLOCKED");
      expect(added).toContain("task");
      expect(added).toContain("a01-orchestrator");
      expect(out.systemPrompt.filter((p) => p.includes("## AgentSwarm"))).toHaveLength(1);
    });
  }
});

describe("fixture negatives are silent except the gsd-autonomous handoff", () => {
  for (const prompt of FIXTURE.negative) {
    test(`negative: ${JSON.stringify(prompt.slice(0, 60))}`, () => {
      const out = hook()({ prompt, systemPrompt: PRIOR }, top());
      if (prompt.toLowerCase().includes("gsd-autonomous")) {
        const added = parts(out).at(-1);
        expect(added).toBe(GSD_HANDOFF);
        expect(added).not.toContain(["E", "POLICY"].join("-"));
      } else {
        expect(out).toBeUndefined();
      }
    });
  }
});

test("gsd-autonomous handoff text does not name the retired policy token", () => {
  expect(GSD_HANDOFF).not.toContain(["E", "POLICY"].join("-"));
  expect(GSD_HANDOFF).not.toContain(["E", "POLICY"].join(""));
  expect(GSD_HANDOFF).toContain("If A01 cannot dispatch");
  expect(GSD_HANDOFF).toContain("Do not hand off with every node blocked");
  expect(GSD_HANDOFF).toContain("finish them inline");
  expect(GSD_HANDOFF).toContain("signed task.assign");
  expect(GSD_HANDOFF).toContain("Do not ask for credentials");
});

test("gsd-autonomous handoff fires only for a leading /gsd-autonomous command", () => {
  const question = hook()({ prompt: "What is gsd-autonomous?", systemPrompt: PRIOR }, top());
  expect(parts(question)).not.toContain(GSD_HANDOFF);
  const command = hook()({ prompt: "/gsd-autonomous", systemPrompt: PRIOR }, top());
  expect(parts(command).at(-1)).toBe(GSD_HANDOFF);
});

// ── WR-01: an Ultrathink/Prompt-Uplift XML is classified by the user's <ORIGINAL> ─────────

/** The plugin escapes the user's words into <ORIGINAL> (plugin src/uplift/xml.ts escapeXml). */
const escapeXml = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/** The plugin's fallback shape: its closing `</UPLIFTED_PROMPT>` contains the NEGATIVE token `/uplift`. */
const fallbackUplift = (original: string) =>
  [
    "<UPLIFTED_PROMPT>",
    `\t<ORIGINAL>${escapeXml(original)}</ORIGINAL>`,
    "\t<SCOPE>Only the work required to fulfill the original request.</SCOPE>",
    "</UPLIFTED_PROMPT>",
  ].join("\n");

/** An LLM uplift whose sections carry SDLC verbs that must not decide for the ORIGINAL. */
const researchUplift = (original: string) =>
  `<RESEARCH_PROMPT>\n<ORIGINAL>${escapeXml(original)}</ORIGINAL>\n<TASK>Build an answer, add sources, implement nothing.</TASK>\n</RESEARCH_PROMPT>`;

describe("uplift XML: HOOK-01 classifies the user's ORIGINAL (WR-01)", () => {
  for (const prompt of FIXTURE.positive) {
    test(`fallback uplift of a positive injects: ${JSON.stringify(prompt.slice(0, 50))}`, () => {
      const out = hook()({ prompt: fallbackUplift(prompt), systemPrompt: PRIOR }, top());
      expect(parts(out)).toEqual([...PRIOR, SWARM_CONTEXT]);
    });
  }
  for (const prompt of FIXTURE.negative) {
    test(`research uplift of a negative is silent unless it is gsd-autonomous: ${JSON.stringify(prompt.slice(0, 50))}`, () => {
      const out = hook()({ prompt: researchUplift(prompt), systemPrompt: PRIOR }, top());
      if (prompt.toLowerCase().includes("gsd-autonomous")) {
        expect(parts(out).at(-1)).toBe(GSD_HANDOFF);
      } else {
        expect(out).toBeUndefined();
      }
    });
  }
  test("an uplift root without an ORIGINAL is classified whole", () => {
    const build = "<BUILD_PROMPT>\n<TASK>implement the billing module</TASK>\n</BUILD_PROMPT>";
    expect(parts(hook()({ prompt: build, systemPrompt: PRIOR }, top()))).toEqual([...PRIOR, SWARM_CONTEXT]);
    const bare = "<UPLIFTED_PROMPT>\n<TASK>implement the billing module</TASK>\n</UPLIFTED_PROMPT>";
    expect(hook()({ prompt: bare, systemPrompt: PRIOR }, top())).toBeUndefined();
  });
  test("an unclosed run of ORIGINAL tags is classified whole without scanning once per opener", () => {
    const prompt = `<BUILD_PROMPT>${"<ORIGINAL>".repeat(20000)}implement the billing module</BUILD_PROMPT>`;
    const started = performance.now();
    expect(classifiedText(prompt)).toBe(prompt);
    expect(performance.now() - started).toBeLessThan(100);
  });
  test("entities inside ORIGINAL are unescaped, and amp last", () => {
    const prompt = "<BUILD_PROMPT><ORIGINAL>a &lt;b&gt; &amp;lt; &quot;c&quot;</ORIGINAL></BUILD_PROMPT>";
    expect(classifiedText(prompt)).toBe(`a <b> &lt; "c"`);
  });
  test("a dotted capital I inside ORIGINAL does not shift the closing tag", () => {
    const original = "Deploy the \u0130stanbul app?";
    const prompt = `<RESEARCH_PROMPT><ORIGINAL>${original}</ORIGINAL></RESEARCH_PROMPT>`;
    const text = classifiedText(prompt);
    expect(text).toBe(original);
    expect(classifyPrompt(text)).toBe(false);
  });
  test("a runtime root with spaces or a quote is one shell word on the command line", () => {
    const root = "/tmp/my swarm/o'clock";
    const part = runtimePart(root);
    expect(part).toContain(`${RUNTIME_HEADING}`);
    expect(part).toContain(`Runtime root: ${root}`);
    expect(part).toContain("python3 '/tmp/my swarm/o'\"'\"'clock'/scripts/<script>.py");
    expect(part).not.toContain("python3 /tmp/my swarm/");
  });
  test("a plain prompt that quotes an ORIGINAL element is classified whole", () => {
    const prompt = "implement a parser for <ORIGINAL>what is a monad</ORIGINAL> tags";
    expect(parts(hook()({ prompt, systemPrompt: PRIOR }, top()))).toEqual([...PRIOR, SWARM_CONTEXT]);
  });
  test("SWARM_CHILD and an existing SWARM_CONTEXT part still silence an SDLC uplift", () => {
    const prompt = fallbackUplift(SDLC);
    expect(hook()({ prompt, systemPrompt: [...PRIOR, SWARM_CONTEXT] }, top())).toBeUndefined();
    process.env.SWARM_CHILD = "1";
    expect(hook()({ prompt, systemPrompt: PRIOR }, top())).toBeUndefined();
  });
});

describe("silence outside a fresh top-level session", () => {
  test("SWARM_CHILD=1", () => {
    process.env.SWARM_CHILD = "1";
    expect(hook()({ prompt: SDLC }, top())).toBeUndefined();
  });
  test("SWARM_AGENT set", () => {
    process.env.SWARM_AGENT = "a05-backend";
    expect(parts(hook()({ prompt: SDLC }, top()))).not.toContain(SWARM_CONTEXT);
  });
  test("session_init for a swarm agent", () => {
    expect(parts(hook()({ prompt: SDLC }, agentCtx("/x", "a05-backend")))).not.toContain(SWARM_CONTEXT);
  });
  test("session_init for a plain task subagent (unrestricted)", () => {
    expect(hook()({ prompt: SDLC }, agentCtx("/x", "task", [], false))).toBeUndefined();
  });
  test("session_init with no agent but restricted tools", () => {
    const ctx = fakeCtx("/x", [{ type: "session_init", restrictToolNames: true }]);
    expect(hook()({ prompt: SDLC }, ctx)).toBeUndefined();
  });
});

/**
 * A base prompt shaped like omp's repo context (G-04-05-2): two prior parts that both mention the hook's heading —
 * one as a heading of its own, one as a CLAUDE.md-style title line — without being the injected part.
 */
const REPO_LIKE_CONTEXT = [
  "# AGENTS.md\n\n## AgentSwarm\n\nThe swarm's own contract text lives here; it names /swarm and a01-orchestrator.\n",
  "# CLAUDE.md — AgentSwarm\n\nProject rules that mention the ## AgentSwarm heading in passing.\n",
];
const LIVE_POSITIVE = FIXTURE.positive[1];

test("repo-like base prompt: prior parts mentioning the heading do not count as the injected part", () => {
  const out = hook()({ prompt: LIVE_POSITIVE, systemPrompt: REPO_LIKE_CONTEXT }, top()) as { systemPrompt: string[] };
  expect(out.systemPrompt).toEqual([...REPO_LIKE_CONTEXT, SWARM_CONTEXT]);
});

test("neutral base prompt that merely mentions the heading still gets the context", () => {
  const prior = ["see ## AgentSwarm below"];
  const out = hook()({ prompt: LIVE_POSITIVE, systemPrompt: prior }, top()) as { systemPrompt: string[] };
  expect(out.systemPrompt).toEqual([...prior, SWARM_CONTEXT]);
});

test("idempotent: an existing SWARM_CONTEXT part is not duplicated", () => {
  const first = hook()({ prompt: SDLC, systemPrompt: PRIOR }, top()) as { systemPrompt: string[] };
  expect(hook()({ prompt: SDLC, systemPrompt: first.systemPrompt }, top())).toBeUndefined();
  expect(hook()({ prompt: SDLC, systemPrompt: [...REPO_LIKE_CONTEXT, SWARM_CONTEXT] }, top())).toBeUndefined();
});

test("idempotent: a prior part that only contains SWARM_CONTEXT as a substring is not the injected part", () => {
  const wrapped = [`preamble\n${SWARM_CONTEXT}\npostamble`];
  const out = hook()({ prompt: SDLC, systemPrompt: wrapped }, top()) as { systemPrompt: string[] };
  expect(out.systemPrompt).toEqual([...wrapped, SWARM_CONTEXT]);
});

test("fail-open: a throwing classifier leaves the prompt untouched", () => {
  const boom = () => {
    throw new Error("classifier exploded");
  };
  expect(swarmContext({ prompt: SDLC, systemPrompt: PRIOR }, top(), {}, boom)).toBeUndefined();
});

test("fail-open: the registered handler returns undefined when the hook module throws", () => {
  const broken: ExtensionContext = {
    cwd: "/x",
    sessionManager: {
      getEntries: () => {
        throw new Error("session log unreadable");
      },
      getBranch: () => [],
    },
  };
  expect(hook()({ prompt: SDLC }, broken)).toBeUndefined();
});

// ── OPEN-3 R1: the runtime part ──────────────────────────────────────────────

describe("runtime part", () => {
  const root = () => realpathSync(REPO_ROOT);

  test.each(SWARM_SLUGS.map((slug) => [slug]))("%s subagent gets the absolute runtime root, once, after the prior parts", (slug) => {
    for (const ctx of [agentCtx("/x", slug), agentCtx("/x", slug, [], false)]) {
      const out = parts(hook()({ prompt: SDLC, systemPrompt: PRIOR }, ctx));
      expect(out).toEqual([...PRIOR, runtimePart(root())]);
      expect(isAbsolute(root())).toBe(true);
      expect(out.at(-1)).toContain(`\nRuntime root: ${root()}\n`);
      expect(out.at(-1)).toContain(`python3 ${shellQuote(root())}/scripts/<script>.py … --root <repo> --json`);
    }
  });

  test("the root is swarmRoot() at handler time: env SWARM_ROOT wins", () => {
    const alt = tmpDir("swarm-omp-root-");
    mkdirSync(join(alt, "scripts"));
    writeFileSync(join(alt, "scripts", "orch_plan.py"), "");
    const handler = hook(); // registered before SWARM_ROOT is set: nothing was resolved at load
    process.env.SWARM_ROOT = alt;
    expect(parts(handler({ prompt: SDLC }, agentCtx("/x", "a05-backend")))).toEqual([runtimePart(alt)]);
  });

  test("the top-level main session never gets it", () => {
    expect(parts(hook()({ prompt: SDLC, systemPrompt: PRIOR }, top()))).toEqual([...PRIOR, SWARM_CONTEXT]);
    expect(hook()({ prompt: "what is this repo?", systemPrompt: PRIOR }, top())).toBeUndefined();
  });

  test.each([
    ["a plain task subagent", agentCtx("/x", "task", [], false)],
    ["a scout subagent (restricted)", agentCtx("/x", "scout")],
    ["a session_init with no agent", fakeCtx("/x", [{ type: "session_init", restrictToolNames: true }])],
  ])("%s does not get it", (_name, ctx) => {
    expect(hook()({ prompt: SDLC, systemPrompt: PRIOR }, ctx)).toBeUndefined();
  });

  test("env bleed: SWARM_AGENT does not make a non-swarm subagent a swarm session", () => {
    process.env.SWARM_AGENT = "a05-backend";
    expect(hook()({ prompt: SDLC }, agentCtx("/x", "task", [], false))).toBeUndefined();
  });

  test("headless: SWARM_AGENT=a05-backend with SWARM_CHILD=1 gets it (and no HOOK-01 context)", () => {
    process.env.SWARM_AGENT = "a05-backend";
    process.env.SWARM_CHILD = "1";
    expect(parts(hook()({ prompt: SDLC, systemPrompt: PRIOR }, top()))).toEqual([...PRIOR, runtimePart(root())]);
  });

  test("headless: a non-slug SWARM_AGENT does not get it", () => {
    process.env.SWARM_AGENT = "not-a-swarm-agent";
    process.env.SWARM_CHILD = "1";
    expect(hook()({ prompt: SDLC, systemPrompt: PRIOR }, top())).toBeUndefined();
  });

  test("idempotent: an existing runtime part is not duplicated", () => {
    const ctx = agentCtx("/x", "a05-backend");
    const first = parts(hook()({ prompt: SDLC, systemPrompt: PRIOR }, ctx));
    expect(hook()({ prompt: SDLC, systemPrompt: first }, ctx)).toBeUndefined();
    expect(hook()({ prompt: SDLC, systemPrompt: [...PRIOR, runtimePart("/elsewhere")] }, ctx)).toBeUndefined();
  });

  test("a prior part that merely mentions the heading does not count as the runtime part", () => {
    const prior = [`see ${RUNTIME_HEADING} below`];
    expect(parts(hook()({ prompt: SDLC, systemPrompt: prior }, agentCtx("/x", "a05-backend")))).toEqual([...prior, runtimePart(root())]);
  });

  test("fail-open: a throwing swarmRoot leaves the prompt untouched", () => {
    const boom = () => {
      throw new Error("agent-swarm scripts not found");
    };
    expect(runtimeContext({ prompt: SDLC, systemPrompt: PRIOR }, agentCtx("/x", "a05-backend"), boom)).toBeUndefined();
  });
});

test("no spawn: Bun.spawn and Bun.spawnSync were never called by any hook test above", () => {
  expect(spawn).toHaveBeenCalledTimes(0);
  expect(spawnSync).toHaveBeenCalledTimes(0);
});
