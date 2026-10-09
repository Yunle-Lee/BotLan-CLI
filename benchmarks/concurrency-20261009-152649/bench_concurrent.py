#!/usr/bin/env python3
"""Short concurrency benchmark for the Spark Duo / jevstep stack.

Levels 1/2/4/8 concurrent clients against /route, /chat/stream and /answer/stream.
Records throughput, latency percentiles and GPU/unified-memory usage per level.
Results saved under ~/spark-duo/benchmarks/concurrency-<timestamp>/.
"""
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench import (HOME, ORCH, VLM, Sampler, http_json, sse, make_state, now_iso, stats)

OUT_ROOT = HOME / "spark-duo" / "benchmarks"

SHORT = ("Acme Cloud: Pro is 20 USD per seat per month. Refunds within 30 days with the order number. "
         "Support hours are 9-18 UTC.")
QUESTION = "What is the refund window?"
CHAT_Q = "Explain in about 50 words why local AI on a GB10 is useful."


def do_route(_):
    t0 = time.perf_counter()
    res = http_json(ORCH + "/route", {"state": SHORT, "question": QUESTION})
    return {"wall_ms": round((time.perf_counter() - t0) * 1000, 1), "gate_ms": res.get("gate_ms"),
            "intent": res.get("intent")}


def do_chat(_):
    ttft, n, done = None, 0, {}
    t0 = time.perf_counter()
    for ev, d in sse(ORCH + "/chat/stream",
                     {"messages": [{"role": "user", "content": CHAT_Q}],
                      "max_tokens": 64, "temperature": 0.6}):
        if ev == "delta":
            if ttft is None:
                ttft = time.perf_counter() - t0
            n += 1
        elif ev == "done":
            done = d
        elif ev == "error":
            raise RuntimeError(d.get("error"))
    tim = done.get("timings") or {}
    return {"wall_ms": round((time.perf_counter() - t0) * 1000, 1),
            "ttft_ms": round(ttft * 1000, 1) if ttft else None,
            "predicted_n": tim.get("predicted_n"), "decode_tok_s": tim.get("predicted_per_second")}


def do_answer(_):
    ttft, done = None, {}
    t0 = time.perf_counter()
    for ev, d in sse(ORCH + "/answer/stream", {"state": SHORT, "question": QUESTION}):
        if ev == "delta":
            if ttft is None:
                ttft = time.perf_counter() - t0
        elif ev == "done":
            done = d
        elif ev == "error":
            raise RuntimeError(d.get("error"))
    st = done.get("stages") or {}
    return {"wall_ms": round((time.perf_counter() - t0) * 1000, 1),
            "ttft_ms": round(ttft * 1000, 1) if ttft else None,
            "gate_ms": st.get("gate_ms"), "vlm_ms": st.get("vlm_ms"), "verify_ms": st.get("verify_ms"),
            "escalate": done.get("escalate")}


def run_level(name, conc, n, fn, sampler):
    lat, errs, res_rows = [], [], []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = {ex.submit(fn, i): i for i in range(n)}
        for f in as_completed(futs):
            try:
                r = f.result()
                lat.append(r["wall_ms"])
                res_rows.append(r)
            except Exception as e:
                errs.append(f"{type(e).__name__}: {e}")
                lat.append(None)
    t1 = time.time()
    good = [x for x in lat if x is not None]
    row = {
        "scenario": name, "concurrency": conc, "requests": n,
        "wall_s": round(t1 - t0, 2),
        "throughput_rps": round(n / (t1 - t0), 2),
        "lat": stats(good), "errors": errs,
        "resources": sampler.window(t0, t1),
    }
    print(f"{name:14} c={conc:<2} n={n:<3} {row['wall_s']:>6.2f}s  "
          f"tput={row['throughput_rps']:>6.2f} req/s  "
          f"lat p50={row['lat'].get('p50')} p95={row['lat'].get('p95')} max={row['lat'].get('max')}  "
          f"gpu_max={row['resources'].get('gpu_util_max')}% avail_min={row['resources'].get('mem_available_min_gb')}GB "
          f"err={len(errs)}")
    return row, res_rows


def main():
    tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = OUT_ROOT / f"concurrency-{tag}"
    outdir.mkdir(parents=True, exist_ok=True)

    pids = {}
    for line in subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout.splitlines():
        if "build-cuda/bin/llama-server" in line:
            pids["llama-server"] = line.split()[0]
        elif "orchestrator.py" in line:
            pids["orchestrator"] = line.split()[0]
        elif "bin/jev-score" in line:
            pids["jev-score"] = line.split()[0]
    props = http_json(VLM + "/props")
    system = {"when": now_iso(), "pids": pids, "build": props.get("build_info"),
              "slots": props.get("total_slots")}
    (outdir / "system.json").write_text(json.dumps(system, indent=2))

    sampler = Sampler(interval=0.5)
    sampler.pids = pids
    sampler.start()
    time.sleep(1)

    levels = [1, 2, 4, 8]
    summary = {}
    raw = {}

    print(f"slots={system['slots']}  output={outdir}")
    for conc in levels:
        print(f"\n--- {conc} concurrent ---")
        row, rows = run_level("route", conc, 16, do_route, sampler)
        summary[f"route-c{conc}"], raw[f"route-c{conc}"] = row, rows
        row, rows = run_level("chat-stream", conc, 8, do_chat, sampler)
        summary[f"chat-c{conc}"], raw[f"chat-c{conc}"] = row, rows
        row, rows = run_level("answer-stream", conc, 8, do_answer, sampler)
        summary[f"answer-c{conc}"], raw[f"answer-c{conc}"] = row, rows

    sampler.stop()
    sampler.join(timeout=3)

    (outdir / "raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    (outdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    reps = {"route": "gate_ms", "chat-stream": "ttft_ms", "answer-stream": "ttft_ms"}
    lines = [f"# Concurrency benchmark — {tag}", "",
             f"- llama.cpp build {system['build']}, llama-server slots={system['slots']}",
             "- Gate calls are serialized inside the orchestrator (one jev-score process, `_LOCK`);",
             "  stage-2 llama-server serves up to `slots` generations at once, the rest queue.",
             "",
             "| scenario | conc | n | wall s | throughput req/s | lat p50 ms | p95 | max | key field p50 | "
             "GPU util max | MemAvail min GB | llama MB | jev MB | errors |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in summary.items():
        scen = s["scenario"]
        key = ""
        vals = [r.get(reps[scen]) for r in raw[k] if r.get(reps[scen]) is not None]
        if vals:
            key = f"{reps[scen]}={stats(vals).get('p50')}"
        r = s["resources"]
        lines.append(f"| {scen} | {s['concurrency']} | {s['requests']} | {s['wall_s']} | {s['throughput_rps']} | "
                     f"{s['lat'].get('p50')} | {s['lat'].get('p95')} | {s['lat'].get('max')} | {key} | "
                     f"{r.get('gpu_util_max')} | {r.get('mem_available_min_gb')} | "
                     f"{r.get('llama-server_gpu_mib_max')} | {r.get('jev-score_gpu_mib_max')} | {len(s['errors'])} |")
    (outdir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    shutil.copy(__file__, outdir / "bench_concurrent.py")
    shutil.copy("/tmp/opencode/bench.py", outdir / "bench.py")
    print("\n" + "\n".join(lines))
    print(f"\nsaved: {outdir}")


if __name__ == "__main__":
    main()
