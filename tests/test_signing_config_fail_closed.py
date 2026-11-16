"""P1-05: a malformed SWARM_ED25519_KEY is a configuration error even when SWARM_SIGNING_KEY is also set.

Python used to exempt that pair and sign with the HMAC key. The verdict is refused instead, so no `hmac:` signature is written.
"""

from __future__ import annotations

import pytest  # type: ignore[import-not-found, unused-ignore]

from swarm.errors import ErrorCode, SwarmError
from swarm.gates import make_verdict


def test_bad_ed25519_seed_beside_an_hmac_key_refuses_to_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_SIGNING_KEY", "k")
    monkeypatch.setenv("SWARM_ED25519_KEY", "not-a-seed")
    with pytest.raises(SwarmError) as caught:
        make_verdict(gate="review", task_id="T-1", agent_id="A09", correlation_id="C-1", audit=False)
    assert caught.value.code is ErrorCode.E_POLICY
    assert "SWARM_ED25519_KEY is set but cannot be loaded" in str(caught.value)
