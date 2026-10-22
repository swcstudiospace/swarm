# Parity corpus backlog (A1)

Read-only cross-reference of the lane scripts against the test tree (n5 §4; n6 input 5). Scripts were read, never
executed.

## Method

- Scope: every top-level `scripts/<lane>_<verb>.py`. Underscore-prefixed internal helpers are excluded:
  `_install_cursor.py`, `_install_grokbot.py`, `_install_omp.py`, `_install_substrate.py`, `_registry_intake.py`,
  `_uplift_prompts.py`, `_write_skills.py`. `scripts/ts/` is excluded (TypeScript).
- Stem: the file name without `.py`. A lane counts as **covered** when at least one file under `tests/` (any type,
  including `tests/ts`, `tests/fixtures`, `tests/perf`, `tests/omp_contract`) contains the literal stem or has it in its
  file name. Mention of a stem is the criterion; it does not measure how deeply a test exercises the script.
- Forty-two lane scripts in scope. Covered: 41. **Uncovered: 1** (listed below).

## Coverage table

| script | covering test files | status |
|---|---|---|
| orch_plan | tests/omp_contract/freeze.md, tests/omp_contract/vectors.json, tests/test_agent_token.py, tests/test_autonomous_run.py, tests/test_blast_radius.py, tests/test_controller_bootstrap.py, tests/test_roster_routing.py, tests/test_runner_gates.py, tests/test_verdicts.py | covered |
| gitops_repo_check | tests/test_scripts_a22.py | covered |
| sol_contract_lint | tests/test_scripts_a26.py | covered |
| orch_from_graph | tests/test_orch_from_graph.py, tests/test_rev_greptile_ingest.py, tests/test_roster_routing.py, tests/test_tools_a01_existing.py | covered |
| event_schema_check | tests/test_scripts_a19.py | covered |
| k8s_manifest_check | tests/test_scripts_a21.py | covered |
| cloud_readiness_scan | tests/test_scripts_a16.py | covered |
| dapp_wallet_check | tests/test_scripts_a29.py | covered |
| swarm_run | tests/fixtures/greptile/threads.json, tests/fixtures/greptile/threads_list.json, tests/perf/baseline.md, tests/perf/run.sh, tests/test_agent_token.py, tests/test_autonomous_run.py, tests/test_blast_radius.py, tests/test_child_guard.py, tests/test_render_rules.py, tests/test_replan.py, tests/test_results.py, tests/test_rev_greptile_ingest.py, tests/test_runner_gates.py, tests/test_swarm_run_key_preflight.py, tests/test_tool_registry.py, tests/test_tools_a01_existing.py, tests/test_ts_scripts.py, tests/test_verdicts.py | covered |
| mesh_policy_check | tests/test_scripts_a23.py | covered |
| chain_config_check | tests/test_scripts_a28.py, tests/test_tools_a29.py | covered |
| finops_cost_scan | tests/test_scripts_a20.py | covered |
| custody_policy_check | tests/test_scripts_a30.py, tests/test_substrate_tee.py | covered |
| resil_manifest_check | tests/test_scripts_a25.py | covered |
| kpol_manifest_scan | tests/test_scripts_a24.py | covered |
| supply_image_lint | tests/test_scripts_a18.py, tests/test_tools_a18.py | covered |
| plat_catalog_lint | tests/generated/test_tool_contracts.py, tests/test_scripts_a17.py, tests/test_tools_a17.py | covered |
| audit_findings_check | tests/test_scripts_a27.py | covered |
| build_agents | tests/conftest.py, tests/fixtures/greptile/threads.json, tests/fixtures/greptile/threads_list.json, tests/test_build_agents_symlinks.py, tests/test_cursor_agents.py, tests/test_grokbot_export.py, tests/test_install_substrate.py, tests/test_install_workspace_cli.py, tests/test_omp_agents.py, tests/test_render_rules.py | covered |
| obs_slo | tests/test_tools_a13_existing.py | covered |
| docs_bundle | tests/test_substrate_tee.py, tests/test_tools_a15_existing.py | covered |
| ux_tokens | tests/fixtures/redact_cases.json, tests/test_redact.py, tests/test_tools_a04_existing.py | covered |
| maint_deps | tests/test_tools_a14.py | covered |
| devops_ci_check | tests/test_tools_a11_existing.py | covered |
| fe_a11y_check | tests/test_tools_a06_existing.py, tests/ts/fe_a11y_check.test.ts | covered |
| sec_gate | tests/fixtures/receipt/sample_state.json, tests/generated/test_tool_contracts.py, tests/omp_contract/freeze.md, tests/omp_contract/vectors.json, tests/test_cursor_agents.py, tests/test_dev_key_fail_closed.py, tests/test_ingest_parity.py, tests/test_tools_a10_existing.py, tests/test_verdicts.py | covered |
| be_contract_conformance | tests/test_tools_a05_existing.py | covered |
| orch_status | tests/omp_contract/freeze.md, tests/omp_contract/vectors.json, tests/perf/baseline.md, tests/perf/run.sh, tests/test_cursor_agents.py, tests/test_gate_lease_integrity.py, tests/test_ingest_parity.py, tests/test_insession_escalation.py, tests/test_omp_runtime.py, tests/test_verdicts.py | covered |
| swarm_tool | tests/omp_contract/freeze.md, tests/omp_contract/vectors.json, tests/test_capability_acceptance.py, tests/test_omp_agents.py, tests/test_render_rules.py, tests/test_swarm_tool.py | covered |
| swarm_live | tests/test_live_contract.py, tests/test_swarm_live.py | covered |
| orch_assign | tests/omp_contract/freeze.md, tests/omp_contract/vectors.json, tests/test_specialist_assignment.py, tests/test_swarm.py, tests/test_tools_a01_existing.py | covered |
| rev_greptile_ingest | tests/test_rev_greptile_ingest.py | covered |
| receipt_export | tests/test_receipt_export.py | covered |
| qa_gate | tests/omp_contract/freeze.md, tests/omp_contract/vectors.json, tests/perf/baseline.md, tests/perf/run.sh, tests/test_cursor_agents.py, tests/test_dev_key_fail_closed.py, tests/test_ingest_parity.py, tests/test_qa_gate_detection.py, tests/test_receipt_export.py, tests/test_verdicts.py | covered |
| rev_gate | tests/fixtures/receipt/sample_state.json, tests/omp_contract/freeze.md, tests/omp_contract/vectors.json, tests/test_cursor_agents.py, tests/test_dev_key_fail_closed.py, tests/test_gate_lease_integrity.py, tests/test_ingest_parity.py, tests/test_insession_escalation.py, tests/test_omp_runtime.py, tests/test_receipt_export.py, tests/test_rev_greptile_ingest.py, tests/test_signing.py, tests/test_verdicts.py | covered |
| rel_plan | tests/omp_contract/freeze.md, tests/omp_contract/vectors.json, tests/test_cursor_agents.py, tests/test_ingest_parity.py, tests/test_release_gate.py, tests/test_substrate_lease.py, tests/test_verdicts.py | covered |
| devops_build_record | tests/generated/test_tool_contracts.py, tests/test_shared_handover.py, tests/test_tools_a11.py | covered |
| arch_adr | tests/generated/test_tool_contracts.py, tests/test_lane_tools.py, tests/test_tools_a03.py, tests/test_tools_a03_existing.py | covered |
| arch_contract_check | none | **uncovered** |
| code_checks | tests/fixtures/greptile/threads.json, tests/fixtures/greptile/threads_list.json, tests/test_grokbot_export.py, tests/test_substrate_tee.py | covered |
| data_migration_check | tests/test_tools_a07_existing.py | covered |
| req_lint | tests/test_ts_scripts.py | covered |

## Uncovered lanes

- **arch_contract_check** (A03). No file under `tests/` names the script. `tests/test_lane_tools.py` and
  `tests/test_tools_a03_existing.py` exercise the sibling registry tool `arch_contract_ops`, which does not cover the
  script itself.

## Notes

- Coverage is by stem, so it is a floor, not a depth measure. The scan marked eleven rows as lower bounds (`≥` in the
  working notes): `orch_plan`, `swarm_run`, `chain_config_check`, `build_agents`, `orch_status`, `swarm_tool`,
  `orch_assign`, `qa_gate`, `rev_gate`, `rel_plan`, `sec_gate`. Their status is unaffected, but their file lists may be
  incomplete.
- The `ux_tokens` row (`tests/fixtures/redact_cases.json`, `tests/test_redact.py`) and the `code_checks` row
  (`tests/fixtures/greptile/*`, `tests/test_grokbot_export.py`, `tests/test_substrate_tee.py`) were recorded from
  content matches and were not opened to confirm that they exercise the script.
- `tests/parity/harness.py` covers the first read-only route (`orch_status`, `swarm_tool` catalog and call). Its cases
  do not change the coverage status above.
