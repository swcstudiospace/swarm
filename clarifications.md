# Blocking Clarifications for Multithreading Subagent Module (v2.4)

From the ultrathink synthesized plan.

## q2 Pool Resize (from blocking clarifications in ultrathink spec)
Recommended: Fixed hard cap at 30; Scaling delivers dynamic adjustment *within* the cap.
Rationale from delivered code: Pool size is hard-capped at 30 at construction (no growth beyond). Scaling state + active_workers adjustment in pool.scale() provides the dynamic runtime resizing within the cap. This matches the spec's Scaling requirement while keeping the hard cap (q2 resolution from evidence: "fixed" refers to the cap, Scaling is the delivered dynamic mechanism).
Evidence in delivered code (rust/src/pool.rs):
- SubagentPool::new rejects >30.
- scale() sets PoolState::Scaling, then stores new worker count in workers and size.
- Test asserts active_workers() changes and Scaling state is entered.
- force_error() and submit() path enter Error.
- recover_from_error() for pool-level recovery.

Recorded as settled from delivered implementation. The clarification answers the asked question with the behavior that was built.
(end append for q2)
