Verdict: REPRODUCIBLE
# A11 reproducibility record: default guest pin 91b0970e (D-P38)

Scope: default sandbox guest pin `91b0970ebe43fb7a80e5c6df68abcf6516572a766260e9780fd6f3c199b9a175` (commit `54fd751`, D-P37), HEAD `144a8d3` on `feat/v2.4-rust-wasm-core`, and the generated web artifacts in `web/generated/`. Read-only on product code, pins, `web/`, `docs/` and `.planning/`. No pin, default or commit was changed.

Verdict basis: HEAD builds `91b0970e...` from a clean worktree (check 1); the tracked web artifacts are byte-identical to a fresh jco 1.39.0 transpile (check 3); `scripts/_check_pins.py` exits 0 (check 4). All three conditions of REPRODUCIBLE hold.

Important correction to the premise of D-P38 (1): `ffa7b28a...` is NOT lost and is NOT environmental. It reproduces from its own commit `eb7a2b9` in a clean worktree with the same toolchain (check 2). It stopped being the default because the guest source changed at `dc46279` (section 1). The re-pin commit message and D-P38 say its origin is "not reproducible from clean trees"; that is true only of the later trees it compared (`dc46279`, HEAD), not of `eb7a2b9`. The STATE.md wording of D-P38 (1) should be corrected by the orchestrator.

## 1. Clean-tree reproduction at HEAD and at dc46279 / eb7a2b9

Environment, identical for all three builds:
- `rustc 1.98.1 (48a229cea 2026-09-01)`, `cargo 1.98.1 (797e8a9bc 2026-08-05)`, from the `rust-toolchain.toml` override (`channel = "1.98.1"`, present at all three commits, identical content).
- No `RUSTFLAGS`, `CARGO_ENCODED_RUSTFLAGS` or `CARGO_BUILD_*` variables set (`env | grep -i "rust\|cargo"` showed only `CARGO_TERM_PROGRESS_WHEN=never`).
- Worktrees (detached) under `/root/.cache/gsd-redact-work/A11/`: `wt-head` (144a8d3), `wt-dc46279` (dc46279), `wt-eb7a2b9` (eb7a2b9). Each has its own `CARGO_TARGET_DIR` (`target-head`, `target-dc46279`, `target-eb7a2b9`), never the shared `target/`.
- Commands: `cargo metadata --offline --locked` (exit 0); HEAD `cargo test -p gsd-sandbox --lib built_guest_digest --locked --offline -- --nocapture` (exit 0); dc46279 and eb7a2b9 `CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 cargo build -p gsd-sandbox --lib --locked --offline` (exit 0 each). The two reduced-debug builds ran no test; their digests are read from the build script output `OUT_DIR/guest-module.sha256`, which `build.rs` writes on every build.

| tree (worktree) | commit | component sha256 (`OUT_DIR/guest-module.sha256`) | matches pin in that tree? | core module sha256 (`gsd_guest.wasm`, pre-wrap) |
|---|---|---|---|---|
| HEAD | `144a8d3` | `91b0970ebe43fb7a80e5c6df68abcf6516572a766260e9780fd6f3c199b9a175` | yes (`guest-module.sha256`); test `guest_digest::built_guest_digest_matches_the_committed_pin ... ok`, `1 passed; 0 failed` | `67743291be6647766411eb2d4aa10812d424fae2154efada1a42fe5099809e09` |
| dc46279 | `dc46279` | `91b0970ebe43fb7a80e5c6df68abcf6516572a766260e9780fd6f3c199b9a175` | yes (not run as a test; digest read from build output) | `67743291be6647766411eb2d4aa10812d424fae2154efada1a42fe5099809e09` |
| eb7a2b9 | `eb7a2b9` | `ffa7b28a7879da7ea99a20c83999d0b79e757bd084878909dcce0054fe66cade` | yes, equals the pin recorded in eb7a2b9 (not run as a test) | `0e4278cd468fc031938b83b057d5bed0e5bf3ee87c1ad613763b2454aad065e1` |

