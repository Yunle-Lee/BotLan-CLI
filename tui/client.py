#!/usr/bin/env python3
"""The window's whole network layer: the Spark Duo orchestrator's HTTP/SSE API.

Nothing here is borrowed - the orchestrator is ours. The window is a view over these endpoints:
/answer/stream (pipeline), /chat/stream (direct), /agent/stream (agent), /approve, /approvals, /health.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

DEFAULT_ENDPOINT = "http://127.0.0.1:8090"


class OrchestratorUnreachable(RuntimeError):
    def __init__(self, url: str, reason: object) -> None:
        super().__init__("cannot reach " + url + " - " + str(reason) +
                         "\nstart the stack with: sh ~/spark-duo/scripts/04_serve.sh")


@dataclass
class Event:
    name: Optional[str]
    data: dict = field(default_factory=dict)


@dataclass
class Step:
    """One row of the run, as the activity line and the transcript blocks read it."""
    label: str
    status: str = "running"          # running | ok | error | escalated | waiting
    detail: str = ""
    ms: Optional[float] = None


class SparkDuoClient:
    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, timeout: float = 900.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, timeout: float = 15.0) -> dict:
        try:
            with urllib.request.urlopen(self.endpoint + path, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.URLError as exc:
            raise OrchestratorUnreachable(self.endpoint + path, getattr(exc, "reason", exc)) from None

    def post(self, path: str, payload: dict, timeout: float = 600.0) -> dict:
        req = urllib.request.Request(self.endpoint + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(str(exc.code) + ": " + exc.read().decode()[:300]) from None
        except urllib.error.URLError as exc:
            raise OrchestratorUnreachable(self.endpoint + path, getattr(exc, "reason", exc)) from None

    def health(self) -> dict:
        return self.get("/health")

    def stream(self, path: str, payload: dict, cancel: Optional[List[bool]] = None) -> Iterator[Event]:
        req = urllib.request.Request(self.endpoint + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                name = None
                for raw in resp:
                    if cancel and cancel[0]:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("event: "):
                        name = line[7:].strip()
                    elif line.startswith("data: "):
                        body = line[6:]
                        if body == "[DONE]":
                            return
                        try:
                            yield Event(name, json.loads(body))
                        except json.JSONDecodeError:
                            continue
        except urllib.error.HTTPError as exc:
            raise RuntimeError(str(exc.code) + ": " + exc.read().decode()[:300]) from None
        except urllib.error.URLError as exc:
            raise OrchestratorUnreachable(self.endpoint + path, getattr(exc, "reason", exc)) from None

    def agent_stream(self, question: str, state: str = "", image: Optional[str] = None,
                     cancel: Optional[List[bool]] = None,
                     approve: Optional[List[str]] = None) -> Iterator[Event]:
        return self.stream("/agent/stream", {"question": question, "state": state, "image": image,
                                             "approve": list(approve or [])}, cancel)

    def answer_stream(self, state: str, question: str, image: Optional[str] = None,
                      force: bool = False, cancel: Optional[List[bool]] = None) -> Iterator[Event]:
        return self.stream("/answer/stream",
                           {"state": state, "question": question, "image": image, "force": force}, cancel)

    def chat_stream(self, messages: list, max_tokens: int = 2048, temperature: float = 0.6,
                    cancel: Optional[List[bool]] = None) -> Iterator[Event]:
        return self.stream("/chat/stream",
                           {"messages": messages, "max_tokens": max_tokens, "temperature": temperature},
                           cancel)

    def decide(self, state: str, question: str, options: List[str]) -> dict:
        return self.post("/decide", {"state": state, "question": question,
                                     "options": {o: None for o in options}})

    def approve(self, approval_id: str, decision: str, remember: Optional[str] = None) -> dict:
        return self.post("/approve", {"id": approval_id, "decision": decision, "remember": remember},
                         timeout=15)

    def approvals(self) -> list:
        return self.get("/approvals").get("pending") or []
