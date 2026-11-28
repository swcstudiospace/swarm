"""Fakes-only tracker adapters (W2.5) — hard-deny live transport.

Design: docs/design-orchestration-SPE-38340.md §§5, 7, C1/R-P2 (plan-CW2).
Layer: data (implements scheduler_ports.TrackerPort).

- Only ``fake://`` base URLs are accepted. Any other URL (real Linear /
  Notion host, any https host) is refused with ``E-DEP`` *before* any
  byte is sent — no flag can override this (crate has no live transport).
- Linear adapter is the fake *workflow* store (TBD IDs, create-or-return by
  idempotency key) — the workflow-status side of §5.
- Notion adapter is read-only *context*: writes raise ``E-POLICY``.
- Transport failure modes are injectable (offline / lost reply) so
  sync_queue tests prove T-OFFLINE-* without a network.

Reuse: errors from ``swarm.errors``; fake-ID scheme ``TBD-LIN-v24-…``
from plan-CW2 §1 (also produced by issue_mapper — same scheme, one
definition here as ``fake_id_for`` to keep both sides byte-identical).

GAPS recorded:
- G-TRACK-1: No tracker adapter existed at all. Added (fake-only).
- G-TRACK-2: No Notion read path existed. Added in-memory read-only.
"""
from __future__ import annotations

import re
from typing import Any

from .errors import ErrorCode, SwarmError

__all__ = ["FakeTrackerAdapter", "FakeNotionAdapter", "fake_id_for", "LiveDenyError"]

_TBD_RE = re.compile(r"^TBD-LIN-v24-[0-9.]+-[PW][0-9]*$|^TBD-[A-Z]+-\d+$")


class LiveDenyError(SwarmError):
    """Refusal of a non-fake tracker base URL (terminal, never retried)."""

    def __init__(self, base_url: str):
        super().__init__(ErrorCode.E_DEP, f"live tracker transport denied: {base_url!r} is not fake://", field="base_url")


def _require_fake_base(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.startswith("fake://"):
        raise LiveDenyError(str(base_url))
    return base_url


def fake_id_for(phase_id: str, *, child_index: int | None = None) -> str:
    """Deterministic fake ID (plan-CW2 §1): parent ``…-P``, child ``…-W<k>``."""
    if not re.match(r"^[0-9]+(\.[0-9]+)*$", phase_id or ""):
        raise SwarmError(ErrorCode.E_INPUT, f"invalid phase_id {phase_id!r}", field="phase_id")
    if child_index is None:
        return f"TBD-LIN-v24-{phase_id}-P"
    if not isinstance(child_index, int) or child_index < 1:
        raise SwarmError(ErrorCode.E_INPUT, "child_index must be >= 1", field="child_index")
    return f"TBD-LIN-v24-{phase_id}-W{child_index:02d}"


class FakeTrackerAdapter:
    """In-memory fake Linear workflow store (TrackerPort).

    ``online=False`` simulates offline: every delivery raises
    ``SwarmError(E-DEP, 'transport')`` (transient for sync_queue).
    ``lost_reply=True`` simulates an accepted write whose reply is lost:
    the record is stored, then ``E-TIMEOUT`` is raised so the caller's
    retry must prove create-or-return (T-OFFLINE-REPLAY).
    """

    def __init__(self, base_url: str = "fake://linear", *, online: bool = True,
                 lost_reply: bool = False):
        self.base_url = _require_fake_base(base_url)
        self.online = online
        self.lost_reply = lost_reply
        self._by_key: dict[str, dict[str, Any]] = {}
        self._by_id: dict[str, dict[str, Any]] = {}
        self.deliveries: int = 0  # counts transport attempts (tests assert retry counts)

    def _check_online(self) -> None:
        self.deliveries += 1
        if not self.online:
            raise SwarmError(ErrorCode.E_DEP, "fake transport offline", field="transport")

    def create_or_return(self, *, idempotency_key: str, fake_id: str,
                         body: dict[str, Any]) -> dict[str, Any]:
        """Create-or-return by idempotency key (§7.2). Replay ⇒ same record."""
        if not idempotency_key or not fake_id:
            raise SwarmError(ErrorCode.E_INPUT, "idempotency_key and fake_id required", field="idempotency_key")
        self._check_online()
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            return {**existing, "deduped": True}
        record = {"fake_id": fake_id, "tbd_real_id": None, "status_fake": body.get("status_fake", "open"),
                  "idempotency_key": idempotency_key, "body": dict(body)}
        self._by_key[idempotency_key] = record
        self._by_id[fake_id] = record
        if self.lost_reply:
            self.lost_reply = False
            raise SwarmError(ErrorCode.E_TIMEOUT, "fake reply lost after storing", field="transport")
        return {**record, "deduped": False}

    def update(self, *, idempotency_key: str, fake_id: str,
               body: dict[str, Any]) -> dict[str, Any]:
        """Update under the update key; unknown fake_id ⇒ E-INPUT (orphan signal)."""
        if not idempotency_key:
            raise SwarmError(ErrorCode.E_INPUT, "idempotency_key required", field="idempotency_key")
        self._check_online()
        record = self._by_id.get(fake_id)
        if record is None:
            raise SwarmError(ErrorCode.E_INPUT, f"unknown fake_id {fake_id!r}", field="fake_id")
        if body.get("status_fake") is not None:
            record["status_fake"] = body["status_fake"]
        record["body"] = dict(body)
        return {**record, "deduped": False}

    def get(self, fake_id: str) -> dict[str, Any] | None:
        record = self._by_id.get(fake_id)
        return dict(record) if record else None

    def list_all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._by_id.values()]

    def seed(self, fake_id: str, *, status_fake: str = "open") -> dict[str, Any]:
        """Direct seed (reconciliation fixtures). Offline mode does not block reads."""
        record = {"fake_id": fake_id, "tbd_real_id": None, "status_fake": status_fake,
                  "idempotency_key": f"seed:{fake_id}", "body": {}}
        self._by_id[fake_id] = record
        self._by_key[record["idempotency_key"]] = record
        return dict(record)


class FakeNotionAdapter:
    """Read-only fake Notion context (§5). No write path exists."""

    def __init__(self, base_url: str = "fake://notion", *, page_ref: str = "https://app.notion.com/p/40abc1a0c7ae81b588b7e01197a8d77c"):
        self.base_url = _require_fake_base(base_url)
        self.page_ref = page_ref
        self._pages: dict[str, str] = {}

    def read_page(self, page_ref: str) -> str:
        return self._pages.get(page_ref, "")

    def add_page(self, page_ref: str, text: str) -> None:
        self._pages[page_ref] = text

    def write_page(self, page_ref: str, text: str) -> None:
        raise SwarmError(ErrorCode.E_POLICY, "Notion adapter is read-only (C1)", field="page_ref")
