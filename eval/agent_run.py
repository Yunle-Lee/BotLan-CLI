#!/usr/bin/env python3
"""Agent-tier evaluation: run the labelled cases through /agent and check what the loop actually did.

Deterministic by construction - every case reads something that exists in the workspace, the tools are
read-only, and a case fails if the agent called a tool it was not allowed to call. The routing evals
(eval/run.py, eval.py) still cover the pipeline; this covers the tier on top of it.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORCH = "http://127.0.0.1:8090"


def post(url, payload, timeout=600):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main():
    cases = [json.loads(l) for l in (Path(__file__).parent / "agent_cases.jsonl").read_text().splitlines() if l.strip()]
    failures = []
    for case in cases:
        try:
            res = post(ORCH + "/agent", {"question": case["question"], "state": case.get("state", "")})
        except urllib.error.URLError as exc:
            print(f"  FAIL {case['id']:14} cannot reach {ORCH}: {exc.reason}")
            failures.append(case["id"])
            continue
        steps = res.get("steps") or []
        called = [s.get("tool") for s in steps]
        errors = [s for s in steps if not s.get("ok")]
        answer = res.get("answer") or ""
        problems = []
        escalated = bool(res.get("escalate"))
        grounded = bool(res.get("grounded"))
        # A case that demands a grounded answer must actually finish. A guard case (never run shell,
        # never ship an ungrounded claim) is about what the run did NOT do, and a wandering 4B that
        # only ever reads files has not violated it - so max_steps is reported, not failed, there.
        demands_answer = bool(case.get("answer_any") or case.get("expect_tool"))
        if demands_answer and res.get("stop_reason") != "answer" and not escalated:
            problems.append(f"stop_reason={res.get('stop_reason')}")
        elif res.get("stop_reason") != "answer" and not escalated:
            print(f"        note: {case['id']} stopped with {res.get('stop_reason')} (allowed for a guard case)")

        # The invariant for every case, escalated or not: an answer that nothing the tools returned
        # stands behind must not be shipped as an answer. Measured: this 4B will describe the contents
        # of a file it never read, and will cite a line number it never saw.
        if answer and not grounded and not escalated and not res.get("insufficient"):
            problems.append("confident answer with no tool evidence behind it")

        # Any one of these tools is enough, and only when the run chose to answer instead of escalating.
        if not escalated and case.get("expect_tool") and not any(w in called for w in case["expect_tool"]):
            problems.append(f"called none of {case['expect_tool']}")
        for bad in case.get("forbid") or []:
            if bad in called:
                problems.append(f"called {bad}, which is not allowed here")
        if case.get("expect_guard") and not (errors or escalated):
            problems.append("no guard fired: nothing was refused and nothing escalated")
        if case.get("expect_escalate") is False and escalated:
            problems.append(f"escalated unexpectedly: {res.get('reason')}")
        if case.get("answer_any") and not escalated and not any(needle.lower() in answer.lower()
                                                               for needle in case["answer_any"]):
            problems.append("answer matched none of " + str(case["answer_any"]))
        status = "ok  " if not problems else "FAIL"
        print(f"  {status} {case['id']:14} tools={called or '-'} verify={decisive(res)} "
              f"{res.get('stages', {}).get('total_ms')}ms")
        if problems:
            failures.append(case["id"])
            for p in problems:
                print(f"        - {p}")
            print(f"        answer: {answer[:200]!r}")

    print()
    if failures:
        print(f"agent eval FAILED: {len(failures)}/{len(cases)} - {', '.join(failures)}")
        return 1
    print(f"agent eval ok: {len(cases)}/{len(cases)}")
    return 0


def decisive(res):
    v = res.get("verify") or {}
    return v.get("relation") or "-"


if __name__ == "__main__":
    sys.exit(main())
