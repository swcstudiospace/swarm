# Blocking Clarifications for Multithreading Subagent Module (v2.4)

## q2 Pool Resize (delivered behaviour)

Pool size is hard-capped at 30 at construction. Scaling delivers dynamic adjustment within the cap (worker list is resized on scale, submit on high load drives the resize).

Evidence:
- new rejects >30
- scale resizes worker_ids list and updates count
- submit drives resize on load via scale
- recover_from_error for Error recovery

Recorded as the delivered behaviour.
(end append for q2)
