# Blocking Clarifications for Multithreading Subagent Module (v2.4)

From the ultrathink synthesized plan.

## q2 Pool Resize (delivered behaviour)

Pool size is hard-capped at 30 at construction (no growth beyond). Scaling state + active_workers adjustment in pool.scale() provides the dynamic runtime resizing within the cap. This matches the spec's Scaling requirement while keeping the hard cap (q2 resolution from evidence: "fixed" refers to the cap, Scaling is the delivered dynamic mechanism).

Evidence in delivered code (rust/src/pool.rs):
- SubagentPool::new rejects >30.
- scale() resizes the worker list and updates the count (real resizing).
- submit() drives dynamic resizing on high load via scale() (real dispatch path).
- force_error() and submit() path enter Error.
- recover_from_error() for pool-level recovery.

Recorded as the delivered behaviour. The clarification records the behaviour that was built.
(end append for q2)
