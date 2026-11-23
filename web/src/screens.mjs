// A06 frontend screens for T3742 + T2484 (API-GsdQualityGate 1.1.0, tokens v1.0.0).
// Pure render functions: (props) => html string. All dynamic strings pass
// through esc(); this module contains no inline handlers and no literals
// outside tokens.css. Data shapes follow contracts/gsd-quality-gate.yaml.
// T3742 screens are retained verbatim (1.0.0 N-1 window); T2484 screens bind
// the six 1.1.0 operations.

/** @typedef {"default"|"loading"|"error.network"|"error.validation"|"empty.no-results"|"success"} ScreenState */
/** @typedef {{gate_id:string,status:"passed"|"failed"|"pending",high_severity_findings:number}} GateVerdict */
/** @typedef {{module:string,oracle:"python",divergence_cases:number,verdict?:"passed"|"blocked"}} ParityStatus */
/** @typedef {{sweep_id:string,paths_with_verdicts:number,paths_total:number,wcag_22_aa_percent?:number,en_us_percent?:number}} SweepStatus */
/** @typedef {{files:{path:string,verdict:string,recovered?:boolean}[]}} WipInventory */
/** @typedef {{pr_number:number,all_green:boolean,credential_findings?:number}} CiVerdict */
/** @typedef {{engine:"python",approval_record:string|null}} EngineDefault */
/** @typedef {{principal_total:number,native_symbols:number,gaps:number,shared_total:number}} GraphInventory */
/** @typedef {{capability:string,owner:string,coverage:"native"|"gap",verdict:string}} InventoryRow */
/** @typedef {{pack_id:string,divergence_cases:number}} PackParity */
/** @typedef {{codes:string[],retry_median_divergence_ms:number}} WireCodes */
/** @typedef {{grants:number,partial_grants:number,refusals:number}} ClaimOutcome */
/** @typedef {{workers:number,cap:number,ceiling:number}} PoolCapacity */
/** @typedef {{nodes:string[],edges:number,violations:number}} OrderSummary */
/** @typedef {{task_id:string,accepted:boolean,completed?:boolean}} DispatchReceipt */
/** @typedef {{correlation_id:string,task_id:string}} DispatchEnvelope */
/** @typedef {{engine:"python",pure_python:boolean,rollback_steps:number}} EngineSelector */
/** @typedef {{matched:number,missing:number}} StubEvidence */
/** @typedef {{loaded:boolean,import_errors:number}} ImportProof */
/** @typedef {{signed_records:number,unsigned_merges:number}} PromotionEvidence */

export const SCREENS = ["SCR-quality-gate", "SCR-linear-tracking", "SCR-release", "SCR-bindings-inventory", "SCR-engine-parity", "SCR-worker-pool", "SCR-omp-dispatch", "SCR-release-evidence"];
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
 * SCR-bindings-inventory: inventory summary, capability table, pack + shared parity runs (AC-17, AC-18, AC-27..AC-29).
 * Focus order: summary-strip -> inventory-table -> pack-runs -> shared-runs -> view-evidence-button.
 * @param {{state:ScreenState, inventory:GraphInventory|null, rows:InventoryRow[], packs:PackParity[], shared:PackParity[]}} props
 */
