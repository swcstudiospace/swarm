#!/usr/bin/env bash
# Print facts about the CI host so every run records what the runner pool offers.
# Read-only apart from one HOME write test; never prints the environment; always exits 0.
set -euo pipefail

section() { printf '\n== %s ==\n' "$1"; }

# Run a command, print its output, and never let a failure abort the script.
try() { "$@" 2>&1 || printf '(exit %s)\n' "$?"; }

section "uname"
try uname -a

section "os-release"
if [ -r /etc/os-release ]; then cat /etc/os-release; else echo "absent"; fi

section "glibc"
if command -v ldd >/dev/null 2>&1; then (ldd --version 2>&1 || true) | sed -n '1p'; else echo "absent"; fi

section "cpu"
if command -v nproc >/dev/null 2>&1; then echo "nproc: $(nproc)"; else echo "nproc: absent"; fi

section "loadavg"
if [ -r /proc/loadavg ]; then cat /proc/loadavg; else echo "absent"; fi

section "memory (MiB)"
if command -v free >/dev/null 2>&1; then try free -m; else echo "absent"; fi

section "disk"
for dir in "${HOME:-}" "${RUNNER_TEMP:-}"; do
  if [ -n "$dir" ] && [ -d "$dir" ]; then try df -h "$dir"; else echo "(unset or missing: ${dir:-<empty>})"; fi
done

section "identity"
try id

section "HOME write test"
if [ -n "${HOME:-}" ] && probe="$(mktemp "$HOME/.ci-host-facts.XXXXXX" 2>/dev/null)"; then
  rm -f "$probe"
  echo "HOME writable: yes"
else
  echo "HOME writable: no"
fi

section "tool versions"
for tool in rustup cargo rustc python3 node bun google-chrome pkg-config; do
  if command -v "$tool" >/dev/null 2>&1; then
    printf '%s: %s\n' "$tool" "$( ("$tool" --version 2>&1 || true) | sed -n '1p')"
  else
    printf '%s: absent\n' "$tool"
  fi
done

section "python shared libpython"
if command -v python3 >/dev/null 2>&1; then
  try python3 -c 'import sysconfig; print("Py_ENABLE_SHARED:", sysconfig.get_config_var("Py_ENABLE_SHARED"))'
else
  echo "absent"
fi

section "openssl (pkg-config)"
if command -v pkg-config >/dev/null 2>&1; then
  printf 'openssl: %s\n' "$(pkg-config --modversion openssl 2>/dev/null || echo absent)"
else
  echo "openssl: absent (no pkg-config)"
fi

section "egress probes"
probe_url() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    local code
    code="$(curl -sS -o /dev/null -m 10 -w '%{http_code}' "$url" 2>/dev/null || true)"
    case "$code" in
      ''|000) echo "$url: unreachable (curl)" ;;
      *) echo "$url: reachable (curl, HTTP $code)" ;;
    esac
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$url" <<'PY' || true
import sys
import urllib.error
import urllib.request

url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=10) as resp:
        print(f"{url}: reachable (urllib, HTTP {resp.status})")
except urllib.error.HTTPError as exc:
    print(f"{url}: reachable (urllib, HTTP {exc.code})")
except Exception as exc:
    print(f"{url}: unreachable (urllib, {type(exc).__name__})")
PY
  else
    echo "$url: unknown (neither curl nor python3)"
  fi
}
for host in static.rust-lang.org index.crates.io registry.npmjs.org pypi.org github.com; do
  probe_url "https://$host/"
done

exit 0
