#!/usr/bin/env python3
"""Surfaces: one agent loop, several places it can be driven from.

Ported from proxysoul/Empryo src/hearth/types.ts (the Surface contract) and src/hearth/surface-host.ts
(the supervisor). The host is dumb on purpose: it builds, starts, stops and reloads surfaces and pushes
render/notify/approval at one of them - it knows nothing about tabs, sessions or tools. That split is
what lets the same loop be driven from a terminal today and from a phone surface later without
touching the loop.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from agent.approvals import ApprovalRegistry


@dataclass
class ApprovalUI:
    approval_id: str
    tool_name: str
    summary: str
    cwd: str
    tab_id: Optional[str] = None
    tool_input: dict = field(default_factory=dict)


@dataclass
class InboundMessage:
    external_id: str
    text: str = ""
    command: Optional[str] = None
    image: Optional[str] = None


@dataclass
class SurfaceRenderInput:
    external_id: str
    tab_id: str
    event: dict


class Surface(ABC):
    """The contract every surface implements - the port of the Surface interface."""

    kind = "surface"

    def __init__(self, surface_id: str) -> None:
        self.id = surface_id
        self._inbound: Optional[Callable[[InboundMessage], None]] = None

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def render(self, req: SurfaceRenderInput) -> None: ...

    @abstractmethod
    def request_approval(self, external_id: str, ui: ApprovalUI) -> dict: ...

    @abstractmethod
    def notify(self, external_id: str, message: str) -> None: ...

    def send_pairing_prompt(self, external_id: str, code: str) -> bool:
        """Only surfaces that reach a human out of band (telegram, discord) need this."""
        return False

    def on_inbound(self, handler: Callable[[InboundMessage], None]) -> None:
        self._inbound = handler

    def deliver_inbound(self, msg: InboundMessage) -> None:
        if self._inbound:
            self._inbound(msg)


class SseSurface(Surface):
    """The terminal surface: events fan out to whichever clients are attached, and an approval parks
    the caller until a client (or the approve CLI) answers it.

    This is the role the TUI plays in Empryo - except there the TUI is in-process and the daemon is the
    fallback owner; here the orchestrator owns everything and the TUI is a subscriber, which is the
    same relationship with one less process to keep alive.
    """

    kind = "tui"

    def __init__(self, surface_id: str = "tui:local", registry: Optional[ApprovalRegistry] = None,
                 timeout_ms: int = 5 * 60_000) -> None:
        super().__init__(surface_id)
        self.registry = registry or ApprovalRegistry()
        self.timeout_ms = timeout_ms
        self._clients: Dict[str, List] = {}
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False
        with self._lock:
            self._clients.clear()

    def is_connected(self) -> bool:
        with self._lock:
            return self._started and bool(self._clients)

    def attach(self, external_id: str, queue: List) -> None:
        with self._lock:
            self._clients.setdefault(external_id, []).append(queue)

    def detach(self, external_id: str, queue: List) -> None:
        with self._lock:
            clients = self._clients.get(external_id) or []
            if queue in clients:
                clients.remove(queue)
            if not clients:
                self._clients.pop(external_id, None)

    def render(self, req: SurfaceRenderInput) -> None:
        with self._lock:
            clients = list(self._clients.get(req.external_id, []))
        for q in clients:
            q.append({"tab_id": req.tab_id, **req.event})

    def notify(self, external_id: str, message: str) -> None:
        self.render(SurfaceRenderInput(external_id, "system", {"type": "message", "text": message}))

    def request_approval(self, external_id: str, ui: ApprovalUI) -> dict:
        """Park until somebody answers. A timeout is a denial, never an implicit yes."""
        answer: List[dict] = []
        done = threading.Event()

        def resolve(res: dict) -> None:
            answer.append(res)
            done.set()

        entry = self.registry.register(
            session_id=ui.tab_id or external_id, tool_name=ui.tool_name, tool_call_id=ui.approval_id,
            cwd=ui.cwd, summary=ui.summary, tab_id=ui.tab_id, tool_input=ui.tool_input,
            timeout_ms=self.timeout_ms, approval_id=ui.approval_id, resolve=resolve)
        self.render(SurfaceRenderInput(external_id, ui.tab_id or "system",
                                      {"type": "approval_request", **entry.public()}))
        if not done.wait(self.timeout_ms / 1000 + 1):
            self.registry.resolve(entry.id, "deny", "approval timed out")
            done.wait(1)
        res = answer[0] if answer else {"decision": "deny", "reason": "no answer"}
        return {"approval_id": entry.id, **res}


class SurfaceHost:
    """Owner-agnostic supervisor - the port of SurfaceHost. No tab, session or tool knowledge."""

    def __init__(self, log: Optional[Callable[[str], None]] = None) -> None:
        self.surfaces: Dict[str, Surface] = {}
        self.router: Optional[Callable[[str, InboundMessage], None]] = None
        self.log = log or (lambda line: None)
        self._started = False

    def register(self, surface: Surface) -> None:
        self.surfaces[surface.id] = surface
        surface.on_inbound(lambda msg, sid=surface.id: self.router and self.router(sid, msg))

    def set_router(self, router: Callable[[str, InboundMessage], None]) -> None:
        self.router = router

    def start(self) -> dict:
        if self._started:
            return {"ok": [], "failed": []}
        self._started = True
        ok, failed = [], []
        for sid, surface in self.surfaces.items():
            try:
                surface.start()
                ok.append(sid)
            except Exception as exc:
                failed.append({"id": sid, "error": f"{type(exc).__name__}: {exc}"})
        return {"ok": ok, "failed": failed}

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        for surface in list(self.surfaces.values()):
            try:
                surface.stop()
            except Exception as exc:
                self.log(f"stop {surface.id}: {exc}")

    def reload(self, desired: List[Surface]) -> dict:
        """Stop what is gone, (re)start what is wanted - the original rebuilds so live config edits
        take effect immediately."""
        started, stopped, errors = [], [], []
        want = {s.id for s in desired}
        for sid in list(self.surfaces):
            if sid in want:
                continue
            try:
                self.surfaces[sid].stop()
            except Exception as exc:
                errors.append({"id": sid, "error": str(exc)})
            self.surfaces.pop(sid, None)
            stopped.append(sid)
        for surface in desired:
            self.register(surface)
            try:
                surface.start()
                started.append(surface.id)
            except Exception as exc:
                errors.append({"id": surface.id, "error": str(exc)})
        return {"started": started, "stopped": stopped, "errors": errors}

    def get(self, surface_id: str) -> Optional[Surface]:
        return self.surfaces.get(surface_id)

    def list(self) -> List[Surface]:
        return list(self.surfaces.values())

    def render(self, surface_id: str, external_id: str, event: dict, tab_id: str = "default") -> None:
        surface = self.surfaces.get(surface_id)
        if not surface:
            return
        try:
            surface.render(SurfaceRenderInput(external_id, tab_id, event))
        except Exception as exc:
            self.log(f"render {surface_id}/{external_id}: {exc}")

    def notify(self, surface_id: str, external_id: str, message: str) -> None:
        surface = self.surfaces.get(surface_id)
        if surface:
            try:
                surface.notify(external_id, message)
            except Exception as exc:
                self.log(f"notify {surface_id}/{external_id}: {exc}")

    def request_approval(self, surface_id: str, external_id: str, ui: ApprovalUI) -> dict:
        surface = self.surfaces.get(surface_id)
        if not surface:
            return {"decision": "deny", "reason": f"no surface {surface_id}"}
        try:
            return surface.request_approval(external_id, ui)
        except Exception as exc:
            self.log(f"approval {surface_id}/{external_id}: {exc}")
            return {"decision": "deny", "reason": str(exc)}
