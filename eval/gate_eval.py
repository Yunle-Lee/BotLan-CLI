#!/usr/bin/env python3
"""Measure the intent gate alone over a labelled set: accuracy, per-intent recall, confidence bands."""
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
sys.path.insert(0, str(Path(CONFIG["jev"]["model_dir"]).expanduser()))
sys.path.insert(0, str(Path(CONFIG["jev"]["model_dir"]).expanduser()))
from jev_style_decision_gguf import JevStyleDecisionGGUF  # noqa: E402

cases = [json.loads(l) for l in (Path(__file__).parent / "cases.jsonl").read_text().splitlines() if l.strip()]
m = JevStyleDecisionGGUF(Path(CONFIG["jev"]["model_dir"]).expanduser(), quant=CONFIG["jev"]["quant"],
                         binary=str(ROOT / "bin" / "jev-score"), n_gpu_layers=CONFIG["jev"].get("n_gpu_layers", 999))

GATE = CONFIG["gate"]
per_intent = collections.defaultdict(lambda: [0, 0])
bands = collections.Counter()
conf_ok, conf_bad = [], []
rows = []

for c in cases:
    r = m.decide(c["state"], GATE["question"], options=GATE["options"], category=GATE["category"])
    got, want = r["answer"], c["intent"]
    p = r["top_probability"]
    per_intent[want][1] += 1
    per_intent[want][0] += got == want
    correct = got == want
    (conf_ok if correct else conf_bad).append(p)
    bands[(round(p, 1), correct)] += 1
    rows.append((c["id"], want, got, p, correct, c.get("question", "")))

n = len(cases)
acc = sum(1 for r in rows if r[4])
print(f"{'case':12} {'want':20} {'got':20} {'p':>5}  ok")
for cid, want, got, p, correct, _ in rows:
    print(f"{cid:12} {want:20} {got:20} {p:>5.2f}  {'ok' if correct else 'XX'}")

print(f"\nintent accuracy: {acc}/{n} = {acc/n:.1%}")
print("\nper intent (recall):")
for k, (ok, tot) in sorted(per_intent.items(), key=lambda x: x[1][0] / x[1][1]):
    print(f"  {k:22} {ok}/{tot}")

print("\nconfidence bands (p -> correct/incorrect):")
for b in [round(i / 10, 1) for i in range(1, 10)]:
    c_ok, c_bad = bands.get((b, True), 0), bands.get((b, False), 0)
    if c_ok or c_bad:
        print(f"  p~{b:.1f}  ok={c_ok:2}  bad={c_bad:2}")

print(f"\nmean p when correct : {sum(conf_ok)/max(len(conf_ok),1):.3f}  (n={len(conf_ok)})")
print(f"mean p when wrong   : {sum(conf_bad)/max(len(conf_bad),1):.3f}  (n={len(conf_bad)})")
best = (None, -1, 0)
for t in [i / 100 for i in range(30, 96, 5)]:
    tp = sum(1 for p in conf_ok if p >= t)          # answered and would be right
    fp = sum(1 for p in conf_bad if p >= t)         # answered and would be wrong
    esc = n - tp - fp
    score = tp - 2 * fp
    if score > best[1]:
        best = (t, score, esc)
    print(f"  threshold {t:.2f}: answer-correct={tp:2} answer-wrong={fp:2} escalate={esc:2}")
print(f"\nbest threshold by (correct - 2*wrong): {best[0]}  (wrongly answered {best[1] and ''}{sum(1 for p in conf_bad if p >= best[0])})")
m.close()
