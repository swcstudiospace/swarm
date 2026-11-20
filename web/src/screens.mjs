// A06 frontend screens for T3742 (API-GsdQualityGate 1.0.0, tokens v1.0.0).
// Pure render functions: (props) => html string. All dynamic strings pass
// through esc(); this module contains no inline handlers and no literals
// outside tokens.css. Data shapes follow contracts/gsd-quality-gate.yaml.

/** @typedef {"default"|"loading"|"error.network"|"error.validation"|"empty.no-results"|"success"} ScreenState */
/** @typedef {{gate_id:string,status:"passed"|"failed"|"pending",high_severity_findings:number}} GateVerdict */
/** @typedef {{module:string,oracle:"python",divergence_cases:number,verdict?:"passed"|"blocked"}} ParityStatus */
/** @typedef {{sweep_id:string,paths_with_verdicts:number,paths_total:number,wcag_22_aa_percent?:number,en_us_percent?:number}} SweepStatus */
/** @typedef {{files:{path:string,verdict:string,recovered?:boolean}[]}} WipInventory */
/** @typedef {{pr_number:number,all_green:boolean,credential_findings?:number}} CiVerdict */
/** @typedef {{engine:"python",approval_record:string|null}} EngineDefault */

export const SCREENS = ["SCR-quality-gate", "SCR-linear-tracking", "SCR-release"];
export const STATES = ["default", "loading", "error.network", "error.validation", "empty.no-results", "success"];

/** Escape untrusted text for HTML text/attribute contexts. */
export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function shell(title, heading, body, extra = "") {
  return `<section aria-labelledby="${esc(title)}-h"><h2 id="${esc(title)}-h">${esc(heading)}</h2>${body}${extra}</section>`;
}

function stateBlock(state, retryLabel) {
  if (state === "loading") {
    return `<p role="status">Loading…</p><div aria-hidden="true"><p class="surface">Loading row</p><p class="surface">Loading row</p></div>`;
  }
  if (state === "error.network") {
    return `<div class="alert" role="alert"><p>Network error. Data may be stale.</p><button type="button" data-action="retry">${esc(retryLabel)}</button></div>`;
  }
  if (state === "error.validation") {
    return `<div class="alert" role="alert"><p>Validation error. Check the highlighted fields.</p></div>`;
  }
  if (state === "empty.no-results") {
    return `<p>No results yet.</p>`;
  }
  if (state === "success") {
    return `<p class="text-success" role="status">All green.</p>`;
  }
  return "";
}

/**
 * SCR-quality-gate: verdict strip + run table + alert (AC-5..AC-8, AC-11, AC-12, AC-15, AC-16).
 * Focus order: verdict-strip -> run-table -> alert-actions -> primary-cta.
 * @param {{state:ScreenState, gates:GateVerdict[], parity:ParityStatus[], sweep:SweepStatus[], ci:CiVerdict|null}} props
 */
export function renderQualityGate(props) {
  const { state, gates = [], parity = [], sweep = [], ci = null } = props;
  const block = stateBlock(state, "Retry gate run");
  const strip = `<ul>` + [
    `Parity gates: ${parity.filter((p) => p.divergence_cases === 0).length}/${parity.length} clean`,
    `Workspace suite: ${gates.length} gates tracked`,
    `Sweep findings: ${sweep.reduce((n, s) => n + (s.paths_total - s.paths_with_verdicts), 0)} paths without verdicts`,
    `CI gate: ${ci ? (ci.all_green ? "all green" : "not green") : "unknown"}`,
  ].map((item) => `<li><span class="badge">${esc(item)}</span></li>`).join("") + `</ul>`;
  const rows = gates.map((g) => `<tr><td>${esc(g.gate_id)}</td><td>${esc(g.status)}</td><td>${esc(g.high_severity_findings)}</td></tr>`).join("");
  const table = rows
    ? `<table aria-label="Quality gate runs"><thead><tr><th scope="col">Gate</th><th scope="col">Verdict</th><th scope="col">High severity findings</th></tr></thead><tbody>${rows}</tbody></table>`
    : "";
  const alertActions = `<div><button type="button" data-action="view-findings">View findings</button> <button type="button" data-variant="ghost" data-action="open-ci">Open CI gate</button></div>`;
  const cta = `<button type="button" data-action="primary-cta">Run quality gate</button>`;
  return `<h1>Quality gate</h1>${block}${shell("verdict-strip", "Verdict strip", strip)}${shell("run-table", "Run table", table)}${shell("alert-actions", "Alert actions", alertActions)}${shell("primary-cta", "Primary action", cta)}`;
}

