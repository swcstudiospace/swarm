// Component catalog for T3742 screens (Storybook-equivalent entry per
// component state; Storybook is not an approved dependency per ADR-0005,
// so this catalog is the consumable surface for A08).
export const COMPONENT_CATALOG = [
  { id: "SCR-quality-gate", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/quality-gate.html", props: ["state", "gates", "parity", "sweep", "ci"] },
  { id: "SCR-linear-tracking", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/linear-tracking.html", props: ["state", "phases"] },
  { id: "SCR-release", states: ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"], page: "web/screens/release.html", props: ["state", "engine", "checklist", "ci"] },
];
