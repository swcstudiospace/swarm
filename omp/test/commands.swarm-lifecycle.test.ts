/**
 * `/swarm` signing-key lifecycle startup (OSA-04): the lifecycle child must be bounded, so a python3 that hangs
 * cannot block the omp event loop. A python3 shim first on PATH is the lifecycle child's python3, and the handler
 * must return without waiting for it; the PATH change is restored once the handler returns.
 */
import { existsSync, writeFileSync } from "node:fs";
import { delimiter, join } from "node:path";
import { expect, test } from "bun:test";
import { type Bridge, SwarmToolError } from "../src/bridge.ts";
import { swarmCommand } from "../src/commands.ts";
import { commandCtx, fakePi, isolateEnv, tmpDir } from "./helpers.ts";

isolateEnv("SWARM_ROOT", "SWARM_DIR", "SWARM_CORRELATION_ID", "SWARM_TASK_ID");

const BRIEF = "add a /health endpoint";

test("lifecycle: a python3 on PATH that sleeps 30 s does not hold /swarm past 5 s (OSA-04)", async () => {
  const cwd = tmpDir("swarm-omp-cwd-");
  const shimDir = tmpDir("swarm-omp-py-shim-");
  const marker = join(shimDir, "ran");
  // the shim records that it ran, then sleeps 30 s in place of python3
  writeFileSync(join(shimDir, "python3"), `#!/bin/sh\necho ran > "${marker}"\nexec sleep 30\n`, { mode: 0o755 });

  // commands.ts imports spawnSync by name, so a spy on the default export never reaches its spawn; the shim is put
  // first on PATH, which the lifecycle child inherits through process.env, and PATH is restored after the call
  const savedPath = process.env.PATH;
  process.env.PATH = `${shimDir}${delimiter}${savedPath ?? ""}`;

  // planning is stubbed out: the handler returns after the lifecycle step with the bridge's refusal
  const bridge: Bridge = async () => {
    throw new SwarmToolError("E-DEP", "planning stubbed out for this test");
  };
  const swarm = swarmCommand(fakePi().api, bridge, {});
  const ctx = commandCtx(cwd);

  const t0 = Date.now();
  try {
    await swarm.handler(BRIEF, ctx);
  } finally {
    if (savedPath === undefined) delete process.env.PATH;
    else process.env.PATH = savedPath;
  }
  const elapsed = Date.now() - t0;

  expect(elapsed).toBeLessThan(5_000);
  expect(existsSync(marker)).toBe(true); // precondition: the shim, not a real python3, was the lifecycle child
}, 45_000);
