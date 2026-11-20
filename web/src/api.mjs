// Typed client for API-GsdQualityGate 1.0.0 (contracts/gsd-quality-gate.yaml).
// Every method maps 1:1 to a contract operationId: getQualityGate,
// getParityStatus, getSweepStatus, getWipInventory, getCiVerdict,
// getEngineDefault. Response shape validation fails closed (E-CONTRACT).

const OPS = {
  getQualityGate: { method: "GET", path: (id) => `/quality/gates/${encodeURIComponent(id)}`, required: ["gate_id", "status", "high_severity_findings"] },
  getParityStatus: { method: "GET", path: (id) => `/quality/parity/${encodeURIComponent(id)}`, required: ["module", "oracle", "divergence_cases"] },
  getSweepStatus: { method: "GET", path: (id) => `/quality/sweep/${encodeURIComponent(id)}`, required: ["sweep_id", "paths_with_verdicts", "paths_total"] },
  getWipInventory: { method: "GET", path: () => "/quality/wip/inventory", required: ["files"] },
  getCiVerdict: { method: "GET", path: (id) => `/quality/ci/${encodeURIComponent(id)}`, required: ["pr_number", "all_green"] },
  getEngineDefault: { method: "GET", path: () => "/quality/engine", required: ["engine"] },
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
    const res = await fetchImpl(`${baseUrl}${def.path(arg)}`, { method: def.method });
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
  };
}

export const OPERATIONS = Object.keys(OPS);
