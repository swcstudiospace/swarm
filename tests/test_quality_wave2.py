"""Red tests from the 2026-11-02 refutation wave (swarm-a group). No existing module owns swarm/errors.py."""
from swarm.errors import ErrorCode, classify


def test_classify_permission_error_is_e_policy():
    """classify checks OSError before PermissionError, and PermissionError subclasses OSError, so the E_POLICY branch
    is unreachable and the call returns E_DEP. Expected to fail today: E_DEP is returned instead of E_POLICY."""
    assert classify(PermissionError(13, "x")) is ErrorCode.E_POLICY
