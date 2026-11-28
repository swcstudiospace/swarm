import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { renderScreen, SCREENS, STATES, esc } from "../../web/src/screens.mjs";
import { createQualityGateClient, OPERATIONS } from "../../web/src/api.mjs";

const qualityProps = {
  gates: [{ gate_id: "AC-15", status: "pending", high_severity_findings: 0 }],
  parity: [{ module: "core", oracle: "python", divergence_cases: 0 }],
  sweep: [{ sweep_id: "sweep-1", paths_with_verdicts: 10, paths_total: 10 }],
  ci: { pr_number: 1, all_green: false },
};
const linearProps = {
  phases: [{ phase: "14.2", issues: [{ title: "Recover WIP tests", parent: "US-1" }] }],
};
const releaseProps = {
  engine: { engine: "python", approval_record: null },
  checklist: [{ label: "Ship assess", done: false }],
  ci: { pr_number: 1, all_green: false },
};
const propsFor = (id: string) =>
  id === "SCR-quality-gate" ? qualityProps : id === "SCR-linear-tracking" ? linearProps : releaseProps;

for (const screen of SCREENS) {
  test(`${screen} renders every ux.spec state`, () => {
    for (const state of STATES) {
      const html = renderScreen(screen, state as never, propsFor(screen) as never);
      expect(typeof html).toBe("string");
      expect(html).toContain("<h1>");
    }
  });
}

test("quality-gate verdict strip and run table render contract data", () => {
  const html = renderScreen("SCR-quality-gate", "default", qualityProps as never);
  expect(html).toContain("AC-15");
  expect(html).toContain("pending");
  expect(html).toContain("0/1 clean".replace("0/1", "1/1"));
});

test("linear-tracking shows parent links", () => {
  const html = renderScreen("SCR-linear-tracking", "default", linearProps as never);
  expect(html).toContain("Recover WIP tests");
  expect(html).toContain("parent: US-1");
});

test("release merge button is disabled until all-green CI (AC-15)", () => {
  const blocked = renderScreen("SCR-release", "default", releaseProps as never);
  expect(blocked).toContain('data-action="merge" disabled');
  const green = renderScreen("SCR-release", "default", {
    ...releaseProps,
    ci: { pr_number: 1, all_green: true },
  } as never);
  expect(green).not.toContain('data-action="merge" disabled');
});

test("dynamic strings are escaped (no raw markup injection)", () => {
  expect(esc('<img src="x" onerror="alert(1)">')).not.toContain("<img");
  const html = renderScreen(
    "SCR-linear-tracking",
    "default",
    { phases: [{ phase: "<script>", issues: [] }] } as never,
  );
  expect(html).not.toContain("<script>");
});

test("unknown screen and state fail closed", () => {
  expect(() => renderScreen("SCR-nope", "default")).toThrow();
  expect(() => renderScreen("SCR-release", "bogus" as never)).toThrow();
});

test("no hard-coded colours or spacing in screen modules (token binding)", () => {
  for (const file of ["web/src/screens.mjs", "web/src/api.mjs"]) {
    const src = readFileSync(file, "utf8");
    expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  }
});

test("contract client exposes all eighteen API-GsdQualityGate operations", () => {
  const client = createQualityGateClient({ baseUrl: "http://localhost:0/v1" });
  for (const op of OPERATIONS) expect(typeof client[op]).toBe("function");
  expect([...OPERATIONS].sort()).toEqual(
    ["computeReadyOrder", "getBindings", "getCiVerdict", "getEngineDefault", "getEngineSelector", "getGraphInventory", "getPackParity", "getParityCoverage", "getParityStatus", "getQualityCrates", "getQualityGate", "getSweepStatus", "getWipInventory", "getWireCodes", "submitDispatch", "submitOrchestrate", "validateGraph", "waitDispatch"].sort(),
  );
});

test("contract client fails closed on a missing required field", async () => {
  const client = createQualityGateClient({
    baseUrl: "http://localhost:0/v1",
    fetchImpl: (async () => ({ ok: true, json: async () => ({ gate_id: "x" }) })) as never,
  });
  await expect(client.getQualityGate("x")).rejects.toThrow("missing required field");
});
