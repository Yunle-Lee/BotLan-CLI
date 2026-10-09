#!/usr/bin/env python3
"""Evaluate the running Spark Duo pipeline: intent routing, branch, escalation policy, grounding."""
import json
import statistics
import urllib.request

ORCH = "http://127.0.0.1:8090"

CASES = [
    dict(id="refund-window", intent="refund_request", branch="answer_from_state", want=["5 business days"],
         state="Refunds are issued within 5 business days of approval. Duplicate charges are refunded once billing confirms them.",
         question="What is the refund window?"),
    dict(id="sla-uptime", intent="service_level", branch="answer_from_state", want=["99.9"],
         state="Our SLA promises 99.9% uptime, measured monthly, excluding scheduled maintenance.",
         question="What uptime do we promise?"),
    dict(id="seat-price", intent="subscription_billing", branch="answer_from_state", want=["42"],
         state="The Pro plan costs 42 USD per seat per month, billed annually. The Team plan costs 79 USD per seat per month.",
         question="How much is a Pro seat?"),
    dict(id="holiday-hours", intent="service_level", branch="answer_from_state", want=["closed", "no"],
         state="Support operates 24/7 except on 25 December, when the desk is closed.",
         question="Are you open on 25 December?"),
    dict(id="trap-free-tier", intent="subscription_billing", branch="answer_from_state", negative=True,
         state="The Team plan costs 79 USD per seat per month. There is no free tier.",
         question="Is there a free tier?"),
    dict(id="trap-restore", intent="account_lifecycle", branch="answer_from_state", negative=True,
         state="Deleting a workspace is permanent and cannot be undone.",
         question="Can a deleted workspace be restored?"),
    dict(id="trap-trial", intent="subscription_billing", branch="answer_from_state", negative=True,
         state="The 14-day trial is the only trial offered. After it ends the account is read-only.",
         question="Do you offer a 30-day trial?"),
    dict(id="abuse-report", intent="security_abuse", branch="safety_review", want=["violation", "phishing", "policy", "abuse"],
         state="A user reported that an account is posting phishing links in the community forum. The account was created 2 hours ago.",
         question="Is this a policy violation?"),
    dict(id="order-status", intent="record_lookup", branch="needs_lookup",
         state="You are a support agent for an online store. You can see policy text but no customer order records.",
         question="Where is order 88123 right now?"),
    dict(id="balance", intent="record_lookup", branch="needs_lookup",
         state="You have the billing policy handbook but no access to any customer account database.",
         question="What is my current account balance?"),
    dict(id="write-me-a-poem", intent="creative_or_chat", branch="off_topic",
         state="You are a support assistant for a billing platform.",
         question="Write me a poem about the ocean."),
]


def post(url, payload, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} -> {e.code}: {e.read().decode()[:400]}") from None


intent_ok = branch_ok = esc_ok = 0
ground_ok = ground_total = answered = 0
calibrated = True
gate_ms, vlm_ms, verify_ms, total_ms = [], [], [], []
rows = []

for c in CASES:
    r = post(ORCH + "/answer", {"state": c["state"], "question": c["question"]})
    st = r.get("stages", {})
    gate_ms.append(st.get("gate_ms", 0)); total_ms.append(st.get("total_ms", 0))
    for key, bucket in (("vlm_ms", vlm_ms), ("verify_ms", verify_ms)):
        if key in st:
            bucket.append(st[key])
    answered += 1 if "vlm_ms" in st else 0
    calibrated = calibrated and bool(r.get("calibrated"))

    i_ok = r.get("intent") == c["intent"]; intent_ok += i_ok
    b_ok = r.get("route") == c["branch"]; branch_ok += b_ok
    should_escalate = c["branch"] in ("needs_lookup", "off_topic")
    e_ok = bool(r.get("escalate")) == should_escalate; esc_ok += e_ok

    answer = (r.get("answer") or "").strip()
    verdict = "-"
    if not should_escalate and not r.get("escalate"):
        ground_total += 1
        low = answer.lower()[:60]
        if c.get("negative"):
            good = ("no" in low or "not" in low or "cannot" in low) and "yes," not in low
        else:
            good = any(w.lower() in answer.lower() for w in c.get("want", []))
        ground_ok += good
        verdict = "ok" if good else "BAD"
    rows.append((c["id"], c["intent"], r.get("intent"), c["branch"], r.get("route"),
                 f"{r.get('confidence',0):.2f}", "esc" if r.get("escalate") else "-", verdict,
                 answer[:58].replace("\n", " ")))

print(f"{'case':18} {'intentExp':20} {'intentGot':20} {'brExp':18} {'brGot':18} {'p':>4} {'esc':>3} {'grnd':>4}")
for r in rows:
    print(f"{r[0]:18} {r[1]:20} {str(r[2]):20} {r[3]:18} {str(r[4]):18} {r[5]:>4} {r[6]:>3} {r[7]:>4}  {r[8]}")

n = len(CASES)
print()
print(f"intent accuracy   : {intent_ok}/{n}")
print(f"branch accuracy   : {branch_ok}/{n}")
print(f"escalation policy : {esc_ok}/{n}  (needs_lookup and off_topic must escalate)")
print(f"grounding         : {ground_ok}/{ground_total}  (answered cases)")
print(f"gate calibrated   : {calibrated}")
print(f"gate   ms p50 {statistics.median(gate_ms):.0f} max {max(gate_ms):.0f}")
if vlm_ms:
    print(f"vlm    ms p50 {statistics.median(vlm_ms):.0f} max {max(vlm_ms):.0f}   ({answered}/{n} generated)")
if verify_ms:
    print(f"verify ms p50 {statistics.median(verify_ms):.0f} max {max(verify_ms):.0f}")
print(f"total  ms p50 {statistics.median(total_ms):.0f} max {max(total_ms):.0f}")