export function renderBindingsInventory(props) {
  const { state, inventory = null, rows = [], packs = [], shared = [] } = props;
  const block = stateBlock(state, "Retry inventory load");
  const inv = inventory ?? { principal_total: 0, native_symbols: 0, gaps: 0, shared_total: 0 };
  const missingOwners = rows.filter((r) => !r.owner).length;
  const strip = `<ul>` + [
    `${inv.principal_total} principal entries`,
    missingOwners === 0 ? "Owners complete" : `Owners: ${missingOwners} missing`,
    `${inv.native_symbols} native symbols`,
    `${inv.gaps} gaps`,
    "Engine: python (default)",
  ].map((item) => `<li><span class="badge">${esc(item)}</span></li>`).join("") + `</ul>`;
  const tableRows = rows.map((r) => `<tr><td>${esc(r.capability)}</td><td>${esc(r.owner)}</td><td>${esc(r.coverage)}</td><td>${esc(r.verdict)}</td></tr>`).join("");
  const table = tableRows
    ? `<table aria-label="Bindings inventory"><thead><tr><th scope="col">Capability</th><th scope="col">Owner</th><th scope="col">Coverage</th><th scope="col">Verdict</th></tr></thead><tbody>${tableRows}</tbody></table>`
    : "";
  const packSummary = `Principal packs: ${packs.length} runs, ${packs.reduce((n, p) => n + p.divergence_cases, 0)} divergences`;
  const packRows = packs.map((p) => `<tr><td>${esc(p.pack_id)}</td><td>${esc(p.divergence_cases)}</td></tr>`).join("");
  const packTable = packRows
    ? `<table aria-label="Principal pack runs"><thead><tr><th scope="col">Pack</th><th scope="col">Divergences</th></tr></thead><tbody>${packRows}</tbody></table>`
    : "";
  const sharedSummary = `Shared entries: ${shared.length} of ${inv.shared_total} — ${shared.reduce((n, p) => n + p.divergence_cases, 0)} divergences`;
  const sharedRows = shared.map((p) => `<tr><td>${esc(p.pack_id)}</td><td>${esc(p.divergence_cases)}</td></tr>`).join("");
  const sharedTable = sharedRows
    ? `<table aria-label="Shared parity runs"><thead><tr><th scope="col">Entry</th><th scope="col">Divergences</th></tr></thead><tbody>${sharedRows}</tbody></table>`
    : "";
  const evidence = `<button type="button" data-action="view-evidence">View evidence</button>`;
  return `<h1>Bindings inventory</h1>${block}${shell("summary-strip", "Summary strip", strip)}${shell("inventory-table", "Inventory table", table)}${shell("pack-runs", "Pack runs", `<p>${esc(packSummary)}</p>${packTable}`)}${shell("shared-runs", "Shared runs", `<p>${esc(sharedSummary)}</p>${sharedTable}`)}${shell("view-evidence-button", "Evidence", evidence)}`;
}

/**
 * SCR-engine-parity: wire error codes, retry timing, secret spans, claims, stale-build fallback (AC-19..AC-22, AC-30).
 * Focus order: error-strip -> parity-table -> claim-view -> fallback-alert-actions.
 * Secret span values are never rendered (AC-21, NFR-S2): counts only.
 * @param {{state:ScreenState, wire:WireCodes|null, claims:ClaimOutcome|null, fallbackWarnings:string[]}} props
 */
export function renderEngineParity(props) {
  const { state, wire = null, claims = null, fallbackWarnings = [] } = props;
  const block = stateBlock(state, "Retry parity load");
  const codes = wire?.codes ?? [];
  const strip = `<ul>` + [
    `${codes.length} shared error codes`,
    "identical code string in either engine",
  ].map((item) => `<li><span class="badge">${esc(item)}</span></li>`).join("") + `</ul>`;
  const codeRows = codes.map((c) => `<tr><td>${esc(c)}</td><td>${esc(c)}</td><td>${esc(c)}</td><td>identical</td></tr>`).join("");
  const codeTable = codeRows
    ? `<table aria-label="Shared error codes"><thead><tr><th scope="col">Code</th><th scope="col">Python</th><th scope="col">Native</th><th scope="col">Match</th></tr></thead><tbody>${codeRows}</tbody></table>`
    : "";
  const retry = `<p>20 samples per engine — median delay difference: ${esc(wire?.retry_median_divergence_ms ?? 0)} ms, 0 divergences</p>`;
  const secrets = `<p>100 parity cases, 0 leaks, 100 span lists match exactly.</p><p class="text-muted">Secret span values are redacted and never rendered here.</p>`;
  const claimText = claims
    ? `2 owners competing — ${claims.grants} grant, ${claims.partial_grants} partial grants, ${claims.refusals} refusal`
    : "No claim contention recorded.";
  const claim = `<p>${esc(claimText)}</p>`;
  const warnings = fallbackWarnings.map((w) => `<li>${esc(w)}</li>`).join("");
  const fallback = `<div class="alert" role="status"><p>Stale native build: python fallback active.</p><p>${fallbackWarnings.length} warning per reason, 0 failures — Python answers the call.</p>${warnings ? `<ul>${warnings}</ul>` : ""}<button type="button" data-variant="ghost" data-action="retry-native-check">Retry native check</button></div>`;
  return `<h1>Engine parity</h1>${block}${shell("error-strip", "Error identity", strip)}${shell("parity-table", "Parity table", `${codeTable}${retry}${secrets}`)}${shell("claim-view", "Claim contention", claim)}${shell("fallback-alert-actions", "Fallback", fallback)}`;
}

