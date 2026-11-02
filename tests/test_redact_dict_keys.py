"""P2-13: a secret used as a dict key must not survive redact()."""
from swarm.redact import REDACTED, redact


def test_bearer_token_in_a_dict_key_is_redacted():
    out = redact({"Authorization: Bearer abcdefghijklmnopqrstu": 1})
    assert "abcdefghijklmnopqrstu" not in repr(out)
    assert out == {f"Authorization: Bearer {REDACTED}": 1}


def test_key_redaction_is_idempotent():
    once = redact({"Authorization: Bearer abcdefghijklmnopqrstu": 1})
    assert redact(once) == once


def test_non_string_keys_pass_through_unchanged():
    assert redact({1: "plain"}) == {1: "plain"}
