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
