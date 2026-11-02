#!/usr/bin/env bash
# Supply-chain gate: check the time-boxed advisory ignores in deny.toml (scripts/_check_deny_ignores.py), then
# install a pinned cargo-deny and run `cargo deny --locked check` (advisories, bans, licenses, sources)
# against deny.toml at the repository root.
#
# Usage: scripts/ci_cargo_deny.sh [REPORT]      REPORT defaults to ./deny-report.txt
# Exit status: the ignore checker's status (1 violation, 2 unreadable deny.toml) when it fails, before cargo-deny runs;
#              else cargo-deny's own status (0 clean, non-zero on any failed check);
#              2 if the tool cannot be installed or the checksum does not match.
#
# Needs no root and no Docker. Writes only under $RUNNER_TEMP (else a mktemp dir) and to REPORT.
# The advisory database is fetched from github.com by cargo-deny itself.
set -euo pipefail

# Pin: cargo-deny 0.20.2, the latest GitHub release and crates.io max_stable_version on 2026-10-28.
# SHA-256 of the x86_64-unknown-linux-musl tarball: computed locally with sha256sum over the downloaded
# file and equal to the .sha256 asset published next to it on the release page.
DENY_VERSION="0.20.2"
DENY_TARGET="x86_64-unknown-linux-musl"
DENY_SHA256="9f12ed4c49936e09b48bf862b595cde2fe64fcbd9d74dfacac6131ca824c8d5f"
DENY_URL="https://github.com/EmbarkStudios/cargo-deny/releases/download/${DENY_VERSION}/cargo-deny-${DENY_VERSION}-${DENY_TARGET}.tar.gz"

report="${1:-deny-report.txt}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
report_abs="$(cd "$(dirname "$report")" && pwd)/$(basename "$report")"

cd "$repo_root"
python3 scripts/_check_deny_ignores.py || exit "$?"

if ! command -v cargo >/dev/null 2>&1; then
  echo "ci_cargo_deny: cargo is absent; cargo-deny needs it for cargo metadata" >&2
  exit 2
fi

if [[ -n "${RUNNER_TEMP:-}" ]]; then
  mkdir -p "$RUNNER_TEMP"
  work="$(mktemp -d "$RUNNER_TEMP/cargo-deny.XXXXXX")"
else
  work="$(mktemp -d)"
fi
trap 'rm -rf "$work"' EXIT
bin_dir="$work/bin"
mkdir -p "$bin_dir"

downloaded=0
if command -v curl >/dev/null 2>&1; then
  curl -fsSL --retry 2 -m 120 -o "$work/cargo-deny.tar.gz" "$DENY_URL" && downloaded=1
elif command -v wget >/dev/null 2>&1; then
  wget -q -T 120 -O "$work/cargo-deny.tar.gz" "$DENY_URL" && downloaded=1
fi

if [[ "$downloaded" == 1 ]]; then
  actual="$(sha256sum "$work/cargo-deny.tar.gz" | cut -d' ' -f1)"
  if [[ "$actual" != "$DENY_SHA256" ]]; then
    echo "ci_cargo_deny: SHA-256 mismatch for $DENY_URL" >&2
    echo "  expected $DENY_SHA256" >&2
    echo "  actual   $actual" >&2
    exit 2
  fi
  tar -xzf "$work/cargo-deny.tar.gz" -C "$work" --strip-components=1 \
    "cargo-deny-${DENY_VERSION}-${DENY_TARGET}/cargo-deny"
  install -m 0755 "$work/cargo-deny" "$bin_dir/cargo-deny"
  echo "ci_cargo_deny: installed cargo-deny ${DENY_VERSION} from the release tarball (sha256 ${DENY_SHA256})"
else
  echo "ci_cargo_deny: download failed; falling back to cargo install cargo-deny ${DENY_VERSION} --locked" >&2
  cargo install cargo-deny --locked --version "$DENY_VERSION" --root "$work"
fi

export PATH="$bin_dir:$PATH"
cargo deny --version

cd "$repo_root"
set +e
cargo deny --locked --color never check 2>&1 | tee "$report_abs"
status="${PIPESTATUS[0]}"
set -e

echo "ci_cargo_deny: cargo-deny exit status ${status}; report saved to ${report_abs}"
exit "$status"