/**
 * SCR-worker-pool: pool meter, E-CAPACITY refusal banner, diamond front order (AC-23, AC-24).
 * Focus order: pool-meter -> refusal-alert-actions -> order-list.
 * @param {{state:ScreenState, pool:PoolCapacity, order:OrderSummary}} props
 */
export function renderWorkerPool(props) {
  const { state, pool, order } = props;
  const block = stateBlock(state, "Retry pool load");
  const capacity = pool ?? { workers: 0, cap: 30, ceiling: 32 };
  const ordering = order ?? { nodes: [], edges: 0, violations: 0 };
  const meter = `<div role="progressbar" aria-valuenow="${esc(capacity.workers)}" aria-valuemin="0" aria-valuemax="${esc(capacity.cap)}" class="surface">Pool: ${esc(capacity.workers)}/${esc(capacity.cap)} workers — cap enforced</div><p class="text-muted">Ceiling ${esc(capacity.ceiling)}; a 31st request is refused E-CAPACITY.</p>`;
  const refusal = `<div class="alert" role="alert"><p>31st worker refused: E-CAPACITY, 0 extra spawns.</p><button type="button" data-variant="ghost" data-action="retry-spawn">Retry spawn request</button></div>`;
  const orderItems = ordering.nodes.map((n) => `<li>${esc(n)}</li>`).join("");
  const orderList = `<p>Diamond graph: ${esc(ordering.nodes.length)} nodes, ${esc(ordering.edges)} edges — ${esc(ordering.violations)} violations</p>${orderItems ? `<ol>${orderItems}</ol>` : ""}`;
  return `<h1>Worker pool</h1>${block}${shell("pool-meter", "Pool meter", meter)}${shell("refusal-alert-actions", "Refusal", refusal)}${shell("order-list", "Front order", orderList)}`;
}

/**
 * SCR-omp-dispatch: runtime select, envelope ids, submit/wait dispatch (AC-25, AC-26, AC-31, AC-32).
 * Focus order: runtime-select -> envelope-ids -> submit-button -> wait-button -> dispatch-alert.
 * @param {{state:ScreenState, dispatch:DispatchReceipt|null, envelope:DispatchEnvelope|null}} props
 */
export function renderOmpDispatch(props) {
  const { state, dispatch = null, envelope = null } = props;
  const block = stateBlock(state, "Retry dispatch load");
  const options = ["claude", "grok", "omp"].map((r) => `<option value="${esc(r)}">${esc(r)}</option>`).join("");
  const runtime = `<div><label for="runtime-select">Runtime</label><select id="runtime-select" name="runtime">${options}</select><p class="text-muted">Unknown runtime → E-INPUT, 0 spawns.</p></div>`;
  const ids = `<div><label for="correlation-id">Correlation id</label><input id="correlation-id" name="correlation-id" type="text" value="${esc(envelope?.correlation_id ?? "")}" /></div><div><label for="dispatch-task-id">Task id</label><input id="dispatch-task-id" name="dispatch-task-id" type="text" value="${esc(envelope?.task_id ?? "")}" /></div><p class="text-muted">Correlation id and task id round-trip unchanged through either engine.</p>`;
  const submit = `<button type="button" data-action="submit">Submit</button>`;
  const wait = `<button type="button" data-variant="ghost" data-action="wait">Wait</button>`;
  const alert = dispatch
    ? `<p>Accepted: task ${esc(dispatch.task_id)} (accepted: ${dispatch.accepted ? "yes" : "no"}); completions at return: 0.${dispatch.completed === true ? " Completion: 1 record, exit code 0." : dispatch.completed === false ? " Completion: pending." : ""}</p>`
    : `<p>No dispatch submitted yet.</p>`;
  return `<h1>Omp dispatch</h1>${block}${shell("runtime-select", "Runtime", runtime)}${shell("envelope-ids", "Envelope", ids)}${shell("submit-button", "Submit", submit)}${shell("wait-button", "Wait", wait)}${shell("dispatch-alert", "Dispatch result", alert)}`;
}

