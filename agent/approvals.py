#!/usr/bin/env python3
"""Approval registry - pending permission requests keyed by id, so any surface can resolve one.

Ported from proxysoul/Empryo src/hearth/approvals.ts (same contract, same defaults): a TTL per
request, a sweeper that denies what expired, a hard cap that denies new requests rather than evicting
older waiters, and cancel-for-session when a tab goes away. Deny is always the default - an approval
nobody answered is a no, and the loop must never block forever on a human who went to lunch.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

DEFAULT_TIMEOUT_MS = 5 * 60_000
MAX_PENDING = 256
SWEEP_INTERVAL_S = 30.0


@dataclass
class PendingApproval:
    id: str
    session_id: str
    tool_name: str
    tool_call_id: str
    cwd: str
    summary: str = ""
    tab_id: Optional[str] = None
    tool_input: dict = field(default_factory=dict)
    created_at: float = 0.0
    expires_at: float = 0.0
    resolve: Optional[Callable[[dict], None]] = None

    def ui(self) -> dict:
        """What an approver is shown - the port of ApprovalUI."""
        return {"approval_id": self.id, "tool_name": self.tool_name, "summary": self.summary,
                "cwd": self.cwd, "tab_id": self.tab_id, "tool_input": self.tool_input}

    def public(self) -> dict:
        d = self.ui()
        d.update({"id": self.id, "session_id": self.session_id, "created_at": self.created_at,
                  "expires_at": self.expires_at})
        return d


class ApprovalRegistry:
    def __init__(self, default_timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 sweep_interval_s: float = SWEEP_INTERVAL_S) -> None:
        self.pending: Dict[str, PendingApproval] = {}
        self.default_timeout_ms = default_timeout_ms
        self._lock = threading.Lock()
        self._stopped = False
        self._timer = threading.Timer(sweep_interval_s, self._tick)
        self._timer.daemon = True
        self._timer.start()

    # -- registering ---------------------------------------------------------------------------
    def register(self, *, session_id: str, tool_name: str, tool_call_id: str, cwd: str,
                 summary: str = "", tab_id: Optional[str] = None, tool_input: Optional[dict] = None,
                 timeout_ms: Optional[int] = None, approval_id: Optional[str] = None,
                 resolve: Optional[Callable[[dict], None]] = None) -> PendingApproval:
        """approval_id lets the caller name the request, which is how the id the loop already
        announced to its client is the same id an approver resolves - no second identifier to
        correlate. Two live requests that reuse an id would collide, so callers qualify it."""
        now = time.time()
        entry = PendingApproval(
            id=approval_id or uuid.uuid4().hex, session_id=session_id, tool_name=tool_name,
            tool_call_id=tool_call_id, cwd=cwd, summary=summary, tab_id=tab_id,
            tool_input=tool_input or {}, created_at=now,
            expires_at=now + (timeout_ms if timeout_ms is not None else self.default_timeout_ms) / 1000,
            resolve=resolve)
        with self._lock:
            if len(self.pending) >= MAX_PENDING:
                # Refuse the new request instead of pushing out an older waiter: a runaway tool loop
                # must not be able to starve the approvals a human is actually looking at.
                self._settle(entry, {"decision": "deny", "reason": "approval registry full"})
                return entry
            self.pending[entry.id] = entry
        return entry

    # -- resolving ------------------------------------------------------------------------------
    def resolve(self, approval_id: str, decision: str, reason: Optional[str] = None,
                remember: Optional[str] = None) -> bool:
        with self._lock:
            entry = self.pending.pop(approval_id, None)
        if entry is None:
            return False
        self._settle(entry, {"decision": decision, "reason": reason, "remember": remember})
        return True

    def cancel_for_session(self, session_id: str, reason: str = "session ended") -> int:
        with self._lock:
            ids = [i for i, x in self.pending.items() if x.session_id == session_id]
        for i in ids:
            self.resolve(i, "deny", reason)
        return len(ids)

    def _settle(self, entry: PendingApproval, response: dict) -> None:
        if entry.resolve is None:
            return
        try:
            entry.resolve(response)
        except Exception:                     # a resolver must never take the registry down
            pass

    # -- introspection --------------------------------------------------------------------------
    def get(self, approval_id: str) -> Optional[PendingApproval]:
        return self.pending.get(approval_id)

    def list(self) -> List[PendingApproval]:
        return list(self.pending.values())

    def count(self) -> int:
        return len(self.pending)

    # -- expiry ---------------------------------------------------------------------------------
    def sweep_expired(self) -> int:
        now = time.time()
        with self._lock:
            expired = [i for i, x in self.pending.items() if x.expires_at <= now]
        for i in expired:
            self.resolve(i, "deny", "approval timed out")
        return len(expired)

    def _tick(self) -> None:
        if self._stopped:
            return
        try:
            self.sweep_expired()
        finally:
            if not self._stopped:
                self._timer = threading.Timer(SWEEP_INTERVAL_S, self._tick)
                self._timer.daemon = True
                self._timer.start()

    def stop(self) -> None:
        self._stopped = True
        try:
            self._timer.cancel()
        except Exception:
            pass
        for i in [x.id for x in self.list()]:
            self.resolve(i, "deny", "shutting down")


class ApprovalPolicy:
    """autoApprove / autoDeny lists plus remembered decisions - the port of the per-chat binding
    fields (autoApprove, autoDeny) and of PermissionResponse.remember: once | session | always."""

    def __init__(self, auto_approve=(), auto_deny=(), switch_on: bool = False) -> None:
        self.auto_approve = set(auto_approve)
        self.auto_deny = set(auto_deny)
        self.switch_on = switch_on           # allow_write / allow_shell from the config
        self.session_rules: Dict[str, str] = {}
        self.always_rules: Dict[str, str] = {}

    def decide(self, tool_name: str) -> Optional[str]:
        """None means 'ask a human'. Deny wins over allow, and deny-list beats the allow-list."""
        if tool_name in self.auto_deny or self.always_rules.get(tool_name) == "deny":
            return "deny"
        if tool_name in self.session_rules:
            return self.session_rules[tool_name]
        if tool_name in self.always_rules:
            return self.always_rules[tool_name]
        if tool_name in self.auto_approve:
            return "allow"
        return None

    def remember(self, tool_name: str, decision: str, scope: Optional[str]) -> None:
        if scope == "session":
            self.session_rules[tool_name] = decision
        elif scope == "always":
            self.always_rules[tool_name] = decision

    def clear_session(self) -> None:
        self.session_rules.clear()