/**
 * SCR-linear-tracking: phase -> issue -> sub-issue tree, parent links (AC-1..AC-4).
 * Focus order: phase-list -> issue-tree -> parent-link -> actions.
 * @param {{state:ScreenState, phases:{phase:string, issues:{title:string, parent:string|null}[]}[]}} props
 */
export function renderLinearTracking(props) {
  const { state, phases = [] } = props;
  const block = stateBlock(state, "Retry tracking load");
  const list = `<ul>` + phases.map((p) => `<li>${esc(p.phase)} — ${esc(p.issues.length)} issues</li>`).join("") + `</ul>`;
  const tree = phases.length
    ? `<ul>` + phases.map((p) => `<li><strong>${esc(p.phase)}</strong><ul>` + p.issues.map((i) => `<li>${esc(i.title)}${i.parent ? ` (parent: ${esc(i.parent)})` : ""}</li>`).join("") + `</ul></li>`).join("") + `</ul>`
    : "";
  const parentLink = `<div><label for="parent-link-input">Parent issue link</label><input id="parent-link-input" name="parent-link" type="text" /></div>`;
  const actions = `<button type="button" data-variant="ghost" data-action="check-duplicates">Check duplicates</button> <button type="button" data-action="sync-linear">Sync Linear</button>`;
  return `<h1>Linear tracking</h1>${block}${shell("phase-list", "Phase list", list)}${shell("issue-tree", "Issue tree", tree)}${shell("parent-link", "Parent link", parentLink)}${shell("actions", "Actions", actions)}`;
}

/**
 * SCR-release: engine indicator + evidence checklist + gated merge (AC-6, AC-9, AC-10, AC-13..AC-16).
 * Focus order: engine-indicator -> checklist -> approval-record -> merge-button.
 * @param {{state:ScreenState, engine:EngineDefault|null, checklist:{label:string,done:boolean}[], ci:CiVerdict|null}} props
 */
export function renderRelease(props) {
  const { state, engine = null, checklist = [], ci = null } = props;
  const block = stateBlock(state, "Retry release load");
  const indicator = engine ? `<p>Default engine: <span class="badge">${esc(engine.engine)}</span>${engine.approval_record ? ` (approval: ${esc(engine.approval_record)})` : ""}</p>` : "";
  const items = checklist.map((c) => `<li><input type="checkbox" id="check-${esc(c.label)}" ${c.done ? "checked" : ""} disabled /> <label for="check-${esc(c.label)}">${esc(c.label)}</label></li>`).join("");
  const approval = engine?.approval_record ? `<p>Approval record: ${esc(engine.approval_record)}</p>` : `<p>No approval record on file.</p>`;
  const mergeEnabled = ci?.all_green === true;
  const merge = `<button type="button" data-action="merge" ${mergeEnabled ? "" : "disabled"}>Merge to main</button><p class="text-muted">${mergeEnabled ? "All-green CI confirmed." : "Merge is disabled until all required CI jobs are green (AC-15)."}</p>`;
  return `<h1>Release</h1>${block}${shell("engine-indicator", "Engine indicator", indicator)}${shell("checklist", "Evidence checklist", `<ul>${items}</ul>`)}${shell("approval-record", "Approval record", approval)}${shell("merge-button", "Merge", merge)}`;
}

/**
 * Render any bound screen. Unknown ids throw (fail closed).
 * @param {string} screenId
 * @param {ScreenState} state
 * @param {object} props
 */
export function renderScreen(screenId, state, props = {}) {
  if (!SCREENS.includes(screenId)) throw new Error(`unknown screen: ${screenId}`);
  if (!STATES.includes(state)) throw new Error(`unknown state: ${state}`);
  if (screenId === "SCR-quality-gate") return renderQualityGate({ ...props, state });
  if (screenId === "SCR-linear-tracking") return renderLinearTracking({ ...props, state });
  return renderRelease({ ...props, state });
}
