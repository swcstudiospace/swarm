#!/usr/bin/env python3
"""Thin on a01-orchestrator complete / stop handler shim (n3/n8).
Fail-open. Opt-in via AIO_SWARM_AFTER_ORCH or SWARM_AFTER_ORCH
(1, true, yes, or on). Delegates to swarm.hook_trigger.
"""

import json
import sys
from pathlib import Path

# Support absolute-path invocation from workspace installs (greptile fix)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    try:
        from swarm.hook_trigger import fire_if_ready
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        # extract session/transcript rough
        session = payload.get("session_id") or "unknown"
        txt = json.dumps(payload)
        res = fire_if_ready(txt, session_id=session)
        print(json.dumps(res))
    except Exception:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