The HEAD component is 150637 bytes in `OUT_DIR`. The copy used for transpile (`component-head.wasm`) is the same file, sha256 `91b0970e...` (verified by `sha256sum`). The HEAD and dc46279 components are byte-identical (same sha256), though they were built with different dev-profile debug settings: the guest is built by `build.rs` with `--release` in a nested invocation, so the host dev-profile settings do not reach it. The HEAD build (default settings) and dc46279 build (reduced debug) agreeing is the observed evidence for that.

Check 2 answer (is `ffa7b28a...` lost, or environmental?): it is neither. A clean worktree at `eb7a2b9`, with the same toolchain and no RUSTFLAGS, builds `ffa7b28a7879da7ea99a20c83999d0b79e757bd084878909dcce0054fe66cade`. Its guest core module differs (`0e4278cd...` versus `67743291...`) because the guest source changed in `dc46279`:
- `crates/gsd-guest/src/lib.rs`: `invoke` now calls `gsd_route::core_invoke` under `write-routes` and `orch_status_invoke` otherwise (+7 / -1).
- `crates/gsd-guest/Cargo.toml`: new `write-routes` feature; `gsd-route` now depends with `default-features = false` (+7 / -1).
- `crates/gsd-route/Cargo.toml`: new feature groups `write-routes`, `write-routes-2`, `host-routes`, with default-features on for hosts (+13). Its own comment states that even dead code moves the guest's string pool and therefore its digest.
- `crates/gsd-sandbox/build.rs`: the nested build learns the `write-routes` feature and chooses a second pin file (+24 / -2).
- `Cargo.lock`: adds workspace crates `gsd-envelope`, `gsd-redact`, `gsd-sched-core`, `gsd-taskwriter` and `loom`/`generator`/`scoped-tls`. `loom` is only a `cfg(loom)` dependency of `gsd-sched-core` (`crates/gsd-sched-core/Cargo.toml`), not in the guest's graph by the manifests read [INFERENCE from manifests, not from `cargo tree`].

So the change of digest is source drift between two commits, not toolchain or environment drift. The default guest at `dc46279` and HEAD is a different program from the one pinned at `eb7a2b9`.

Caveat on shared target dirs (observed): the unit-hash directory name `gsd-sandbox-8ef0ec980523f3de` is identical in the main `target/debug/build/` (whose `out/guest-module.sha256` reads `ffa7b28a...`) and in my HEAD build (`91b0970e...`). Cargo's metadata hash for workspace path crates does not depend on the worktree path, so a shared target directory can return another tree's guest output. The `54fd751` verification says it used a shared target dir. The four digests above come from separate target directories and are not affected.

Path dependence: all builds ran from worktrees, not the main checkout. The three core modules contain no absolute path strings (`grep -a -c -e '/root/' -e 'wt-' -e 'gsd-redact-work'` returned 0 for each). That is consistent with path-independence [INFERENCE]; building in the main checkout path was not run.

## 2. Web artifacts (`web/generated`, jco 1.39.0)

- jco: `npm ci --ignore-scripts --no-audit --no-fund` from HEAD's `web/package-lock.json` in scratch `/root/.cache/gsd-redact-work/A11/web/` (exit 0, "added 75 packages"). `node_modules/@bytecodealliance/jco/package.json` version `1.39.0`, equal to `web/jco.pin.json` `version`.
- Transpile: HEAD `web/transpile.mjs` and `web/jco.pin.json` copied to scratch, `node transpile.mjs <component-head.wasm>` (the component whose sha256 equals `component_sha256` `91b0970e...`; the script enforces it). Exit 0. Run twice; `diff -r` between runs empty.
- Tracked `web/generated` was NOT overwritten; `git diff HEAD -- web/generated` is empty.

