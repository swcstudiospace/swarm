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


def test_numeric_and_boolean_values_under_a_sensitive_key_are_redacted():
    out = redact({"api_key": 4821937, "token": 0.5, "secret": True})
    assert out == {"api_key": REDACTED, "token": REDACTED, "secret": REDACTED}


def test_non_sensitive_scalars_are_unchanged():
    assert redact({"n": 3, "ok": True, "ratio": 0.5}) == {"n": 3, "ok": True, "ratio": 0.5}


def test_none_under_a_sensitive_key_stays_none():
    assert redact({"api_key": None}) == {"api_key": None}
