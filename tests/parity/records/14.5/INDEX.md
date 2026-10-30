# Records of Phase 14.5 (`tests/parity/records/14.5/`)

Index written by unit U3.8b on 2026-10-30. Every file in this directory is listed; nothing else is in it.

| File | Verdict or purpose |
|---|---|
| `A10-guest-pin-91b0970e.md` | A10 security gate for the default guest pin `91b0970e…` (commit `54fd751`, D-P38). Verdict **SECURED**, scoped to the default read-only guest; not a clearance of any write route. Findings: F1 (canary scan did not cover the P2 request bytes) is closed by commit `d0617a5`, which postdates this record, so the record text still says open; F2 (STATE wording of `ffa7b28a…`) corrected in STATE; F3 (`regex` edge) closed by D-P40; F4 to F8 minor or info |
| `A11-guest-pin-reproduce.md` | A11 reproducibility of `91b0970e…`: **REPRODUCIBLE**. HEAD `144a8d3` and `dc46279` build the pin from clean worktrees; `web/generated` is byte-identical to a fresh jco 1.39.0 transpile; `scripts/_check_pins.py` exits 0 |
| `INDEX.md` | This index |

Not in this directory, and not covered by either record:

- The write guest pin `50b06310480fd0dbce7c285ce24191a58eb5cca9bbe86ca28a23e89ea5ba15d4` (P2, `guest-module-write.sha256`) has **no A10 or A11 record**. Every write-route flip needs one.
- The D-P37 sandbox-write harness output (`sandbox.json`, `host.json`, 786 transition checks each) is in scratch, `/root/.cache/gsd-redact-work/U37d/`, outside the repo. Copy it here before any flip.
- The parity and cargo outputs of U3.8b are in scratch, `/root/.cache/gsd-redact-work/U38b/`, outside the repo.
- Related retained records for the read and transition routes are in `tests/parity/records/14.3/` (14.3 R7 release-build record: `final/R7.md`, with the interim record in `interim-64f5759/`). They predate the P2 pin.
