/** SWARM_DISABLED=1 (operator kill switch): the extension registers nothing, so a stuck swarm never blocks the session. */
import { afterEach, beforeEach, expect, test } from "bun:test";
import { createSwarmExtension } from "../src/index.ts";
import { fakePi, isolateEnv } from "./helpers.ts";
import type { Bridge } from "../src/bridge.ts";

isolateEnv("SWARM_DISABLED", "SWARM_AGENT", "SWARM_TASK_ID");
const unreachable: Bridge = async () => {
  throw new Error("no bridge call expected");
};

beforeEach(() => {
  delete process.env.SWARM_DISABLED;
});
afterEach(() => {
  delete process.env.SWARM_DISABLED;
});

test("SWARM_DISABLED=1 registers no tools, no /swarm command and no hooks", () => {
  process.env.SWARM_DISABLED = "1";
  const pi = fakePi();
  createSwarmExtension({ bridge: unreachable })(pi.api);
  expect(pi.tools).toEqual([]);
  expect(pi.commands.size).toBe(0);
  expect(pi.handlers.size).toBe(0);
});

test("without the switch the extension still registers its tools and guard", () => {
  const pi = fakePi();
  createSwarmExtension({ bridge: unreachable })(pi.api);
  expect(pi.tools.length).toBeGreaterThan(0);
  expect(pi.handlers.has("tool_call")).toBe(true);
});
