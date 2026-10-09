#!/usr/bin/env python3
"""Spark Duo — Jev gate + StepFun VLM, one pipeline on the DGX Spark.

Stage 1  Jev-Style-0.8B-Decision-v3 (0.53 GB, discriminative) classifies the request into one of
         twelve intents in a single call. The intent decides the branch and whether the state can
         answer at all. It never generates.
Stage 2  GELab-Zero-4B-preview (stepfun-ai's smallest model, a Qwen3-VL-4B fine-tune) answers,
         seeing only the context stage 1 selected.
Stage 3  Jev scores how the answer relates to the context (entailed / contradicted / insufficient).

Both models stay resident in the GB10's unified memory at the same time, so neither stage evicts
the other. The gate runs at a FITTED calibration temperature — `require_fitted()` refuses to start
otherwise, because lookup_temperature() falls back to the global T silently.

Endpoints: GET /health, POST /route, POST /answer, POST /v1/chat/completions
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
JEV_DIR = Path(CONFIG["jev"]["model_dir"]).expanduser()
VLM_URL = CONFIG["vlm"]["base_url"].rstrip("/")
BRANCHES = CONFIG["branches"]
GATE = CONFIG["gate"]
AGENT = CONFIG.get("agent") or {}
VERIFY_EVIDENCE_CHARS = 40000        # stage 3 checks the answer against this much of the tool output

_LOCK = threading.Lock()

# The agent's tool set. Read-only and jailed to AGENT["root"] unless the config opens something up.
AGENT_REGISTRY = None
if AGENT.get("enabled"):
    from agent.tools import Registry

    AGENT_REGISTRY = Registry(root=AGENT.get("root", "~/spark-duo"),
                              enabled=AGENT.get("tools"),
                              timeout=AGENT.get("tool_timeout_s", 30),
                              max_bytes=AGENT.get("max_output_bytes", 60000),
                              max_hits=AGENT.get("max_hits", 200),
                              allow_shell=AGENT.get("allow_shell", False),
                              allow_write=AGENT.get("allow_write", False),
                              require_approval=AGENT.get("require_approval", True))

# Surfaces, approvals and tabs - the layer ported from Empryo's src/hearth. The orchestrator is the
# daemon: the terminal is one surface attached to it, and an approval can be answered from that
# surface, from a second terminal over the socket, or not at all (which is a denial).
APPROVALS = None
APPROVAL_POLICY = None
SURFACES = None
SURFACE = None
APPROVAL_SOCKET = None
if AGENT.get("enabled"):
    from agent.approvals import ApprovalPolicy, ApprovalRegistry
    from agent.protocol import SocketServer
    from agent.surface import ApprovalUI, SseSurface, SurfaceHost

    APPROVALS = ApprovalRegistry(default_timeout_ms=AGENT.get("approval_timeout_ms", 300000))
    APPROVAL_POLICY = ApprovalPolicy(auto_approve=AGENT.get("auto_approve") or [],
                                     auto_deny=AGENT.get("auto_deny") or [],
                                     switch_on=bool(AGENT.get("allow_write") or AGENT.get("allow_shell")))
    SURFACES = SurfaceHost()
    SURFACE = SseSurface("tui:local", registry=APPROVALS,
                         timeout_ms=AGENT.get("approval_timeout_ms", 300000))
    SURFACES.register(SURFACE)
    SURFACES.start()


def mem_available_gb() -> float | None:
    """On GB10 the GPU allocates from the same coherent pool as the CPU, so this IS the pool the
    models live in — nvidia-smi reports no separate device memory here."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return round(int(line.split()[1]) / 1024 / 1024, 2)
    except OSError:
        pass
    return None


