# A06 impl notes — T3742-fe (correlation 37429cc8-2d34-4941-80fa-05f909fa9eda)

Bind: ux.spec docs/design/ux-spec.md (LOFI, PROVISIONAL), tokens
design/tokens.json v1.0.0, contract API-GsdQualityGate 1.0.0
(contracts/gsd-quality-gate.yaml, sha256:0c9a84...).
Toolchain: python + js both present; check plan ruff,mypy,pytest,eslint,tsc,
npm test,cargo clippy,cargo test.

Screens (all six ux.spec states each: default, loading, error.network,
error.validation, empty.no-results, success):
- SCR-quality-gate: verdict strip, run table, alert actions, primary CTA.
- SCR-linear-tracking: phase list, issue tree with parent links, actions.
- SCR-release: engine indicator (Python default), evidence checklist,
  approval record, merge button disabled until all-green CI (AC-15).

Styling: tokens.css is the only file with colour/spacing literals; all
components bind var() tokens. No new dependency (ADR-0005): framework-free
render functions in web/src/screens.mjs; Storybook catalog in
web/src/stories.mjs (Storybook is not an approved dependency).

Contract client: web/src/api.mjs binds all six operations of
API-GsdQualityGate 1.0.0 and fails closed (E-CONTRACT) on missing fields.

Security: no innerHTML with untrusted data (esc() everywhere), no inline
handlers, no external assets, no storage of PII.

Perf: 16,954 bytes total (9.7% of 170 KB budget), largest chunk screens.mjs.
No Lighthouse run available in this static delivery; CWV UNVERIFIED.

Deviations: none from ux.spec states or tokens. Storybook actual package not
installed (no-approved-deps policy); catalog JSON is the A08 surface.

---

# A06 impl notes — T2484-fe (correlation 24844b3d-8121-45d5-8189-71e5302ff6c9)

Bind: ux.spec docs/design/ux-spec-t2484.md (LOFI, PROVISIONAL),
tokens design/tokens.json v1.0.0 (unchanged), contract
API-GsdQualityGate 1.1.0 (contracts/gsd-quality-gate.yaml, 12 operations)
plus architecture blueprint v2.5 and ADR-0006/ADR-0007.
Toolchain: python + js both present; check plan
ruff,mypy,pytest,eslint,tsc,npm test,cargo clippy,cargo test.

Screens (all six ux.spec states each: default, loading, error.network,
error.validation, empty.no-results, success; state parity tool confirms
exact parity on all five):
- SCR-bindings-inventory: 394/18/376 summary strip, capability table,
  principal pack runs, shared entries, Python-default engine indicator,
  view-evidence CTA (getGraphInventory, getPackParity).
- SCR-engine-parity: 7 wire codes, retry medians (0 ms), secret-span
  counts only — values are never rendered (AC-21, NFR-S2), claim
  contention (1 grant / 1 refusal), stale-build fallback banner
  (getWireCodes).
- SCR-worker-pool: 30/30 pool meter (role progressbar), E-CAPACITY
  refusal banner, diamond front order (AC-23, AC-24).
- SCR-omp-dispatch: runtime select limited to claude/grok/omp
  (unknown → E-INPUT), labelled correlation/task ids, submit (acceptance,
  0 completions at return) + wait (1 completion, exit 0) buttons
  (submitDispatch, waitDispatch; AC-25, AC-26, AC-31, AC-32).
- SCR-release-evidence: Python-default selector (pure-python on, rollback
  1 step), quality checklist, 18/18 stub evidence, SWARM_PURE_PYTHON=1
  import proof, signed-envelope promotion evidence, Promote CTA
  (getEngineSelector; AC-28, AC-33..AC-38).

Contract client: web/src/api.mjs binds all 12 operations of
API-GsdQualityGate 1.1.0 (6 retained 1.0.0 under the 30-day N-1 window +
6 added 1.1.0); POST support for submitDispatch; fails closed
(E-CONTRACT) on missing required fields.

Styling: tokens.css unchanged (var() only); screens.mjs carries no
colour/spacing literals (asserted in tests). No new dependency
(ADR-0005): framework-free render functions + static pages generated
from the renderers; Storybook-equivalent catalog in web/src/stories.mjs.
Component tests: web/graph-engineering.test.mjs (19 tests: contract
binding, per-screen states/content, redaction, injection, token binding).

Security: esc() on every interpolation; secret spans never rendered; no
inline handlers (client-security scan: 0 findings); no external assets;
no storage of PII.

Perf: 36,150 bytes total shipped surface (screens+api+stories+tokens+
new pages), 20.77% of the 170 KB budget (fe_bundle_budget_check:
budget_ok true, largest chunk screens.mjs). Growth over T3742's 16,954
bytes is the five new required screens. No Lighthouse run available in
this static delivery; CWV UNVERIFIED.

Deviations: none from ux.spec states or tokens. Branch/PR: changes left
in the shared wave worktree for A01 to collect; no branch switch or push
performed to avoid disturbing concurrent wave artifacts.
