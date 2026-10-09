#!/usr/bin/env python3
"""Strict benchmark of the Spark Duo / jevstep stack.

Records, per scenario: wall latency, server-side stage timings, token counts and
GPU/system resource usage (GB10 util, per-process VRAM, unified MemAvailable).
All raw samples + summaries are written to ~/spark-duo/benchmarks/<timestamp>/.
"""
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ORCH = "http://127.0.0.1:8090"
VLM = "http://127.0.0.1:8080"
HOME = Path.home()
OUT_ROOT = HOME / "spark-duo" / "benchmarks"
WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
         "november oscar papa quebec romeo sierra tango uniform victor whiskey xray").split()


def now_iso():
    return datetime.now().isoformat(timespec="milliseconds")


def http_json(url, payload=None, timeout=600):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def sse(url, payload, timeout=900):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        event = None
        for raw in r:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                try:
                    yield event, json.loads(line[6:])
                except json.JSONDecodeError:
                    continue


def make_state(n_lines, seed=0):
    import random
    rnd = random.Random(seed)
    return "\n".join(" ".join(rnd.choice(WORDS) for _ in range(12)) for _ in range(n_lines))


# ---------------------------------------------------------------- resource sampler
class Sampler(threading.Thread):
    def __init__(self, interval=0.5):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples = []
        self._stop = threading.Event()
        self.pids = {}

    def run(self):
        while not self._stop.is_set():
            s = {"t": time.time()}
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,power.draw,temperature.gpu",
                     "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout.strip()
                u, p, t = [x.strip() for x in out.split(",")]
                s.update(gpu_util=float(u), power_w=float(p), temp_c=float(t))
            except Exception:
                pass
            try:
                apps = subprocess.run(
                    ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
                     "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
                m = {}
                for line in apps.strip().splitlines():
                    pid, mib = [x.strip() for x in line.split(",")]
                    m[pid] = float(mib)
                s["gpu_mib"] = {name: m.get(pidm) for name, pidm in self.pids.items()}
                s["gpu_mib_total"] = sum(v for v in s["gpu_mib"].values() if v)
            except Exception:
                pass
            try:
                mem = {}
                for line in open("/proc/meminfo"):
                    k, v = line.split(":", 1)
                    mem[k] = int(v.split()[0])
                s["mem_available_gb"] = round(mem["MemAvailable"] / 1048576, 2)
                s["mem_total_gb"] = round(mem["MemTotal"] / 1048576, 2)
            except Exception:
                pass
            self.samples.append(s)
            time.sleep(self.interval)

    def stop(self):
        self._stop.set()

    def window(self, t0, t1):
        ws = [s for s in self.samples if t0 - 0.5 <= s["t"] <= t1 + 0.5]
        if not ws:
            return {}
        out = {
            "gpu_util_max": max((s.get("gpu_util", 0) for s in ws), default=0),
            "gpu_util_mean": round(statistics.mean([s.get("gpu_util", 0) for s in ws]), 1),
            "power_max_w": max((s.get("power_w", 0) for s in ws), default=0),
            "temp_max_c": max((s.get("temp_c", 0) for s in ws), default=0),
            "mem_available_min_gb": min((s.get("mem_available_gb", 999) for s in ws), default=None),
            "samples": len(ws),
        }
        for name in ("llama-server", "jev-score"):
            vals = [s.get("gpu_mib", {}).get(name) for s in ws]
            vals = [v for v in vals if v is not None]
            if vals:
                out[f"{name}_gpu_mib_max"] = max(vals)
        tot = [s.get("gpu_mib_total", 0) for s in ws]
        if tot:
            out["gpu_mib_total_max"] = max(tot)
        return out


# ---------------------------------------------------------------- scenarios
def timed_call(fn):
    t0 = time.perf_counter()
    res = fn()
    return res, time.perf_counter() - t0


def collect_common(done):
    stages = done.get("stages") or {}
    return {
        "gate_ms": stages.get("gate_ms"),
        "vlm_ms": stages.get("vlm_ms"),
        "verify_ms": stages.get("verify_ms"),
        "total_ms": stages.get("total_ms"),
        "decode_tok_s": done.get("decode_tok_s"),
        "gate_tokens": done.get("gate_tokens"),
        "escalate": done.get("escalate"),
        "loop_cut": done.get("loop_cut"),
        "verify": (done.get("verify") or {}).get("relation"),
        "answer_chars": len(done.get("answer") or ""),
    }


def bench_route(state, timeout=120):
    return http_json(ORCH + "/route", {"state": state, "question": "Which team handles this?"})


def bench_decide(state):
    return http_json(ORCH + "/decide", {"state": state, "question": "Is a refund possible?",
                                        "options": {"yes": None, "no": None}})


def bench_answer(state, question):
    return http_json(ORCH + "/answer", {"state": state, "question": question})


def bench_answer_stream(state, question):
    ttft, text, done = None, "", {}
    t0 = time.perf_counter()
    for ev, d in sse(ORCH + "/answer/stream", {"state": state, "question": question}):
        if ev == "delta":
            if ttft is None:
                ttft = time.perf_counter() - t0
            text += d["text"]
        elif ev == "done":
            done = d
        elif ev == "error":
            raise RuntimeError(d.get("error"))
    out = collect_common(done)
    out.update(ttft_s=round(ttft, 4) if ttft else None, answer_chars=len(text))
    return out


def bench_chat_stream(question, max_tokens=256):
    ttft, text, done = None, "", {}
    t0 = time.perf_counter()
    for ev, d in sse(ORCH + "/chat/stream",
                     {"messages": [{"role": "user", "content": question}],
                      "max_tokens": max_tokens, "temperature": 0.6}):
        if ev == "delta":
            if ttft is None:
                ttft = time.perf_counter() - t0
            text += d["text"]
        elif ev == "done":
            done = d
        elif ev == "error":
            raise RuntimeError(d.get("error"))
    tim = done.get("timings") or {}
    return {"ttft_s": round(ttft, 4) if ttft else None,
            "predicted_n": tim.get("predicted_n"),
            "prompt_n": tim.get("prompt_n"),
            "decode_tok_s": tim.get("predicted_per_second"),
            "prefill_tok_s": tim.get("prompt_per_second"),
            "loop_cut": done.get("loop_cut"), "answer_chars": len(text)}


def bench_vlm_raw(question, max_tokens=256):
    payload = {"messages": [{"role": "user", "content": question}], "max_tokens": max_tokens,
               "temperature": 0.6, "repeat_penalty": 1.1, "repeat_last_n": 256, "top_p": 0.9}
    t0 = time.perf_counter()
    res = http_json(VLM + "/v1/chat/completions", payload)
    dt = time.perf_counter() - t0
    tim = res.get("timings") or {}
    return {"wall_s": round(dt, 3), "predicted_n": tim.get("predicted_n"),
            "prompt_n": tim.get("prompt_n"), "decode_tok_s": tim.get("predicted_per_second"),
            "prefill_tok_s": tim.get("prompt_per_second")}


def bench_agent(question):
    answer, done, tools = "", {}, 0
    for ev, d in sse(ORCH + "/agent/stream", {"question": question, "state": ""}):
        if ev == "delta":
            answer += d["text"]
        elif ev == "tool_call":
            tools += 1
        elif ev == "done":
            done = d
        elif ev == "error":
            raise RuntimeError(d.get("error"))
    return {"tool_calls": tools, "stop": done.get("stop_reason"), "grounded": done.get("grounded"),
            "escalate": done.get("escalate"), "answer_chars": len(answer),
            "total_ms": (done.get("stages") or {}).get("total_ms")}


def stats(values):
    values = [v for v in values if isinstance(v, (int, float))]
    if not values:
        return {}
    values = sorted(values)
    def pct(p):
        i = min(len(values) - 1, int(round((p / 100) * (len(values) - 1))))
        return round(values[i], 3)
    return {"n": len(values), "min": round(values[0], 3), "p50": pct(50), "p90": pct(90),
            "p95": pct(95), "max": round(values[-1], 3), "mean": round(statistics.mean(values), 3),
            "stdev": round(statistics.stdev(values), 3) if len(values) > 1 else 0.0}


def main():
    tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = OUT_ROOT / f"jevstep-{tag}"
    outdir.mkdir(parents=True, exist_ok=True)

    # -- system snapshot
    pids = {}
    for line in subprocess.run(["ps", "-eo", "pid,comm,args"], capture_output=True, text=True).stdout.splitlines():
        if "llama-server" in line and "build-cuda" in line:
            pids["llama-server"] = line.split()[0]
        elif "orchestrator.py" in line:
            pids["orchestrator"] = line.split()[0]
        elif "bin/jev-score" in line and "query" not in line:
            pids["jev-score"] = line.split()[0]
    props = http_json(VLM + "/props")
    health = http_json(ORCH + "/health")
    dgs = props.get("default_generation_settings") or {}
    system = {
        "when": now_iso(),
        "kernel": subprocess.run(["uname", "-a"], capture_output=True, text=True).stdout.strip(),
        "gpu": subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,compute_cap,memory.total",
             "--format=csv,noheader"], capture_output=True, text=True).stdout.strip(),
        "cuda": subprocess.run(["bash", "-lc", "nvcc --version | tail -1"], capture_output=True,
                               text=True).stdout.strip() or "/usr/local/cuda-13.0",
        "pids": pids,
        "llama_props": {k: props.get(k) for k in ("build_info", "total_slots", "model_path")},
        "n_ctx": dgs.get("n_ctx"),
        "jevs": subprocess.run(["ps", "-o", "args=", "-p", pids.get("jev-score", "0")],
                               capture_output=True, text=True).stdout.strip(),
        "config": {k: health.get(k) for k in ("intents", "branches", "vlm", "jev", "sampling", "agent")},
    }
    (outdir / "system.json").write_text(json.dumps(system, ensure_ascii=False, indent=2))
    print(f"output dir: {outdir}")

    sampler = Sampler(interval=0.5)
    sampler.pids = pids
    sampler.start()
    time.sleep(1.0)
    idle = sampler.window(time.time() - 1, time.time())
    print("idle baseline:", json.dumps(idle))

    short_state = "Acme Cloud: Pro is 20 USD per seat per month. Refunds within 30 days with the order number. Support 9-18 UTC."
    long_state = make_state(900, seed=1) + "\nThe secret code is BLUE-42.\n" + make_state(200, seed=2)
    question = "What does the context say?"

    scenarios = []
    raw = {}

    def run_scenario(name, iters, fn, warmup=1, note=""):
        print(f"\n-- {name} ({iters} iters) {note}")
        for i in range(warmup):
            try:
                fn(0)
            except Exception as e:
                print("   warmup error:", e)
        rows = []
        for i in range(iters):
            t0 = time.time()
            try:
                res = fn(i)
                err = None
            except Exception as e:
                res, err = {}, f"{type(e).__name__}: {e}"
            t1 = time.time()
            row = {"iter": i, "wall_ms": round((t1 - t0) * 1000, 1), "result": res, "error": err,
                   "resources": sampler.window(t0, t1)}
            rows.append(row)
            r = res or {}
            key = r.get("total_ms") or r.get("gate_ms") or r.get("wall_s")
            print(f"   #{i}: wall={row['wall_ms']}ms result_key={key} "
                  f"gpu_max={row['resources'].get('gpu_util_max')}% "
                  f"mem_avail_min={row['resources'].get('mem_available_min_gb')}GB err={err}")
        raw[name] = rows
        return rows

    # 1. gate short
    run_scenario("gate-short", 20, lambda i: {"gate_ms": bench_route(short_state)["gate_ms"]}, warmup=2)
    # 2. gate long: cold = different states, warm = same state
    run_scenario("gate-long-cold", 3,
                 lambda i: {"gate_ms": bench_route(make_state(900, seed=100 + i) + " code-X")["gate_ms"]},
                 warmup=0, note="distinct ~10k-token states")
    run_scenario("gate-long-warm", 5,
                 lambda i: {"gate_ms": bench_route(long_state)["gate_ms"]}, warmup=1,
                 note="same ~10k-token state (prefix KV reuse)")
    # correct the gate summary key (gate_ms, not total_ms) happens automatically in aggregation
    # 3. decide
    run_scenario("decide-short", 20, lambda i: {"ms": bench_decide(short_state)["ms"]}, warmup=2)
    # 4. answer blocking
    run_scenario("answer-short", 10, lambda i: collect_common(bench_answer(short_state, question)), warmup=1)
    run_scenario("answer-long", 3,
                 lambda i: collect_common(bench_answer(make_state(600, seed=300 + i) + "\nThe refund window is 30 days.",
                                                        "What is the refund window?")), warmup=0,
                 note="distinct ~7k-token states")
    # 5. answer stream
    run_scenario("answer-stream-short", 10,
                 lambda i: bench_answer_stream(short_state, question), warmup=1)
    # 6. direct chat stream / raw vlm
    chat_q = "Write a short paragraph (about 60 words) about why local AI on a GB10 is useful."
    run_scenario("chat-stream", 5, lambda i: bench_chat_stream(chat_q, 256), warmup=1)
    run_scenario("vlm-raw", 3, lambda i: bench_vlm_raw(chat_q, 256), warmup=1)
    # 7. agent
    run_scenario("agent-read", 3, lambda i: bench_agent("list the files in the scripts/ directory"), warmup=1)

    time.sleep(1.0)
    sampler.stop()
    sampler.join(timeout=3)

    # -- aggregate
    summary = {}
    for name, rows in raw.items():
        ok = [r for r in rows if not r["error"]]
        wall = stats([r["wall_ms"] for r in ok])
        fields = {}
        for key in ("gate_ms", "vlm_ms", "verify_ms", "total_ms", "ttft_s", "decode_tok_s",
                    "prefill_tok_s", "predicted_n", "gate_tokens", "ms", "tool_calls", "wall_s"):
            vals = [(r["result"] or {}).get(key) for r in ok]
            if any(v is not None for v in vals):
                fields[key] = stats(vals)
        res_fields = {}
        for key in ("gpu_util_max", "gpu_util_mean", "mem_available_min_gb", "power_max_w",
                    "temp_max_c", "llama-server_gpu_mib_max", "jev-score_gpu_mib_max",
                    "gpu_mib_total_max"):
            vals = [(r["resources"] or {}).get(key) for r in ok]
            if any(v is not None for v in vals):
                res_fields[key] = stats(vals)
        errors = [r["error"] for r in rows if r["error"]]
        summary[name] = {"iters": len(rows), "ok": len(ok), "errors": errors,
                         "wall_ms": wall, "fields": fields, "resources": res_fields}

    (outdir / "raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    (outdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    # -- markdown
    lines = [f"# jevstep benchmark — {tag}", "",
             f"- GPU: {system['gpu']}",
             f"- llama.cpp: {system['llama_props'].get('build_info')}, n_ctx={system['n_ctx']}, slots={system['llama_props'].get('total_slots')}",
             f"- idle: {json.dumps(idle)}", "",
             "| scenario | n | wall p50 ms | wall p95 ms | key metric | GPU util max/mean % | mem_avail min GB | llama-server MiB | jev-score MiB |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, s in summary.items():
        w = s["wall_ms"]
        f = s["fields"]
        key = ""
        for k in ("total_ms", "gate_ms", "ms", "ttft_s"):
            if k in f:
                key = f"{k} p50={f[k]['p50']}"
                break
        if "decode_tok_s" in f:
            key += f", decode p50={f['decode_tok_s']['p50']} tok/s"
        r = s["resources"]
        lines.append(
            f"| {name} | {s['ok']}/{s['iters']} | {w.get('p50')} | {w.get('p95')} | {key} | "
            f"{r.get('gpu_util_max', {}).get('max')}/{r.get('gpu_util_mean', {}).get('mean')} | "
            f"{r.get('mem_available_min_gb', {}).get('min')} | "
            f"{r.get('llama-server_gpu_mib_max', {}).get('max')} | "
            f"{r.get('jev-score_gpu_mib_max', {}).get('max')} |")
    (outdir / "summary.md").write_text("\n".join(lines) + "\n")
    shutil.copy(__file__, outdir / "bench.py")
    print("\n" + "\n".join(lines))
    print(f"\nsaved: {outdir}")


if __name__ == "__main__":
    main()
