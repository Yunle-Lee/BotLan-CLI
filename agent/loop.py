#!/usr/bin/env python3
"""The agent's step loop.

One streaming model turn at a time, with tool calls executed in between. The loop owns no HTTP and no
model handle - it is handed a turn() generator and a Registry - which is what makes it testable
against a canned model and keeps the transport in one place (orchestrator.vlm_turn_stream).

It is a generator: every step arrives as an (event, data) pair so the TUI rail is live, and the final
answer rides in the last ("done", {...}) event. One event, "evidence", is internal - it carries what
the tools actually returned so stage 3 can check the answer against it rather than against an empty
context - and the SSE handler deliberately does not forward it.

Verified before writing this: llama-server streams tool calls incrementally for GELab
(finish_reason == "tool_calls", name and arguments assembled from deltas), so no output parsing and
no retry loop is needed.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from agent.tools import Registry

DEFAULT_SYSTEM = ("You are an agent working inside a sandboxed workspace. Use a tool when you need to "
                  "look at something; never guess file contents. When you have enough, answer directly "
                  "and cite the file you read.")

# Measured: asked to list a directory with nothing in the state, this 4B replies "what is the path?"
# and answers from nothing. One nudge fixes that case; more than one just burns steps.
NUDGE = ("You have not used a tool. Do not ask the user for a path - find it yourself. Call a tool "
         "now, and only answer directly if the request genuinely needs no lookup.")


class AgentLoop:
    def __init__(self, registry: Registry, turn: Callable, config: Optional[dict] = None,
                 system: Optional[str] = None, on_approval: Optional[Callable] = None) -> None:
        cfg = config or {}
        self.registry = registry
        self.turn = turn                                  # turn(messages, tools, max_tokens) -> iterator
        # on_approval(tool, arguments, call_id) -> (decision, reason). It BLOCKS: the port of the
        # onApproveDestructive / onApproveOutsideCwd seams in the original TabLoop. The approval
        # itself is offered to the surface as an event before this is called, so a client can answer
        # while the loop waits.
        self.on_approval = on_approval
        self.system = system or cfg.get("system") or DEFAULT_SYSTEM
        self.max_steps = int(cfg.get("max_steps", 6))
        self.max_seconds = float(cfg.get("max_seconds", 180))
        self.max_tokens = int(cfg.get("max_tokens", 640))
        self.max_repeats = int(cfg.get("max_repeats", 2))
        self.nudge = bool(cfg.get("nudge", True))

    def run(self, question: str, state: str = "", history: Optional[List[dict]] = None,
            image: Optional[str] = None, cancel: Optional[List[bool]] = None,
            approved: Optional[set] = None) -> Iterator[Tuple[str, dict]]:
        """approved is the set of tool names the caller has authorised for this run - the model can
        propose a mutating call, but only a decision made outside the loop lets it through."""
        t0 = time.perf_counter()
        messages = list(history or [{"role": "system", "content": self.system}])
        prompt = question if not state else f"Context:\n{state}\n\nRequest: {question}"
        if image:
            messages.append({"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image}}]})
        else:
            messages.append({"role": "user", "content": prompt})

        steps: List[dict] = []
        seen: Dict[tuple, int] = {}
        answer, stop = "", "answer"
        tool_calls_made = 0
        nudged = False

        for n in range(1, self.max_steps + 1):
            if cancel and cancel[0]:
                stop = "cancelled"
                break
            if time.perf_counter() - t0 > self.max_seconds:
                stop = "max_seconds"
                break
            yield "step", {"n": n, "phase": "think"}
            deltas: List[str] = []
            calls: Dict[int, dict] = {}
            for kind, payload in self.turn(messages, self.registry.schemas(), self.max_tokens):
                if cancel and cancel[0]:
                    stop = "cancelled"
                    break
                if kind == "delta":
                    deltas.append(payload)
                    yield "delta", {"text": payload}
                elif kind == "tool_calls":
                    calls = payload
                elif kind == "timings":
                    yield "timings", payload
            if stop == "cancelled":
                break

            text = "".join(deltas).strip()
            if not calls:
                used_ok = any(s.get("ok") for s in steps)
                if self.nudge and not nudged and not used_ok and not (state or "").strip():
                    nudged = True
                    messages.append({"role": "assistant", "content": text or "(nothing)"})
                    messages.append({"role": "system", "content": NUDGE})
                    yield "step", {"n": n, "phase": "nudge"}
                    continue
                answer = text
                stop = "answer" if text else "empty"
                break

            # The model asked for tools: run them, hand back the results, go round again.
            messages.append({"role": "assistant", "content": text or None,
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["name"], "arguments": c["arguments"]}}
                                            for _, c in sorted(calls.items())]})
            for _, call in sorted(calls.items()):
                tool_calls_made += 1
                try:
                    arguments = __import__("json").loads(call["arguments"] or "{}")
                except Exception:
                    arguments = {"_raw": call["arguments"]}
                key = (call["name"], call["arguments"])
                seen[key] = seen.get(key, 0) + 1
                yield "tool_call", {"n": n, "id": call["id"], "name": call["name"],
                                    "arguments": arguments if isinstance(arguments, dict) else {}}
                if seen[key] > self.max_repeats:
                    step = {"n": n, "tool": call["name"], "ok": False, "ms": 0.0, "bytes": 0,
                            "error": f"repeated {self.max_repeats + 1} times with identical arguments",
                            "preview": ""}
                    steps.append(step)
                    yield "tool_result", dict(step, id=call["id"], truncated=False, repeat=True)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                     "content": "ERROR: identical call repeated; stop and answer."})
                    continue
                decision, reason = "allow", ""
                if self.registry.is_mutating(call["name"]):
                    yield "approval", {"n": n, "id": call["id"], "tool": call["name"],
                                       "arguments": arguments if isinstance(arguments, dict) else {}}
                    if self.on_approval is not None:
                        decision, reason = self.on_approval(call["name"], arguments, call["id"], n)
                if decision != "allow":
                    note = reason or f"{call['name']} was not approved"
                    step = {"n": n, "tool": call["name"], "ok": False, "ms": 0.0, "bytes": 0,
                            "error": note, "preview": ""}
                    steps.append(step)
                    yield "tool_result", dict(step, id=call["id"], truncated=False, denied=True)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                     "content": f"ERROR: {note}"})
                    continue
                # Reaching here means the approval path already said yes (or the tool does not need
                # one), so the registry-level check is satisfied rather than asked twice.
                env = self.registry.call(call["name"], call["arguments"], approved=True)
                step = {"n": n, "tool": call["name"], "ok": bool(env["ok"]), "ms": env["ms"],
                        "bytes": env["bytes"], "error": env["error"],
                        "preview": (env["content"] or "")[:240].replace("\n", " / ")}
                steps.append(step)
                yield "tool_result", dict(step, id=call["id"], truncated=env["truncated"])
                if env["ok"] and env["content"]:
                    # Internal event, not for the wire: it is the evidence stage 3 checks the answer
                    # against. In agent mode the state is often empty, and verifying against nothing
                    # would always say insufficient.
                    yield "evidence", {"text": env["content"]}
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                 "content": env["content"] if env["ok"] else f"ERROR: {env['error']}"})
        else:
            stop = "max_steps"

        if stop in ("max_steps", "max_seconds", "cancelled") and not answer:
            answer = ("I stopped before reaching an answer "
                      f"({stop.replace('_', ' ')} after {len(steps)} tool call(s)).")
        yield "done", {"answer": answer, "steps": steps, "stop_reason": stop,
                       "tool_calls": tool_calls_made,
                       # grounded = something the tools actually returned stands behind the answer.
                       # Stage 3 uses this: an ungrounded answer from an empty state is the one
                       # failure mode this 4B reaches for most readily (it will describe a file it
                       # never read), and it is invisible without this flag.
                       "grounded": any(s.get("ok") for s in steps),
                       "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
