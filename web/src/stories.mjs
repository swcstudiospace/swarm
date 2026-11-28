// Component catalog for T3742 screens plus T2484 and T74c2 Graph Engineering
// tranches (Storybook-equivalent entry per component state; Storybook is not
// an approved dependency per ADR-0005, so this catalog is the consumable surface for A08).
export const COMPONENT_CATALOG = [
  { id: "SCR-quality-gate", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/quality-gate.html", props: ["state", "gates", "parity", "sweep", "ci"] },
  { id: "SCR-linear-tracking", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/linear-tracking.html", props: ["state", "phases"] },
  { id: "SCR-release", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/release.html", props: ["state", "engine", "checklist", "ci"] },
  { id: "SCR-bindings-inventory", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/bindings-inventory.html", props: ["state", "inventory", "rows", "packs", "shared"] },
  { id: "SCR-engine-parity", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/engine-parity.html", props: ["state", "wire", "claims", "fallbackWarnings"] },
  { id: "SCR-worker-pool", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/worker-pool.html", props: ["state", "pool", "order"] },
  { id: "SCR-omp-dispatch", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/omp-dispatch.html", props: ["state", "dispatch", "envelope"] },
  { id: "SCR-release-evidence", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/release-evidence.html", props: ["state", "selector", "checklist", "stubs", "importProof", "promotion"] },
  { id: "SCR-graph-decomposition", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/graph-decomposition.html", props: ["state", "validation", "subtasks", "readyOrder"] },
  { id: "SCR-capability-inventory", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/capability-inventory.html", props: ["state", "bindings", "rows", "agentRows"] },
  { id: "SCR-parity-verification", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/parity-verification.html", props: ["state", "coverage", "rows"] },
  { id: "SCR-orchestration-trace", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/orchestration-trace.html", props: ["state", "receipt", "records"] },
  { id: "SCR-upgrade-quality", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/upgrade-quality.html", props: ["state", "quality", "checklist"] },
];
