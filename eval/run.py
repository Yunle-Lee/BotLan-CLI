#!/usr/bin/env python3
"""End-to-end evaluation of the running Spark Duo pipeline over the labelled case set."""
import json
import statistics
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORCH = "http://127.0.0.1:8090"
BRANCH_BY_INTENT = json.loads((ROOT / "config.json").read_text())["gate"]["branch_by_intent"]
ESCALATE_INTENTS = {"record_lookup", "creative_or_chat"}


def post(url, payload, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} -> {e.code}: {e.read().decode()[:300]}") from None


cases = [json.loads(l) for l in (Path(__file__).parent / "cases.jsonl").read_text().splitlines() if l.strip()]
intent_ok = branch_ok = esc_ok = branch_neutral = 0
gate_ms, total_ms = [], []
generated = 0
per_intent = defaultdict(lambda: [0, 0])
wrong = []

for c in cases:
    r = post(ORCH + "/answer", {"state": c["state"], "question": c["question"]})
    st = r.get("stages", {})
    gate_ms.append(st.get("gate_ms", 0)); total_ms.append(st.get("total_ms", 0))
    generated += 1 if "vlm_ms" in st else 0

    i_ok = r.get("intent") == c["intent"]
    intent_ok += i_ok
    per_intent[c["intent"]][1] += 1
    per_intent[c["intent"]][0] += i_ok

    exp_branch = BRANCH_BY_INTENT.get(c["intent"])
    b_ok = r.get("route") == exp_branch
    branch_ok += b_ok
    if not i_ok:
        # a misroute only costs behaviour when it lands on a different branch
        branch_neutral += r.get("route") == exp_branch
        wrong.append((c["id"], c["intent"], r.get("intent"), exp_branch, r.get("route"),
                      f"{r.get('confidence',0):.2f}"))

    should_esc = c["intent"] in ESCALATE_INTENTS
    esc_ok += bool(r.get("escalate")) == should_esc

n = len(cases)
print(f"cases {n}\n")
print(f"intent accuracy      : {intent_ok}/{n} = {intent_ok/n:.1%}")
print(f"branch accuracy      : {branch_ok}/{n} = {branch_ok/n:.1%}")
print(f"escalation policy    : {esc_ok}/{n} = {esc_ok/n:.1%}")
print(f"generated            : {generated}/{n}")
print(f"gate ms  p50 {statistics.median(gate_ms):.0f} max {max(gate_ms):.0f}")
print(f"total ms p50 {statistics.median(total_ms):.0f} max {max(total_ms):.0f}")

print("\nper-intent recall (worst first):")
for k, (ok, tot) in sorted(per_intent.items(), key=lambda x: x[1][0] / x[1][1]):
    print(f"  {k:22} {ok}/{tot}")

print(f"\nmisroutes: {len(wrong)}  (of which {branch_neutral} land on the same branch = harmless)")
for w in wrong:
    print(f"  {w[0]:10} want={w[1]:20} got={w[2]:20} branch {w[3]} -> {w[4]}  p={w[5]}")
