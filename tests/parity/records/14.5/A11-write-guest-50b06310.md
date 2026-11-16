Verdict: REPRODUCIBLE

# A11 reproducibility record: P2 write guest 50b06310 (D-P38, extended)

Scope: the P2 write guest `50b06310480fd0dbce7c285ce24191a58eb5cca9bbe86ca28a23e89ea5ba15d4` (`crates/gsd-sandbox/guest-module-write.sha256`, built by `gsd-sandbox --features write-routes`, which builds the nested guest with `write-routes-2`). Checked at HEAD `d8a354e` on `feat/v2.4-rust-wasm-core`. Read-only on product code, pins, `web/`, `docs/` and `.planning/`. No pin, default, or commit was changed. The only file created is this record.

Verdict basis: a clean worktree at `d8a354e` builds `50b06310...` and the test that compares build, embedded guest, and pin passes (check 1); the default guest built in the same scratch target still builds `91b0970e...` and each build reads its own pin (check 2 and check 5); `scripts/_check_pins.py` exits 0 (check 4). Check 3 finds no transpiled web artifact for the write guest, and that is the one gap this record reports. It does not change the verdict on reproducibility.

## Environment

- `rustc 1.98.1 (48a229cea 2026-09-01)`, `cargo 1.98.1 (797e8a9bc 2026-08-05)`, from `rust-toolchain.toml` (`channel = "1.98.1"`), observed with `rustc -V` and `cargo -V` in the main checkout.
- Clean worktree: `git worktree add --detach /root/.cache/gsd-redact-work/A11w/wt d8a354e`. `git status --short` was empty before the build.
- Scratch target: `CARGO_TARGET_DIR=/root/.cache/gsd-redact-work/A11w/target` (not the shared `target/`). `CARGO_TERM_PROGRESS_WHEN=never`. No `RUSTFLAGS` or `CARGO_BUILD_*` variables were set (checked in the previous A11 record's environment; not re-checked here).
- `--locked --offline` on every cargo command. The shared `target/` was not touched and nothing was cleaned.

## Commands and exit codes

| # | command (cwd = clean worktree unless noted) | exit | result |
|---|---|---|---|
| 1 | `CARGO_TARGET_DIR=…/A11w/target sh -c 'cargo test -p gsd-sandbox --features write-routes --locked --offline --lib built_guest_digest -- --nocapture; echo EXIT=$?'` | 0 | `guest_digest::built_guest_digest_matches_the_committed_pin ... ok`, `1 passed; 0 failed`. Build took 3m 40s (cold scratch target). |
| 2 | `CARGO_TARGET_DIR=…/A11w/target sh -c 'cargo test -p gsd-sandbox --locked --offline --lib built_guest_digest -- --nocapture; echo EXIT=$?'` (default features, same scratch target, run after #1) | 0 | same test `ok`, `1 passed; 0 failed`. Build took 24.4s (only the default nested guest and `gsd-sandbox` were rebuilt). |
| 3 | `node transpile.mjs <write component>` in `web/` (component = the write build's `gsd_guest.component.wasm`, copied to scratch) | 1 | `transpile: component sha256 50b06310… is not the pin 91b0970e…`. Refused before any file was written. `web/generated/` unchanged. |
| 4 | `SWARM_GSD_STATE_FILE= SWARM_CLAUDE_STATE_DIR=<scratch>/claude python3 scripts/_check_pins.py` (clean worktree at `d8a354e`) | 0 | `pin check: ok (3f65737caa5d023691a1f0474880f191a9eb588890852ab6ed5ab751256e3b9e)` |
| 5 | `git worktree remove --force <wt>` | 0 | worktree removed; `git worktree list` no longer shows it |

Test function: `crates/gsd-sandbox/src/lib.rs:43` (`mod guest_digest`, `fn built_guest_digest_matches_the_committed_pin`). It asserts three things: `OUT_DIR/guest-module.sha256` equals SHA-256 of the embedded guest; the embedded guest equals the pin file named by `GSD_GUEST_PIN_FILE`; and the pin file is `guest-module-write.sha256` under `write-routes`, `guest-module.sha256` otherwise.

## Digests (commit `d8a354e`, toolchain above)

| build | component sha256 (`OUT_DIR/guest-module.sha256`) | component size | core wasm sha256 (`guest-target/.../gsd_guest.wasm`, pre-wrap) | pin read by build.rs |
|---|---|---|---|---|
| write (`--features write-routes`) | `50b06310480fd0dbce7c285ce24191a58eb5cca9bbe86ca28a23e89ea5ba15d4` | 691,799 bytes | `0b8dda9d0a2473545d336456666debe624bb5945efcfdd7426c1bbc2a406cff7` | `crates/gsd-sandbox/guest-module-write.sha256` |
| default | `91b0970ebe43fb7a80e5c6df68abcf6516572a766260e9780fd6f3c199b9a175` | 150,637 bytes | `67743291be6647766411eb2d4aa10812d424fae2154efada1a42fe5099809e09` | `crates/gsd-sandbox/guest-module.sha256` |

The default numbers match the earlier A11 record (`A11-guest-pin-reproduce.md`, commits `144a8d3` and `dc46279`): same component size, same core digest, same component digest. So the default guest source has not changed since then.

## Isolation (check 5)

The worry from the earlier A11 observation is two guest digests under one unit-hash directory in the shared `target/`. In this run both builds shared one scratch target and the separation is shown directly:

1. Two distinct build-script output directories exist in the scratch target, one per feature set: `target/debug/build/gsd-sandbox-06fe2b0b980b87d7/` (write) and `target/debug/build/gsd-sandbox-8ef0ec980523f3de/` (default). The two build-script binaries (`gsd-sandbox-45618daf6fdaabee`, `gsd-sandbox-b8e1c36ca3a3d8e5`) have no `out/` directory and carry no guest.
2. Each build-script `output` file names its own pin, with no crossover:
   - write: `cargo:rustc-env=GSD_GUEST_PIN_FILE=…/crates/gsd-sandbox/guest-module-write.sha256`
   - default: `cargo:rustc-env=GSD_GUEST_PIN_FILE=…/crates/gsd-sandbox/guest-module.sha256`
3. Each `out/` has its own `guest-target/` (`OUT_DIR/guest-target`, the nested `cargo build --target-dir`). The nested fingerprints record the features actually compiled:
   - write: `gsd-guest` and `gsd-route` `"features":["write-routes","write-routes-2"]`
   - default: `gsd-guest` and `gsd-route` `"features":[]`
4. The write `out/` still held `50b06310...` after the default build ran (read after #2). The default build did not overwrite it. The write component file was not modified (mtime `10:57`, default's at `10:58`, in different directories).

So the nested write guest is built from `write-routes-2` features with its own target and digest, and no default artifact reached it. [INFERENCE] The earlier two-digests observation most likely came from the shared `target/` before the per-`OUT_DIR` guest target was in place. I did not reproduce that observation, and I cannot say from this run what caused it.

Caveat on check 2: the default build was not in a fresh scratch target. It reused the target that had the write build. The separation evidence above is what covers that. The clean default reproduction at `144a8d3`/`dc46279` is in `A11-guest-pin-reproduce.md`.

## Check 3: web artifacts for the write guest

There is none, and the pipeline refuses to make one.

- `web/jco.pin.json:5-6` pins `component_pin = crates/gsd-sandbox/guest-module.sha256` and `component_sha256 = 91b0970e...`. No write component pin exists under `web/`.
- `web/transpile.mjs:30` refuses any component whose SHA-256 differs from `component_sha256`. Observed in #3: the write component is refused with exit 1.
- `web/generated/` is tracked (`gsd-core.core.wasm`, `gsd-core.d.ts`, `gsd-core.js`) and contains only the default transpile.
- `web/component-loader.mjs:9` says the browser loader "implements no write route", and it imports only the read route (`orch_status`).
- `grep -rn "50b06310\|guest-module-write" web scripts` finds nothing. Outside the pin file itself, the write digest appears only in docs and `.planning/` (`docs/operations-runbook.md:442`, `tests/parity/records/14.5/INDEX.md`, `tests/parity/flip_record.md`, and the phase verification file).

Consequence for the browser path: no write route is reachable from the browser. The write guest runs only in the Rust sandbox (`gsd-adapter --features sandbox-write`). Putting it in the browser would need a new `web/jco.pin.json` entry for a new component digest, which is a product and pin decision. A11 does not make it.

## Other findings (file:line)

- **F-W1 (minor): the write pin's digest test is not run by CI.** `crates/gsd-sandbox/src/lib.rs:43` is the only test that compares the write-built guest to `guest-module-write.sha256`, and only under `--features write-routes --lib`. CI line `.github/workflows/ci.yml:116` runs `cargo test -p gsd-sandbox --features write-routes --test guest_imports` (the integration test, not `--lib`). CI line `:151` (`cargo test --workspace`) builds default features, so it runs the default pin test. CI line `:157` runs `gsd-adapter --features sandbox-write`, which builds the write guest but runs no digest assertion. Mitigation: at runtime the adapter loads the embedded guest against its embedded pin (`crates/gsd-adapter/src/sandbox_engine.rs:86-92`, `with_module`, "must match `pin` … on load"), and the `WRONG_PIN` tests (`sandbox_engine.rs:955` and following) exercise that load check. So a pin mismatch is caught at runtime in the write-enabled adapter tests, but not by the digest assertion. [INFERENCE] that the loader rejects the mismatch, from the `WRONG_PIN` test names and the `with_module` doc comment; not re-run here.
- **F-W2 (info): `scripts/_check_pins.py` does not cover guest digests.** Its docstring (lines 1-12) names `rust-toolchain.toml` and the `[workspace.metadata.gsd-pins]` table only. It passes at HEAD (#4) and says nothing about either guest pin.
- **F-W3 (info): the build's `output` file is the only record of which pin a build used.** Nothing in CI reads it. Included here for the isolation proof, not as a defect.

## What I could not verify

- The write-guest `guest_imports` and `write_guest` integration tests were not run in this record. Only the digest test was in scope.
- The write guest's imports and exports were not inspected here. The pin test does not check them.
- The 4.0 GB scratch target was not removed (see Cleanup).
- The earlier two-digests observation was not reproduced. The cause is [INFERENCE].

## Cleanup

- Worktree `/root/.cache/gsd-redact-work/A11w/wt` removed (#5, exit 0). Scratch copy of the write component removed.
- Scratch target `/root/.cache/gsd-redact-work/A11w/target` (about 4.0 GB) was NOT removed. The shell guard refused `rm -rf` with "BLOCKED needs: human-approval (destructive: rm-rf-protected)". I did not work around the guard. Removing it needs a human to approve, or the orchestrator to do it. Logs `write-test.log`, `default-test.log`, and an empty `claude/` directory remain under `/root/.cache/gsd-redact-work/A11w/`.
- Main checkout: `git status --short` shows only the pre-existing untracked `deny-report.txt`, which I did not touch. This record is the only new file.
