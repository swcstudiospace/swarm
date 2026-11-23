// Typed client for API-GsdQualityGate 1.2.0 (contracts/gsd-quality-gate.yaml).
// Every method maps 1:1 to a contract operationId: getQualityGate,
// getParityStatus, getSweepStatus, getWipInventory, getCiVerdict,
// getEngineDefault (1.0.0, retained for the 30-day N-1 window) plus
// getGraphInventory, getWireCodes, submitDispatch, waitDispatch,
// getPackParity, getEngineSelector (1.1.0 Graph Engineering surface) plus
// validateGraph, computeReadyOrder, getBindings, getParityCoverage,
// submitOrchestrate, getQualityCrates (1.2.0 graph-engine specialization).
// Response shape validation fails closed (E-CONTRACT).

const OPS = {
  getQualityGate: { method: "GET", path: (id) => `/quality/gates/${encodeURIComponent(id)}`, required: ["gate_id", "status", "high_severity_findings"] },
  getParityStatus: { method: "GET", path: (id) => `/quality/parity/${encodeURIComponent(id)}`, required: ["module", "oracle", "divergence_cases"] },
  getSweepStatus: { method: "GET", path: (id) => `/quality/sweep/${encodeURIComponent(id)}`, required: ["sweep_id", "paths_with_verdicts", "paths_total"] },
  getWipInventory: { method: "GET", path: () => "/quality/wip/inventory", required: ["files"] },
  getCiVerdict: { method: "GET", path: (id) => `/quality/ci/${encodeURIComponent(id)}`, required: ["pr_number", "all_green"] },
  getEngineDefault: { method: "GET", path: () => "/quality/engine", required: ["engine"] },
  getGraphInventory: { method: "GET", path: () => "/graph/inventory", required: ["principal_total", "native_symbols", "gaps", "shared_total"] },
  getWireCodes: { method: "GET", path: () => "/graph/codes", required: ["codes", "retry_median_divergence_ms"] },
  submitDispatch: { method: "POST", path: () => "/graph/dispatch", required: ["task_id", "accepted"] },
  waitDispatch: { method: "GET", path: (id) => `/graph/dispatch/${encodeURIComponent(id)}`, required: ["task_id", "accepted"] },
  getPackParity: { method: "GET", path: (id) => `/graph/packs/${encodeURIComponent(id)}/parity`, required: ["pack_id", "divergence_cases"] },
  getEngineSelector: { method: "GET", path: () => "/graph/engine/selector", required: ["engine", "pure_python", "rollback_steps"] },
  validateGraph: { method: "GET", path: () => "/graph/validation", required: ["subtasks", "predecessor_links", "cycles", "dangling_links"] },
  computeReadyOrder: { method: "POST", path: () => "/graph/order", required: ["nodes", "start_node", "violations"] },
  getBindings: { method: "GET", path: () => "/graph/bindings", required: ["principal_total", "native_symbols", "gaps", "agents", "uncounted", "missing_agents"] },
  getParityCoverage: { method: "GET", path: () => "/graph/parity/coverage", required: ["entries", "divergence_cases", "retry_samples", "retry_median_divergence_ms", "secret_leaks"] },
  submitOrchestrate: { method: "POST", path: () => "/graph/orchestrate", required: ["correlation_id", "task_id", "record_count"] },
  getQualityCrates: { method: "GET", path: () => "/graph/quality/crates", required: ["crates", "findings", "exit_code"] },
};

export class ContractError extends Error {
  constructor(message) {
    super(message);
    this.code = "E-CONTRACT";
  }
}

/**
 * @param {{baseUrl:string, fetchImpl?:typeof fetch}} options
 */
export function createQualityGateClient({ baseUrl, fetchImpl = fetch }) {
  if (!baseUrl) throw new ContractError("baseUrl is required");
  async function call(op, arg) {
    const def = OPS[op];
    if (!def) throw new ContractError(`unknown operation: ${op}`);
    const init = { method: def.method };
    if (def.method === "POST") {
      init.headers = { "content-type": "application/json" };
      init.body = JSON.stringify(arg ?? {});
    }
    const res = await fetchImpl(`${baseUrl}${def.path(arg)}`, init);
    if (!res.ok) throw new ContractError(`${op} failed: HTTP ${res.status}`);
    const body = await res.json();
    for (const field of def.required) {
      if (!(field in body)) throw new ContractError(`${op} response missing required field: ${field}`);
    }
    return body;
  }
  return {
    getQualityGate: (gateId) => call("getQualityGate", gateId),
    getParityStatus: (module) => call("getParityStatus", module),
    getSweepStatus: (sweepId) => call("getSweepStatus", sweepId),
    getWipInventory: () => call("getWipInventory"),
    getCiVerdict: (prNumber) => call("getCiVerdict", prNumber),
    getEngineDefault: () => call("getEngineDefault"),
    getGraphInventory: () => call("getGraphInventory"),
    getWireCodes: () => call("getWireCodes"),
    submitDispatch: (payload) => call("submitDispatch", payload),
    waitDispatch: (taskId) => call("waitDispatch", taskId),
    getPackParity: (packId) => call("getPackParity", packId),
    getEngineSelector: () => call("getEngineSelector"),
    validateGraph: () => call("validateGraph"),
    computeReadyOrder: (payload) => call("computeReadyOrder", payload),
    getBindings: () => call("getBindings"),
    getParityCoverage: () => call("getParityCoverage"),
    submitOrchestrate: (payload) => call("submitOrchestrate", payload),
    getQualityCrates: () => call("getQualityCrates"),
  };
}

export const OPERATIONS = Object.keys(OPS);