def _message_text(msg) -> str:
    """A message's text: OpenAI sends content as a string, or as a list of parts when it is multimodal
    (which the old join assumed was a string and crashed on)."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def openai_request(req) -> tuple[str, str | None]:
    """Map an OpenAI chat request onto the pipeline's (state, question).

    The gate must READ the request — a state that does not carry it is the 35/48 mode — so the last
    user turn is the question and the turns before it are the state. A single message carries both,
    exactly as the documented "<question + context>" usage sends it, so it stays the state alone and
    the branch keeps its default question."""
    msgs = req.get("messages") or []
    users = [m for m in msgs if m.get("role") == "user"]
    explicit_q = req.get("question")
    if req.get("state") is not None:
        question = explicit_q or (_message_text(users[-1]).strip() if users else None)
        return req["state"], question or None
    if explicit_q is not None:
        return "\n\n".join(t for t in map(_message_text, msgs) if t.strip()), explicit_q
    if not users:
        return "", None
    if len(msgs) == 1:
        return _message_text(msgs[0]).strip(), None
    history = [t for t in map(_message_text, msgs[:-1]) if t.strip()]
    return "\n\n".join(history), (_message_text(users[-1]).strip() or None)


class Gate:
    """Stage 1 (+3): the Jev decision model, held open in its own jev-score process."""

    def __init__(self) -> None:
        sys.path.insert(0, str(JEV_DIR))
        from jev_style_decision_gguf import JevStyleDecisionGGUF, family, option_bucket

        self.family, self.bucket = family, option_bucket
        self.engine = JevStyleDecisionGGUF(
            JEV_DIR,
            quant=CONFIG["jev"]["quant"],
            binary=os.environ.get("JEV_SCORE_BIN"),
            n_gpu_layers=CONFIG["jev"].get("n_gpu_layers", 999),
            many_mode=CONFIG["jev"].get("many_mode", "exact"),
        )

    def calibration_key(self, spec) -> str:
        n = 2 if spec["qtype"] == "noul" else len(spec["options"])
        return f"{self.family(spec['category'])}|{spec['qtype']}|{self.bucket(n)}"

    def require_fitted(self) -> None:
        """Refuse to start on an unfitted group: the fallback to the global temperature is silent,
        and a gate compared against uncalibrated numbers is the bug this check exists for."""
        self.groups = self.engine.temperatures.get("groups") or {}
        specs = [("gate", GATE), ("verify", CONFIG["verify"])]
        for name, spec in specs:
            if not spec.get("enabled", True):
                continue
            key = self.calibration_key(spec)
            if key not in self.groups:
                raise RuntimeError(
                    f"{name}: no fitted temperature for {key!r} — it would silently run at the "
                    f"global T. Fitted keys: {sorted(self.groups)}"
                )
            g = self.groups[key]
            print(f"  {name:8} {key:24} T={g['T']:.4f} (n={g.get('n')})")

    def _ask(self, state, spec, options, question=None) -> tuple[dict, float]:
        t0 = time.perf_counter()
        with _LOCK:
            res = self.engine.decide(state, question or spec["question"], options=options,
                                     qtype=spec["qtype"], category=spec["category"])
        ms = round((time.perf_counter() - t0) * 1000, 1)
        res["calibration_key"] = self.calibration_key(spec)
        res["calibrated"] = res["calibration_key"] in self.groups
        return res, ms

    def compose(self, state, question=None) -> str:
        """The intent is a property of the REQUEST, so the gate must see what the customer asked.
        Classifying from the state alone made intents that live in the question (creative_or_chat,
        record_lookup, sales) unreachable: measured 35/48 vs 41/48 with the request included."""
        tmpl = GATE.get("compose")
        if not tmpl or not question:
            return state
        if question in (state or ""):
            return state      # already inside the state; appending it would only pay for it twice
        return tmpl.format(state=state, question=question)

    def route(self, state, question=None) -> tuple[dict, float]:
        return self._ask(self.compose(state, question), GATE, GATE["options"])

    def verify(self, context, answer) -> tuple[dict, float]:
        spec = CONFIG["verify"]
        return self._ask({"context": context, "answer": answer}, spec, spec["options"])


def vlm_chat(messages, max_tokens=None, temperature=None, sampling=None) -> tuple[str, float]:
    body = json.dumps(
        {
            "messages": messages,
            "max_tokens": max_tokens or CONFIG["vlm"]["max_tokens"],
            "temperature": CONFIG["vlm"]["temperature"] if temperature is None else temperature,
            **{**CONFIG["vlm"].get("sampling", {}), **(sampling or {})},
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        VLM_URL + "/v1/chat/completions", data=body, headers={"Content-Type": "application/json"}
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=CONFIG["vlm"]["timeout_s"]) as resp:
        payload = json.loads(resp.read())
    ms = round((time.perf_counter() - t0) * 1000, 1)
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        text = json.dumps(payload)[:500]
    return text, ms


def vlm_stream(messages, max_tokens=None, temperature=None, sampling=None):
    """Yield ("delta", text) as the 4B produces it, then ("timings", {...}) if the server sent it."""
    body = json.dumps({
        "messages": messages,
        "max_tokens": max_tokens or CONFIG["vlm"]["max_tokens"],
        "temperature": CONFIG["vlm"]["temperature"] if temperature is None else temperature,
        **{**CONFIG["vlm"].get("sampling", {}), **(sampling or {})},
        "stream": True,
    }).encode()
    req = urllib.request.Request(VLM_URL + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=CONFIG["vlm"]["timeout_s"]) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choice = (chunk.get("choices") or [{}])[0]
            piece = (choice.get("delta") or {}).get("content") or ""
            if piece:
                yield "delta", piece
            if chunk.get("timings"):
                yield "timings", chunk["timings"]


class LoopGuard:
    """Detect a reply that has started repeating itself so generation can be cut short.

    Sampling alone does not catch every degeneration, and a 4B model that runs out of memorised
    text has no way to say so unless it is stopped. Deliberately dumb and exact: look for the same
    block of p characters repeated consecutively three times, for p from min to max."""

    def __init__(self, cfg):
        cfg = cfg or {}
        self.enabled = cfg.get("enabled", True)
        self.max_period = int(cfg.get("max_period", 128))
        self.repeats = int(cfg.get("repeats", 3))
        self.min_period = int(cfg.get("min_period", 4))
        self.exit_cjk = cfg.get("exit_cjk", "\u5443\u2026\u2026\u6211\u53ea\u8bb0\u5f97\u8fd9\u4e48\u591a\u4e86\u3002")
        self.exit_latin = cfg.get("exit_latin", "\u2026 that is as much as I can recall.")
        self.text = ""

    def feed(self, piece):
        """Append a delta; return the period length when the tail has started looping, else 0."""
        if not self.enabled or not piece:
            return 0
        self.text += piece
        for p in range(self.min_period, self.max_period + 1):
            if len(self.text) < p * self.repeats:
                break
            block = self.text[-p:]
            if block * self.repeats == self.text[-p * self.repeats:]:
                return p
        return 0

    def admission(self) -> str:
        cjk = sum(1 for ch in self.text if "\u4e00" <= ch <= "\u9fff")
        return self.exit_cjk if cjk * 3 > len(self.text) else self.exit_latin


def summarise_call(tool: str, arguments) -> str:
    """One line an approver can decide on - the equivalent of the ApprovalUI summary upstream."""
    if not isinstance(arguments, dict):
        return tool
    if tool == "write_file":
        body = str(arguments.get("content") or "")
        return f"write {len(body.encode())} bytes to {arguments.get('path')}"
    if tool == "shell":
        return f"run: {arguments.get('command')}"
    return tool + " " + json.dumps(arguments, ensure_ascii=False)[:80]


def ask_approval(tool: str, arguments, call_id: str, tab: str | None = None) -> tuple[str, str]:
    """Allow only on a yes: the policy first, then the surface, then the timeout."""
    decision = APPROVAL_POLICY.decide(tool)
    if decision is not None:
        return decision, f"policy: {decision} (no human needed)"
    ui = ApprovalUI(approval_id=call_id, tool_name=tool, summary=summarise_call(tool, arguments),
                    cwd=str(AGENT_REGISTRY.root), tab_id=tab or "default",
                    tool_input=arguments if isinstance(arguments, dict) else {})
    res = SURFACES.request_approval(SURFACE.id, "local", ui)
    APPROVAL_POLICY.remember(tool, res.get("decision", "deny"), res.get("remember"))
    return res.get("decision", "deny"), res.get("reason") or f"{tool}: {res.get('decision')}"


def approval_rpc(req: dict) -> dict:
    """What jev-approve talks to. Same three ops as the socket in the original: ask, resolve, status."""
    op = req.get("op")
    if op == "status":
        return {"pending": [p.public() for p in APPROVALS.list()], "count": APPROVALS.count()}
    if op == "resolve":
        ok = APPROVALS.resolve(req.get("id"), req.get("decision", "deny"), req.get("reason"),
                               req.get("remember"))
        return {"resolved": bool(ok), "decision": req.get("decision")}
    if op == "approve":
        ui = ApprovalUI(approval_id=req.get("tool_call_id") or "cli",
                        tool_name=req.get("tool_name") or "cli",
                        summary=f"approve CLI request from {req.get('cwd')}",
                        cwd=req.get("cwd") or os.getcwd(), tab_id=req.get("session_id"),
                        tool_input=req.get("tool_input") or {})
        res = SURFACES.request_approval(SURFACE.id, "local", ui)
        return {"decision": res.get("decision", "deny"), "reason": res.get("reason"),
                "approval_id": res.get("approval_id")}
    return {"error": f"unknown op {op!r}"}


def vlm_turn_stream(messages, tools=None, max_tokens=None, temperature=None):
    """One agent turn, streamed: ("delta", text) as it is produced, then ("tool_calls", {...}) with
    the assembled calls, then ("timings", {...}).

    llama-server streams tool calls incrementally - the name and the JSON arguments arrive in pieces
    and finish_reason is "tool_calls" - so the rail fills in live and no output parsing is needed.
    """
    body = json.dumps({
        "messages": messages,
        "max_tokens": max_tokens or CONFIG["vlm"]["max_tokens"],
        "temperature": CONFIG["vlm"]["temperature"] if temperature is None else temperature,
        **CONFIG["vlm"].get("sampling", {}),
        "stream": True,
        **({"tools": tools, "tool_choice": "auto"} if tools else {}),
    }).encode()
    req = urllib.request.Request(VLM_URL + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    calls: dict = {}
    with urllib.request.urlopen(req, timeout=CONFIG["vlm"]["timeout_s"]) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                yield "delta", piece
            for tc in delta.get("tool_calls") or []:
                i = tc.get("index", 0)
                cur = calls.setdefault(i, {"id": f"call_{i}", "name": "", "arguments": ""})
                if tc.get("id"):
                    cur["id"] = tc["id"]
                fn = tc.get("function") or {}
                cur["name"] += fn.get("name") or ""
                cur["arguments"] += fn.get("arguments") or ""
            if chunk.get("timings"):
                yield "timings", chunk["timings"]
    if calls:
        yield "tool_calls", calls


def agent_events(question, state=None, image=None, approved=None, tab=None):
    """The agent path as one event stream: stage 1 decides, the loop works with tools, stage 3 checks.

    Stage 1 is not decoration here. It is what tells a request the context can already answer from one
    that needs a lookup - needs_lookup (record_lookup) is exactly the case the pipeline used to
    escalate and the agent can now resolve with a tool. off_topic stays refused.
    """
    from agent.loop import AgentLoop

    t_all = time.perf_counter()
    gate_res, gate_ms = GATE_CLIENT.route(state or "", question)
    intent = gate_res["answer"]
    branch = GATE["branch_by_intent"].get(intent, "answer_from_state")
    stages = {"gate_ms": gate_ms}
    yield "gate", {"intent": intent, "branch": branch,
                   "confidence": round(gate_res["top_probability"], 4),
                   "calibrated": gate_res["calibrated"],
                   "calibration_key": gate_res["calibration_key"], "gate_ms": gate_ms,
                   "needs_lookup": branch == "needs_lookup"}

    # The pipeline's refusal list was fitted for support traffic, and in the agent tier it fires on
    # work that is perfectly legitimate: "Write a file notes/hello.txt with ..." classifies as
    # creative_or_chat (0.54) and was refused before a single tool ran. agent.respect_refusals keeps
    # that policy available; with it off, the tools, the jail and the approval gate are the boundary.
    if (AGENT.get("respect_refusals", True) and intent in GATE["refuse_intents"]
            and gate_res["top_probability"] >= GATE["threshold"]):
        yield "done", {"answer": CONFIG["fallback"]["message"].format(branch=branch), "escalate": True,
                       "reason": f"intent {intent} is out of scope (p={gate_res['top_probability']:.3f})",
                       "route": branch, "intent": intent, "steps": [], "tool_calls": 0,
                       "stages": dict(stages, total_ms=round((time.perf_counter() - t_all) * 1000, 1))}
        return

    pre = set(approved or ())

    def approve(tool: str, arguments, call_id: str, step: int) -> tuple[str, str]:
        if tool in pre:
            return "allow", "pre-approved by the caller"
        return ask_approval(tool, arguments, call_id, tab)

    loop = AgentLoop(AGENT_REGISTRY, vlm_turn_stream, AGENT, on_approval=approve)
    done, answer, evidence = {}, "", []
    for event, data in loop.run(question, state or "", image=image, approved=set(approved or ())):
        if event == "delta":
            answer += data["text"]
            yield event, data
        elif event == "done":
            done = data
        elif event == "tool_result":
            stages["tools_ms"] = round(stages.get("tools_ms", 0.0) + float(data.get("ms") or 0.0), 1)
            yield event, data
        elif event == "evidence":
            evidence.append(data["text"])          # internal: stage 3 checks the answer against this
        else:
            yield event, data

    final = done.get("answer") or answer
    if CONFIG["verify"].get("enabled") and final:
        # In agent mode the state is often empty and the answer came from tool output, so stage 3 has
        # to see the evidence. Capped: six tool results can outrun the model's own input budget, and
        # an InputBudgetError here would surface as a 500 after the answer was already streamed.
        context = state or "\n\n".join(evidence)[:VERIFY_EVIDENCE_CHARS]
        ver, ms = GATE_CLIENT.verify(context, final)
        stages["verify_ms"] = ms
        yield "verify", {"relation": ver["answer"], "probability": round(ver["top_probability"], 4), "ms": ms}
        done["verify"] = {"relation": ver["answer"], "probability": ver["top_probability"]}
        if ver["answer"] == CONFIG["verify"]["bad_option"] and ver["top_probability"] >= CONFIG["verify"]["threshold"]:
            done["escalate"] = True
            done["reason"] = "stage 3 says the evidence contradicts the answer"
        if ver["answer"] == "insufficient":
            # Computed and, until now, thrown away. It is the honest "cannot tell" and the one signal
            # that catches an agent answer with nothing behind it.
            done["insufficient"] = True

    # An answer to a question with no state that no tool grounded is a guess with a confident voice.
    # Measured on this stack: asked to read /etc/hostname it described the file without reading it.
    if not done.get("grounded", True) and not (state or "").strip():
        done["escalate"] = True
        done["reason"] = "the answer is not grounded in anything the tools returned"

    stages["total_ms"] = round((time.perf_counter() - t_all) * 1000, 1)
    yield "done", dict(done, answer=final, route=branch, intent=intent, stages=stages,
                       mem_available_gb=mem_available_gb())


def run_agent(question, state=None, image=None, approved=None, tab=None) -> dict:
    """The agent path, drained: for /agent and for the eval, where a caller wants one dict."""
    out: dict = {}
    for event, data in agent_events(question, state, image, approved, tab):
        if event == "done":
            out = data
    return out


def _escalate(out, reason, t_all):
    out["answer"] = CONFIG["fallback"]["message"].format(branch=out.get("route"))
    out["escalate"] = True
    out["reason"] = reason
    out["stages"]["total_ms"] = round((time.perf_counter() - t_all) * 1000, 1)
    return out


def run_pipeline(state, question=None, image=None, force=False) -> dict:
    out: dict = {"stages": {}}
    t_all = time.perf_counter()

    # Jev is text-only: it cannot know an image is attached, and with one the branch is forced to
    # answer_from_state anyway. So the gate is skipped rather than spending a whole state prefill
    # (~2 s on a 23k-token state) on a decision whose branch is then discarded.
    image_bypass = bool(image) and GATE.get("image_bypass", True)
    gate_res = None
    if image_bypass:
        intent, branch = None, "answer_from_state"
        out["stages"]["gate_ms"] = 0.0
        out["gate_bypassed"] = "image attached; Jev cannot see pixels"
    else:
        gate_res, gate_ms = GATE_CLIENT.route(state, question)
        intent = gate_res["answer"]
        branch = GATE["branch_by_intent"].get(intent, "answer_from_state")
        out["stages"]["gate_ms"] = gate_ms
        out["calibrated"] = gate_res["calibrated"]
        out["calibration_key"] = gate_res["calibration_key"]
        out["gate_tokens"] = gate_res.get("input_tokens")

    out["intent"] = intent
    out["route"] = branch
    out["confidence"] = gate_res["top_probability"] if gate_res else None
    out["probabilities"] = gate_res["probabilities"] if gate_res else {}

    spec = BRANCHES.get(branch)
    if spec is None:
        return _escalate(out, f"no branch configured for intent {intent!r}", t_all)

    if not force and not image_bypass:
        if intent in GATE["unanswerable_intents"] and gate_res["top_probability"] >= GATE["threshold"]:
            return _escalate(out, f"intent {intent} needs data the state does not hold "
                                  f"(p={gate_res['top_probability']:.3f})", t_all)
        if intent in GATE["refuse_intents"] and gate_res["top_probability"] >= GATE["threshold"]:
            return _escalate(out, f"intent {intent} is out of scope (p={gate_res['top_probability']:.3f})", t_all)
        thr = spec.get("threshold", GATE["threshold"])
        if gate_res["top_probability"] < thr:
            return _escalate(out, f"gate confidence {gate_res['top_probability']:.3f} < {thr} for {intent}", t_all)

    content = spec["prompt"].format(state=state, question=question or CONFIG["vlm"]["default_question"])
    messages = [{"role": "system", "content": spec.get("system", CONFIG["vlm"]["system"])}]
    if image:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": content},
            {"type": "image_url", "image_url": {"url": image}}]})
    else:
        messages.append({"role": "user", "content": content})

    out["answer"], out["stages"]["vlm_ms"] = vlm_chat(messages, spec.get("max_tokens"))
    out["escalate"] = False

    if CONFIG["verify"].get("enabled"):
        ver, ms = GATE_CLIENT.verify(state, out["answer"])
        out["stages"]["verify_ms"] = ms
        out["verify"] = {"relation": ver["answer"], "probability": ver["top_probability"]}
        if ver["answer"] == CONFIG["verify"]["bad_option"] and ver["top_probability"] >= CONFIG["verify"]["threshold"]:
            out["escalate"] = True
            out["reason"] = "stage 3 says the context contradicts the answer"

    out["stages"]["total_ms"] = round((time.perf_counter() - t_all) * 1000, 1)
    out["mem_available_gb"] = mem_available_gb()
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def log_message(self, *a):  # timings travel in the payload; keep the journal quiet
        pass

    def _stream_chat(self, req):
        """A plain conversation with the 4B, with the loop guard applied. No gate: this is `direct`."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event, obj):
            self.wfile.write(f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        messages = req.get("messages") or []
        text, timings, cut = "", {}, 0
        guard = LoopGuard(CONFIG["vlm"].get("loop_guard"))
        t0 = time.perf_counter()
        try:
            for kind, payload in vlm_stream(messages, req.get("max_tokens", 2048),
                                            req.get("temperature"), req.get("sampling")):
                if kind == "delta":
                    text += payload
                    emit("delta", {"text": payload})
                    if guard.feed(payload):
                        text = guard.text[:-len(payload)]
                        note = "\n\n" + guard.admission()
                        emit("delta", {"text": note})
                        text += note
                        cut = 1
                        break
                elif kind == "timings":
                    timings = payload
            emit("done", {"reply": text, "loop_cut": bool(cut), "timings": timings,
                          "total_ms": round((time.perf_counter() - t0) * 1000, 1)})
        except Exception as exc:                      # the stream is already open: report, do not 500
            emit("error", {"error": f"{type(exc).__name__}: {exc}"})

    def _stream_answer(self, req):
        """Server-sent events: gate -> delta... -> done.

        Stage 1 must finish before any token is generated (it decides whether to generate at all),
        so the first event is the routing decision, ~50 ms in. Stage 3 runs after the text is
        complete, so its verdict arrives in the trailing `done` event."""
        state, question = req.get("state"), req.get("question")
        image, force = req.get("image"), bool(req.get("force"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event, obj):
            self.wfile.write(f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        t_all = time.perf_counter()
        try:
            # Same skip as run_pipeline: with an image the branch is forced, so the gate decides nothing.
            image_bypass = bool(image) and GATE.get("image_bypass", True)
            if image_bypass:
                gate_res, gate_ms, intent, branch = None, 0.0, None, "answer_from_state"
            else:
                gate_res, gate_ms = GATE_CLIENT.route(state, question)
                intent = gate_res["answer"]
                branch = GATE["branch_by_intent"].get(intent, "answer_from_state")
            emit("gate", {"intent": intent, "branch": branch,
                          "confidence": round(gate_res["top_probability"], 4) if gate_res else None,
                          "calibrated": gate_res["calibrated"] if gate_res else None,
                          "calibration_key": gate_res["calibration_key"] if gate_res else None,
                          "gate_ms": gate_ms, "image_bypass": image_bypass})

            spec = BRANCHES.get(branch)
            reason = None
            if spec is None:
                reason = f"no branch configured for intent {intent!r}"
            elif not force and not image_bypass:
                if intent in GATE["unanswerable_intents"] and gate_res["top_probability"] >= GATE["threshold"]:
                    reason = f"intent {intent} needs data the state does not hold (p={gate_res['top_probability']:.3f})"
                elif intent in GATE["refuse_intents"] and gate_res["top_probability"] >= GATE["threshold"]:
                    reason = f"intent {intent} is out of scope (p={gate_res['top_probability']:.3f})"
                elif gate_res["top_probability"] < GATE["threshold"]:
                    reason = (f"gate confidence {gate_res['top_probability']:.3f} < "
                              f"{GATE['threshold']} for {intent}")
            if reason:
                emit("done", {"answer": CONFIG["fallback"]["message"].format(branch=branch),
                              "escalate": True, "reason": reason, "route": branch, "intent": intent,
                              "stages": {"gate_ms": gate_ms,
                                         "total_ms": round((time.perf_counter() - t_all) * 1000, 1)}})
                return

            content = spec["prompt"].format(state=state, question=question or CONFIG["vlm"]["default_question"])
            messages = [{"role": "system", "content": spec.get("system", CONFIG["vlm"]["system"])}]
            if image:
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": content},
                    {"type": "image_url", "image_url": {"url": image}}]})
            else:
                messages.append({"role": "user", "content": content})

            text, timings, cut = "", {}, 0
            guard = LoopGuard(CONFIG["vlm"].get("loop_guard"))
            t_vlm = time.perf_counter()
            for kind, payload in vlm_stream(messages, spec.get("max_tokens")):
                if kind == "delta":
                    text += payload
                    emit("delta", {"text": payload})
                    if guard.feed(payload):
                        cut = len(guard.text) - len(payload)      # keep it out of the answer
                        text = guard.text[:-len(payload)]
                        note = "\n\n" + guard.admission()
                        emit("delta", {"text": note})
                        text += note
                        break
                elif kind == "timings":
                    timings = payload
            vlm_ms = round((time.perf_counter() - t_vlm) * 1000, 1)

            out = {"answer": text, "route": branch, "intent": intent, "escalate": False,
                   "loop_cut": bool(cut),
                   "stages": {"gate_ms": gate_ms, "vlm_ms": vlm_ms,
                              "total_ms": round((time.perf_counter() - t_all) * 1000, 1)}}
            if timings:
                out["decode_tok_s"] = timings.get("predicted_per_second")
            if CONFIG["verify"].get("enabled"):
                ver, ms = GATE_CLIENT.verify(state, text)
                out["stages"]["verify_ms"] = ms
                out["verify"] = {"relation": ver["answer"], "probability": ver["top_probability"]}
                if ver["answer"] == CONFIG["verify"]["bad_option"] and ver["top_probability"] >= CONFIG["verify"]["threshold"]:
                    out["escalate"] = True
                    out["reason"] = "stage 3 says the context contradicts the answer"
            emit("done", out)
        except Exception as exc:                      # the stream is already open: report, do not 500
            emit("error", {"error": f"{type(exc).__name__}: {exc}"})

    def _stream_agent(self, req):
        """SSE for the agent path: gate -> step/tool_call/tool_result... -> delta -> verify -> done."""
        question = req.get("question") or ""
        state, image = req.get("state"), req.get("image")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event, obj):
            self.wfile.write(f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        try:
            for event, data in agent_events(question, state, image, req.get("approve"),
                                            req.get("session")):
                emit(event, data)
        except Exception as exc:                      # the stream is already open: report, do not 500
            emit("error", {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self):
        if self.path == "/approvals":
            self._send({"pending": [p.public() for p in APPROVALS.list()] if APPROVALS else [],
                        "count": APPROVALS.count() if APPROVALS else 0})
        elif self.path == "/health":
            agent_info = AGENT_REGISTRY.info() if AGENT_REGISTRY else {"enabled": False}
            self._send({"ok": True, "intents": list(GATE["options"]), "branches": list(BRANCHES),
                        "vlm": VLM_URL, "jev": str(JEV_DIR), "sampling": CONFIG["vlm"].get("sampling", {}),
                        "agent": agent_info, "mem_available_gb": mem_available_gb(),
                        "approvals": {"pending": APPROVALS.count() if APPROVALS else 0,
                                      "policy": {"auto_approve": sorted(APPROVAL_POLICY.auto_approve),
                                                 "auto_deny": sorted(APPROVAL_POLICY.auto_deny)}
                                      if APPROVAL_POLICY else None}})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        try:
            req = self._read()
            if self.path == "/route":
                res, ms = GATE_CLIENT.route(req.get("state"), req.get("question"))
                self._send({"intent": res["answer"], "branch": GATE["branch_by_intent"].get(res["answer"]),
                            "probabilities": res["probabilities"], "confidence": res["top_probability"],
                            "calibrated": res["calibrated"], "calibration_key": res["calibration_key"],
                            "gate_ms": ms})
            elif self.path == "/decide":
                # raw access to Jev alone: your own question, your own options
                spec = {"qtype": req.get("qtype", "choice"),
                        "category": req.get("category", GATE["category"]),
                        "question": req["question"],
                        "options": req["options"]}
                res, ms = GATE_CLIENT._ask(req["state"], spec, spec["options"])
                self._send({"verdict": res["answer"], "probabilities": res["probabilities"],
                            "confidence": res["top_probability"], "temperature": res["temperature"],
                            "calibrated": res["calibrated"], "calibration_key": res["calibration_key"],
                            "scores": res.get("scores"), "ms": ms})
            elif self.path == "/chat/stream":
                self._stream_chat(req)
            elif self.path == "/answer/stream":
                self._stream_answer(req)
            elif self.path == "/agent/stream":
                self._stream_agent(req)
            elif self.path == "/agent":
                self._send(run_agent(req.get("question"), req.get("state"), req.get("image"),
                                     req.get("approve"), req.get("session")))
            elif self.path == "/approve":
                ok = bool(APPROVALS and APPROVALS.resolve(req.get("id"), req.get("decision", "deny"),
                                                          req.get("reason"), req.get("remember")))
                self._send({"resolved": ok, "id": req.get("id"), "decision": req.get("decision")})
            elif self.path == "/answer":
                self._send(run_pipeline(req.get("state"), req.get("question"), req.get("image"), bool(req.get("force"))))
            elif self.path == "/v1/chat/completions":
                state, question = openai_request(req)
                res = run_pipeline(state, question, req.get("image"))
                self._send({
                    "id": f"spark-duo-{int(time.time()*1000)}",
                    "object": "chat.completion",
                    "model": "spark-duo/jev+gelab",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": res["answer"]}}],
                    "spark_duo": res,
                })
            else:
                self._send({"error": "not found"}, 404)
        except Exception as exc:  # surfaced rather than swallowed
            self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main():
    ap = argparse.ArgumentParser(description="Spark Duo orchestrator")
    ap.add_argument("--port", type=int, default=CONFIG["orchestrator"]["port"])
    ap.add_argument("--host", default=CONFIG["orchestrator"]["host"])
    args = ap.parse_args()

    if APPROVALS is not None:
        global APPROVAL_SOCKET                      # noqa: PLW0603 - one socket per process
        path = os.path.expanduser(AGENT.get("approval_socket", "~/.spark-duo/approvals.sock"))
        APPROVAL_SOCKET = SocketServer(path, approval_rpc)
        APPROVAL_SOCKET.start()
        print(f"  approvals {path} (jev-approve status | allow <id> | deny <id>)")

    global GATE_CLIENT
    print(f"loading Jev gate from {JEV_DIR} (quant {CONFIG['jev']['quant']}, ngl {CONFIG['jev'].get('n_gpu_layers')})")
    GATE_CLIENT = Gate()
    print("calibration check (an unlisted key would silently fall back to the global T):")
    GATE_CLIENT.require_fitted()
    print(f"gate ready: {GATE_CLIENT.engine.info.get('desc')}")
    print(f"stage 2 vlm: {VLM_URL}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