| file | tracked at HEAD `web/generated` (sha256) | fresh transpile (sha256) |
|---|---|---|
| `gsd-core.core.wasm` (150191 bytes) | `e7b8b21b36bc9b5a97cc83d7d69a03e8513b78bf35ec6426677659b300901601` | `e7b8b21b36bc9b5a97cc83d7d69a03e8513b78bf35ec6426677659b300901601` |
| `gsd-core.d.ts` | `f8ceabb4fcfc2a65bd1f5e91eb2c62de3b58d5a852a322c06a2811c9deaab5fe` | `f8ceabb4fcfc2a65bd1f5e91eb2c62de3b58d5a852a322c06a2811c9deaab5fe` |
| `gsd-core.js` | `99bc5c54197139c1711d88dec5c2e54268910acef1fdee035d7be395b7275ad1` | `99bc5c54197139c1711d88dec5c2e54268910acef1fdee035d7be395b7275ad1` |

`diff -r` between the scratch output and the tracked `web/generated` directory: empty. Byte-identical.

Extra check on the superseded web artifacts (eb7a2b9): the same method with `eb7a2b9`'s `transpile.mjs`/`jco.pin.json` and the reproduced `ffa7b28a` component gives `gsd-core.core.wasm` `950a550980c091a08a9d77c59dfc59fb210193fd811a0d3f3a6f9719b097798e`, `gsd-core.d.ts` `f8ceabb4...`, `gsd-core.js` `99bc5c54...`. `diff -r` against `git archive eb7a2b9 web/generated`: empty. The `.js` and `.d.ts` did not change between `eb7a2b9` and HEAD; only the core wasm did, as the re-pin commit states.

## 3. `scripts/_check_pins.py` at HEAD

- Main checkout (`/root/src/repos/agent-swarm`, HEAD `144a8d3`, clean except pre-existing untracked `deny-report.txt`): `SWARM_GSD_STATE_FILE= SWARM_CLAUDE_STATE_DIR=<scratch>/claude python3 scripts/_check_pins.py` printed `pin check: ok (3f65737caa5d023691a1f0474880f191a9eb588890852ab6ed5ab751256e3b9e)`, exit 0.
- Same command in `wt-head`: same output, exit 0.
- Scope note: this script checks `rust-toolchain.toml` and the `[workspace.metadata.gsd-pins]` table against `scripts/pins.sha256`. It does not check the guest digest files, so it does not cover `91b0970e...`. The guest digest is covered by the `built_guest_digest` test (section 1) and by `web/jco.pin.json`.

## 4. Commands and exit codes (summary)

1. `git worktree add --detach <scratch>/wt-head 144a8d3` (and `wt-dc46279`, `wt-eb7a2b9`): ok.
2. `cargo metadata --offline --locked` in wt-head: 0.
3. `cargo test -p gsd-sandbox --lib built_guest_digest --locked --offline -- --nocapture` in wt-head, `CARGO_TARGET_DIR=target-head`: 0.
4. `CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 cargo build -p gsd-sandbox --lib --locked --offline` in wt-dc46279 and wt-eb7a2b9: 0 and 0.
5. `python3 scripts/_check_pins.py` in main checkout and wt-head: 0 and 0.
6. `npm ci --ignore-scripts --no-audit --no-fund` in scratch web: 0.
7. `node transpile.mjs <component>` (HEAD, two runs; eb7a2b9 once): 0 each.
8. `git worktree remove --force` for the three worktrees: 0 each; `git worktree list` shows no A11 entries afterward.

## 5. Notes for the orchestrator

- Scratch build outputs remain under `/root/.cache/gsd-redact-work/A11/`: `target-head/` (about 3.4 GB), `target-dc46279/` (about 1.5 GB), `target-eb7a2b9/` (about 1.5 GB), plus `web/`, `web-eb7a2b9/` and the three extracted component/core `.wasm` files. The disk was at 99 percent (2.7 GB free) when this record was written; a shell guard refused `rm -rf` on these directories, so they were not deleted. Remove them when the disk is needed.
- The `54fd751` message's verification ("shared target dir") is the hazard in section 1; the separate target dirs here are the cleaner evidence.
- No product code, pin, `web/` or `docs/` file was changed. This record is the only file created in the repository.
