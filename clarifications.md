# Blocking Clarifications for Multithreading Subagent Module (v2.4)

## q2 Pool Resize (user answer)

User answer: Dynamic resizing (within the hard cap at creation).

Rationale from delivered code: Pool size is hard-capped at 30 at construction (no growth beyond). Scaling delivers dynamic adjustment within the cap (worker list is resized on scale, submit on high load drives the resize).

Evidence:
- new rejects >30
- scale resizes worker_ids list and spawns workers
- submit drives resize on load via scale (real dispatch path)
- recover_from_error for Error recovery

Recorded as the user answer. The clarification records the choice that matches the delivered behaviour.
(end append for q2)
