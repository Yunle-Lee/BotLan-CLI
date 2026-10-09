#!/usr/bin/env python3
"""TabLoop - one long-running agent loop per tab (session).

Ported from proxysoul/Empryo src/hearth/tab-loop.ts. The properties worth keeping:
  * prompts arrive from a surface through a queue, and a prompt enqueued while a turn runs is served by
    the same loop on its next turn instead of racing it;
  * the queue is capped and drops the oldest entry, so a flooding client cannot grow the daemon;
  * abort renews the handle BEFORE forwarding, because the next turn attaches to a fresh handle -
    aborting the one the loop already saw would leave the following turn unabortable;
  * closing aborts and then releases every parked waiter, so nothing hangs on a dead tab.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

MAX_QUEUED_PROMPTS = 200


@dataclass
class QueuedPrompt:
    id: str
    text: str
    enqueued_at: float
    payload: dict = field(default_factory=dict)
    events: List[dict] = field(default_factory=list)     # what the surface drains while the turn runs
    finished: threading.Event = field(default_factory=threading.Event)


class TabLoop:
    def __init__(self, tab_id: str, label: str = "", executor: Optional[Callable] = None) -> None:
        self.tab_id = tab_id
        self.label = label or tab_id
        self.executor = executor          # executor(prompt, emit, cancel) -> None
        self.queue: List[QueuedPrompt] = []
        self.waiters: List[Callable] = []
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._closed = False
        self._started = False
        self._thread: Optional[threading.Thread] = None
        self._current: Optional[QueuedPrompt] = None
        self.last_error: Optional[str] = None

    # -- lifecycle ------------------------------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._closed:
            prompt = self._read_prompt()
            if prompt is None:
                break
            self._current = prompt
            self._cancel = threading.Event()
            try:
                self.executor(prompt, prompt.events.append, self._cancel)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                prompt.events.append({"type": "error", "error": self.last_error})
            finally:
                prompt.events.append({"type": "done"})
                prompt.finished.set()
                self._current = None
        self._closed = True
        with self._lock:
            waiters = self.waiters[:]
            self.waiters.clear()
        for w in waiters:
            try:
                w(None)
            except Exception:
                pass

    # -- prompts --------------------------------------------------------------------------------
    def enqueue_prompt(self, text: str, payload: Optional[dict] = None) -> Optional[str]:
        prompt = QueuedPrompt(uuid.uuid4().hex, text, time.time(), payload or {})
        with self._lock:
            if self._closed:
                return None
            waiter = self.waiters.pop(0) if self.waiters else None
            if waiter is None and len(self.queue) >= MAX_QUEUED_PROMPTS:
                self.queue.pop(0)          # drop the oldest rather than reject the newest
            if waiter is None:
                self.queue.append(prompt)
        if waiter is not None:
            waiter(prompt)
        return prompt.id

    def read_prompt(self, timeout: float = 0.0) -> Optional[QueuedPrompt]:
        """Non-blocking by default; with a timeout it parks a waiter like the original readPrompt."""
        with self._lock:
            if self._closed:
                return None
            if self.queue:
                return self.queue.pop(0)
        if timeout <= 0:
            return None
        ready = threading.Event()
        box: List[Optional[QueuedPrompt]] = []

        def wait(prompt):
            box.append(prompt)
            ready.set()

        with self._lock:
            if self._closed:
                return None
            self.waiters.append(wait)
        ready.wait(timeout)
        with self._lock:
            if wait in self.waiters:
                self.waiters.remove(wait)
        if box and box[0] is not None:
            return box[0]
        return None

    def _read_prompt(self) -> Optional[QueuedPrompt]:
        while not self._closed:
            prompt = self.read_prompt(1.0)
            if prompt is not None:
                return prompt
        return None

    # -- control --------------------------------------------------------------------------------
    def abort_turn(self) -> None:
        """Renew before forwarding: the running turn holds the old handle and the next turn gets a
        fresh one, so an interrupt cannot leave the following turn permanently cancelled."""
        current = self._cancel
        self._cancel = threading.Event()
        current.set()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        with self._lock:
            waiters = self.waiters[:]
            self.waiters.clear()
        for w in waiters:
            try:
                w(None)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    # -- introspection --------------------------------------------------------------------------
    def is_closed(self) -> bool:
        return self._closed

    def is_busy(self) -> bool:
        return self._current is not None

    def queue_length(self) -> int:
        with self._lock:
            return len(self.queue)

    def waiter_count(self) -> int:
        with self._lock:
            return len(self.waiters)

    def last_error_message(self) -> Optional[str]:
        return self.last_error


class TabRegistry:
    """One TabLoop per session id, created on demand, oldest evicted past max_tabs."""

    def __init__(self, executor: Callable, max_tabs: int = 5) -> None:
        self.executor = executor
        self.max_tabs = max_tabs
        self.tabs: Dict[str, TabLoop] = {}
        self._lock = threading.Lock()

    def get_or_create(self, tab_id: str, label: str = "") -> TabLoop:
        with self._lock:
            loop = self.tabs.get(tab_id)
            if loop is not None:
                return loop
            if len(self.tabs) >= self.max_tabs:
                oldest = next(iter(self.tabs))
                self.tabs.pop(oldest).close()
            loop = TabLoop(tab_id, label, self.executor)
            self.tabs[tab_id] = loop
            loop.start()
            return loop

    def close(self, tab_id: str) -> bool:
        with self._lock:
            loop = self.tabs.pop(tab_id, None)
        if loop is None:
            return False
        loop.close()
        return True

    def close_all(self) -> None:
        for loop in list(self.tabs.values()):
            loop.close()
        self.tabs.clear()

    def status(self) -> List[dict]:
        return [{"tab_id": t.tab_id, "label": t.label, "busy": t.is_busy(),
                 "queued": t.queue_length(), "closed": t.is_closed()} for t in self.tabs.values()]