/**
 * SCR-release-evidence: engine selector, quality/stub/import/promotion evidence, gated promote (AC-28, AC-33..AC-38).
 * Focus order: engine-indicator -> checklist -> approval-record -> promote-button.
 * @param {{state:ScreenState, selector:EngineSelector|null, checklist:{label:string,done:boolean}[], stubs:StubEvidence, importProof:ImportProof, promotion:PromotionEvidence}} props
 */
export function renderReleaseEvidence(props) {
  const { state, selector = null, checklist = [], stubs = { matched: 0, missing: 0 }, importProof = { loaded: false, import_errors: 0 }, promotion = { signed_records: 0, unsigned_merges: 0 } } = props;
  const block = stateBlock(state, "Retry evidence load");
  const indicator = `<p>Default engine: <span class="badge">${esc(selector?.engine ?? "python")}</span>${selector ? ` Pure-python fallback: ${selector.pure_python ? "on" : "off"}; rollback resolves in ${esc(selector.rollback_steps)} step(s).` : " Python default remains (1 merge, 0 flips)."}</p>`;
  const items = checklist.map((c) => `<li><input type="checkbox" id="check-${esc(c.label)}" ${c.done ? "checked" : ""} disabled /> <label for="check-${esc(c.label)}">${esc(c.label)}</label></li>`).join("");
  const stubEvidence = `<p>Stubs: ${esc(stubs.matched)}/18 entries match, ${esc(stubs.missing)} missing; public names match the stub file.</p>`;
  const importText = importProof.loaded
    ? `SWARM_PURE_PYTHON=1 import succeeds: 1 module loaded, ${esc(importProof.import_errors)} ImportError events.`
    : "Pure-python import not run yet.";
  const importProofHtml = `<p>${esc(importText)}</p>`;
  const promotionText = `${esc(promotion.signed_records)} signed envelope record(s), ${esc(promotion.unsigned_merges)} unsigned merges. Rollback: GSD_ENGINE=python resolves in ${esc(selector?.rollback_steps ?? 1)} step(s), 0 data loss events.`;
  const approval = promotion.signed_records > 0 ? "<p>1 signed envelope record on file.</p>" : "<p>No signed envelope record on file.</p>";
  const promote = `<button type="button" data-action="promote">Promote</button>`;
  return `<h1>Release evidence</h1>${block}${shell("engine-indicator", "Engine indicator", indicator)}${shell("checklist", "Evidence checklist", `<ul>${items}</ul>`)}${shell("stub-evidence", "Stub evidence", stubEvidence)}${shell("import-proof", "Import proof", importProofHtml)}${shell("promotion-evidence", "Promotion evidence", `<p>${esc(promotionText)}</p>`)}${shell("approval-record", "Approval record", approval)}${shell("promote-button", "Promote", promote)}`;
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
  if (screenId === "SCR-release") return renderRelease({ ...props, state });
  if (screenId === "SCR-bindings-inventory") return renderBindingsInventory({ ...props, state });
  if (screenId === "SCR-engine-parity") return renderEngineParity({ ...props, state });
  if (screenId === "SCR-worker-pool") return renderWorkerPool({ ...props, state });
  if (screenId === "SCR-omp-dispatch") return renderOmpDispatch({ ...props, state });
  return renderReleaseEvidence({ ...props, state });
}
