# 14.5 records

Index of `tests/parity/records/14.5/`. Phase 14.5 closes as `pass_with_recorded_debt` (D-P43, D-P44); every route stays opt-in and the default is `python`. Nothing here is a flip approval.

| File | What it is | Verdict |
|---|---|---|
| `A10-guest-pin-91b0970e.md` | A10 security gate for the default read-only guest pin `91b0970e...` (commit `54fd751`, D-P38). Findings F1 (canary scan of P2 bytes) closed by `d0617a5`, which postdates the record; F3 (`regex` edge) closed by D-P40, which corrected its origin to the qa_gate route; F4 to F8 minor or info. | SECURED, scoped to the default read-only guest |
| `A10-write-guest-50b06310.md` | A10 security gate for the P2 write guest `50b06310...` (commit `54fd751`). Minor findings: the P2 requests carry signed envelope text (doc corrected in `c74de94`), catalog has no host route, the shadow reuses the guest's Rust code. Not a clearance for any write flip. | SECURED, scoped |
| `A11-guest-pin-reproduce.md` | A11 reproducibility of `91b0970e...`. HEAD and `dc46279` build the pin from clean worktrees; `ffa7b28a...` reproduces from its own commit `eb7a2b9`; `web/generated` is byte-identical to a fresh jco 1.39.0 transpile; `scripts/_check_pins.py` exits 0. | REPRODUCIBLE |
| `A11-write-guest-50b06310.md` | A11 reproducibility of the P2 write guest `50b06310...` at HEAD `d8a354e`. No web artifact exists for the write guest. CI does not assert its digest (F-W1). | REPRODUCIBLE |
| `U37d-sandbox-write-harness.json` | Harness output of `python3 tests/parity/harness.py --rust --backend sandbox --sandbox-write --json` at the P2 pin (12 of 12 cases; `served_by` rust 696, python 87, fallback 81 and 3). | evidence, not a flip record |
| `U37d-host-harness.json` | The same run on the host backend. | evidence, not a flip record |

Not in this directory:

- The 14.3 release-build record (`tests/parity/records/14.3/final/R7.md`) predates the P2 pin; its sandbox leg served zero cells at `0b63a38`.
- The U3.8b parity and cargo outputs are in scratch (`/root/.cache/gsd-redact-work/U38b/`); they are not part of the repository.
- The quiet-host A2 record for the write routes (U3.8a) does not exist; the user deferred it (D-P36, D-P43).
